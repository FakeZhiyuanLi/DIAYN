"""
postings_contract.py
~~~~~~~~~~~~~~~~~~~~
The bot's half of the postings.db read contract, v1 (DIAYN's CONTRACT.md):
opening the file the scraper writes, and the few reads the finder makes of it
beyond `postings` itself.

It exists because the scraper is moving to its own repository and process.
While both halves were one module, the schema was whatever `db_init` had just
made; now the bot opens a file somebody else wrote, possibly at another
version, possibly a copy, possibly a restore. Everything the finder assumes
about that file is checked once, here, when it is opened (B2), and a file
that fails raises `ContractError`, which turns off only the tracker.

**Read-only, and never created.** The file is opened as `file:<path>?mode=ro`:
a write raises, and a missing file is refused rather than made. A new, empty
postings.db is a false bootstrap, every open posting "new" on its first sweep,
so no path through this module can leave one behind (B1).

**Nothing here imports the scraper.** The machine-readable half of the
contract is kept in `contract/` and tested against; this module only
knows the names CONTRACT.md lists.
"""

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

USER_VERSION = 2
CONTRACT_VERSION = "1"              # scraper_meta values are text
#: The finder reads the last 30 days; a file pruned sooner would show less than it says.
WINDOW_DAYS = 30
OPEN_TIMEOUT_S = 1

#: The tables and columns the bot may read, and nothing else. llm_cache is read
#: only as COUNT(*), so it needs to exist and nothing more. `postings.rowid` is
#: not a column: it is probed separately, because it is part of the contract (P3).
REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "postings": frozenset({"platform", "external_id", "company", "title", "location", "url",
                           "published", "first_seen", "unbounded"}),
    "seen": frozenset({"platform", "external_id", "first_seen"}),
    "sweeps": frozenset({"started", "duration", "errors", "new_rows"}),
    "llm_usage": frozenset({"day", "n", "prompt_tokens", "output_tokens"}),
    "llm_cache": frozenset(),
    "scraper_meta": frozenset({"key", "value"}),
    "boards": frozenset({"platform", "slug", "company", "sector"}),
    "blocked_companies": frozenset({"name"}),
}


class ContractError(RuntimeError):
    """postings.db is not a file this bot can read under contract v1. The message names
    the check that failed and never a path: it is shown to users as the tracker's reason."""


def open_readonly(path: str | os.PathLike) -> sqlite3.Connection:
    """
    `path`, opened read-only once every B2 check has passed. The checks run on the
    file the path resolves to, symlinks followed, because that is what the scraper
    records as `db_path`. Raises ContractError, or sqlite3.Error for a file that is
    not a database at all.
    """
    real = resolve(path)
    if not real.is_file():
        raise ContractError("postings.db does not exist, and the bot never creates it")
    conn = sqlite3.connect(real.as_uri() + "?mode=ro", uri=True, timeout=OPEN_TIMEOUT_S)
    try:
        check(conn, str(real))
    except BaseException:
        conn.close()
        raise
    return conn


def resolve(path: str | os.PathLike) -> Path:
    """The file `path` names, symlinks followed. Raises ContractError, whose message is
    shown to users: Path.resolve() puts the path in its own (Python 3.12 raises
    RuntimeError("Symlink loop from '<path>'") for a link that points back at itself)."""
    try:
        return Path(path).resolve()
    except (RuntimeError, OSError) as error:
        raise ContractError("POSTINGS_DB cannot be resolved") from error


def check(conn: sqlite3.Connection, real: str) -> None:
    """Every B2 check on an open connection to the file at `real` (resolved); raises
    ContractError for the first that fails. A few PRAGMAs and one small SELECT: cheap
    enough to run again whenever the file changes, as ContractSource does."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != USER_VERSION:
        raise ContractError(f"postings.db is user_version {version}, expected {USER_VERSION}")
    missing = _missing(conn)
    if missing:
        raise ContractError("postings.db lacks " + ", ".join(missing)
                            + " (a file from before the contract needs DIAYN's upgrade-db)")
    meta = read_meta(conn)
    if meta.get("contract_version") != CONTRACT_VERSION:
        raise ContractError(f"postings.db is contract_version {meta.get('contract_version')!r}, "
                            f"expected {CONTRACT_VERSION!r}")
    if meta.get("db_path") != real:
        raise ContractError("scraper_meta.db_path is not the file POSTINGS_DB names: "
                            "the scraper writes another copy")
    prune_days = _as_int(meta.get("prune_days"))
    if prune_days is None or prune_days < WINDOW_DAYS:
        raise ContractError(f"scraper_meta.prune_days is {meta.get('prune_days')!r}; "
                            f"the finder needs at least {WINDOW_DAYS}")


def _missing(conn: sqlite3.Connection) -> list[str]:
    """Every required table or `table.column` the file does not have, in a stable order."""
    missing = []
    for table, columns in REQUIRED_COLUMNS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing += [table] if not have else [f"{table}.{c}" for c in sorted(columns - have)]
    if "postings" not in missing:
        try:
            conn.execute("SELECT rowid FROM postings LIMIT 0")
        except sqlite3.OperationalError:
            missing.append("postings.rowid")
    return missing


def _as_int(value: str | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ reads

def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """scraper_meta as written: every value is text."""
    return {key: value for key, value in conn.execute("SELECT key, value FROM scraper_meta")}


def read_boards(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    """The registry the scraper polls, after its blocklist, in a stable order."""
    return tuple(conn.execute("SELECT platform, slug, company, sector FROM boards "
                              "ORDER BY rowid"))


def read_blocked(conn: sqlite3.Connection) -> tuple[str, ...]:
    """The blocklist as written. The bot applies it with its own company_norm (B7)."""
    return tuple(name for (name,) in conn.execute("SELECT name FROM blocked_companies "
                                                  "ORDER BY rowid"))


def data_version(conn: sqlite3.Connection) -> int:
    """Changes whenever another connection commits; this connection's own commits never
    change it (B4)."""
    return conn.execute("PRAGMA data_version").fetchone()[0]


def inode(path: str | os.PathLike) -> tuple[int, int]:
    """The identity of the file at `path`. A restore or a copy over the name changes it,
    and the connection already open still reads the old file. Raises OSError."""
    stat = os.stat(path)
    return stat.st_dev, stat.st_ino


def first_seen_floor(conn: sqlite3.Connection) -> float | None:
    """MIN(first_seen) of the ledger, which is never pruned (P2): the bootstrap guard's
    input (B3). None for an empty ledger."""
    return conn.execute("SELECT MIN(first_seen) FROM seen").fetchone()[0]


def last_sweep(conn: sqlite3.Connection) -> float | None:
    """When the newest recorded sweep started (B6). None before the first one."""
    return conn.execute("SELECT MAX(started) FROM sweeps").fetchone()[0]
