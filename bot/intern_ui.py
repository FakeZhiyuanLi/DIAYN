"""
intern_ui.py
~~~~~~~~~~~~
What every Discord-facing part of the internship finder shares: the handles
`app.py` injects, the one way to send a private reply, the cached
window of recent postings, and the error handling that keeps user text out of
the log.

It exists so the rules below live in one place instead of being re-derived in
each view and command.

**The bot is private.** `need_access` is the first check of every callback
that shows or stores anything, and each callback makes it itself rather than
a shared base view, so the ones that only remove data or reduce contact can
leave it out: nobody is ever stuck with their data or their alerts. Who may
use the bot is `access.allowed`; the member cache it is handed is the client's.

**Every personal reply is private and mentions nobody.** `refuse` and
`private_send` are the only send paths the finder's modules use for text of
their own; both set `ephemeral=True` and `allowed_mentions=NO_MENTIONS`.

**Never `view=None` on a send.** discord.py 2.7.1 defaults `view` to MISSING:
`InteractionResponse.send_message(view=None)` sends and *then* raises on
`view.is_finished()`, and `Webhook.send(view=None)` raises before sending. Both
helpers drop a None view instead of forwarding it (spec 6.14).

**Defer before the window.** A cold window is ~30 ms of SQL plus seconds of
tagging in a thread, past Discord's three-second limit on a first response.
Handlers refuse first, then defer, then await `window()`. Autocomplete cannot
defer, so it only ever reads `cached_window()`.

**Errors are logged by type.** A traceback can carry what a user typed, so
every view and modal routes its errors to `fail_softly`, which prints the
exception's class name and a fixed context string, never its message.

**Postings come from a `source`, and may be reopened.** Everything the finder
reads besides `postings` itself goes through `postings_source`, which reads
the contract tables. The file is the scraper's, so `ensure_postings` reopens
it when it has been replaced or failed to open: on every delivery tick, and at
most once a minute from commands.
"""

import asyncio
import dataclasses
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime

import aiohttp
import discord
from discord import app_commands

import access
import intern_clock
import intern_delivery
import intern_fit
import intern_match
import intern_store
import intern_text
import postings_contract
import postings_source
import rate_limit
import resume_parse
from intern_match import Candidate
from intern_taxonomy import company_norm

# Injected by app.py once it has opened both databases (spec 3.2). Nothing here
# reads them at import, so the tests import cleanly.
db: sqlite3.Connection | None = None        # users.db
pconn: sqlite3.Connection | None = None     # postings.db, read-only
pconn_error: str | None = None
source: postings_source.Source | None = None
postings_path: str | None = None            # the scraper's SETTINGS.postings_db, reopened from here
bot: discord.Client | None = None
intern_error: str | None = None

NO_MENTIONS = discord.AllowedMentions.none()
WINDOW_TTL_S = 300
#: A person is not going to upload six resumes in an hour; a script would.
UPLOAD_LIMIT = 5
UPLOAD_WINDOW_S = 3600
upload_limiter = rate_limit.RateLimiter(limit=UPLOAD_LIMIT, window=UPLOAD_WINDOW_S)
#: How long a non-persistent view or a modal keeps its state (and a draft) in memory.
VIEW_TIMEOUT_S = 900
#: How often a command may try to reopen postings.db (B2); a delivery tick always may.
POSTINGS_RETRY_S = 60
_postings_tried_at: float | None = None     # time.monotonic() of the last reopen
_UNSET = object()
#: The tracker's state as last written to the log: None while open, else the reason.
#: Starts as whatever app.py already printed at start-up.
_logged_error: object = _UNSET


# ------------------------------------------------------------------ sending

def private_send(interaction) -> Callable[..., Awaitable]:
    """followup.send, always ephemeral, mentions off unless given, a None view dropped.
    The only place in the finder that calls interaction.followup.send."""
    async def send(content=None, *, view=None, **kw):
        mentions = kw.pop("allowed_mentions", NO_MENTIONS)
        if view is not None:
            kw["view"] = view
        return await interaction.followup.send(content, ephemeral=True,
                                               allowed_mentions=mentions, **kw)
    return send


async def refuse(interaction, text: str, *, view: discord.ui.View | None = None) -> None:
    """
    A private reply: the first response when there has been none (principle 2 —
    a refusal comes before any defer), else a followup. Also the way any first
    private message with an optional view is sent. A None view is never forwarded.
    """
    if interaction.response.is_done():
        await private_send(interaction)(text, view=view)
    elif view is None:
        await interaction.response.send_message(text, ephemeral=True,
                                                allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True,
                                                allowed_mentions=NO_MENTIONS, view=view)


async def defer_reply(interaction) -> None:
    """Acknowledge now; a new private message follows ("thinking")."""
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)


async def defer_update(interaction) -> None:
    """Acknowledge a component or modal submit; the message it came from is edited next.
    A slash command has no message to update, so it gets the thinking reply instead."""
    if interaction.response.is_done():
        return
    if interaction.type == discord.InteractionType.application_command:
        await interaction.response.defer(thinking=True, ephemeral=True)
    else:
        await interaction.response.defer(ephemeral=True)


def select_options(pairs, chosen) -> list[discord.SelectOption]:
    """(value, label) pairs as select options, the ones in `chosen` preselected."""
    return [discord.SelectOption(label=label, value=value, default=value in chosen)
            for value, label in pairs]


def with_banner(p, text: str) -> str:
    """Every personal reply to a user whose DMs keep failing starts with why (J3, J12)."""
    blocked = p is not None and p.dm_failures >= intern_store.DM_FAILURE_LIMIT
    return f"{intern_text.dm_blocked_banner()}\n{text}" if blocked else text


# ------------------------------------------------------------------ gates

def _member_of(user_id: int) -> access.MemberOf:
    """Whether `user_id` is a member of any of the servers asked about, by the client's
    member cache, which the members intent keeps full. Nobody is, before the client is."""
    def member_of(guild_ids: frozenset[int]) -> bool:
        client = bot
        if client is None:
            return False
        for guild_id in guild_ids:
            guild = client.get_guild(guild_id)
            if guild is not None and guild.get_member(user_id) is not None:
                return True
        return False
    return member_of


def has_access(user_id: int, guild_id: int | None = None) -> bool:
    """Whether `user_id` may use this bot inside the server `guild_id` (None: a DM), with
    the grants users.db holds now (`access.allowed`). Raises sqlite3.Error."""
    return access.allowed(access.grants(db), user_id, guild_id, _member_of(user_id))


def dm_access() -> Callable[[int], bool]:
    """Who may be DMed now: `access.allowed` as in a DM, against one read of the grants,
    for a delivery tick to ask about each person in turn. Raises sqlite3.Error, so a
    tick that cannot read the grants sends nothing."""
    granted = access.grants(db)
    return lambda user_id: access.allowed(granted, user_id, None, _member_of(user_id))


def may_use(user_id: int, guild_id: int | None = None) -> bool:
    """`has_access`, failing closed: when the grants cannot be read, only the owner may.
    Never raises; a failure is logged by type."""
    try:
        return has_access(user_id, guild_id)
    except sqlite3.Error as error:
        log_failure("reading who may use this bot", error)
        return access.is_owner(user_id)


async def need_access(interaction) -> bool:
    """False, after refusing privately, for anyone this bot is not open to, here."""
    if may_use(interaction.user.id, getattr(interaction, "guild_id", None)):
        return True
    await refuse(interaction, intern_text.no_access())
    return False


async def need_finder(interaction) -> bool:
    """False, after refusing, when the finder's tables could not be made at start-up."""
    if intern_error is None and db is not None:
        return True
    await refuse(interaction, intern_text.disabled_finder(intern_error or "not started"))
    return False


def ensure_postings(*, throttle: bool = True) -> bool:
    """
    True when postings.db is open and is still the file `postings_path` names. A file
    that failed to open, or was replaced (a restore changes its inode), is reopened under
    the contract (B2); with `throttle`, at most once per POSTINGS_RETRY_S. Never raises.
    """
    global pconn, source, pconn_error, _postings_tried_at, _logged_error
    if _logged_error is _UNSET:
        _logged_error = pconn_error
    if pconn is not None and source is not None and not source.moved():
        return True
    at = time.monotonic()
    if throttle and _postings_tried_at is not None and at - _postings_tried_at < POSTINGS_RETRY_S:
        if pconn is not None:
            # It changed under us and the reopen must wait: stop serving the old file
            # everywhere (autocomplete and debug read pconn directly), and say why.
            postings_source.close_quietly(pconn)
            pconn, source, pconn_error = None, None, "postings.db was replaced; reopening shortly"
            invalidate()
        return False
    _postings_tried_at = at
    postings_source.close_quietly(pconn)
    pconn, source, pconn_error = postings_source.open_contract(postings_path)
    invalidate()                    # whatever was cached came from the file that is gone
    _log_state()
    return pconn is not None


def _log_state() -> None:
    """Writes each change of the tracker's state to the log, once. A tracker that goes
    down after start-up stops every alert, and the start-up line is printed only once,
    so without this the operator's log would say nothing."""
    global _logged_error
    if pconn is None and pconn_error != _logged_error:
        print(f"internship tracker disabled: {pconn_error}", file=sys.stderr)
        _logged_error = pconn_error
    elif pconn is not None and _logged_error is not None:
        print("internship tracker reopened", file=sys.stderr)
        _logged_error = None


async def need_tracker(interaction) -> bool:
    """False, after refusing, when postings.db is unusable."""
    if ensure_postings():
        return True
    await refuse(interaction, intern_text.disabled_tracker(pconn_error or "not started."))
    return False


async def profile_for(interaction, *, tracker: bool = False):
    """The stored profile of whoever pressed, after the refusals that come before any
    defer: finder off, tracker off (when `tracker`), no profile any more."""
    if not await need_finder(interaction):
        return None
    if tracker and not await need_tracker(interaction):
        return None
    p = intern_store.load(db, interaction.user.id)
    if p is None:
        await refuse(interaction, intern_text.alert_reply("no_profile"))
    return p


def touch(user_id: int) -> None:
    """Marks the user active (the 365-day expiry counts from here); no-op without a profile."""
    if intern_error is not None or db is None:
        return
    try:
        intern_store.touch(db, user_id, time.time())
    except sqlite3.Error as error:
        log_failure("marking a user active", error)


# ------------------------------------------------------------------ the window

@dataclasses.dataclass(frozen=True)
class _Window:
    loaded_at: float                 # time.monotonic()
    generation: int                  # _generation when its load began
    data_version: int | None         # postings.db's data_version when its load began (B4)
    cands: tuple[Candidate, ...]
    gmap: Mapping[int, str]
    supply: Mapping[str, int]


_window: _Window | None = None
#: Bumped by every `invalidate()`. A load that began under an older generation read
#: its rows before the sweep that invalidated it, so it is served but never fresh.
_generation = 0
_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _window_lock() -> asyncio.Lock:
    # One lock per event loop: a lock made on one loop cannot be awaited on
    # another, and the tests run each case in its own asyncio.run.
    global _lock
    loop = asyncio.get_running_loop()
    if _lock is None or _lock[0] is not loop:
        _lock = (loop, asyncio.Lock())
    return _lock[1]


def _derive(cands: Sequence[Candidate]) -> tuple[dict[int, str], dict[str, int]]:
    gmap = intern_match.group_map(cands)
    return gmap, intern_match.field_supply(cands, gmap)


def _data_version() -> int | None:
    """postings.db's data_version: it changes whenever another process commits, so a
    sweep DIAYN ran shows up before the TTL, with no `invalidate()` from here (B4).
    None with no connection, which no cached window matches."""
    try:
        return None if pconn is None else postings_contract.data_version(pconn)
    except sqlite3.Error:
        return None


async def window(now: float | None = None) -> list[Candidate]:
    """
    The last `source.window_days` of postings, tagged; cached for WINDOW_TTL_S, until
    `invalidate()`, or until another process commits to postings.db. Concurrent
    callers on a cold cache share one load. Callers must have deferred first
    (module docstring).
    """
    global _window
    cached = cached_window()
    if cached is not None:
        return cached
    async with _window_lock():
        cached = cached_window()      # loaded by whoever held the lock before us
        if cached is not None:
            return cached
        at = time.time() if now is None else now
        generation, version = _generation, _data_version()
        cands = await intern_match.load_window(pconn, now=at, max_age_days=source.window_days,
                                               is_blocked=source.is_blocked)
        gmap, supply_ = await asyncio.to_thread(_derive, cands)
        # Kept even when a sweep invalidated it meanwhile, so `window_gmap()` and
        # `supply()` still match what this caller gets; `_fresh` will not serve it again.
        _window = _Window(time.monotonic(), generation, version, tuple(cands), gmap, supply_)
        return list(_window.cands)


def _fresh() -> _Window | None:
    current = _window
    if (current is None or current.generation != _generation
            or time.monotonic() - current.loaded_at > WINDOW_TTL_S
            or current.data_version != _data_version()):
        return None
    return current


def cached_window() -> list[Candidate] | None:
    """The cached window when it is fresh, else None. Never loads."""
    current = _fresh()
    return None if current is None else list(current.cands)


def window_gmap() -> dict[int, str]:
    """group_map of the cached window (call right after `await window()`)."""
    current = _window
    return {} if current is None else dict(current.gmap)


def supply() -> dict[str, int]:
    """field_supply of the cached window (call right after `await window()`)."""
    current = _window
    return {} if current is None else dict(current.supply)


@dataclasses.dataclass(frozen=True)
class WindowParts:
    cands: list[Candidate]
    gmap: dict[int, str]
    supply: dict[str, int]


async def window_parts() -> WindowParts | None:
    """The window with its group map and field supply, or None with the tracker down.
    Callers must have deferred first."""
    if not ensure_postings():
        return None
    cands = await window()
    return WindowParts(cands, window_gmap(), supply())


def hidden(user_id: int) -> frozenset[str]:
    """The groups a user hid: dropped from their lists and counts."""
    return intern_store.seen_hashes(db, user_id, states=("hidden",))


def invalidate() -> None:
    """
    Drops the cache; called after a reopen. The scraper's sweeps need no call: they
    are seen through data_version (B4). A load already under way read its rows before
    the call: its callers get them, but it is never cached as fresh.
    """
    global _window, _generation
    _window, _generation = None, _generation + 1


def _board_companies() -> tuple[str, ...]:
    """One company per tracked board, blocked ones dropped; none with no source, or
    with a file that cannot be read. Never raises: `_notices` quotes the count even
    with the tracker down, and a raise there would stop the expiry warnings."""
    try:
        current = source
        if current is None:
            return ()
        return tuple(c for c in current.board_companies()
                     if isinstance(c, str) and not current.is_blocked(c))
    except sqlite3.Error as error:
        log_failure("reading the board registry", error)
        return ()


def known_companies() -> dict[str, str]:
    """company_norm -> display name: every tracked board, plus companies in the cached window."""
    names: dict[str, str] = {}
    for company in _board_companies():
        names.setdefault(company_norm(company), company)
    for c in cached_window() or ():
        names.setdefault(c.company_norm, c.company)
    names.pop("", None)
    return names


def companies_watched() -> int:
    """How many companies the sweeper polls; 0 with no source (see `_board_companies`)."""
    try:
        return 0 if source is None else source.boards_count()
    except sqlite3.Error as error:
        log_failure("counting the companies watched", error)
        return 0


def today(now: float | None = None) -> date:
    """Today's date in DIAYN_TZ, the zone the finder keeps its hours and dates in, at
    `now` (default: now). Resume dates and upcoming terms are read against it."""
    zone = intern_clock.zone()
    return (datetime.now(zone) if now is None else datetime.fromtimestamp(now, zone)).date()


# ------------------------------------------------------------------ errors

def log_failure(context: str, error: BaseException) -> None:
    """The exception's class name and `context`: never its message, a traceback, an id,
    a filename or a URL (spec 2.3). A command's wrapped error is unwrapped first."""
    e = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    print(f"internship finder: {context} failed: {type(e).__name__}", file=sys.stderr)


async def fail_softly(interaction, context: str, error: BaseException) -> None:
    """log_failure, then a generic private apology (the interaction may have expired)."""
    log_failure(context, error)
    try:
        await refuse(interaction, intern_text.generic_failure())
    except discord.HTTPException:
        pass


class FinderView(discord.ui.View):
    """Base of every finder view: errors go to fail_softly, and use counts as activity."""

    async def interaction_check(self, interaction) -> bool:
        touch(interaction.user.id)
        return True

    async def on_error(self, interaction, error, item) -> None:
        await fail_softly(interaction, self.__class__.__name__, error)


class OwnedView(FinderView):
    """A view only the person it was made for may press (spec 1.5, "the owner only")."""

    def __init__(self, owner_id: int, *, timeout: float | None = VIEW_TIMEOUT_S) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await refuse(interaction, intern_text.not_yours())
            return False
        return await super().interaction_check(interaction)


class FinderModal(discord.ui.Modal):
    """Base of every finder modal. Times out so a dismissed modal does not stay in memory."""

    def __init__(self, *, title: str) -> None:
        super().__init__(title=title, timeout=VIEW_TIMEOUT_S)

    async def interaction_check(self, interaction) -> bool:
        touch(interaction.user.id)
        return True

    async def on_error(self, interaction, error) -> None:
        await fail_softly(interaction, self.__class__.__name__, error)


# ------------------------------------------------------------------ DMs and the start card

async def send_dm(user_id: int, msg: intern_delivery.DmMessage) -> None:
    """
    One DM, spec 5.7. A closed DM or a gone account raises DmForbidden (counted);
    Discord or network trouble raises DmTransient (retried next tick).
    """
    import intern_alert_views      # lazily: intern_alert_views imports this module
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        view = (intern_alert_views.AlertControlsView.for_message(msg.hide_options)
                if msg.with_controls else None)
        await user.send(content=msg.text, view=view, allowed_mentions=NO_MENTIONS)
    except (discord.Forbidden, discord.NotFound):
        raise intern_delivery.DmForbidden() from None
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
        raise intern_delivery.DmTransient() from None


async def show_start(interaction, *, extra: str | None = None) -> None:
    """The start card (J1) with its two buttons; `extra` is appended (J9)."""
    import intern_upload           # lazily: intern_upload imports this module
    text = intern_text.start_card(pdf_ok=resume_parse.pdf_supported(),
                                  gemini=intern_fit.available())
    await refuse(interaction, f"{text}\n\n{extra}" if extra else text,
                 view=intern_upload.StartView(interaction.user.id))
