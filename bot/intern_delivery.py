"""
intern_delivery.py
~~~~~~~~~~~~~~~~~~
When the internship finder DMs someone, what it sends them, and what it
writes down afterwards.

The bot's five-minute delivery loop calls `track_access` (the 30 days a profile
outlives its owner's access), `run_tick` (alerts), `run_notices` (the
quiet-period note and the expiry warning) and, once a day in DIAYN_TZ,
`run_housekeeping`. Everything that decides who is due and what a send, a
refusal or a network failure changes lives here; the loop only supplies
`send_dm`, `load_window`, `allowed` and `check_fit`. Split from the Discord
modules for the reason `intern_store` is: those arrive as arguments, so a test
drives a real tick with fakes under bare `python3`.

**An alert may be checked, never held.** `check_fit` (the Gemini fit check,
`intern_fit.checker`) gets each due user's ranked matches and returns the ones
to send: a no_fit dropped, fit before unsure, each with its reason. It never
raises; when it cannot check, the matches come back as they went. A role it
drops is not sent and not recorded, and the cursor moves past it all the same.

**The check's notice goes first.** Nobody is checked before they have been
shown what the check sends (`intern_fit.enabled`). `fit_notice`
(`intern_fit.notice_due`) says whose alerts would be checked but for that; their
next alert leads with the notice's one line (`intern_text.fit_notice_line`)
and goes out unchecked. A migrated subscriber's introduction says the same, so
it needs no line. Once a DM carrying either has been delivered, the notice is
recorded, and the check starts with the next alert. A DM that was refused, or
never went, records nothing.

Four rules shape it.

**Only those the bot is open to are DMed.** `allowed(user_id)` says whether
someone may use the bot (`access.allowed`, as in a DM), and every entry point
must be given it. Someone without access is never due and never sent a note;
their cursor stays where it was, so access given back brings one digest of
what arrived meanwhile, not a flood. An alert asks again right before its send,
so a revocation during a tick stops the DMs not yet sent.

**Only the settled past is offered, once.** A user's cursor is a `first_seen`
watermark. A tick offers rows first seen after it and at least SETTLE_S ago
(the horizon), so a row a sweep is still committing is picked up next time
rather than stepped over; then the cursor moves to the horizon, and every
group offered goes into the user's ledger, which is what stops a repost or a
late regional copy of the same role (spec 4.6).

**A deleted user stays deleted, and nothing is held open across an await.**
`/internships delete` can land while `send_dm` is in flight. The profile is
re-read immediately before every send, and every write after one goes through
`intern_store`, whose delivery writes are UPDATEs or INSERT ... SELECTs that
find nothing once the profile is gone. Each commits before the next await. So
can Pause, while the fit check or a send is awaited: a catch-up clears only the
pause that has ended, never one pressed since (`intern_store.end_pause`).

**One user's failure is theirs alone.** Each user runs in their own `try`. A
refused DM is counted (three in a row stop the DMs until the user turns them
back on); a transient one changes nothing and is retried next tick; anything
else is logged by its type name only, never an id or the message.

Alert hours are wall-clock hours in DIAYN_TZ (`intern_clock`), the zone the
host sets, and so is the day housekeeping runs once in.

Delivery is at-least-once: a crash between a send and its record repeats one
digest. Pure: the standard library and the finder's pure modules, apart from
reading the zone.
"""

import asyncio
import sqlite3
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import intern_clock
import intern_match
import intern_store as store
import intern_text
from intern_match import Candidate, Match
from intern_profile import Profile

SETTLE_S = 600
DM_MAX_LISTINGS = intern_text.ALERT_LISTINGS_MAX       # 5; format_alert enforces it
MAX_DMS_PER_TICK = 50
SEND_GAP_S = 0.5
QUIET_AFTER_S = store.QUIET_AFTER_S                    # 14 days
MAX_NOTICES_PER_TICK = 20
PAUSE_S = 7 * 86400
#: 59 minutes rather than 60, so a five-minute tick never slips an hourly user an hour.
HOURLY_GAP_S = 3540
HOUSEKEEPING_KEY = "housekeeping_day"

_ALERTING = ("hourly", "daily", "weekly")
_LEDGER = ("sent", "hidden")
#: Outcomes that reached `send_dm`: each counts against a tick's cap and is
#: followed by SEND_GAP_S. "failed" is included because the error may have come
#: from Discord itself.
_ATTEMPTS = frozenset({"sent", "forbidden", "transient", "failed"})
_ONE_DAY, _ONE_WEEK = timedelta(days=1), timedelta(days=7)


def __getattr__(name: str) -> object:
    # `TZ` (spec 6.13), DIAYN_TZ, is looked up on every use rather than at
    # import: ZoneInfo reads the tz database, no finder module does I/O at
    # import, and the zone is the one the scraper's settings hold now.
    if name == "TZ":
        return intern_clock.zone()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class DmForbidden(Exception):
    """The user cannot be DMed (DMs closed, bot blocked, account gone). Counted."""


class DmTransient(Exception):
    """Discord or the network failed this time. Nothing is recorded; the next tick retries."""


@dataclass(frozen=True)
class DmMessage:
    """One DM as `send_dm` receives it. `hide_options` feed the alert's hide menu."""

    text: str
    hide_options: tuple[tuple[str, str], ...]
    with_controls: bool


@dataclass(frozen=True)
class TickReport:
    """What one tick did, as counts only: the loop writes them to `intern_meta`."""

    due: int; sent: int; empty: int; forbidden: int; transient: int; deferred: int


SendDm = Callable[[int, DmMessage], Awaitable[None]]
LoadWindow = Callable[[], Awaitable[list[Candidate]]]
#: Whether a user may use the bot now, and so be DMed (`access.allowed`, as in a DM).
Allowed = Callable[[int], bool]
#: The fit check (`intern_fit.checker`): a user's ranked matches in, the ones to send out.
CheckFit = Callable[[Profile, Sequence[Match], float], Awaitable[Sequence[Match]]]
#: Whether a profile's next alert must carry the fit check's notice (`intern_fit.notice_due`).
FitNotice = Callable[[Profile], bool]
_Window = tuple[Sequence[Candidate], Mapping[int, str]]        # candidates and their group map


# ------------------------------------------------------------------ cadence (5.2)

def horizon(now: float) -> float:
    """The newest `first_seen` a tick may offer; anything younger waits for a later tick."""
    return now - SETTLE_S


def _stopped(p: Profile) -> bool:
    """Never due until the user acts: alerts off, DMs refused DM_FAILURE_LIMIT times, or gone
    from every server they shared with this bot."""
    return (p.alerts not in _ALERTING or p.dm_failures >= store.DM_FAILURE_LIMIT
            or p.left_at is not None)


def _paused(p: Profile, now: float) -> bool:
    return p.paused_until is not None and p.paused_until > now


def catching_up(p: Profile, now: float) -> bool:
    """A pause has ended and its catch-up digest has not run yet."""
    return p.paused_until is not None and p.paused_until <= now


def _slot_on(day: date, hour: int) -> float:
    """`hour`:00 in DIAYN_TZ on `day`. Built from the wall clock, so a DST change moves the
    timestamp and never the hour."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=intern_clock.zone()).timestamp()


def _current_slot(p: Profile, now: float) -> float:
    """Daily: today's slot, possibly still ahead. Weekly: the latest Monday slot at or before now."""
    today = datetime.fromtimestamp(now, intern_clock.zone()).date()
    if p.alerts == "daily":
        return _slot_on(today, p.alert_hour)
    monday = today - timedelta(days=today.weekday())
    slot = _slot_on(monday, p.alert_hour)
    return slot if slot <= now else _slot_on(monday - _ONE_WEEK, p.alert_hour)


def _slot_after(p: Profile, ts: float) -> float:
    """The first daily or weekly slot strictly after `ts`."""
    day = datetime.fromtimestamp(ts, intern_clock.zone()).date()
    step = _ONE_WEEK if p.alerts == "weekly" else _ONE_DAY
    if p.alerts == "weekly":
        day -= timedelta(days=day.weekday())
    slot = _slot_on(day, p.alert_hour)
    return slot if slot > ts else _slot_on(day + step, p.alert_hour)


def is_due(p: Profile, now: float) -> bool:
    """Spec 5.2: whether a tick at `now` should run this user's alerts."""
    if _stopped(p) or _paused(p, now):
        return False
    if catching_up(p, now):
        return True
    last = p.last_run_at or 0.0
    if p.alerts == "hourly":
        return now - last >= HOURLY_GAP_S
    slot = _current_slot(p, now)
    return slot <= now and last < slot


def next_slot(p: Profile, now: float) -> float | None:
    """When `p` is next due as it stands (`now` when it already is); None when it never will be."""
    if _stopped(p):
        return None
    if p.paused_until is not None:
        return max(now, p.paused_until)
    last = p.last_run_at or 0.0
    if p.alerts == "hourly":
        return max(now, last + HOURLY_GAP_S)
    slot = _current_slot(p, now)
    return max(now, slot if last < slot else _slot_after(p, last))


# ------------------------------------------------------------------ one send

async def _isolated(work: Awaitable[str], context: str) -> str:
    """Runs one user's delivery. An unexpected error ends only that user, logged by type."""
    try:
        return await work
    except Exception as error:                   # one user must never stop the tick
        print(f"internship finder: {context} failed: {type(error).__name__}", file=sys.stderr)
        return "failed"


async def _send(db: sqlite3.Connection, send_dm: SendDm, uid: int, msg: DmMessage,
                now: float, *, offers_postings: bool) -> str:
    """
    One DM: "sent", "forbidden" or "transient". A refusal is counted whatever the DM
    carried; an alert's also moves the cursor past the postings it offered (5.4). A
    notice offered none, so its refusal leaves the cursor and the cadence alone: moved,
    they would step past postings no DM ever carried. A transient failure changes nothing.
    """
    try:
        await send_dm(uid, msg)
    except DmForbidden:
        if offers_postings:
            store.mark_dm_failure(db, uid, now, cursor=horizon(now))
        else:
            store.count_dm_failure(db, uid)
        return "forbidden"
    except DmTransient:
        return "transient"
    return "sent"


# ------------------------------------------------------------------ alerts (5.4)

def _digest(p: Profile, matches: Sequence[Match], now: float, catch_up: bool,
            fit_notice: bool) -> DmMessage:
    body, shown = intern_text.format_alert(matches, now, cadence=p.alerts, intro=p.intro_pending,
                                           catch_up=catch_up, expiry_note=None, with_controls=True,
                                           fit_notice=fit_notice)
    return DmMessage(body, intern_text.hide_options(shown), True)


async def _alert(db: sqlite3.Connection, uid: int, cands: Sequence[Candidate],
                 gmap: Mapping[int, str], send_dm: SendDm, now: float, allowed: Allowed,
                 check_fit: CheckFit | None, fit_notice: FitNotice | None) -> str:
    """One user's digest, spec 5.4 step 3. Returns the outcome the report counts."""
    p = store.load(db, uid)
    if p is None or not is_due(p, now) or not allowed(uid):
        return "skipped"
    # The introduction says what the check sends; anyone else not yet told gets the line.
    notice = fit_notice is not None and fit_notice(p) and not p.intro_pending
    edge, catch_up = horizon(now), catching_up(p, now)
    mine = [c for c in cands if p.cursor < c.first_seen <= edge]
    matches = intern_match.rank(p, mine, now, min_score=p.min_score,
                                exclude=store.seen_hashes(db, uid, states=_LEDGER), gmap=gmap)
    if matches and check_fit is not None:
        matches = list(await check_fit(p, matches, now))
    if not matches:
        # Nothing matched, or the check found none of it fits: past it all the same.
        store.advance(db, uid, cursor=edge, now=now, sent=False, clear_pause=catch_up)
        return "empty"
    msg = _digest(p, matches, now, catch_up, notice)
    # The check awaited, and a delete or a revocation may have landed meanwhile:
    # this read is the guard that must stay next to the send.
    fresh = store.load(db, uid)
    if fresh is None or not is_due(fresh, now) or not allowed(uid):
        return "skipped"
    outcome = await _send(db, send_dm, uid, msg, now, offers_postings=True)
    if outcome == "sent":
        # Every group matched, not only the ones the DM had room for (4.5.6).
        store.record_sent(db, uid, [h for m in matches for h in m.ledger], now)
        store.advance(db, uid, cursor=edge, now=now, sent=True, clear_pause=catch_up)
        if notice or p.intro_pending:
            store.mark_fit_notice(db, uid, now)     # it has now been told: checked from here
    elif outcome == "forbidden" and catch_up:
        # The refusal moved the cursor past what the pause held back, so the catch-up is
        # spent; left paused, the user would be due again on every five-minute tick. A
        # pause pressed while the DM was in flight is a new one, and stays.
        store.end_pause(db, uid, now)
    return outcome


async def run_tick(db: sqlite3.Connection, *, load_window: LoadWindow, send_dm: SendDm,
                   now: float, companies_watched: int, allowed: Allowed,
                   check_fit: CheckFit | None = None,
                   fit_notice: FitNotice | None = None) -> TickReport:
    """
    Spec 5.4: DMs every due user what is new to them since their cursor. Only a user
    `allowed` says may use the bot is due (module docstring). With `check_fit`, each
    user's matches pass through it before their digest is written; without it, they are
    sent as ranked. With `fit_notice`, the alert of anyone it names carries the check's
    notice (module docstring); without it, no alert mentions the check.

    At most MAX_DMS_PER_TICK sends are attempted, SEND_GAP_S apart; the due
    users after that keep their cursor and are due again next tick. The window
    is loaded only when somebody is due. `companies_watched` is taken so both
    entry points accept the loop's same arguments; a digest does not quote it.
    """
    due = [p.user_id for p in store.alerting_profiles(db) if is_due(p, now) and allowed(p.user_id)]
    if not due:
        return TickReport(due=0, sent=0, empty=0, forbidden=0, transient=0, deferred=0)
    cands = await load_window()
    gmap = intern_match.group_map(cands)
    tally = Counter()
    for index, uid in enumerate(due):
        if sum(tally[k] for k in _ATTEMPTS) >= MAX_DMS_PER_TICK:
            tally["deferred"] = len(due) - index
            break
        outcome = await _isolated(_alert(db, uid, cands, gmap, send_dm, now, allowed, check_fit,
                                         fit_notice), "an alert")
        tally[outcome] += 1
        if outcome in _ATTEMPTS:
            await asyncio.sleep(SEND_GAP_S)
    return TickReport(due=len(due), sent=tally["sent"], empty=tally["empty"],
                      forbidden=tally["forbidden"], transient=tally["transient"],
                      deferred=tally["deferred"])


# ------------------------------------------------------------------ notices (5.5)

def _quiet_due(p: Profile, now: float) -> bool:
    """The rule `intern_store.quiet_candidates` selects by, re-checked on a fresh read."""
    silence = max(p.last_sent_at or 0.0, p.created_at)
    return (not _stopped(p) and not _paused(p, now) and silence <= now - QUIET_AFTER_S
            and (p.last_quiet_at or 0.0) <= now - QUIET_AFTER_S)


def _expiry_due(p: Profile, now: float) -> bool:
    """The rule `intern_store.expiring_profiles` selects by, re-checked on a fresh read."""
    return p.expiry_warned_at is None and p.active_at <= now - store.EXPIRY_WARN_S


async def _expiry(db: sqlite3.Connection, uid: int, send_dm: SendDm, now: float) -> str:
    p = store.load(db, uid)
    if p is None or not _expiry_due(p, now):
        return "skipped"                         # e.g. they ran a command meanwhile
    msg = DmMessage(intern_text.expiry_note(p.active_at + store.IDLE_EXPIRE_S), (), False)
    outcome = await _send(db, send_dm, uid, msg, now, offers_postings=False)
    if outcome in ("sent", "forbidden"):
        store.mark_expiry_warned(db, uid, now)   # attempted once, whatever the answer
    return outcome


async def _quiet(db: sqlite3.Connection, uid: int, window: _Window, send_dm: SendDm, now: float,
                 companies: int) -> str:
    p = store.load(db, uid)
    if p is None or not _quiet_due(p, now):
        return "skipped"
    cands, gmap = window
    # At the user's alert threshold and past their ledger: "would add N" means N more DMs' worth.
    relax = intern_match.relaxations(p, cands, now, min_score=p.min_score,
                                     exclude=store.seen_hashes(db, uid, states=_LEDGER), gmap=gmap)
    msg = DmMessage(intern_text.quiet_note(p, relax, companies=companies), (), False)
    outcome = await _send(db, send_dm, uid, msg, now, offers_postings=False)
    if outcome == "sent":
        store.mark_quiet_sent(db, uid, now)
    return outcome


async def _notify(kind: str, work: Awaitable[str], tally: Counter) -> None:
    """One notice, isolated like an alert, counted when delivered, then the send gap."""
    outcome = await _isolated(work, f"a {kind} notice")
    if outcome == "sent":
        tally[kind] += 1
    if outcome in _ATTEMPTS:
        await asyncio.sleep(SEND_GAP_S)


async def run_notices(db: sqlite3.Connection, *, load_window: LoadWindow | None, send_dm: SendDm,
                      now: float, companies_watched: int, allowed: Allowed) -> dict[str, int]:
    """
    Spec 5.5: expiry warnings first, then quiet notes by longest silence, at most
    MAX_NOTICES_PER_TICK of them together; the rest wait for the next tick.
    Returns how many of each were delivered. Nobody `allowed` refuses is sent either.

    The warnings go out before the window is read, so a window that fails to
    load cannot hold back the notice before a deletion. `load_window=None`
    (postings.db is down) sends warnings only: nothing is being watched, so
    "still watching" would be untrue.
    """
    budget = MAX_NOTICES_PER_TICK
    expiring = [p.user_id for p in store.expiring_profiles(db, now) if allowed(p.user_id)][:budget]
    quiet = ([p.user_id for p in store.quiet_candidates(db, now)
              if allowed(p.user_id)][:budget - len(expiring)]
             if load_window is not None else [])
    tally = Counter()
    for uid in expiring:
        await _notify("expiry", _expiry(db, uid, send_dm, now), tally)
    if quiet:                                    # only a quiet note's relaxations read the window
        cands = await load_window()
        window: _Window = (cands, intern_match.group_map(cands))
        for uid in quiet:
            await _notify("quiet", _quiet(db, uid, window, send_dm, now, companies_watched), tally)
    return {"quiet": tally["quiet"], "expiry": tally["expiry"]}


# ------------------------------------------------------------------ housekeeping (5.6)

def _local_day(now: float) -> float:
    """`now`'s date in DIAYN_TZ as YYYYMMDD, the number `intern_meta` stores."""
    day = datetime.fromtimestamp(now, intern_clock.zone()).date()
    return float(day.year * 10_000 + day.month * 100 + day.day)


def track_access(db: sqlite3.Connection, allowed: Allowed, now: float) -> dict[str, int]:
    """
    Starts the 30 days (`intern_store.ACCESS_GRACE_S`) for every profile whose owner
    the bot is no longer open to, and stops them for anyone it is open to again; a
    repeat never restarts them. Housekeeping deletes a profile once they run out.
    Returns how many started and stopped.
    """
    lapsed = restored = 0
    for uid, lapsed_at in store.access_states(db):
        if allowed(uid):
            if lapsed_at is not None:
                store.clear_access_lapsed(db, uid)
                restored += 1
        elif lapsed_at is None:
            store.mark_access_lapsed(db, uid, now)
            lapsed += 1
    return {"lapsed": lapsed, "restored": restored}


def housekeeping_due(db: sqlite3.Connection, now: float) -> bool:
    return store.get_meta(db, HOUSEKEEPING_KEY) != _local_day(now)


def run_housekeeping(db: sqlite3.Connection, now: float) -> dict[str, int]:
    """
    `intern_store.housekeeping`, then today's date in DIAYN_TZ recorded so it runs
    once a day. Recorded after, so a run that fails is tried again on the next tick.
    """
    counts = store.housekeeping(db, now)
    store.set_meta(db, HOUSEKEEPING_KEY, _local_day(now))
    return counts
