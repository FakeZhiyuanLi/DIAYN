"""
intern_alert_views.py
~~~~~~~~~~~~~~~~~~~~~
Alerts outside any command: the buttons that live in DMs, and the background
work that decides what those DMs carry — the delivery loop and the leave/join
hooks. The scraper sweeps, in a task of its own; nothing here does.

**The buttons outlive the process that sent them.** An alert DM can be pressed
a week later, after any number of restarts, so every view here except
`ResumeNowView` is persistent: registered once in `on_ready` with placeholder
options, fixed custom ids, `timeout=None`. Such a view is one object answering
every user, so it must never trust anything it was built with — the hide
menu's options, a profile — and acts only on `interaction.user.id` and the
values of this one interaction. Instances built for one message
(`AlertControlsView.for_message`) carry the real options so the menu renders,
and time out after VIEW_TIMEOUT_S so a year of DMs does not stay in memory;
after that the registered instance answers, identically.

**The bot is private.** Hide, Try again and Resume now check
`intern_ui.need_access` first; Stop and Pause answer anyone, so nobody is ever
stuck with alerts they no longer want.

**The loop never dies** (spec 5.1): it wraps each part of its body and logs an
exception's type only. `app.py` reaches the loop and hooks through
`intern_commands`, which re-exports them under the names it wires in.

**Deletions do not wait on delivery.** The privacy notice promises that idle
profiles and those of people who left are removed. That housekeeping needs
nothing from postings.db, so the delivery loop starts whenever the finder is
on, runs it in a `try` of its own, and only skips alerts and quiet notes while
postings.db is down (a change to spec 5.1, which gated the loop on both).

**Only those the bot is open to are DMed.** Each tick reads the grants once
(`intern_ui.dm_access`) and hands delivery the answer, so a revocation stops
alerts and notes at the next tick. Grants that cannot be read stop the DMs,
and never the deletions.

**A bootstrap is never news.** The scraper sweeps, not the bot, so the bot
never sees the first sweep happen. Every delivery tick therefore starts from
the ledger itself: reopen postings.db if it was replaced, raise every cursor to
the oldest `first_seen` it has not yet accounted for (B3), and skip the alerts,
moving no cursor, when the file cannot be read (B5). CONTRACT.md has the rules.
"""

import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Sequence

import discord
from discord.ext import tasks

import intern_delivery
import intern_store
import intern_text
import intern_ui
import postings_contract
import postings_source
from intern_profile import Profile

DELIVERY_MINUTES = 5
#: B6: a stale sweep is logged at most this often; /internships debug says it every time.
STALE_LOG_EVERY_S = 3600
#: What each delivery tick records in intern_meta for `/internships debug` (counts only).
REPORT_KEYS = ("delivery_last_at", "delivery_last_due", "delivery_last_sent",
                "delivery_last_empty", "delivery_last_forbidden")
_ROLE, _COMPANY = "r:", "c:"
_PLACEHOLDER = (("none", "-"),)
_MAX_OPTIONS = 25


async def send_welcome(interaction, p: Profile) -> bool:
    """
    The welcome DM (J5 step 2), which doubles as the check that DMs arrive: when it
    does, refused DMs are forgotten, as Try again does, so the promise it makes is
    kept. When Discord refuses it, the user is told how to open their DMs, with Try
    again. Call only after the interaction has had its first response. True when
    the DM arrived.
    """
    msg = intern_delivery.DmMessage(intern_text.welcome_dm(p), (), False)
    try:
        await intern_ui.send_dm(p.user_id, msg)
    except intern_delivery.DmForbidden:
        await intern_ui.private_send(interaction)(intern_text.dm_blocked_text(), view=DmCheckView())
        return False
    except intern_delivery.DmTransient as error:
        intern_ui.log_failure("the welcome DM", error)
        return False
    if p.dm_failures:
        intern_store.reset_dm_failures(intern_ui.db, p.user_id)
    return True


def _profile_or_none(user_id: int) -> Profile | None:
    return intern_store.load(intern_ui.db, user_id)


class AlertControlsView(intern_ui.FinderView):
    """Persistent (timeout=None). Select intern:alert:hide, buttons intern:alert:pause,
    intern:alert:stop. Callbacks act on interaction.user.id only."""

    def __init__(self, options: Sequence[tuple[str, str]] = _PLACEHOLDER) -> None:
        super().__init__(timeout=None)
        choices = [discord.SelectOption(label=label[:100], value=value[:100])
                   for value, label in tuple(options)[:_MAX_OPTIONS]] or [
            discord.SelectOption(label=label, value=value) for value, label in _PLACEHOLDER]
        self.hide = discord.ui.Select(custom_id="intern:alert:hide",
                                      placeholder="Hide a role or company...",
                                      min_values=1, max_values=1, options=choices, row=0)
        self.hide.callback = self._on_hide
        self.add_item(self.hide)

    @classmethod
    def for_message(cls, options: Sequence[tuple[str, str]]) -> "AlertControlsView":
        view = cls(options)
        view.timeout = intern_ui.VIEW_TIMEOUT_S
        return view

    async def _on_hide(self, interaction) -> None:
        if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
            return
        uid, now = interaction.user.id, time.time()
        if _profile_or_none(uid) is None:
            await intern_ui.refuse(interaction, intern_text.alert_reply("no_profile"))
            return
        value = next(iter(self.hide.values), "")
        if value.startswith(_ROLE):
            intern_store.hide(intern_ui.db, uid, value[len(_ROLE):].split(":"), now)
            await intern_ui.refuse(interaction, intern_text.alert_reply("hidden_role"))
        elif value.startswith(_COMPANY) and len(value) > len(_COMPANY):
            intern_store.hide_company(intern_ui.db, uid, value[len(_COMPANY):], now)
            await intern_ui.refuse(interaction, intern_text.alert_reply("hidden_company"))
        else:                            # the placeholder, or a value no alert offers
            await intern_ui.refuse(interaction, intern_text.generic_failure())

    @discord.ui.button(label="Pause for a week", style=discord.ButtonStyle.secondary,
                       custom_id="intern:alert:pause", row=1)
    async def pause(self, interaction, button) -> None:
        if not await intern_ui.need_finder(interaction):
            return
        uid, now = interaction.user.id, time.time()
        if _profile_or_none(uid) is None:
            await intern_ui.refuse(interaction, intern_text.alert_reply("no_profile"))
            return
        until = now + intern_delivery.PAUSE_S
        intern_store.set_paused_until(intern_ui.db, uid, until, now)
        await intern_ui.refuse(interaction, intern_text.alert_reply("paused", until=until),
                               view=ResumeNowView(uid))

    @discord.ui.button(label="Stop alerts", style=discord.ButtonStyle.secondary,
                       custom_id="intern:alert:stop", row=1)
    async def stop_alerts(self, interaction, button) -> None:
        if not await intern_ui.need_finder(interaction):
            return
        uid, now = interaction.user.id, time.time()
        p = _profile_or_none(uid)
        if p is None:
            await intern_ui.refuse(interaction, intern_text.alert_reply("no_profile"))
            return
        intern_store.set_alerts(intern_ui.db, uid, "off", p.alert_hour, now,
                                cursor=intern_delivery.horizon(now))
        await intern_ui.refuse(interaction, intern_text.alert_reply("stopped"))


class DmCheckView(intern_ui.FinderView):
    """Persistent: Try again, under "I couldn't DM you" (J5)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Try again", style=discord.ButtonStyle.primary,
                       custom_id="intern:dm:retry")
    async def retry(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
            return
        uid = interaction.user.id
        p = _profile_or_none(uid)
        if p is None:
            await intern_ui.refuse(interaction, intern_text.alert_reply("no_profile"))
            return
        # A DM can take longer than the three seconds a first response is allowed.
        await intern_ui.defer_update(interaction)
        msg = intern_delivery.DmMessage(intern_text.welcome_dm(p), (), False)
        try:
            await intern_ui.send_dm(uid, msg)
        except (intern_delivery.DmForbidden, intern_delivery.DmTransient):
            await interaction.edit_original_response(content=intern_text.dm_blocked_text(),
                                                     allowed_mentions=intern_ui.NO_MENTIONS)
            return
        intern_store.reset_dm_failures(intern_ui.db, uid)
        await interaction.edit_original_response(content=intern_text.dm_retry_ok(), view=None,
                                                 allowed_mentions=intern_ui.NO_MENTIONS)


class ResumeNowView(intern_ui.OwnedView):
    """Resume now, under "Paused until ..." (J7). Not persistent: the pause stands without it."""

    @discord.ui.button(label="Resume now", style=discord.ButtonStyle.primary)
    async def resume(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
            return
        intern_store.set_paused_until(intern_ui.db, interaction.user.id, None, time.time())
        self.stop()
        await interaction.response.edit_message(content=intern_text.alert_reply("resumed"),
                                                view=None, allowed_mentions=intern_ui.NO_MENTIONS)


# ------------------------------------------------------------------ the loop (5.1) and hooks (2.6)

_stale_logged_at: float | None = None       # when B6 last logged a stale sweep


async def _step(context: str, work: Callable[[], Awaitable[None]]) -> None:
    """One part of a delivery tick. Its failure is logged by type and costs only that part."""
    try:
        await work()
    except Exception as error:       # the loop must never die, nor one part stop the next
        intern_ui.log_failure(context, error)


async def _housekeeping(db: sqlite3.Connection, now: float) -> None:
    if intern_delivery.housekeeping_due(db, now):
        counts = intern_delivery.run_housekeeping(db, now)
        print("internship finder: housekeeping " + ", ".join(f"{k} {v}" for k, v in counts.items()),
              file=sys.stderr)


def _guard_bootstrap(db: sqlite3.Connection, pconn: sqlite3.Connection) -> None:
    """
    B3. A bootstrap stamps every seed row with one `first_seen`, and `seen` is never
    pruned, so a ledger whose oldest `first_seen` is newer than the recorded floor, or
    with no floor at all, began with a bootstrap the bot did not see. Every cursor
    rises to that `first_seen`, and since offers need `cursor < first_seen`, the seed
    is never news. On the file carried over it only records the floor, and after that
    it is a no-op: advance_all_cursors takes the MAX of both and never lowers anything.
    """
    oldest = postings_contract.first_seen_floor(pconn)
    if oldest is None:
        return
    floor = intern_store.get_meta(db, intern_store.CURSOR_FLOOR_KEY)
    if floor is None or oldest > floor:
        intern_store.advance_all_cursors(db, oldest)


async def _alerts(db: sqlite3.Connection, now: float) -> None:
    # The guard reads postings.db before anything else: a file that cannot be read
    # raises here, and the tick ends with every cursor where it was (B5).
    _guard_bootstrap(db, intern_ui.pconn)
    report = await intern_delivery.run_tick(db, load_window=intern_ui.window,
                                            send_dm=intern_ui.send_dm, now=now,
                                            companies_watched=intern_ui.companies_watched(),
                                            allowed=intern_ui.dm_access())
    for key, value in zip(REPORT_KEYS, (now, report.due, report.sent, report.empty,
                                         report.forbidden)):
        intern_store.set_meta(db, key, value)


async def _notices(db: sqlite3.Connection, now: float, tracker: bool) -> None:
    # Without postings.db there is no window: expiry warnings still go, quiet notes wait.
    await intern_delivery.run_notices(db, load_window=intern_ui.window if tracker else None,
                                      send_dm=intern_ui.send_dm, now=now,
                                      companies_watched=intern_ui.companies_watched(),
                                      allowed=intern_ui.dm_access())


async def _heartbeat(now: float) -> None:
    """B6. A scraper that stopped sweeping looks exactly like a quiet week, so a sweep
    STALE_SWEEPS intervals late is logged, at most once per STALE_LOG_EVERY_S."""
    global _stale_logged_at
    source = intern_ui.source
    if source is None:
        return
    stale = postings_source.stale_for(intern_ui.pconn, source, now)
    if stale is None or (_stale_logged_at is not None
                         and now - _stale_logged_at < STALE_LOG_EVERY_S):
        return
    _stale_logged_at = now
    print(f"internship finder: no sweep recorded for {stale / 3600:.1f}h "
          f"({source.sweeper_label} sweeps every {source.sweep_interval_s // 60}m)",
          file=sys.stderr)


@tasks.loop(minutes=DELIVERY_MINUTES)
async def intern_delivery_loop() -> None:
    """Housekeeping, alerts, notices, each in its own `try` (module docstring)."""
    db, now = intern_ui.db, time.time()
    await _step("housekeeping", lambda: _housekeeping(db, now))
    tracker = intern_ui.ensure_postings(throttle=False)      # never raises
    if tracker:
        await _step("the delivery tick", lambda: _alerts(db, now))
        await _step("the sweep heartbeat", lambda: _heartbeat(now))
    await _step("the delivery notices", lambda: _notices(db, now, tracker))


@intern_delivery_loop.before_loop
async def _delivery_wait_ready() -> None:
    await intern_ui.bot.wait_until_ready()


def member_left(member: discord.Member) -> None:
    """Leaving every server the bot shares pauses alerts; 30 days later the profile goes (2.6)."""
    if intern_ui.intern_error is not None or member.mutual_guilds:
        return
    try:
        intern_store.mark_left(intern_ui.db, member.id, time.time())
    except sqlite3.Error as error:
        intern_ui.log_failure("recording a member leaving", error)


def member_joined(member: discord.Member) -> None:
    if intern_ui.intern_error is not None:
        return
    try:
        intern_store.clear_left(intern_ui.db, member.id)
    except sqlite3.Error as error:
        intern_ui.log_failure("recording a member joining", error)
