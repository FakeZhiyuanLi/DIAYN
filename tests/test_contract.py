"""
The read contract: what postings.db promises the bot that reads it.

    python3 -m unittest discover -s tests      # no install needed

The scraper writes postings.db and the BaronChairStair bot reads it, from
another repository and another process. Nothing but this contract holds the two
together, so its machine-readable half lives in contract/ and these tests pin
the code to it:

  * the schema db_init() makes is exactly contract/postings_v1.sql;
  * the three contract tables are filled, and refreshed inside a sweep's own
    transaction;
  * rowids and first_seen survive a second sweep, and no statement in the source
    can renumber postings;
  * `_norm` and the blocklist rule give the verdicts company_norm_cases.json
    records, which the bot checks its own copy of the rule against;
  * every adapter stores URLs in the shape sample_urls.json records.

Every database here is built in a temporary directory. Nothing makes a request:
the adapters run against canned responses, and the sweeps against a canned
fetch.
"""

import ast
import asyncio
import contextlib
import glob
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
CONTRACT = os.path.join(ROOT, "contract")
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()

import internship_poller as poller  # noqa: E402

# Exactly the keys CONTRACT.md lists, written out here rather than taken from
# the module, so a key dropped from the code fails this test.
SCRAPER_META_KEYS = {"contract_version", "scraper_version", "db_path",
                     "prune_days", "sweep_interval_s", "gemini_model", "llm_rpd",
                     "llm_rpm", "llm_tpm", "llm_day_tz", "started_at"}

# The bot's retention window. prune_days below it would delete rows the bot
# still shows.
WINDOW_DAYS = 30

BOARDS = (("greenhouse", "acme", "Acme", "tech"),
          ("lever", "globex", "Globex", "finance"))


def contract_file(name):
    return os.path.join(CONTRACT, name)


def load_json(name):
    with open(contract_file(name), encoding="utf-8") as f:
        return json.load(f)


def schema(conn):
    """Every table's columns and every explicit index, as SQLite reports them.

    Compared through PRAGMA table_info rather than sqlite_master's SQL text, so
    whitespace and a column added by ALTER do not count as differences, while
    a column's name, order, type, default or key does.
    """
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    indexes = sorted(
        (name, table, tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name})")))
        for name, table in conn.execute(
            "SELECT name, tbl_name FROM sqlite_master "
            "WHERE type='index' AND sql IS NOT NULL"))
    return {"tables": {t: conn.execute(f"PRAGMA table_info({t})").fetchall()
                       for t in tables},
            "indexes": indexes}


def contract_schema():
    """The schema contract/postings_v1.sql makes, in memory."""
    conn = sqlite3.connect(":memory:")
    with open(contract_file("postings_v1.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    try:
        return schema(conn), conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


@contextlib.contextmanager
def scraper(directory, boards=BOARDS, **environ):
    """The module booted against a postings.db in `directory`; put back after.

    Binds SETTINGS and BOARDS the way boot() does, from a mapping rather than
    the process environment, so no .env and no real data path is involved.
    Yields the database path.
    """
    path = os.path.join(directory, "postings.db")
    saved = poller.SETTINGS, poller.BOARDS
    poller.SETTINGS = poller.configure({"POSTINGS_DB": path, **environ})
    poller.BOARDS = tuple(boards)
    try:
        yield path
    finally:
        poller.SETTINGS, poller.BOARDS = saved


def posting(external_id, title="Software Engineering Intern", company="Acme"):
    return poller.Posting(
        "greenhouse", external_id, company, "tech", title, "Irvine, CA",
        f"https://job-boards.greenhouse.io/acme/jobs/{external_id}",
        time.time() - 3600)


def canned_fetch(postings):
    """A stand-in for fetch_all that returns `postings` and polls nothing."""
    async def fetch_all(etags=None, on_status=None, sector=None):
        return list(postings), {"ok": 1, "not_modified": 0, "error": 0,
                                "new_etags": {("greenhouse", "acme"): 'W/"1"'}}
    return fetch_all


def sweep(conn, postings, **kwargs):
    with mock.patch.object(poller, "fetch_all", canned_fetch(postings)):
        return asyncio.run(poller.cmd_sweep(conn, quiet=True, **kwargs))


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name


class Schema(TempDirTest):
    def test_db_init_makes_exactly_the_contract_schema(self):
        # The bot vendors postings_v1.sql and builds its fixtures from it, so
        # a column db_init adds, drops or reorders without it is a bot tested
        # against a file that no longer exists.
        expected, _ = contract_schema()
        with scraper(self.dir):
            conn = poller.db_init(create=True)
        try:
            self.assertEqual(schema(conn), expected)
        finally:
            conn.close()

    def test_the_contract_and_db_init_agree_on_user_version_2(self):
        _, contract_version = contract_schema()
        with scraper(self.dir) as path:
            poller.db_init(create=True).close()
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            conn.close()
        self.assertEqual(contract_version, 2)
        self.assertEqual(poller.SCHEMA_VERSION, 2)

    def test_db_init_switches_the_file_to_wal(self):
        # The bot reads while the scraper writes; in WAL mode neither blocks
        # the other, and a reader sees only committed sweeps.
        with scraper(self.dir) as path:
            poller.db_init(create=True).close()
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def test_db_init_waits_five_seconds_for_a_lock(self):
        with scraper(self.dir):
            conn = poller.db_init(create=True)
        try:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        finally:
            conn.close()


class NoSilentDatabase(TempDirTest):
    """P6: the scraper never creates postings.db unless it is asked to."""

    def test_a_missing_file_is_refused_and_nothing_is_created(self):
        # sqlite3.connect creates a file that is not there. Against the wrong
        # path — a typo in POSTINGS_DB, a volume not mounted yet — that is an
        # empty ledger, and the next sweep announces every open posting.
        data = os.path.join(self.dir, "data")
        with scraper(data) as path, self.assertRaises(poller.DatabaseRefused) as caught:
            poller.db_init()
        self.assertIn(path, str(caught.exception))
        self.assertFalse(os.path.exists(data))

    def test_a_missing_file_in_a_directory_that_exists_is_refused_too(self):
        # The data directory is usually there; only the file is missing.
        with scraper(self.dir), self.assertRaises(poller.DatabaseRefused):
            poller.db_init()
        self.assertEqual(os.listdir(self.dir), [])

    def test_create_makes_the_file_and_its_directory(self):
        data = os.path.join(self.dir, "data")
        with scraper(data) as path:
            poller.db_init(create=True).close()
        self.assertTrue(os.path.isfile(path))

    def test_an_existing_file_opens_without_create(self):
        with scraper(self.dir):
            poller.db_init(create=True).close()
            poller.db_init().close()


class ContractTables(TempDirTest):
    def _published(self, **environ):
        with scraper(self.dir, **environ) as path:
            conn = poller.db_init(create=True)
            poller.publish_registry(conn)
            conn.commit()
        return path, conn

    def test_scraper_meta_has_exactly_the_contract_keys(self):
        _, conn = self._published()
        with conn:
            meta = dict(conn.execute("SELECT key, value FROM scraper_meta"))
        conn.close()
        self.assertEqual(set(meta), SCRAPER_META_KEYS)
        self.assertEqual(meta["contract_version"], "1")
        self.assertEqual(meta["scraper_version"], poller.__version__)
        self.assertEqual(meta["llm_day_tz"], "America/Los_Angeles")
        self.assertEqual(meta["gemini_model"], poller.SETTINGS.gemini_model)
        self.assertEqual(meta["sweep_interval_s"], str(poller.DEFAULT_INTERVAL_S))
        float(meta["started_at"])

    def test_db_path_is_the_real_path_the_scraper_opened(self):
        # B2: the bot refuses a file whose db_path is not its own POSTINGS_DB,
        # which is how two processes pointed at different files find out.
        link = os.path.join(self.dir, "link")
        os.mkdir(os.path.join(self.dir, "real"))
        os.symlink(os.path.join(self.dir, "real"), link)
        with scraper(link) as path:
            conn = poller.db_init(create=True)
            poller.publish_registry(conn)
        db_path = conn.execute(
            "SELECT value FROM scraper_meta WHERE key='db_path'").fetchone()[0]
        conn.close()
        self.assertEqual(db_path, os.path.realpath(path))
        self.assertNotEqual(db_path, path)

    def test_the_limits_come_from_the_settings(self):
        _, conn = self._published(GEMINI_RPD="500", GEMINI_RPM="15",
                                  LLM_DAY_TZ="UTC")
        meta = dict(conn.execute("SELECT key, value FROM scraper_meta"))
        conn.close()
        self.assertEqual((meta["llm_rpd"], meta["llm_rpm"], meta["llm_day_tz"]),
                         ("500", "15", "UTC"))

    def test_prune_days_never_reaches_inside_the_bot_s_window(self):
        # P2: the bot shows 30 days of postings. A shorter retention would
        # delete rows it is still showing, and their rowids with them.
        _, conn = self._published()
        prune_days = conn.execute(
            "SELECT value FROM scraper_meta WHERE key='prune_days'").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(int(prune_days), WINDOW_DAYS)
        self.assertGreaterEqual(poller.PRUNE_DAYS, WINDOW_DAYS)

    def test_boards_is_the_registry_the_scraper_polls(self):
        _, conn = self._published()
        rows = set(conn.execute(
            "SELECT platform, slug, company, sector FROM boards"))
        conn.close()
        self.assertEqual(rows, set(BOARDS))

    def test_blocked_companies_is_the_blocklist(self):
        _, conn = self._published()
        names = {r[0] for r in conn.execute("SELECT name FROM blocked_companies")}
        conn.close()
        self.assertEqual(names, set(poller.BLOCKED_COMPANIES))

    def test_a_blank_blocklist_entry_is_not_published(self):
        # An entry that normalises to "" is a prefix of every name. The scraper
        # ignores it; publishing it would hand the bot a block on everything.
        with mock.patch.object(poller, "BLOCKED_COMPANIES", {"Rocket Lab", "", "--"}):
            _, conn = self._published()
        names = {r[0] for r in conn.execute("SELECT name FROM blocked_companies")}
        conn.close()
        self.assertEqual(names, {"Rocket Lab"})

    def test_publishing_again_replaces_rather_than_accumulates(self):
        with scraper(self.dir):
            conn = poller.db_init(create=True)
            poller.publish_registry(conn)
            poller.BOARDS = BOARDS[:1]
            poller.publish_registry(conn)
            conn.commit()
        rows = conn.execute("SELECT platform, slug FROM boards").fetchall()
        keys = conn.execute("SELECT COUNT(*) FROM scraper_meta").fetchone()[0]
        conn.close()
        self.assertEqual(rows, [("greenhouse", "acme")])
        self.assertEqual(keys, len(SCRAPER_META_KEYS))

    def test_a_board_listed_twice_does_not_fail_the_publish(self):
        # boards.json is edited by hand. A repeated row is polled twice, which
        # is harmless; a publish that raised on it would fail every sweep.
        with scraper(self.dir, boards=BOARDS + BOARDS[:1]):
            conn = poller.db_init(create=True)
            poller.publish_registry(conn)
        count = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        conn.close()
        self.assertEqual(count, len(BOARDS))


class Sweeps(TempDirTest):
    def test_a_second_sweep_keeps_existing_rowids_and_first_seen(self):
        # P1 and P3: the rowid is what /internships info offers and what the
        # bot groups by, and first_seen is what delivery compares its cursors
        # against. Either moving under a stored row breaks the bot silently.
        with scraper(self.dir):
            conn = poller.db_init(create=True)
            sweep(conn, [posting("1"), posting("2", "Data Science Intern")])
            before = conn.execute(
                "SELECT rowid, external_id, first_seen FROM postings").fetchall()
            sweep(conn, [posting("1"), posting("2", "Data Science Intern"),
                         posting("3", "Hardware Intern")])
            after = conn.execute(
                "SELECT rowid, external_id, first_seen FROM postings").fetchall()
            seen = dict(conn.execute("SELECT external_id, first_seen FROM seen"))
        conn.close()
        self.assertEqual(after[:2], before)
        self.assertEqual(len(after), 3)
        self.assertGreater(after[2][0], max(r[0] for r in before))
        # P1: the same first_seen in both tables.
        self.assertEqual({r[1]: r[2] for r in after}, seen)

    def test_a_sweep_refreshes_the_contract_tables(self):
        with scraper(self.dir):
            conn = poller.db_init(create=True)
            sweep(conn, [posting("1")], interval=600)
        interval = conn.execute(
            "SELECT value FROM scraper_meta WHERE key='sweep_interval_s'").fetchone()
        boards = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        conn.close()
        self.assertEqual(interval, ("600",))
        self.assertEqual(boards, len(BOARDS))

    def test_the_registry_is_written_inside_the_sweep_s_transaction(self):
        # P8, and P4 with it: the sweep, its prune and its registry commit
        # together or not at all. The row below is old enough to prune; a
        # prune that committed on its own would land even though the sweep
        # failed, and the new rows with it.
        with scraper(self.dir) as path:
            conn = poller.db_init(create=True)
            old = time.time() - 40 * 86400
            conn.execute("INSERT INTO seen(platform, external_id, first_seen) "
                         "VALUES('greenhouse', 'old', ?)", (old,))
            conn.execute("INSERT INTO postings(platform, external_id, company, "
                         "published, unbounded, first_seen) "
                         "VALUES('greenhouse', 'old', 'Acme', ?, 0, ?)", (old, old))
            conn.commit()
            with mock.patch.object(poller, "publish_registry",
                                   side_effect=RuntimeError("publish failed")):
                with self.assertRaises(RuntimeError):
                    sweep(conn, [posting("1")])
        other = sqlite3.connect(path)
        try:
            counts = [other.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("seen", "postings", "sweeps")]
        finally:
            other.close()
            conn.close()
        self.assertEqual(counts, [1, 1, 0])


def code_strings(path):
    """Every string literal in `path` that code could hand to SQLite.

    Docstrings, comments and SQL's own `--` comments are left out, so prose may
    name what the code must never do. Adjacent literals arrive joined, as
    Python joins them.
    """
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    prose = {id(stmt.value) for node in ast.walk(tree)
             if isinstance(getattr(node, "body", None), list)
             for stmt in node.body
             if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)}
    return [re.sub(r"--[^\n]*", "", node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in prose]


INSERT_RE = re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)(\s*\()?", re.I)


class Statements(unittest.TestCase):
    """P3, read off the source: nothing in the scraper can renumber postings."""

    @classmethod
    def setUpClass(cls):
        cls.strings = [s for path in sorted(glob.glob(os.path.join(ROOT, "*.py")))
                       for s in code_strings(path)]

    def test_the_scan_finds_the_scraper_s_sql(self):
        # Without this, a scan that found nothing would pass every test below.
        tables = {m.group(1) for s in self.strings for m in INSERT_RE.finditer(s)}
        self.assertTrue({"seen", "postings", "sweeps", "etags", "llm_cache",
                         "llm_usage", "boards", "scraper_meta"} <= tables, tables)

    def test_every_insert_names_its_columns(self):
        # A positional INSERT breaks, or worse writes the wrong columns, the
        # day a table gains one — llm_usage already did.
        positional = [m.group(0) for s in self.strings
                      for m in INSERT_RE.finditer(s) if not m.group(2)]
        self.assertEqual(positional, [])

    def test_nothing_vacuums_the_file(self):
        # VACUUM renumbers the rows of a table without an INTEGER PRIMARY KEY.
        self.assertEqual([s for s in self.strings if re.search(r"\bVACUUM\b", s, re.I)],
                         [])

    def test_nothing_replaces_a_row_of_postings(self):
        # REPLACE deletes the row and inserts a new one, with a new rowid.
        self.assertEqual(
            [s for s in self.strings
             if re.search(r"\bREPLACE\s+INTO\s+postings\b", s, re.I)], [])


class LlmCache(TempDirTest):
    """Gemini verdicts are committed batch by batch, and upserted."""

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "postings.db")

    def _classify(self, postings, answer):
        with scraper(self.dir, GEMINI_API_KEY="test-key", GEMINI_BATCH="1"):
            conn = poller.db_init(create=True)
            with mock.patch.object(poller.aiohttp, "ClientSession", self.Session), \
                    mock.patch.object(poller, "_llm_call", answer):
                result = asyncio.run(
                    poller.llm_classify(conn, postings, verbose=False))
        return conn, result

    def _other_process(self):
        return contextlib.closing(sqlite3.connect(self.path))

    @staticmethod
    def verdict(i):
        return {"i": i, "is_intern": True, "is_tech": True, "category": "swe",
                "term": None, "region": "us"}

    def test_each_batch_is_committed_before_the_next_is_requested(self):
        # LlmBudget sleeps between requests to stay inside the per-minute
        # limits. Rows left uncommitted across that sleep hold the write lock,
        # and every other writer waits on a CLI --llm run.
        committed = []

        async def answer(sess, budget, batch):
            with self._other_process() as other:
                committed.append(
                    other.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0])
            return {i: self.verdict(i) for i, _ in batch}

        conn, result = self._classify(
            [posting("1"), posting("2", "Data Science Intern"),
             posting("3", "Hardware Intern")], answer)
        conn.close()
        self.assertEqual(committed, [0, 1, 2])
        self.assertEqual(len(result), 3)

    def test_a_verdict_another_process_cached_first_is_updated(self):
        # Two runs can classify the same posting at once. The second insert
        # must update the row rather than fail the batch.
        p = posting("1")

        async def answer(sess, budget, batch):
            with self._other_process() as other:
                other.execute("INSERT INTO llm_cache(hash, payload, created) "
                              "VALUES(?, '{}', 0)", (poller.posting_hash(p),))
                other.commit()
            return {i: self.verdict(i) for i, _ in batch}

        conn, _ = self._classify([p], answer)
        payload = conn.execute("SELECT payload FROM llm_cache").fetchall()
        conn.close()
        self.assertEqual(len(payload), 1)
        self.assertEqual(json.loads(payload[0][0])["category"], "swe")


# Noon UTC on 30 September 2026: already 1 October at UTC+14, still the early
# hours of 30 September at UTC-11. No box's own zone gives both answers.
MOMENT = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
ZONE_DAYS = {"Pacific/Kiritimati": "2026-10-01", "Pacific/Pago_Pago": "2026-09-30"}


class FixedNow(datetime):
    """`datetime` with its clock stopped at MOMENT, in whatever zone is asked."""

    @classmethod
    def now(cls, tz=None):
        return MOMENT.astimezone(tz) if tz else MOMENT.astimezone().replace(tzinfo=None)


class QuotaDay(TempDirTest):
    """P7: llm_usage.day is the date in llm_day_tz, not the box's own zone.

    The free tier's daily cap resets at midnight Pacific, and the bot's quota
    panel reads today's row by the date in that zone. A scraper counting by
    the VPS's zone (UTC) would start a new row in the afternoon, Pacific
    time, and the panel would show a budget reset hours before Gemini's.
    """

    @staticmethod
    def _usage(conn):
        """Calls on two days: 7 on 1 October, 40 on 30 September."""
        conn.execute("DELETE FROM llm_usage")
        conn.executemany("INSERT INTO llm_usage(day, n) VALUES(?, ?)",
                         [("2026-10-01", 7), ("2026-09-30", 40)])
        conn.commit()

    def test_the_day_is_the_date_in_the_configured_zone(self):
        for zone, day in ZONE_DAYS.items():
            with self.subTest(zone=zone), scraper(self.dir, LLM_DAY_TZ=zone):
                self.assertEqual(poller.quota_day(MOMENT.timestamp()), day)

    def test_the_budget_counts_the_calls_of_that_day(self):
        for zone, (day, used) in {"Pacific/Kiritimati": ("2026-10-01", 7),
                                  "Pacific/Pago_Pago": ("2026-09-30", 40)}.items():
            with self.subTest(zone=zone), scraper(self.dir, LLM_DAY_TZ=zone):
                conn = poller.db_init(create=True)
                self._usage(conn)
                with mock.patch.object(poller, "datetime", FixedNow):
                    budget = poller.LlmBudget(conn)
                conn.close()
                self.assertEqual((budget.day, budget.used), (day, used))

    def test_stats_reports_the_calls_of_that_day(self):
        with scraper(self.dir, LLM_DAY_TZ="Pacific/Kiritimati"):
            conn = poller.db_init(create=True)
            self._usage(conn)
            # stats reports the day's calls only once something is cached.
            conn.execute("INSERT INTO llm_cache(hash, payload, created) "
                         "VALUES('abc', '{}', 0)")
            conn.commit()
            out = io.StringIO()
            with mock.patch.object(poller, "datetime", FixedNow), \
                    contextlib.redirect_stdout(out):
                poller.cmd_stats(conn)
            conn.close()
        self.assertIn("7 api calls today", out.getvalue())


class DetailsLeftToTheBot(unittest.TestCase):
    def test_the_scraper_fetches_no_single_posting(self):
        # Salary and description are fetched by the bot, on demand, for the
        # one role a user asks about. The scraper only sweeps list endpoints.
        for name in ("fetch_details", "strip_html", "find_salary_in_text",
                     "SALARY_TEXT_RE", "GH_JOB_URL_RE", "LEVER_JOB_URL_RE",
                     "ASHBY_JOB_URL_RE", "WD_JOB_URL_RE"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(poller, name))
        self.assertTrue(callable(poller.polite_session))


class CompanyNorm(unittest.TestCase):
    """B7: the bot applies the blocklist itself, so both sides must agree."""

    cases = load_json("company_norm_cases.json")

    def test_norm_matches_the_contract(self):
        for case in self.cases["norm"]:
            with self.subTest(input=case["input"]):
                self.assertEqual(poller._norm(case["input"]), case["norm"])

    def test_the_prefix_rule_matches_the_contract(self):
        for group in self.cases["prefix"]:
            blocklist = poller._blocked_norm(group["blocked_companies"])
            with mock.patch.object(poller, "_BLOCKED_NORM", blocklist):
                for name in group["blocked"]:
                    with self.subTest(blocklist=group["blocked_companies"], name=name):
                        self.assertTrue(poller.is_blocked_company(name))
                for name in group["allowed"]:
                    with self.subTest(blocklist=group["blocked_companies"], name=name):
                        self.assertFalse(poller.is_blocked_company(name))

    def test_the_live_blocklist_is_the_same_rule(self):
        self.assertEqual(poller._BLOCKED_NORM,
                         poller._blocked_norm(poller.BLOCKED_COMPANIES))


class CannedResponse:
    """One HTTP response: awaitable and an async context manager, like aiohttp's."""

    def __init__(self, status, body, content_type="application/json"):
        self.status = status
        self.body = body
        self.headers = {"content-type": content_type}

    async def json(self, content_type=None):
        return self.body

    async def text(self):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __await__(self):
        async def itself():
            return self
        return itself().__await__()


class CannedSession:
    """Answers each request from `routes`, a list of (url fragment, response)."""

    def __init__(self, routes):
        self.routes = routes

    def _answer(self, url):
        for fragment, response in self.routes:
            if fragment in url:
                return response
        return CannedResponse(404, {}, "text/html")

    def get(self, url, **kwargs):
        return self._answer(url)

    def post(self, url, **kwargs):
        return self._answer(url)


def canned_routes(platform, url):
    """What each platform's API answers for one job, whose stored URL is `url`.

    Where the board returns the URL itself, `url` goes in the field the adapter
    must read it from. Where the adapter builds the URL, the job's id is
    written out here, and `url` is not used: the adapter has to build it.
    """
    ok = lambda body: CannedResponse(200, body)  # noqa: E731
    return {
        "greenhouse": [("boards-api.greenhouse.io", ok({"jobs": [{
            "id": 4567890, "title": "Software Engineering Intern",
            "location": {"name": "Irvine, CA"}, "absolute_url": url,
            "first_published": "2026-09-01T09:00:00-07:00"}]}))],
        "lever": [("api.lever.co", ok([{
            "id": "0f8a4c7e-2b1d-4e6a-9c3f-5d7b8a9e1f20",
            "text": "Software Engineering Intern",
            "categories": {"location": "Irvine, CA"}, "hostedUrl": url,
            "createdAt": 1788000000000}]))],
        "ashby": [("api.ashbyhq.com", ok({"jobs": [{
            "id": "6b1e2f3a-4c5d-4e7f-8a9b-0c1d2e3f4a5b",
            "title": "Software Engineering Intern", "location": "Irvine, CA",
            "jobUrl": url, "publishedAt": "2026-09-01T16:00:00Z",
            "isListed": True}]}))],
        "workday": [("myworkdayjobs.com/wday/cxs/acme/Acme_Careers/jobs", ok({
            "total": 1, "jobPostings": [{
                "title": "Software Engineering Intern", "locationsText": "Irvine, CA",
                "externalPath": "/job/Irvine-CA/Software-Engineering-Intern_R12345",
                "postedOn": "Posted Today"}]}))],
        "icims": [("/sitemap.xml", CannedResponse(
            200, f"<urlset><url><loc>{url}</loc></url></urlset>", "text/xml"))],
        "eightfold": [("acme.eightfold.ai/api/pcsx/search", ok({"data": {
            "count": 1, "positions": [{
                "id": 563000123456789, "name": "Software Engineering Intern",
                "locations": ["Irvine, CA"], "postedTs": 1788000000}]}}))],
        "taleo": [("searchjobs", ok({"requisitionList": [{
            "jobId": "2400123",
            "column": ["Software Engineering Intern",
                       '["United States-California-Irvine"]', "Sep 1, 2026"]}]})),
                  ("jobsearch.ftl", CannedResponse(200, "", "text/html"))],
    }[platform]


class SampleUrls(unittest.TestCase):
    """What each adapter stores in postings.url, pinned to sample_urls.json."""

    samples = load_json("sample_urls.json")["platforms"]

    def test_every_platform_has_a_sample(self):
        self.assertEqual(set(self.samples), set(poller.ADAPTERS))

    def test_each_adapter_stores_the_sample_url(self):
        for platform, sample in sorted(self.samples.items()):
            with self.subTest(platform=platform):
                session = CannedSession(canned_routes(platform, sample["url"]))
                status, postings, _ = asyncio.run(poller.ADAPTERS[platform](
                    session, sample["slug"], "Acme", "tech", None))
                self.assertEqual(status, 200)
                self.assertEqual([p.url for p in postings], [sample["url"]])


if __name__ == "__main__":
    unittest.main()
