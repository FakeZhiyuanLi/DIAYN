"""
postings_source.py
~~~~~~~~~~~~~~~~~~
Where the internship finder reads postings from: the scraper's own
postings.db, at the path the scraper's settings name (`SETTINGS.postings_db`),
opened read-only under the contract (`postings_contract`). The scraper is the
one process that writes it (P5); the bot only reads, so it neither sweeps nor
takes the sweeper's lock.

Everything in the finder that reads more than `postings` itself — the
window's age, the blocklist, the board registry, the Gemini quota, the file's
path — reads it through a `Source`: `ContractSource`, which answers from the
contract tables. The tests hold it to the same answers as the scraper that
wrote them.

**Opening never raises.** `open_from_env` runs while the bot starts; whatever
is wrong with the file, the answer is "the tracker is off, and here is why",
never a bot that will not start.

Nothing here imports discord or aiohttp at import time. The scraper is
imported, for the settings its main() bound, only when `open_from_env` is
given none of its own.
"""

import dataclasses
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import postings_contract as contract
from intern_taxonomy import company_norm

#: The gap between sweeps when the file does not say: the scraper's DEFAULT_INTERVAL_S.
DEFAULT_SWEEP_S = 15 * 60
#: B6: a sweep this many intervals late is reported.
STALE_SWEEPS = 3
#: The Gemini quota day's zone when the file names none it can use: the
#: scraper's own default for LLM_DAY_TZ.
DEFAULT_DAY_TZ = "America/Los_Angeles"
_COMPANY_AT, _SLUG_AT = 2, 1        # columns of a board row: platform, slug, company, sector

#: The protocol, by name, for the test that holds ContractSource to all of it.
SOURCE_MEMBERS = ("window_days", "sweep_interval_s", "sweeper_label", "db_path", "is_blocked",
                  "drop_blocked", "board_companies", "boards_count", "icims_hosts", "quota",
                  "moved")


@dataclasses.dataclass(frozen=True)
class Quota:
    """The Gemini budget the scraper spends, as `/diayn debug` shows it. `today` is
    the `llm_usage.day` key the budget is counted under right now, a date in `zone`: the
    scraper's LLM_DAY_TZ, where the quota resets at midnight."""
    model: str
    rpd: int
    rpm: int
    tpm: int
    today: str
    zone: str


class Source(Protocol):
    """What the finder reads besides `postings` itself. Blocked companies are matched as a
    normalised prefix, the rule `internship_poller.is_blocked_company` documents."""
    window_days: int
    sweep_interval_s: int
    sweeper_label: str                  # the scraper and its version
    db_path: str

    def is_blocked(self, name) -> bool: ...
    def drop_blocked(self, rows: Sequence, company_at: int = 0) -> list: ...
    def board_companies(self) -> tuple[str, ...]: ...    # one per tracked board, in order
    def boards_count(self) -> int: ...                    # what `companies_watched` reports
    def icims_hosts(self) -> frozenset[str]: ...          # iCIMS boards' own hosts
    def quota(self, now: float | None = None) -> Quota: ...
    def moved(self) -> bool: ...          # the path names another file, or it fails the contract


# ------------------------------------------------------------------ opening

def open_from_env(settings=None) -> tuple[sqlite3.Connection | None, "Source | None", str | None]:
    """
    (connection, source, None), or (None, None, why) — never a raise. Opens the file at
    `settings.postings_db` read-only, under the contract. `settings` are the scraper's
    (`internship_poller.Settings`); without them, the ones its main() bound from the
    environment, `internship_poller.SETTINGS`, so the bot and the scraper always name the
    same file, which the contract's `db_path` check insists on.
    """
    try:
        if settings is None:
            import internship_poller
            settings = internship_poller.SETTINGS
        return open_contract(settings.postings_db)
    except Exception as error:          # the bot must start regardless
        return None, None, describe(error)


def open_contract(path: str | None) -> tuple[sqlite3.Connection | None, "Source | None", str | None]:
    """The read-only open, also run again whenever the bot reopens the file. Never raises."""
    if not path:
        return None, None, "no path to postings.db was given"
    conn = None
    try:
        conn = contract.open_readonly(path)
        return conn, ContractSource(conn, path), None
    except Exception as error:
        close_quietly(conn)             # the file went between the open and the stat
        return None, None, describe(error)


def describe(error: BaseException) -> str:
    """The tracker's reason, which users are shown, so it never names a path. The message
    is kept only where it cannot carry one: a ContractError's (written never to), and
    sqlite3's. An OSError keeps its strerror, since its message names the file it failed
    on; anything else keeps its type alone, since nobody vouches for its message."""
    name = type(error).__name__
    if isinstance(error, (contract.ContractError, sqlite3.Error)):
        return f"{name}: {error}"
    if isinstance(error, OSError) and error.strerror:
        return f"{name}: {error.strerror}"
    return name


def close_quietly(conn: sqlite3.Connection | None) -> None:
    """Closes a connection that is being replaced; one that will not close is already gone."""
    try:
        if conn is not None:
            conn.close()
    except sqlite3.Error:
        pass


def board_host(slug: str) -> str:
    """An iCIMS board's slug as the adapter reads it: a host, or an origin that names one."""
    host = slug.strip().rstrip("/").lower()
    for scheme in ("https://", "http://"):
        host = host.removeprefix(scheme)
    return host.split("/", 1)[0]


def _icims_hosts(boards) -> frozenset[str]:
    return frozenset(board_host(b[_SLUG_AT]) for b in boards
                     if len(b) > _SLUG_AT and b[0] == "icims" and isinstance(b[_SLUG_AT], str))


# ------------------------------------------------------------------ the contract tables

@dataclasses.dataclass(frozen=True)
class _Registry:
    """The contract tables as of one `data_version`: re-read only after the scraper commits."""
    version: int
    meta: Mapping[str, str]
    boards: tuple[tuple[str, str, str, str], ...]
    blocked: tuple[str, ...]            # normalised, empties dropped: "" would block everything

    @classmethod
    def read(cls, conn: sqlite3.Connection, version: int) -> "_Registry":
        blocked = {company_norm(name) for name in contract.read_blocked(conn)}
        return cls(version, contract.read_meta(conn), contract.read_boards(conn),
                   tuple(sorted(b for b in blocked if b)))


class ContractSource:
    """The contract tables of the file at `path`, read through `conn` (opened read-only)."""
    window_days = contract.WINDOW_DAYS

    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self._conn, self.db_path = conn, str(path)
        self._inode = contract.inode(path)
        self._registry: _Registry | None = None
        self._checked = contract.data_version(conn)     # the contract held as of this commit

    def _current(self) -> _Registry:
        version = contract.data_version(self._conn)
        if self._registry is None or self._registry.version != version:
            self._registry = _Registry.read(self._conn, version)
        return self._registry

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._current().meta.get(key))
        except (TypeError, ValueError):
            return default

    @property
    def sweep_interval_s(self) -> int:
        return self._int("sweep_interval_s", DEFAULT_SWEEP_S)

    @property
    def sweeper_label(self) -> str:
        version = self._current().meta.get("scraper_version")
        return f"DIAYN {version}" if version else "DIAYN"

    def is_blocked(self, name) -> bool:
        if not isinstance(name, str):
            return False
        candidate = company_norm(name)
        return any(candidate.startswith(b) for b in self._current().blocked)

    def drop_blocked(self, rows: Sequence, company_at: int = 0) -> list:
        return [r for r in rows if not self.is_blocked(r[company_at])]

    def board_companies(self) -> tuple[str, ...]:
        # The scraper publishes the registry after its blocklist; filtered again with the
        # bot's own rule (B7), so the two can never disagree about a board.
        return tuple(b[_COMPANY_AT] for b in self._current().boards
                     if not self.is_blocked(b[_COMPANY_AT]))

    def boards_count(self) -> int:
        return len(set(self.board_companies()))

    def icims_hosts(self) -> frozenset[str]:
        return _icims_hosts(self._current().boards)

    def quota(self, now: float | None = None) -> Quota:
        meta = self._current().meta
        name = meta.get("llm_day_tz") or DEFAULT_DAY_TZ
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            name, zone = DEFAULT_DAY_TZ, ZoneInfo(DEFAULT_DAY_TZ)
        when = datetime.now(zone) if now is None else datetime.fromtimestamp(now, zone)
        return Quota(meta.get("gemini_model") or "unknown", self._int("llm_rpd", 0),
                     self._int("llm_rpm", 0), self._int("llm_tpm", 0), when.strftime("%Y-%m-%d"),
                     name)

    def moved(self) -> bool:
        """True once this is no longer the file that passed the contract: the path names
        another file (a restore by rename changes the inode), or the file itself fails
        it now (an in-place `.restore` of an older copy keeps the inode). The contract
        is re-checked only after a commit by someone else, so between the scraper's
        commits this costs two PRAGMAs and a stat."""
        try:
            if contract.inode(self.db_path) != self._inode:
                return True
            version = contract.data_version(self._conn)
            if version != self._checked:
                contract.check(self._conn, str(contract.resolve(self.db_path)))
                self._checked = version
            return False
        except (OSError, contract.ContractError, sqlite3.Error):
            return True


# ------------------------------------------------------------------ the heartbeat

def stale_for(conn: sqlite3.Connection, source: Source, now: float) -> float | None:
    """B6: seconds since the last recorded sweep when that is over STALE_SWEEPS intervals,
    else None. No sweep recorded yet is not stale: debug already says "none yet"."""
    last = contract.last_sweep(conn)
    if last is None:
        return None
    age = now - last
    return age if age > STALE_SWEEPS * source.sweep_interval_s else None
