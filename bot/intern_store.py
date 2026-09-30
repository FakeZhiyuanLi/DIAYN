"""
intern_store.py
~~~~~~~~~~~~~~~
Internship profiles on disk: the finder's tables in DIAYN's own users.db
(`intern_profiles`, `intern_seen` and `intern_meta`), and every read and write
of them. `init_db` creates all three; nothing else creates or alters them.

Split from the Discord modules because it is sqlite3 and nothing else, so its
rules can be tested under bare `python3` against an in-memory database.

Three rules shape every function here.

**Named columns, and only the columns a caller owns.** A card rendered ten
minutes ago still holds the profile as it was then. `save` therefore writes
only what a user edits; `alerts` and `alert_hour` go through `set_alerts`,
which owns the cursor rules; delivery bookkeeping is written only by the
delivery helpers. A stale card can change a user's fields. It can never rewind
their cursor or forget a refused DM.

**A deleted user stays deleted.** `/internships delete` can land while a DM to
that user is in flight. Every delivery write is an UPDATE of an existing row,
or an INSERT ... SELECT that finds nothing once the profile is gone, so a send
that finishes after the delete records nothing. `delete_user` clears every
`intern_*` table with a `user_id` column — found when it runs, not listed
here, so a table added later is covered — in one transaction, on the users.db
connection, where `init_db` has SQLite zero freed pages.

**The cursor only moves forward.** Delivery offers postings first seen after
the cursor. Moving it back would re-offer rows the user was already past,
including the seed a bootstrap sweep stepped over, so every write after the
first keeps the larger of the old and new values. The first write is held to
the same seed: `advance_all_cursors` records the bootstrap as a floor that no
cursor written later starts below.

Every function commits its own writes and lets `sqlite3.Error` reach the
caller, which logs the type name only.
"""

import dataclasses
import json
import re
import sqlite3
from collections.abc import Iterable

import intern_vocab as vocab
from intern_profile import (CADENCES, EDITABLE, Profile, legacy_fields, legacy_locations,
                            with_changes)

DAY_S = 86400
SENT_RETAIN_S = 45 * DAY_S
HIDDEN_RETAIN_S = 90 * DAY_S
IDLE_EXPIRE_S = 365 * DAY_S
EXPIRY_WARN_S = 351 * DAY_S
LEFT_GRACE_S = 30 * DAY_S
DM_FAILURE_LIMIT = 3
#: The quiet-period note's silence (spec 5.5); `intern_delivery` should reuse it.
QUIET_AFTER_S = 14 * DAY_S
#: intern_meta: when the latest bootstrap sweep stored its seed. No cursor starts below it.
CURSOR_FLOOR_KEY = "cursor_floor"
#: intern_meta: set once the old tracker's subscribers were imported, to how many profiles
#: the import wrote. Its presence refuses a second import (`write_migrated`).
LEGACY_IMPORT_KEY = "legacy_imported"

#: Every `intern_profiles` column, in table order, as `/internships delete`
#: names it to the user (J11). Plain words, no markdown: the caller bolds them.
STORED_COLUMNS: dict[str, str] = {
    "user_id": "Your Discord id",
    "source": "Where your profile came from",
    "consent_version": "Privacy notice version you agreed to",
    "majors": "Your major(s)",
    "minors": "Your minor(s)",
    "degree": "Your degree",
    "grad_year": "Graduation year",
    "grad_month": "Graduation month",
    "skills": "Your skills",
    "keywords": "Your extra keywords",
    "fields": "Fields you picked",
    "fields_locked": "Fields picked by hand (not re-read from your major)",
    "levels": "Kinds of role you're looking for",
    "levels_locked": "Kinds of role picked by hand (not re-read from your graduation date)",
    "locations": "Where you want to work",
    "terms": "Terms you picked",
    "companies_only": "Only these companies",
    "companies_hidden": "Companies you hid",
    "alerts": "How often I DM you",
    "alert_hour": "Hour of day for alerts",
    "min_score": "Lowest match you're alerted about",
    "paused_until": "Alerts paused until",
    "cursor": "When I last checked for new roles for you",
    "last_run_at": "When I last ran your alerts",
    "last_sent_at": "When I last sent you an alert",
    "last_quiet_at": "When I last sent you a nothing-new note",
    "dm_failures": "DMs to you that failed in a row",
    "intro_pending": "Whether the note about the new finder is still to come",
    "left_at": "When you left the last server you shared with this bot",
    "expiry_warned_at": "When I warned you an unused profile would be deleted",
    "created_at": "When your profile was created",
    "updated_at": "When you last changed your profile",
    "active_at": "When you last used the finder",
}

_PROFILES_DDL = """
    CREATE TABLE IF NOT EXISTS intern_profiles (
        user_id          INTEGER PRIMARY KEY,
        source           TEXT    NOT NULL CHECK (source IN ('resume','pasted','manual','migrated')),
        consent_version  INTEGER NOT NULL DEFAULT 0,
        majors           TEXT    NOT NULL DEFAULT '[]',
        minors           TEXT    NOT NULL DEFAULT '[]',
        degree           TEXT    CHECK (degree IN ('associate','bachelor','master','mba','pharmd','phd')),
        grad_year        INTEGER CHECK (grad_year BETWEEN 2000 AND 2100),
        grad_month       INTEGER CHECK (grad_month BETWEEN 1 AND 12),
        skills           TEXT    NOT NULL DEFAULT '[]',
        keywords         TEXT    NOT NULL DEFAULT '[]',
        fields           TEXT    NOT NULL DEFAULT '[]',
        fields_locked    INTEGER NOT NULL DEFAULT 0,
        levels           TEXT    NOT NULL DEFAULT '["intern","coop"]',
        levels_locked    INTEGER NOT NULL DEFAULT 0,
        locations        TEXT    NOT NULL DEFAULT '["us","unlisted"]',
        terms            TEXT    NOT NULL DEFAULT '[]',
        companies_only   TEXT    NOT NULL DEFAULT '[]',
        companies_hidden TEXT    NOT NULL DEFAULT '[]',
        alerts           TEXT    NOT NULL DEFAULT 'daily' CHECK (alerts IN ('hourly','daily','weekly','off')),
        alert_hour       INTEGER NOT NULL DEFAULT 9 CHECK (alert_hour BETWEEN 0 AND 23),
        min_score        INTEGER NOT NULL DEFAULT 60 CHECK (min_score IN (45, 60, 75)),
        paused_until     REAL,
        cursor           REAL    NOT NULL,
        last_run_at      REAL,
        last_sent_at     REAL,
        last_quiet_at    REAL,
        dm_failures      INTEGER NOT NULL DEFAULT 0,
        intro_pending    INTEGER NOT NULL DEFAULT 0,
        left_at          REAL,
        expiry_warned_at REAL,
        created_at       REAL    NOT NULL,
        updated_at       REAL    NOT NULL,
        active_at        REAL    NOT NULL
    )
"""
_SEEN_DDL = """
    CREATE TABLE IF NOT EXISTS intern_seen (
        user_id   INTEGER NOT NULL,
        role_hash TEXT    NOT NULL,
        state     TEXT    NOT NULL CHECK (state IN ('sent','hidden')),
        at        REAL    NOT NULL,
        PRIMARY KEY (user_id, role_hash)
    )
"""
_SEEN_INDEX = "CREATE INDEX IF NOT EXISTS idx_intern_seen_at ON intern_seen(at)"
_META_DDL = """
    CREATE TABLE IF NOT EXISTS intern_meta (
        key TEXT PRIMARY KEY,
        value REAL
    )
"""
#: Future columns, as (name, declaration): added in place by `init_db`, never
#: by recreating the table, so a users.db from an older release keeps its rows.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = ()

_COLUMNS = tuple(STORED_COLUMNS)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM intern_profiles"
_JSON_DEFAULTS: dict[str, tuple[str, ...]] = {
    "majors": (), "minors": (), "skills": (), "keywords": (), "fields": (),
    "levels": vocab.DEFAULT_LEVELS, "locations": vocab.DEFAULT_LOCATIONS, "terms": (),
    "companies_only": (), "companies_hidden": ()}
_BOOLEANS = frozenset({"fields_locked", "levels_locked", "intro_pending"})
#: What `save` may write over an existing row (spec 3.1 "Writes").
_SAVED = tuple(c for c in _COLUMNS
               if c in (EDITABLE - {"alerts", "alert_hour"}) | {"updated_at", "active_at"})
#: Written with every bump of `active_at` over an existing row: using the finder
#: again re-arms the one warning before an idle profile is deleted (spec 5.5).
_REARM = "expiry_warned_at = NULL"
_STATES = ("sent", "hidden")
_MAX_HASH = 64
_TABLE_NAME = re.compile(r"intern_[A-Za-z0-9_]+")
_IF_PROFILE = "WHERE EXISTS (SELECT 1 FROM intern_profiles WHERE user_id = ?)"
_RECORD_SENT = ("INSERT OR IGNORE INTO intern_seen (user_id, role_hash, state, at) "
                f"SELECT ?, ?, 'sent', ? {_IF_PROFILE}")
_HIDE = ("INSERT INTO intern_seen (user_id, role_hash, state, at) "
         f"SELECT ?, ?, 'hidden', ? {_IF_PROFILE} "
         "ON CONFLICT(user_id, role_hash) DO UPDATE SET state = 'hidden', at = excluded.at")
_FLOOR = ("INSERT INTO intern_meta (key, value) VALUES (?, ?) ON CONFLICT(key) "
          "DO UPDATE SET value = MAX(COALESCE(value, excluded.value), excluded.value)")
_SET_META = ("INSERT INTO intern_meta (key, value) VALUES (?, ?) "
             "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
_MIGRATE = ("INSERT OR IGNORE INTO intern_profiles "
            "(user_id, source, consent_version, fields, fields_locked, levels, levels_locked, "
            "locations, alerts, alert_hour, min_score, cursor, last_run_at, intro_pending, "
            "created_at, updated_at, active_at) "
            "VALUES (?, 'migrated', 0, ?, 1, '[\"intern\",\"coop\"]', 1, ?, 'hourly', 9, 45, "
            "?, ?, 1, ?, ?, ?)")


class LegacyImportError(Exception):
    """An import of the old tracker's subscribers that must not report success. The
    message gives counts and reasons only, never a user."""


@dataclasses.dataclass(frozen=True)
class ImportCounts:
    """What `write_migrated` did: every legacy row is either written or already there."""
    legacy: int
    written: int
    already: int


# ------------------------------------------------------------------ schema

def init_db(db: sqlite3.Connection) -> None:
    """
    Creates the finder's tables in users.db if absent, and has SQLite zero the
    pages a delete frees on this connection. Safe on every boot; raises
    sqlite3.Error. Never creates the old tracker's `intern_pings`.
    """
    db.execute("PRAGMA secure_delete=ON")
    for ddl in (_PROFILES_DDL, _SEEN_DDL, _SEEN_INDEX, _META_DDL):
        db.execute(ddl)
    for name, decl in _ADDED_COLUMNS:
        try:
            db.execute(f"ALTER TABLE intern_profiles ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            pass                                 # added by an earlier boot
    db.commit()


def _json(values: Iterable[str]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _json_list(text: object, default: tuple[str, ...]) -> tuple[str, ...]:
    """A JSON list column as a tuple of its strings; anything malformed is the default."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else default


def _to_profile(row: tuple) -> Profile:
    raw = dict(zip(_COLUMNS, row))
    decoded = {column: _json_list(raw[column], default) for column, default in _JSON_DEFAULTS.items()}
    flags = {column: bool(raw[column]) for column in _BOOLEANS}
    return Profile(**{**raw, **decoded, **flags})


def _encode(p: Profile, columns: tuple[str, ...]) -> tuple:
    def value(column: str) -> object:
        v = getattr(p, column)
        if column in _JSON_DEFAULTS:
            return _json(v)
        return int(v) if column in _BOOLEANS else v
    return tuple(value(column) for column in columns)


def _update(db: sqlite3.Connection, user_id: int, assignments: str, params: tuple = ()) -> int:
    with db:
        return db.execute(f"UPDATE intern_profiles SET {assignments} WHERE user_id = ?",
                          (*params, user_id)).rowcount


def _load_many(db: sqlite3.Connection, where: str, params: tuple = ()) -> list[Profile]:
    return [_to_profile(row) for row in db.execute(f"{_SELECT} {where}", params)]


# ------------------------------------------------------------------ profiles

def load(db: sqlite3.Connection, user_id: int) -> Profile | None:
    row = db.execute(f"{_SELECT} WHERE user_id = ?", (user_id,)).fetchone()
    return None if row is None else _to_profile(row)


def save(db: sqlite3.Connection, p: Profile, *, now: float, cursor: float | None = None) -> Profile:
    """
    Upserts `p` and returns what is stored.

    A new row takes every column, with `cursor` (required, else ValueError;
    never below the bootstrap floor) and `last_run_at = now`. An existing row
    takes only the user-editable columns, so the cursor passed for it is
    ignored, and is marked active, which re-arms the expiry warning.
    """
    if cursor is None:
        row = dataclasses.replace(p, updated_at=now, active_at=now)
        sets = ", ".join([*(f"{column} = ?" for column in _SAVED), _REARM])
        if not _update(db, p.user_id, sets, _encode(row, _SAVED)):
            raise ValueError("a new profile needs a cursor: pass intern_delivery.horizon(now)")
        return load(db, p.user_id)
    row = dataclasses.replace(p, cursor=_floored(db, cursor), last_run_at=now, updated_at=now,
                              active_at=now)
    updates = ", ".join([*(f"{column} = excluded.{column}" for column in _SAVED), _REARM])
    with db:
        db.execute(f"INSERT INTO intern_profiles ({', '.join(_COLUMNS)}) "
                   f"VALUES ({', '.join('?' * len(_COLUMNS))}) "
                   f"ON CONFLICT(user_id) DO UPDATE SET {updates}", _encode(row, _COLUMNS))
    return load(db, p.user_id)


def touch(db: sqlite3.Connection, user_id: int, now: float) -> None:
    """Marks the user active, which also re-arms the expiry warning for the next idle year."""
    _update(db, user_id, f"active_at = ?, {_REARM}", (now,))


def _floored(db: sqlite3.Connection, cursor: float) -> float:
    """`cursor`, raised to the latest bootstrap: the seed is never new to anyone."""
    floor = get_meta(db, CURSOR_FLOOR_KEY)
    return cursor if floor is None else max(cursor, floor)


def set_alerts(db: sqlite3.Connection, user_id: int, alerts: str, alert_hour: int, now: float,
               *, cursor: float) -> Profile | None:
    """
    Sets cadence and hour. Turning alerts on — from off, or after DMs were
    refused DM_FAILURE_LIMIT times — restarts delivery from `cursor` and now,
    so the time alerts were off never arrives as a flood.
    """
    if alerts not in CADENCES:
        raise ValueError(f"unknown alert cadence {alerts!r}; expected one of {CADENCES}")
    if isinstance(alert_hour, bool) or not isinstance(alert_hour, int):
        raise ValueError(f"alert hour must be an int, not {alert_hour!r}")
    current = load(db, user_id)
    if current is None:
        return None
    hour = min(max(alert_hour, 0), 23)
    restart = alerts != "off" and (current.alerts == "off" or current.dm_failures >= DM_FAILURE_LIMIT)
    if restart:
        _update(db, user_id, "alerts = ?, alert_hour = ?, updated_at = ?, cursor = MAX(cursor, ?), "
                "last_run_at = ?, dm_failures = 0, paused_until = NULL",
                (alerts, hour, now, _floored(db, cursor), now))
    else:
        _update(db, user_id, "alerts = ?, alert_hour = ?, updated_at = ?", (alerts, hour, now))
    return load(db, user_id)


def set_paused_until(db: sqlite3.Connection, user_id: int, until: float | None, now: float) -> None:
    _update(db, user_id, "paused_until = ?, updated_at = ?", (until, now))


def reset_dm_failures(db: sqlite3.Connection, user_id: int) -> None:
    _update(db, user_id, "dm_failures = 0")


def hide_company(db: sqlite3.Connection, user_id: int, company_norm: str, now: float) -> Profile | None:
    """Adds a company to the hidden list; at the cap the oldest makes room, so "Hidden." is true."""
    current = load(db, user_id)
    if current is None:
        return None
    # with_changes normalises the name exactly as every other company filter is.
    added = with_changes(current, now, companies_hidden=(company_norm,)).companies_hidden
    if not added or added[0] in current.companies_hidden:
        return current
    hidden = (current.companies_hidden + added)[-vocab.MAX_COMPANIES_HIDDEN:]
    _update(db, user_id, "companies_hidden = ?, updated_at = ?", (_json(hidden), now))
    return load(db, user_id)


def _table_names(db: sqlite3.Connection) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


def _user_tables(db: sqlite3.Connection) -> list[str]:
    """Every finder table that names a user, found at run time."""
    finder = [name for name in _table_names(db) if _TABLE_NAME.fullmatch(name)]
    return [name for name in finder
            if "user_id" in {row[1] for row in db.execute(f'PRAGMA table_info("{name}")')}]


def delete_user(db: sqlite3.Connection, user_id: int) -> int:
    """Erases the user from every finder table there is (D19). Returns rows removed."""
    db.execute("PRAGMA secure_delete=ON")        # init_db sets it; this delete depends on it
    tables = _user_tables(db)
    with db:
        # Explicit, so the deletes are one transaction on any connection, not
        # only one whose driver opens a transaction before the first DELETE.
        if not db.in_transaction:
            db.execute("BEGIN")
        return sum(db.execute(f'DELETE FROM "{table}" WHERE user_id = ?', (user_id,)).rowcount
                   for table in tables)


# ------------------------------------------------------------------ delivery ledger

def alerting_profiles(db: sqlite3.Connection) -> list[Profile]:
    return _load_many(db, "WHERE alerts != 'off' ORDER BY user_id")


def seen_hashes(db: sqlite3.Connection, user_id: int,
                states: tuple[str, ...] = _STATES) -> frozenset[str]:
    wanted = tuple(dict.fromkeys(states))
    if set(wanted) - set(_STATES):
        raise ValueError(f"unknown ledger state in {states!r}; expected {_STATES}")
    if not wanted:
        return frozenset()
    marks = ", ".join("?" * len(wanted))
    rows = db.execute(f"SELECT role_hash FROM intern_seen WHERE user_id = ? AND state IN ({marks})",
                      (user_id, *wanted))
    return frozenset(row[0] for row in rows)


def _hashes(hashes: Iterable[str]) -> tuple[str, ...]:
    # Hide menu values come back from a Discord client, so junk is dropped here.
    if isinstance(hashes, (str, bytes)):
        raise TypeError("hashes must be an iterable of strings, not one string")
    return tuple(dict.fromkeys(h for h in hashes if isinstance(h, str) and 0 < len(h) <= _MAX_HASH))


def _write_ledger(db: sqlite3.Connection, sql: str, user_id: int, hashes: Iterable[str],
                  now: float) -> None:
    with db:
        db.executemany(sql, [(user_id, h, now, user_id) for h in _hashes(hashes)])


def record_sent(db: sqlite3.Connection, user_id: int, hashes: Iterable[str], now: float) -> None:
    """Marks groups as sent; never downgrades a hidden one, never writes for a deleted user."""
    _write_ledger(db, _RECORD_SENT, user_id, hashes, now)


def hide(db: sqlite3.Connection, user_id: int, hashes: Iterable[str], now: float) -> None:
    """Marks groups as hidden (upgrading sent ones); never writes for a deleted user."""
    _write_ledger(db, _HIDE, user_id, hashes, now)


def advance(db: sqlite3.Connection, user_id: int, *, cursor: float, now: float, sent: bool,
            clear_pause: bool = False) -> None:
    """One tick's outcome: the cursor moves up, and a sent digest clears the counters."""
    assignments, params = "cursor = MAX(cursor, ?), last_run_at = ?", (cursor, now)
    if sent:
        assignments, params = (assignments + ", last_sent_at = ?, dm_failures = 0, intro_pending = 0",
                               params + (now,))
    if clear_pause:
        assignments += ", paused_until = NULL"
    _update(db, user_id, assignments, params)


def _dm_failures(db: sqlite3.Connection, user_id: int) -> int:
    row = db.execute("SELECT dm_failures FROM intern_profiles WHERE user_id = ?", (user_id,)).fetchone()
    return 0 if row is None else row[0]


def mark_dm_failure(db: sqlite3.Connection, user_id: int, now: float, *, cursor: float) -> int:
    """Counts a refused alert and moves past what it carried. Returns the new count (0: no profile)."""
    _update(db, user_id, "dm_failures = dm_failures + 1, cursor = MAX(cursor, ?), last_run_at = ?",
            (cursor, now))
    return _dm_failures(db, user_id)


def count_dm_failure(db: sqlite3.Connection, user_id: int) -> int:
    """
    Counts a refused notice. It offered no postings, so the cursor and the
    cadence stay where they are: moving them would step past postings no DM
    carried. Returns the new count (0: no profile).
    """
    _update(db, user_id, "dm_failures = dm_failures + 1")
    return _dm_failures(db, user_id)


def mark_quiet_sent(db: sqlite3.Connection, user_id: int, now: float) -> None:
    _update(db, user_id, "last_quiet_at = ?", (now,))


def mark_expiry_warned(db: sqlite3.Connection, user_id: int, now: float) -> None:
    _update(db, user_id, "expiry_warned_at = ?", (now,))


def mark_left(db: sqlite3.Connection, user_id: int, now: float) -> None:
    """Starts the 30 days after leaving; a repeat does not restart them."""
    _update(db, user_id, "left_at = COALESCE(left_at, ?)", (now,))


def clear_left(db: sqlite3.Connection, user_id: int) -> None:
    _update(db, user_id, "left_at = NULL")


def advance_all_cursors(db: sqlite3.Connection, cursor: float) -> int:
    """
    After a bootstrap sweep: the seed it stored is never offered as new. Every
    cursor moves up to `cursor`, which is also kept as the floor for cursors
    written later: a profile saved within SETTLE_S of the sweep would otherwise
    start at a horizon from before the seed. Returns the profiles advanced.
    """
    with db:
        db.execute(_FLOOR, (CURSOR_FLOOR_KEY, cursor))
        return db.execute("UPDATE intern_profiles SET cursor = MAX(cursor, ?)", (cursor,)).rowcount


# ------------------------------------------------------------------ lifecycle

def read_legacy(old: sqlite3.Connection) -> list[tuple]:
    """
    Every subscriber of the old `/internships ping` tracker, as (user_id, categories,
    us_only), from `old`: a connection to that tracker's bot's own stats.db, which the
    caller opens read-only. Nothing is written to it. A database without the tracker's
    `intern_pings` table is refused with LegacyImportError: it is not that bot's file.
    """
    tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "intern_pings" not in tables:
        raise LegacyImportError("that database has no intern_pings table, so it is not the "
                                "old tracker's stats.db")
    return old.execute("SELECT user_id, categories, us_only FROM intern_pings").fetchall()


def write_migrated(db: sqlite3.Connection, rows: Iterable[tuple], now: float, *,
                   cursor: float) -> ImportCounts:
    """
    Gives every legacy subscriber in `rows` (from `read_legacy`) a migrated profile in
    users.db (spec 3.3), starting from `cursor` (pass intern_delivery.horizon(now)),
    never below the bootstrap floor.

    One transaction, all or nothing. INSERT OR IGNORE never overwrites a profile, so a
    subscriber who already has one is counted, not written. Unless what was written
    plus what was already there accounts for every row, nothing is kept and
    LegacyImportError is raised. It runs once: the import is recorded under
    LEGACY_IMPORT_KEY, and a second is refused, so nobody who has since deleted their
    data comes back.
    """
    rows = list(rows)
    with db:
        if not db.in_transaction:
            db.execute("BEGIN")
        if get_meta(db, LEGACY_IMPORT_KEY) is not None:
            raise LegacyImportError("the old tracker's subscribers were already imported into "
                                    "this users.db; a second import could bring back someone "
                                    "who has since deleted their data")
        have = {row[0] for row in db.execute("SELECT user_id FROM intern_profiles")}
        start = _floored(db, cursor)
        fresh = [(uid, _json(legacy_fields(categories)), _json(legacy_locations(us_only)),
                  start, now, now, now, now)
                 for uid, categories, us_only in rows if uid not in have]
        written = db.executemany(_MIGRATE, fresh).rowcount if fresh else 0
        already = sum(1 for uid, *_ in rows if uid in have)
        if written + already != len(rows):
            raise LegacyImportError(f"{written} written and {already} already there do not "
                                    f"account for all {len(rows)} legacy subscribers, so "
                                    "nothing was imported")
        db.execute(_SET_META, (LEGACY_IMPORT_KEY, float(written)))
    return ImportCounts(legacy=len(rows), written=written, already=already)


def _user_ids(db: sqlite3.Connection, where: str, params: tuple) -> list[int]:
    return [row[0] for row in db.execute(f"SELECT user_id FROM intern_profiles WHERE {where}", params)]


def housekeeping(db: sqlite3.Connection, now: float) -> dict[str, int]:
    """Daily: prune the ledger, delete idle profiles and those whose owner left 30 days ago."""
    with db:
        pruned = db.execute(
            "DELETE FROM intern_seen WHERE (state = 'sent' AND at < ?) OR (state = 'hidden' AND at < ?)",
            (now - SENT_RETAIN_S, now - HIDDEN_RETAIN_S)).rowcount
    expired = _user_ids(db, "active_at <= ?", (now - IDLE_EXPIRE_S,))
    for uid in expired:
        delete_user(db, uid)
    left = _user_ids(db, "left_at IS NOT NULL AND left_at < ?", (now - LEFT_GRACE_S,))
    for uid in left:
        delete_user(db, uid)
    return {"seen_pruned": pruned, "expired": len(expired), "left_deleted": len(left)}


def expiring_profiles(db: sqlite3.Connection, now: float) -> list[Profile]:
    return _load_many(db, "WHERE active_at <= ? AND expiry_warned_at IS NULL "
                          "ORDER BY active_at, user_id", (now - EXPIRY_WARN_S,))


def quiet_candidates(db: sqlite3.Connection, now: float) -> list[Profile]:
    """
    Alerting users who have heard nothing for two weeks, longest silence first.
    "Alerting" is read as delivery would: alerts on, DMs not refused, still in
    a server shared with this bot, and not paused.
    """
    silence = "MAX(COALESCE(last_sent_at, 0), created_at)"
    return _load_many(
        db, "WHERE alerts != 'off' AND dm_failures < ? AND left_at IS NULL "
            "AND (paused_until IS NULL OR paused_until <= ?) "
            f"AND {silence} <= ? AND COALESCE(last_quiet_at, 0) <= ? "
            f"ORDER BY {silence}, user_id",
        (DM_FAILURE_LIMIT, now, now - QUIET_AFTER_S, now - QUIET_AFTER_S))


# ------------------------------------------------------------------ reporting

def privacy_rows(db: sqlite3.Connection, user_id: int) -> dict[str, object] | None:
    """Every stored column, decoded, plus the ledger counts: all `/internships delete` shows."""
    p = load(db, user_id)
    if p is None:
        return None
    counts = dict(db.execute("SELECT state, COUNT(*) FROM intern_seen WHERE user_id = ? "
                             "GROUP BY state", (user_id,)).fetchall())
    return {**{column: getattr(p, column) for column in _COLUMNS},
            "sent_count": counts.get("sent", 0), "hidden_count": counts.get("hidden", 0)}


def summary(db: sqlite3.Connection) -> dict[str, int]:
    """Counts for `/internships debug`. Aggregates only: no key or value names a user."""
    rows = db.execute("SELECT alerts, dm_failures, left_at, fields FROM intern_profiles").fetchall()
    chosen = [_json_list(fields, ()) for *_, fields in rows]
    cadences = {cadence: sum(1 for alerts, *_ in rows if alerts == cadence)
                for cadence in ("hourly", "daily", "weekly")}
    return {
        "profiles": len(rows),
        "alerting": sum(1 for alerts, *_ in rows if alerts != "off"),
        **cadences,
        "dm_blocked": sum(1 for _, failures, *_ in rows if failures >= DM_FAILURE_LIMIT),
        "left": sum(1 for _, _, left_at, _ in rows if left_at is not None),
        **{f"field:{field}": sum(1 for picked in chosen if field in picked)
           for field in vocab.FIELD_IDS},
    }


def get_meta(db: sqlite3.Connection, key: str) -> float | None:
    row = db.execute("SELECT value FROM intern_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def set_meta(db: sqlite3.Connection, key: str, value: float) -> None:
    with db:
        db.execute(_SET_META, (key, value))
