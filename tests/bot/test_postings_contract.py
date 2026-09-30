"""
The bot's half of the postings.db read contract (DIAYN's CONTRACT.md, v1).

    python3 -m unittest discover -s tests      # no install needed

Once the scraper runs in its own process the bot opens a file somebody else
writes, and every rule that used to hold because both halves were one module
is now a check at open time (B2). A check that passes a file it should refuse
is not an error anywhere: the bot starts, the tracker looks up, and it reads a
database that means something else. So each refusal is pinned by the one
mistake that trips it, on a file otherwise built exactly as DIAYN builds one.

Every file here is made in a temporary directory from the contract's DDL,
`contract/postings_v1.sql`, so a fixture has the live file's shape. Nothing
opens the real postings.db. The other test files that need a contract file
build it with `contract_db` below rather than keeping a second copy.
"""

import os
import pathlib
import sqlite3
import tempfile
import unittest

import postings_contract as contract

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "contract"
NOW = 1_790_000_000.0

#: What DIAYN writes into scraper_meta, less db_path, which is the file's own.
META = {"contract_version": "1", "scraper_version": "1.0.0", "prune_days": "30",
        "sweep_interval_s": "900", "gemini_model": "gemini-3.5-flash-lite", "llm_rpd": "250",
        "llm_rpm": "5", "llm_tpm": "250000", "llm_day_tz": "America/Los_Angeles",
        "started_at": str(NOW)}
BOARDS = (("greenhouse", "acme", "Acme", "tech"), ("lever", "acme", "Acme", "tech"),
          ("icims", "careers-kimley-horn.icims.com", "Kimley-Horn", "industrial"),
          ("icims", "https://careers.rivian.com/", "Rivian", "industrial"),
          ("workday", "boeing/wd1/EXTERNAL_CAREERS", "Boeing", "defense"))
BLOCKED = ("Rocket Lab",)


def ddl() -> str:
    """The contract's schema, as the scraper's db_init() creates it."""
    return (FIXTURES / "postings_v1.sql").read_text(encoding="utf-8")


def contract_db(directory, name: str = "postings.db", *, meta: dict | None = None,
                boards=BOARDS, blocked=BLOCKED) -> pathlib.Path:
    """
    A postings.db as DIAYN leaves it: the v1 schema in WAL mode, scraper_meta
    naming its own real path, the board registry and the blocklist. `meta`
    overrides or, with a None value, removes keys.
    """
    path = pathlib.Path(directory) / name
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl())
        values = {**META, "db_path": os.path.realpath(path), **(meta or {})}
        conn.executemany("INSERT INTO scraper_meta VALUES (?, ?)",
                         [(k, v) for k, v in values.items() if v is not None])
        conn.executemany("INSERT INTO boards VALUES (?, ?, ?, ?)", boards)
        conn.executemany("INSERT INTO blocked_companies VALUES (?)", [(b,) for b in blocked])
        conn.commit()
    finally:
        conn.close()
    return path


def add_seen(path, *rows) -> None:
    """(platform, external_id, first_seen) rows in both ledgers, as one sweep commits them."""
    conn = sqlite3.connect(path)
    try:
        conn.executemany("INSERT INTO seen VALUES (?, ?, ?)", rows)
        conn.executemany("INSERT INTO postings (platform, external_id, company, title, location, "
                         "url, published, unbounded, first_seen) VALUES (?, ?, 'Acme', "
                         "'Software Engineering Intern', 'Irvine, CA', 'https://example.com', "
                         "NULL, 0, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def execute(path, sql: str, *args) -> None:
    """One statement against the file, committed, through a connection of its own."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


class OpenRefusals(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def refused(self, path, words: str) -> None:
        with self.assertRaises(contract.ContractError) as caught:
            contract.open_readonly(path)
        self.assertIn(words, str(caught.exception))

    def test_a_healthy_file_opens(self):
        conn = contract.open_readonly(contract_db(self.dir.name))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0], len(BOARDS))

    def test_a_missing_file_is_refused_and_not_created(self):
        # sqlite3.connect creates what it cannot find: a new, empty file is a
        # false bootstrap, every open posting "new" on the first sweep (P6, B1).
        path = pathlib.Path(self.dir.name) / "postings.db"
        self.refused(path, "does not exist")
        self.assertEqual(sorted(os.listdir(self.dir.name)), [])

    def test_a_write_raises(self):
        conn = contract.open_readonly(contract_db(self.dir.name))
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO blocked_companies VALUES ('Acme')")

    def test_a_wrong_user_version_is_refused(self):
        path = contract_db(self.dir.name)
        execute(path, "PRAGMA user_version = 3")
        self.refused(path, "user_version 3")

    def test_a_wrong_contract_version_is_refused(self):
        self.refused(contract_db(self.dir.name, meta={"contract_version": "2"}), "contract_version")

    def test_a_file_from_before_the_contract_is_refused(self):
        # The root poller's own db_init makes no scraper_meta: upgrade-db first.
        path = contract_db(self.dir.name)
        execute(path, "DROP TABLE scraper_meta")
        self.refused(path, "scraper_meta")

    def test_a_missing_column_is_refused(self):
        path = contract_db(self.dir.name)
        execute(path, "ALTER TABLE postings DROP COLUMN unbounded")
        self.refused(path, "postings.unbounded")

    def test_a_db_path_naming_another_file_is_refused(self):
        # A copy, or a second checkout's file: not the one the scraper writes.
        self.refused(contract_db(self.dir.name, meta={"db_path": "/srv/other/postings.db"}),
                     "db_path")

    def test_a_missing_db_path_is_refused(self):
        self.refused(contract_db(self.dir.name, meta={"db_path": None}), "db_path")

    def test_a_retention_shorter_than_the_window_is_refused(self):
        self.refused(contract_db(self.dir.name, meta={"prune_days": "14"}), "prune_days")

    def test_a_retention_that_is_not_a_number_is_refused(self):
        self.refused(contract_db(self.dir.name, meta={"prune_days": "thirty"}), "prune_days")

    def test_a_symlink_to_the_file_opens(self):
        # db_path is the realpath; POSTINGS_DB may name the file through a link.
        link = pathlib.Path(self.dir.name) / "link.db"
        link.symlink_to(contract_db(self.dir.name))
        contract.open_readonly(link).close()

    def test_a_file_that_is_not_a_database_is_refused(self):
        path = pathlib.Path(self.dir.name) / "postings.db"
        path.write_bytes(b"not a database, but long enough to look like a header" * 4)
        with self.assertRaises((contract.ContractError, sqlite3.DatabaseError)):
            contract.open_readonly(path)


class Reads(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = contract_db(self.dir.name)
        self.conn = contract.open_readonly(self.path)
        self.addCleanup(self.conn.close)

    def test_meta_is_read_as_text(self):
        meta = contract.read_meta(self.conn)
        self.assertEqual(meta["sweep_interval_s"], "900")
        self.assertEqual(meta["db_path"], os.path.realpath(self.path))

    def test_boards_and_the_blocklist_are_read_whole(self):
        self.assertEqual(sorted(contract.read_boards(self.conn)), sorted(BOARDS))
        self.assertEqual(contract.read_blocked(self.conn), BLOCKED)

    def test_data_version_changes_after_another_connection_commits(self):
        before = contract.data_version(self.conn)
        self.assertEqual(contract.data_version(self.conn), before)
        execute(self.path, "INSERT INTO blocked_companies VALUES ('Acme')")
        self.assertNotEqual(contract.data_version(self.conn), before)

    def test_the_bootstrap_floor_is_the_oldest_first_seen(self):
        self.assertIsNone(contract.first_seen_floor(self.conn))
        add_seen(self.path, ("greenhouse", "1", NOW), ("greenhouse", "2", NOW - 60))
        self.assertEqual(contract.first_seen_floor(self.conn), NOW - 60)

    def test_the_last_sweep_is_the_newest_started(self):
        self.assertIsNone(contract.last_sweep(self.conn))
        for started in (NOW - 900, NOW):
            execute(self.path, "INSERT INTO sweeps VALUES (?, 1, 0, 0, 0, 0)", started)
        self.assertEqual(contract.last_sweep(self.conn), NOW)

    def test_inode_changes_when_the_file_is_replaced(self):
        # A restore writes a new file over the name; the open one is not it.
        before = contract.inode(self.path)
        replacement = contract_db(self.dir.name, "restored.db")
        os.replace(replacement, self.path)
        self.assertNotEqual(contract.inode(self.path), before)


class TheContractDDL(unittest.TestCase):
    """The schema in contract/, which every fixture here is built from."""

    def test_the_ddl_has_every_column_the_bot_requires(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(ddl())
        for table, columns in contract.REQUIRED_COLUMNS.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            with self.subTest(table=table):
                self.assertTrue(have, f"{table} is not in the DDL")
                self.assertLessEqual(set(columns), have)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], contract.USER_VERSION)


if __name__ == "__main__":
    unittest.main()
