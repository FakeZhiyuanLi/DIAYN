"""
The commands that open postings.db, run the way pm2 runs them.

    python3 -m unittest discover -s tests      # no install needed

Two things are pinned here, both about the file the bot reads:

  * P6: `sweep` and `watch` never create a database, and never sweep into an
    empty ledger, unless told to with --init. A sweep into an empty ledger
    records every open posting as new, and the bot then alerts all of them.
  * `upgrade-db` switches the live v2 file to WAL and adds the contract tables
    without renumbering a row or touching a first_seen.

Each test runs the scraper in a child process, against a copy of the module in
a temporary checkout, with POSTINGS_DB pointing into the temporary directory.
The child replaces `fetch_all` before main() runs, so nothing is ever fetched:
by default it fails loudly (exit 99), which is how a refusal test proves the
refusal came before any request. With CANNED_FETCH=1 it returns one posting.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()

import internship_poller as poller  # noqa: E402
from test_contract import contract_schema, schema  # noqa: E402

# Every variable the scraper reads, so a child process starts clean.
SCRAPER_VARIABLES = ("POLLER_ENV_FILE", "POSTINGS_DB", "BOARDS_FILE", "YC_CACHE")
SCRAPER_PREFIXES = ("POLL_", "GEMINI_", "LLM_")

# The child: stub aiohttp if it is missing, load the copied module, replace
# fetch_all, then run main() with the given arguments.
CHILD = """
import importlib.util, os, sys, time
tests, script = sys.argv[1], sys.argv[2]
sys.path.insert(0, tests)
from aiohttp_stub import stub_aiohttp
stub_aiohttp()
spec = importlib.util.spec_from_file_location("internship_poller", script)
poller = importlib.util.module_from_spec(spec)
sys.modules["internship_poller"] = poller
spec.loader.exec_module(poller)

async def fetch_all(etags=None, on_status=None, sector=None):
    if not os.environ.get("CANNED_FETCH"):
        print("fetch_all was called", file=sys.stderr)
        raise SystemExit(99)
    posting = poller.Posting(
        "greenhouse", "1", "Acme", "tech", "Software Engineering Intern",
        "Irvine, CA", "https://job-boards.greenhouse.io/acme/jobs/1",
        time.time() - 3600)
    return [posting], {"ok": 1, "not_modified": 0, "error": 0, "new_etags": {}}

poller.fetch_all = fetch_all
sys.argv = [script] + sys.argv[3:]
poller.main()
"""

# The v2 schema exactly as a live postings.db holds it today, in rollback
# (delete) journal mode, from before the contract tables existed.
V2_SCHEMA = """
CREATE TABLE seen(
  platform TEXT, external_id TEXT, first_seen REAL,
  PRIMARY KEY(platform, external_id));
CREATE TABLE postings(
  platform TEXT, external_id TEXT, company TEXT, sector TEXT, title TEXT,
  location TEXT, url TEXT, category TEXT, term TEXT, region TEXT,
  is_intern INT, is_tech INT, published REAL, unbounded INT,
  first_seen REAL, PRIMARY KEY(platform, external_id));
CREATE INDEX idx_pub ON postings(published);
CREATE TABLE etags(
  platform TEXT, slug TEXT, etag TEXT, PRIMARY KEY(platform, slug));
CREATE TABLE sweeps(
  started REAL, duration REAL, not_modified INT, errors INT,
  new_rows INT, pruned INT);
CREATE TABLE llm_cache(
  hash TEXT PRIMARY KEY, payload TEXT, created REAL);
CREATE TABLE llm_usage(day TEXT PRIMARY KEY, n INT, prompt_tokens INT DEFAULT 0,
  output_tokens INT DEFAULT 0);
PRAGMA user_version = 2;
"""


def v2_fixture(path, rows=True):
    """A v2 postings.db at `path`, in delete mode, with gaps in its rowids.

    Four postings are inserted and the second deleted, as the pruner would,
    so MAX(rowid) is not COUNT(*): a rebuild that renumbered the rows would
    change one without the other.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V2_SCHEMA)
        if rows:
            base = 1785000000.0
            for n in range(1, 5):
                conn.execute(
                    "INSERT INTO seen(platform, external_id, first_seen) "
                    "VALUES('greenhouse', ?, ?)", (str(n), base + n))
                conn.execute(
                    "INSERT INTO postings(platform, external_id, company, sector, "
                    "title, location, url, category, term, region, is_intern, "
                    "is_tech, published, unbounded, first_seen) "
                    "VALUES('greenhouse', ?, 'Acme', 'tech', 'Intern', 'Irvine, CA', "
                    "'https://job-boards.greenhouse.io/acme/jobs/1', 'swe', NULL, "
                    "'us', 1, 1, ?, 0, ?)", (str(n), base, base + n))
            conn.execute("DELETE FROM postings WHERE external_id='2'")
            conn.execute("INSERT INTO sweeps(started, duration, not_modified, errors, "
                         "new_rows, pruned) VALUES(?, 12.5, 3, 0, 4, 0)", (base,))
            conn.execute("INSERT INTO etags(platform, slug, etag) "
                         "VALUES('greenhouse', 'acme', 'W/\"1\"')")
            conn.execute("INSERT INTO llm_cache(hash, payload, created) "
                         "VALUES('abc', '{}', ?)", (base,))
            conn.execute("INSERT INTO llm_usage(day, n) VALUES('2026-09-01', 3)")
        conn.commit()
    finally:
        conn.close()
    return path


def snapshot(path):
    """What upgrade-db must leave alone: every ledger row with its rowid."""
    conn = sqlite3.connect(path)
    try:
        return {
            "seen": conn.execute("SELECT rowid, platform, external_id, first_seen "
                                 "FROM seen ORDER BY rowid").fetchall(),
            "postings": conn.execute("SELECT rowid, platform, external_id, "
                                     "first_seen FROM postings ORDER BY rowid").fetchall(),
            "counts": [conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {t}").fetchone()
                       for t in ("seen", "postings", "sweeps", "etags",
                                 "llm_cache", "llm_usage")],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        }
    finally:
        conn.close()


def pragma(path, name):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        conn.close()


class Cli(unittest.TestCase):
    """`checkout/` holds a copy of the module; `data/` is where the database goes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.checkout = os.path.join(self.tmp, "checkout")
        self.data = os.path.join(self.tmp, "data")
        os.mkdir(self.checkout)
        self.script = shutil.copy(poller.__file__, self.checkout)
        self.db = os.path.join(self.data, "postings.db")

    def _run(self, *args, canned=False):
        env = {k: v for k, v in os.environ.items()
               if k not in SCRAPER_VARIABLES and not k.startswith(SCRAPER_PREFIXES)}
        env.update(PYTHONDONTWRITEBYTECODE="1", POSTINGS_DB=self.db,
                   BOARDS_FILE=os.path.join(self.data, "boards.json"))
        if canned:
            env["CANNED_FETCH"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-c", CHILD, TESTS, self.script, *args],
            cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)

    def _assert_refused(self, result, *phrases):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("fetch_all was called", result.stderr)
        for phrase in phrases:
            self.assertIn(phrase, result.stderr)

    def _empty_ledger(self):
        os.mkdir(self.data)
        return v2_fixture(self.db, rows=False)


class MissingDatabase(Cli):
    def test_sweep_refuses_a_missing_file_and_creates_nothing(self):
        result = self._run("sweep")
        self._assert_refused(result, self.db, "--init")
        self.assertFalse(os.path.exists(self.data))

    def test_watch_refuses_a_missing_file_and_creates_nothing(self):
        result = self._run("watch", "--interval", "1")
        self._assert_refused(result, self.db, "--init")
        self.assertFalse(os.path.exists(self.data))

    def test_sweep_refuses_a_missing_file_in_a_directory_that_exists(self):
        os.mkdir(self.data)
        self._assert_refused(self._run("sweep"), self.db, "--init")
        self.assertEqual(os.listdir(self.data), [])

    def test_no_other_command_creates_one_either(self):
        for command in ("stats", "prune", "llm-diff", "upgrade-db"):
            with self.subTest(command=command):
                self._assert_refused(self._run(command), self.db)
                self.assertFalse(os.path.exists(self.data))

    def test_sweep_init_creates_the_database_and_bootstraps(self):
        result = self._run("sweep", "--init", canned=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pragma(self.db, "journal_mode"), "wal")
        conn = sqlite3.connect(self.db)
        try:
            seen = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            version = conn.execute("SELECT value FROM scraper_meta "
                                   "WHERE key='contract_version'").fetchone()
        finally:
            conn.close()
        self.assertEqual(seen, 1)
        self.assertEqual(version, ("1",))

    def test_once_bootstrapped_sweep_needs_no_init(self):
        self.assertEqual(self._run("sweep", "--init", canned=True).returncode, 0)
        result = self._run("sweep", canned=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class EmptyLedger(Cli):
    def test_sweep_refuses_an_empty_ledger(self):
        self._empty_ledger()
        result = self._run("sweep")
        self._assert_refused(result, "empty", "--init")
        self.assertEqual(pragma(self.db, "user_version"), 2)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0], 0)
        finally:
            conn.close()

    def test_watch_refuses_an_empty_ledger(self):
        self._empty_ledger()
        self._assert_refused(self._run("watch", "--interval", "1"), "empty", "--init")

    def test_sweep_init_accepts_an_empty_ledger(self):
        self._empty_ledger()
        result = self._run("sweep", "--init", canned=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class UpgradeDb(Cli):
    def setUp(self):
        super().setUp()
        os.mkdir(self.data)

    def test_it_keeps_every_row_rowid_and_first_seen(self):
        v2_fixture(self.db)
        before = snapshot(self.db)
        result = self._run("upgrade-db")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.db), before)
        self.assertEqual(before["user_version"], 2)

    def test_it_switches_a_delete_mode_file_to_wal(self):
        v2_fixture(self.db)
        self.assertEqual(pragma(self.db, "journal_mode"), "delete")
        result = self._run("upgrade-db")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pragma(self.db, "journal_mode"), "wal")
        self.assertIn("delete -> wal", result.stdout)

    def test_it_leaves_exactly_the_contract_schema_with_the_tables_filled(self):
        v2_fixture(self.db)
        self.assertEqual(self._run("upgrade-db").returncode, 0)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(schema(conn), contract_schema()[0])
            meta = dict(conn.execute("SELECT key, value FROM scraper_meta"))
            boards = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(meta["contract_version"], "1")
        self.assertEqual(meta["db_path"], os.path.realpath(self.db))
        self.assertGreater(boards, 0)

    def test_it_prints_the_counts_and_max_rowid_before_and_after(self):
        # The operator's own check that nothing moved: three postings, the
        # highest rowid 4, on both sides of the arrow.
        v2_fixture(self.db)
        result = self._run("upgrade-db")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines()
                 if line.startswith("postings:")]
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertEqual(lines[0].count("3 rows, max rowid 4"), 2, lines[0])
        self.assertIn("integrity_check: ok", result.stdout)

    def test_it_can_run_twice(self):
        v2_fixture(self.db)
        self.assertEqual(self._run("upgrade-db").returncode, 0)
        before = snapshot(self.db)
        result = self._run("upgrade-db")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.db), before)
        self.assertIn("wal -> wal", result.stdout)

    def test_it_refuses_a_file_that_is_not_v2(self):
        # A file with no user_version is not a postings.db this scraper made.
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE unrelated(x)")
        conn.commit()
        conn.close()
        result = self._run("upgrade-db")
        self._assert_refused(result, "user_version")
        self.assertEqual(pragma(self.db, "journal_mode"), "delete")
        conn = sqlite3.connect(self.db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertEqual(tables, {"unrelated"})


if __name__ == "__main__":
    unittest.main()
