"""
Where the finder reads postings from: the scraper's own postings.db, at the
path the scraper's settings name, opened read-only under the contract.

    python3 -m unittest discover -s tests      # no install needed

Three things are pinned here. First, that the bot and the scraper agree on
the file: the bot opens `SETTINGS.postings_db`, the path the scraper writes,
and opens it read-only, and it never sweeps or takes the sweeper's lock
itself. Second, that the contract tables give the bot the same answers as the
scraper that wrote them: the blocklist, the companies, the board count, the
window and the Gemini quota. Third, that no postings.db, however broken, stops
the bot from starting: the answer is "the tracker is off, and here is why",
and the why never names a path.

Every file is made in a temporary directory, by the scraper's own db_init and
publish_registry or by the contract fixture, so nothing opens the real
postings.db. The scraper is imported as it is; tests/bot/__init__.py stubs
aiohttp for a bare `python3`.
"""

import ast
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import internship_poller as poller
import postings_contract as contract
import postings_source as sources
import test_postings_contract as fixture

#: 2026-09-30 03:00 UTC: already the 30th in UTC, still the 29th in Pacific time.
AFTER_UTC_MIDNIGHT = 1_790_737_200.0
ICIMS = ("icims", "https://Careers.Rivian.com/", "Rivian", "industrial")


def scraper_settings(directory, **environ) -> "poller.Settings":
    """The scraper's settings as configure() binds them, every data file inside `directory`."""
    return poller.configure({"DIAYN_DATA": os.path.realpath(directory), **environ})


def scraper_file(settings, boards) -> str:
    """postings.db as the scraper leaves it at start-up: made by db_init, the registry published."""
    with mock.patch.object(poller, "SETTINGS", settings), \
            mock.patch.object(poller, "BOARDS", boards), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        conn = poller.db_init(create=True)
        try:
            poller.publish_registry(conn)
            conn.commit()
        finally:
            conn.close()
    return settings.postings_db


def closing(test: unittest.TestCase, opened: tuple) -> tuple:
    """`opened`, with its connection closed when the test ends."""
    if opened[0] is not None:
        test.addCleanup(opened[0].close)
    return opened


class TheBotOpensTheScrapersFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.settings = scraper_settings(self.dir.name)

    def open(self, settings=None) -> tuple:
        return closing(self, sources.open_from_env(settings or self.settings))

    def at(self, path) -> tuple:
        """Opened as though POSTINGS_DB named `path`."""
        return self.open(poller.configure({"POSTINGS_DB": str(path)}))

    def test_it_opens_the_path_the_scraper_s_settings_name(self):
        scraper_file(self.settings, [ICIMS])

        conn, source, error = self.open()

        self.assertIsNone(error)
        self.assertIsInstance(source, sources.ContractSource)
        self.assertEqual(source.db_path, self.settings.postings_db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0], 1)

    def test_without_settings_it_reads_the_ones_the_scraper_bound(self):
        scraper_file(self.settings, [ICIMS])

        with mock.patch.object(poller, "SETTINGS", self.settings):
            conn, source, error = closing(self, sources.open_from_env())

        self.assertIsNone(error)
        self.assertEqual(source.db_path, self.settings.postings_db)

    def test_the_connection_cannot_write(self):
        scraper_file(self.settings, [ICIMS])
        conn, _, _ = self.open()

        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO blocked_companies VALUES ('Acme')")

    def test_a_missing_file_is_an_error_and_is_not_created(self):
        conn, source, error = self.open()

        self.assertEqual((conn, source), (None, None))
        self.assertIn("does not exist", error)
        self.assertEqual(os.listdir(self.dir.name), [])

    def test_a_garbage_file_is_an_error_not_a_raise(self):
        path = pathlib.Path(self.dir.name) / "postings.db"
        path.write_bytes(b"\x00garbage" * 512)

        conn, source, error = self.at(path)

        self.assertEqual((conn, source), (None, None))
        self.assertTrue(error)

    def test_a_directory_is_an_error_not_a_raise(self):
        conn, source, error = self.at(self.dir.name)

        self.assertEqual((conn, source), (None, None))
        self.assertTrue(error)

    def test_an_os_error_is_named_without_its_path(self):
        # The reason is shown to users ("The internship tracker is disabled: ..."),
        # and an OSError's message carries the server's path.
        path = fixture.contract_db(self.dir.name)
        gone = FileNotFoundError(2, "No such file or directory", "/srv/private/postings.db")

        with mock.patch.object(contract, "inode", side_effect=gone):
            conn, source, error = self.at(path)

        self.assertEqual((conn, source), (None, None))
        self.assertEqual(error, "FileNotFoundError: No such file or directory")

    def test_a_connection_whose_source_cannot_be_built_is_closed(self):
        # The file can be renamed between the open and the stat, and the connection
        # opened for it must not be left for the garbage collector.
        path = fixture.contract_db(self.dir.name)
        real_open, opened = contract.open_readonly, []
        self.addCleanup(lambda: [c.close() for c in opened])

        def recording(p):
            opened.append(real_open(p))
            return opened[-1]

        gone = FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(contract, "open_readonly", side_effect=recording), \
                mock.patch.object(contract, "inode", side_effect=gone):
            conn, source, error = sources.open_contract(str(path))

        self.assertEqual((conn, source), (None, None))
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_a_contract_failure_is_named(self):
        conn, source, error = self.at(fixture.contract_db(self.dir.name, meta={"prune_days": "7"}))

        self.assertIsNone(conn)
        self.assertTrue(error.startswith("ContractError: "), error)

    def test_a_reopen_with_no_path_is_an_error_not_a_raise(self):
        conn, source, error = sources.open_contract("")

        self.assertEqual((conn, source), (None, None))
        self.assertTrue(error)


class TheBotNeverSweeps(unittest.TestCase):
    """One sweeper (P5): the scraper. The bot only reads, so it has no sweep and no lock."""

    def test_there_is_no_in_process_sweep_to_choose(self):
        for name in ("ModuleSource", "sweep_mode", "contract_path", "IN_PROCESS", "EXTERNAL",
                     "sweeper_lock"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(sources, name))

    def test_the_reader_takes_no_file_lock(self):
        tree = ast.parse(pathlib.Path(sources.__file__).read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

        self.assertNotIn("fcntl", imported)


class NoReasonNamesAPath(unittest.TestCase):
    """
    The tracker's reason is shown to every user of `/internships recent`, `matches`
    and `info` ("The internship tracker is disabled: ..."), so it must never carry
    the server's paths. Every way the open can fail is tried here, inside a
    directory whose name is then looked for in the reason.
    """

    PRIVATE = "/srv/private/finder"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = os.path.realpath(self.dir.name)

    def assert_no_path(self, error: str | None) -> None:
        self.assertTrue(error)
        for path in (self.root, self.dir.name, self.PRIVATE):
            self.assertNotIn(path, error)

    def reason(self, path) -> str | None:
        settings = poller.configure({"POSTINGS_DB": str(path)})
        return closing(self, sources.open_from_env(settings))[2]

    def test_every_broken_file(self):
        root = pathlib.Path(self.root)
        (root / "garbage.db").write_bytes(b"\x00garbage" * 512)
        (root / "loop-a").symlink_to(root / "loop-b")
        (root / "loop-b").symlink_to(root / "loop-a")
        refused = fixture.contract_db(self.root, "refused.db", meta={"prune_days": "7"})
        for path in (root / "missing.db", root, root / "garbage.db", root / "loop-a", refused):
            with self.subTest(path=path.name):
                self.assert_no_path(self.reason(path))

    def test_a_path_that_cannot_be_resolved(self):
        # Python 3.12 raises RuntimeError("Symlink loop from '<path>'") out of resolve().
        loop = RuntimeError(f"Symlink loop from '{self.PRIVATE}/postings.db'")
        with mock.patch.object(pathlib.Path, "resolve", side_effect=loop):
            error = self.reason(pathlib.Path(self.root) / "postings.db")
        self.assertEqual(error, "ContractError: POSTINGS_DB cannot be resolved")

    def test_describe_keeps_a_message_only_where_it_names_no_file(self):
        for error, reason in (
                (FileNotFoundError(2, "No such file or directory", f"{self.PRIVATE}/postings.db"),
                 "FileNotFoundError: No such file or directory"),
                (OSError(f"{self.PRIVATE}/postings.db"), "OSError"),
                (RuntimeError(f"Symlink loop from '{self.PRIVATE}/postings.db'"), "RuntimeError"),
                (ValueError(f"bad value in {self.PRIVATE}/.env"), "ValueError"),
                (contract.ContractError("postings.db lacks boards"),
                 "ContractError: postings.db lacks boards"),
                (sqlite3.OperationalError("database is locked"),
                 "OperationalError: database is locked")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(sources.describe(error), reason)


class TheScraperAndTheReaderAgree(unittest.TestCase):
    """
    The registry, blocklist, window and quota, read back through the contract,
    against the scraper that wrote them: its seed boards as its start-up binds
    them, plus an iCIMS board on a company's own careers host.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.settings = scraper_settings(self.dir.name)
        with mock.patch.object(poller, "SETTINGS", self.settings), \
                contextlib.redirect_stderr(io.StringIO()):
            self.boards = [*poller.load_boards(), ICIMS]
        scraper_file(self.settings, self.boards)
        conn, self.source, error = closing(self, sources.open_from_env(self.settings))
        self.assertIsNone(error)
        self.names = [*{b[2] for b in self.boards}, "Rocket Lab USA, Inc.",
                      "rocketlab/wd1/RocketLab_Careers", "Astro Rocket Labs", "", None, 7]

    def test_the_blocklist(self):
        for name in self.names:
            with self.subTest(name=name):
                self.assertEqual(self.source.is_blocked(name), poller.is_blocked_company(name))
        rows = [(n, i) for i, n in enumerate(self.names)]
        self.assertEqual(self.source.drop_blocked(rows), poller.drop_blocked(rows))
        self.assertTrue(self.source.is_blocked("Rocket Lab USA"))

    def test_the_companies_and_the_board_count(self):
        companies = tuple(b[2] for b in self.boards)

        self.assertEqual(self.source.board_companies(), companies)
        self.assertEqual(self.source.boards_count(), len(set(companies)))
        self.assertGreater(self.source.boards_count(), 50)

    def test_the_icims_board_hosts(self):
        expected = {sources.board_host(b[1]) for b in self.boards if b[0] == "icims"}

        self.assertEqual(self.source.icims_hosts(), expected)
        self.assertIn("careers.rivian.com", self.source.icims_hosts())

    def test_the_window_and_the_sweep_interval(self):
        self.assertEqual(self.source.window_days, poller.MAX_AGE_DAYS)
        self.assertEqual(self.source.window_days, contract.WINDOW_DAYS)
        self.assertEqual(self.source.sweep_interval_s, poller.DEFAULT_INTERVAL_S)
        self.assertEqual(sources.DEFAULT_SWEEP_S, poller.DEFAULT_INTERVAL_S)

    def test_the_quota_is_the_scraper_s_budget_on_the_scraper_s_day(self):
        with mock.patch.object(poller, "SETTINGS", self.settings):
            today = poller.quota_day(AFTER_UTC_MIDNIGHT)

        quota = self.source.quota(AFTER_UTC_MIDNIGHT)

        self.assertEqual(quota, sources.Quota(
            self.settings.gemini_model, self.settings.llm_rpd, self.settings.llm_rpm,
            self.settings.llm_tpm, today, self.settings.llm_day_tz))
        self.assertEqual((quota.today, quota.zone), ("2026-09-29", "America/Los_Angeles"))

    def test_the_quota_day_follows_llm_day_tz_not_the_bot_s_zone(self):
        settings = scraper_settings(self.dir.name, LLM_DAY_TZ="UTC",
                                    DIAYN_TZ="America/Los_Angeles")
        os.remove(settings.postings_db)
        scraper_file(settings, self.boards)
        _, source, _ = closing(self, sources.open_from_env(settings))

        quota = source.quota(AFTER_UTC_MIDNIGHT)

        self.assertEqual((quota.today, quota.zone), ("2026-09-30", "UTC"))

    def test_the_fallback_zone_is_the_scraper_s_default(self):
        self.assertEqual(sources.DEFAULT_DAY_TZ, poller.Settings().llm_day_tz)

    def test_it_offers_the_whole_protocol(self):
        for member in sources.SOURCE_MEMBERS:
            with self.subTest(member=member):
                self.assertTrue(hasattr(self.source, member))


class TheContractSource(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = fixture.contract_db(self.dir.name)
        conn = contract.open_readonly(self.path)
        self.addCleanup(conn.close)
        self.source = sources.ContractSource(conn, str(self.path))

    def test_the_blocklist_follows_the_scrapers_next_commit(self):
        self.assertFalse(self.source.is_blocked("Acme Corp"))
        fixture.execute(self.path, "INSERT INTO blocked_companies VALUES ('Acme')")
        self.assertTrue(self.source.is_blocked("Acme Corp"))
        self.assertEqual(self.source.board_companies(), ("Kimley-Horn", "Rivian", "Boeing"))

    def test_the_company_norm_cases(self):
        cases = json.loads((fixture.FIXTURES / "company_norm_cases.json").read_text("utf-8"))
        for case in cases["prefix"]:
            fixture.execute(self.path, "DELETE FROM blocked_companies")
            for name in case["blocked_companies"]:
                fixture.execute(self.path, "INSERT INTO blocked_companies VALUES (?)", name)
            for name in case["blocked"]:
                with self.subTest(blocklist=case["blocked_companies"], blocked=name):
                    self.assertTrue(self.source.is_blocked(name))
            for name in case["allowed"]:
                with self.subTest(blocklist=case["blocked_companies"], allowed=name):
                    self.assertFalse(self.source.is_blocked(name))

    def test_the_quota_is_keyed_by_the_scrapers_zone_not_the_box(self):
        quota = self.source.quota(AFTER_UTC_MIDNIGHT)
        self.assertEqual(quota, sources.Quota("gemini-3.5-flash-lite", 250, 5, 250000, "2026-09-29",
                                              "America/Los_Angeles"))
        fixture.execute(self.path, "UPDATE scraper_meta SET value = 'UTC' WHERE key = 'llm_day_tz'")
        quota = self.source.quota(AFTER_UTC_MIDNIGHT)
        self.assertEqual((quota.today, quota.zone), ("2026-09-30", "UTC"))

    def test_an_unknown_zone_falls_back_to_the_scraper_s_default(self):
        fixture.execute(self.path, "UPDATE scraper_meta SET value = 'Mars/Olympus' "
                                   "WHERE key = 'llm_day_tz'")
        quota = self.source.quota(AFTER_UTC_MIDNIGHT)
        self.assertEqual((quota.today, quota.zone), ("2026-09-29", sources.DEFAULT_DAY_TZ))

    def test_the_sweeper_is_named_with_its_version(self):
        self.assertEqual(self.source.sweeper_label, "DIAYN 1.0.0")
        self.assertEqual(self.source.sweep_interval_s, 900)

    def test_it_notices_the_file_being_replaced(self):
        self.assertFalse(self.source.moved())
        os.replace(fixture.contract_db(self.dir.name, "restored.db"), self.path)
        self.assertTrue(self.source.moved())

    def test_it_notices_the_file_being_removed(self):
        os.remove(self.path)
        self.assertTrue(self.source.moved())


class TheHeartbeat(unittest.TestCase):
    """B6: a sweep three intervals late is worth the host's attention."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = fixture.contract_db(self.dir.name)
        self.conn = contract.open_readonly(self.path)
        self.addCleanup(self.conn.close)
        self.source = sources.ContractSource(self.conn, str(self.path))

    def swept(self, started) -> None:
        fixture.execute(self.path, "INSERT INTO sweeps VALUES (?, 1, 0, 0, 0, 0)", started)

    def test_no_sweep_yet_is_not_stale(self):
        self.assertIsNone(sources.stale_for(self.conn, self.source, fixture.NOW))

    def test_a_recent_sweep_is_not_stale(self):
        self.swept(fixture.NOW - 2 * 900)
        self.assertIsNone(sources.stale_for(self.conn, self.source, fixture.NOW))

    def test_three_intervals_late_is_stale(self):
        self.swept(fixture.NOW - 3 * 900 - 1)
        self.assertEqual(sources.stale_for(self.conn, self.source, fixture.NOW), 3 * 900 + 1)


if __name__ == "__main__":
    unittest.main()
