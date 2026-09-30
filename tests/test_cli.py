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
`polite_session` is replaced too (exit 98), so `discover`, which opens its own
session rather than going through fetch_all, can never reach the network either.
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
from test_contract import contract_schema, plant, schema, untouched  # noqa: E402

# Every variable the scraper reads, so a child process starts clean.
SCRAPER_VARIABLES = ("POLLER_ENV_FILE", "POSTINGS_DB", "BOARDS_FILE", "YC_CACHE",
                     "DISCORD_TOKEN")
SCRAPER_PREFIXES = ("POLL_", "GEMINI_", "LLM_", "DIAYN_", "FIT_")

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

def polite_session(**kwargs):
    print("polite_session was called", file=sys.stderr)
    raise SystemExit(98)

poller.fetch_all = fetch_all
poller.polite_session = polite_session
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

    def _command(self, *args):
        """The child's argv: the scraper copy, run with `args`."""
        return [sys.executable, "-B", "-c", CHILD, TESTS, self.script, *args]

    def _env(self, canned=False):
        """A clean environment, with the data files in this test's `data/`."""
        env = {k: v for k, v in os.environ.items()
               if k not in SCRAPER_VARIABLES and not k.startswith(SCRAPER_PREFIXES)}
        env.update(PYTHONDONTWRITEBYTECODE="1", POSTINGS_DB=self.db,
                   BOARDS_FILE=os.path.join(self.data, "boards.json"))
        if canned:
            env["CANNED_FETCH"] = "1"
        return env

    def _run(self, *args, canned=False):
        return subprocess.run(self._command(*args), cwd=self.tmp,
                              env=self._env(canned), capture_output=True,
                              text=True, timeout=60)

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
        result = self._run("watch", "--interval", "60")
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
        self._assert_refused(self._run("watch", "--interval", "60"), "empty", "--init")

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


class Stats(Cli):
    """`stats` only reads: no WAL switch, no contract tables, no lock (P5).

    It runs beside a sweeper, and before stage 2's upgrade-db beside the bot's
    own in-process sweep, so a write from it would change the live file under
    a writer that holds no lock of ours.
    """

    def setUp(self):
        super().setUp()
        os.mkdir(self.data)

    def _tables(self):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def test_it_leaves_a_pre_contract_file_exactly_as_it_was(self):
        v2_fixture(self.db)
        before, tables = untouched(self.db), self._tables()
        result = self._run("stats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recent sweeps", result.stdout)
        self.assertEqual(untouched(self.db), before)
        self.assertEqual(self._tables(), tables)
        self.assertEqual(before[1], "delete")
        self.assertNotIn("scraper_meta", tables)

    def test_it_changes_nothing_while_a_sweeper_holds_the_lock(self):
        v2_fixture(self.db)
        before = untouched(self.db)
        with poller.sweeper_lock(self.db):
            result = self._run("stats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(untouched(self.db), before)

    def test_it_reads_a_file_the_scraper_has_upgraded(self):
        v2_fixture(self.db)
        self.assertEqual(self._run("upgrade-db").returncode, 0)
        result = self._run("stats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 postings", result.stdout)

    def test_it_refuses_a_file_that_is_not_a_postings_db(self):
        plant(self.db, "CREATE TABLE unrelated(x)")
        before = untouched(self.db)
        result = self._run("stats")
        self._assert_refused(result, self.db)
        self.assertNotIn("--init", result.stderr)
        self.assertEqual(untouched(self.db), before)


class WatchInterval(Cli):
    """--interval is published to the bot, and paces every sweep after the first.

    0 or a negative number would sweep every board back to back, and tell the
    bot to expect it; the floor is a minute.
    """

    def setUp(self):
        super().setUp()
        os.mkdir(self.data)
        v2_fixture(self.db)

    def test_an_interval_under_a_minute_is_refused_before_anything_runs(self):
        before = snapshot(self.db)
        for command in ("watch", "sweep", "upgrade-db"):
            for seconds in ("0", "-60", "59"):
                with self.subTest(command=command, seconds=seconds):
                    result = self._run(command, "--interval", seconds)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("60", result.stderr)
                    self.assertNotIn("fetch_all was called", result.stderr)
        self.assertEqual(snapshot(self.db), before)
        self.assertFalse(os.path.exists(self.db + ".lock"))

    def test_a_minute_is_allowed(self):
        result = self._run("sweep", "--interval", "60", canned=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class Prune(Cli):
    """P2: no command deletes a row the bot still shows."""

    def setUp(self):
        super().setUp()
        os.mkdir(self.data)
        v2_fixture(self.db)

    def test_a_max_age_inside_the_bot_s_window_is_refused(self):
        # 0 used to mean "the default"; now it is refused like any number
        # below 30, rather than quietly read as 30.
        before = snapshot(self.db)
        for days in ("0", "7", "29"):
            with self.subTest(days=days):
                result = self._run("prune", "--max-age", days)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("30", result.stderr)
                self.assertEqual(snapshot(self.db), before)

    def test_thirty_days_or_more_is_allowed(self):
        result = self._run("prune", "--max-age", "30", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would prune", result.stdout)

    def test_prune_itself_refuses_a_shorter_window(self):
        # The same floor for any caller in code, not only the command line.
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaises(ValueError):
                poller.prune(conn, days=29)
        finally:
            conn.close()
        self.assertEqual(snapshot(self.db)["counts"][1], (3, 4))


if __name__ == "__main__":
    unittest.main()
