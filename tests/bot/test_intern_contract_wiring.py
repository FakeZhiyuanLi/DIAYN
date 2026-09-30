"""
The finder reading postings.db through the contract: what the delivery loop,
the window and the commands do with a file the bot only ever reads.

    .venv/bin/python -m unittest discover -s tests -p 'test_intern_contract_wiring.py'

Every rule here is one the bot promises in CONTRACT.md (B2-B6), and each
fails silently when broken. A bootstrap the scraper did becomes a flood of
"new" roles; a read that fails halfway moves cursors past postings nobody was
told about; a restored file is never reopened and the tracker reads the old
one for ever; a scraper that stopped sweeping looks exactly like a quiet week.

Everything runs against a contract file built in a temporary directory from
the contract's DDL and an in-memory users.db, with the DM send faked. These
import the finder's Discord modules, so they skip when discord.py is missing,
the only dependency that earns a skip.
"""

import asyncio
import dataclasses
import io
import os
import time
import pathlib
import sqlite3
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

import access
import intern_delivery
import intern_profile
import intern_store
import postings_contract as contract
import postings_source as sources
import test_postings_contract as fixture

try:
    import discord
except ModuleNotFoundError as missing:  # pragma: no cover - depends on the environment
    if missing.name != "discord":
        raise
    discord = None

#: A stub `discord` another test installs has no `__file__` (see test_intern_surface).
REAL_DISCORD = discord is not None and getattr(discord, "__file__", None) is not None
if REAL_DISCORD:
    import intern_alert_views
    import intern_commands
    import intern_ui
else:  # pragma: no cover - depends on the environment
    intern_alert_views = intern_commands = intern_ui = None

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

DAY = 86400
NOW = fixture.NOW
DUE, IDLE_OFF = 11, 12
#: When the scraper bootstrapped: every seed row carries this one first_seen.
SEEDED_AT = NOW - 3600


def add_postings(path, *rows) -> None:
    """(external_id, company, title, first_seen) rows, in both ledgers, one commit."""
    conn = sqlite3.connect(path)
    try:
        conn.executemany("INSERT INTO seen VALUES ('greenhouse', ?, ?)",
                         [(ext, seen) for ext, _, _, seen in rows])
        conn.executemany(
            "INSERT INTO postings (platform, external_id, company, title, location, url, "
            "published, unbounded, first_seen) VALUES ('greenhouse', ?, ?, ?, 'Irvine, CA', "
            "'https://job-boards.greenhouse.io/acme/jobs/1', NULL, 0, ?)", rows)
        conn.commit()
    finally:
        conn.close()


class _ContractCase(unittest.TestCase):
    """A users.db, a contract file, and intern_ui wired to both as app.py wires them."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = fixture.contract_db(self.dir.name)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        intern_store.init_db(self.db)
        access.init_db(self.db)
        self.sent = []

        async def send_dm(uid, msg):
            self.sent.append((uid, msg.text))

        conn, source, error = sources.open_contract(str(self.path))
        self.assertIsNone(error)
        for target, name, value in (
                (intern_ui, "db", self.db), (intern_ui, "intern_error", None),
                (intern_ui, "pconn", conn), (intern_ui, "source", source),
                (intern_ui, "pconn_error", None), (intern_ui, "postings_path", str(self.path)),
                (intern_ui, "_postings_tried_at", None), (intern_ui, "send_dm", send_dm),
                (intern_ui, "_logged_error", intern_ui._UNSET),
                (intern_alert_views, "time", types.SimpleNamespace(time=lambda: NOW)),
                (intern_alert_views, "_stale_logged_at", None),
                (intern_delivery, "SEND_GAP_S", 0)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # Registered after the patches, so it runs first: whichever connection the
        # test left open, reopened or not, is closed before intern_ui is restored.
        self.addCleanup(lambda: sources.close_quietly(intern_ui.pconn))
        intern_ui.invalidate()
        self.addCleanup(intern_ui.invalidate)

    def enrol(self, uid, at, **fields):
        """A profile whose owner may use the bot (granted by id)."""
        base = intern_profile.new_profile(uid, at, source="manual",
                                          cursor=intern_delivery.horizon(at))
        p = dataclasses.replace(base, fields=("software",), degree="bachelor", **fields)
        intern_store.save(self.db, p, now=at, cursor=intern_delivery.horizon(at))
        access.grant(self.db, "user", uid, granted_by=None, now=at)

    def cursors(self) -> dict:
        return dict(self.db.execute("SELECT user_id, cursor FROM intern_profiles"))

    def run_loop(self) -> str:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            asyncio.run(intern_alert_views.intern_delivery_loop.coro())
        return stderr.getvalue()

    def floor(self):
        return intern_store.get_meta(self.db, intern_store.CURSOR_FLOOR_KEY)


@needs_discord
class TheBootstrapGuard(_ContractCase):
    """B3. The scraper's first sweep stores every open posting; none of them is news."""

    def test_a_bootstrap_by_the_scraper_never_becomes_alerts(self):
        self.enrol(DUE, NOW - 10 * DAY, alerts="hourly")
        add_postings(self.path, ("s1", "Seedco", "Software Engineer Intern", SEEDED_AT),
                     ("s2", "Oldco", "Software Engineer Intern", SEEDED_AT),
                     ("n1", "Newco", "Software Engineer Intern", SEEDED_AT + 60))

        log = self.run_loop()

        self.assertNotIn("failed", log)
        self.assertEqual(self.floor(), SEEDED_AT)
        self.assertEqual([uid for uid, _ in self.sent], [DUE])
        (text,) = [text for _, text in self.sent]
        self.assertIn("Newco", text)                 # found after the bootstrap: news
        self.assertNotIn("Seedco", text)
        self.assertNotIn("Oldco", text)

    def test_the_file_carried_over_moves_no_cursor_and_keeps_the_higher_floor(self):
        self.enrol(IDLE_OFF, NOW - 10 * DAY, alerts="off")
        add_postings(self.path, ("old", "Acme", "Software Engineer Intern", NOW - 60 * DAY))
        intern_store.advance_all_cursors(self.db, NOW - 20 * DAY)   # an in-process bootstrap
        before = self.cursors()

        intern_alert_views._guard_bootstrap(self.db, intern_ui.pconn)

        self.assertEqual(self.floor(), NOW - 20 * DAY)
        self.assertEqual(self.cursors(), before)

    def test_a_first_tick_on_the_carried_file_records_the_floor_and_moves_nothing(self):
        self.enrol(IDLE_OFF, NOW - 10 * DAY, alerts="off")
        add_postings(self.path, ("old", "Acme", "Software Engineer Intern", NOW - 60 * DAY))
        before = self.cursors()

        intern_alert_views._guard_bootstrap(self.db, intern_ui.pconn)

        self.assertEqual(self.floor(), NOW - 60 * DAY)
        self.assertEqual(self.cursors(), before)

    def test_an_empty_ledger_records_nothing(self):
        intern_alert_views._guard_bootstrap(self.db, intern_ui.pconn)
        self.assertIsNone(self.floor())


@needs_discord
class AReadFailureMovesNoCursor(_ContractCase):
    """B5. A tick that cannot read postings.db skips alerts and touches no cursor."""

    def setUp(self):
        super().setUp()
        self.enrol(DUE, NOW - 10 * DAY, alerts="hourly")
        add_postings(self.path, ("n1", "Newco", "Software Engineer Intern", NOW - 3600))
        self.before = self.cursors()

    def test_a_read_that_fails_mid_tick(self):
        # A ledger older than the profile, so the bootstrap guard (B3) has nothing to
        # move and any change would be the failed tick's.
        fixture.add_seen(self.path, ("greenhouse", "old", NOW - 20 * DAY))

        async def failing_window(now=None):
            raise sqlite3.OperationalError("disk I/O error")     # after the tick's open check

        with mock.patch.object(intern_ui, "window", failing_window):
            log = self.run_loop()

        self.assertIn("the delivery tick failed: OperationalError", log)
        self.assertEqual((self.cursors(), self.sent), (self.before, []))

    def test_a_connection_broken_before_the_tick_is_reopened_first(self):
        old = intern_ui.pconn
        old.close()                               # every read on it would now raise

        self.run_loop()

        self.assertIsNotNone(intern_ui.pconn)
        self.assertIsNot(intern_ui.pconn, old)    # the contract check found it and reopened

    def test_a_file_that_is_gone(self):
        os.remove(self.path)

        log = self.run_loop()

        self.assertEqual((self.cursors(), self.sent), (self.before, []))
        self.assertIsNone(intern_ui.pconn)
        self.assertIn("does not exist", intern_ui.pconn_error)
        self.assertNotIn("the delivery tick", log)


@needs_discord
class TheFileIsReopened(_ContractCase):
    """B2. Retried on every tick, at most once a minute from commands, and on a new inode."""

    def test_a_replaced_file_is_reopened_on_the_next_tick(self):
        old = intern_ui.pconn
        restored = fixture.contract_db(self.dir.name, "restored.db",
                                       boards=(("lever", "zeta", "Zeta", "tech"),),
                                       meta={"db_path": os.path.realpath(self.path)})
        os.replace(restored, self.path)

        self.run_loop()

        self.assertIsNot(intern_ui.pconn, old)
        self.assertEqual(intern_ui.source.board_companies(), ("Zeta",))
        with self.assertRaises(sqlite3.ProgrammingError):
            old.execute("SELECT 1")               # the old connection was closed

    def test_a_failed_open_is_retried_on_a_later_tick(self):
        os.remove(self.path)
        self.run_loop()
        self.assertIsNone(intern_ui.pconn)

        fixture.contract_db(self.dir.name)
        self.run_loop()

        self.assertIsNotNone(intern_ui.pconn)
        self.assertIsNone(intern_ui.pconn_error)

    def test_commands_retry_at_most_once_a_minute(self):
        os.remove(self.path)
        self.assertFalse(intern_ui.ensure_postings())
        fixture.contract_db(self.dir.name)

        self.assertFalse(intern_ui.ensure_postings())          # within the minute
        intern_ui._postings_tried_at -= intern_ui.POSTINGS_RETRY_S + 1
        self.assertTrue(intern_ui.ensure_postings())

    def test_with_no_path_there_is_nothing_to_open_and_no_raise(self):
        with mock.patch.object(intern_ui, "postings_path", None), \
                mock.patch.object(intern_ui, "pconn", None), redirect_stderr(io.StringIO()):
            self.assertFalse(intern_ui.ensure_postings(throttle=False))
            self.assertIsNone(intern_ui.pconn)
            self.assertIn("no path", intern_ui.pconn_error)


@needs_discord
class TheTrackerSaysWhenItChanges(_ContractCase):
    """A tracker that goes down after start-up stops every alert; the log must say so,
    once, and say so again when it comes back."""

    def test_a_file_the_contract_refuses_is_logged_once(self):
        refused = fixture.contract_db(self.dir.name, "refused.db", meta={"contract_version": "9"})
        os.replace(refused, self.path)

        logs = self.run_loop() + self.run_loop()

        self.assertEqual(logs.count("internship tracker disabled:"), 1)
        self.assertIsNone(intern_ui.pconn)

    def test_coming_back_is_logged(self):
        os.remove(self.path)
        first = self.run_loop()
        fixture.contract_db(self.dir.name)

        second = self.run_loop()

        self.assertIn("internship tracker disabled:", first)
        self.assertIn("internship tracker reopened", second)

    def test_a_healthy_tick_says_nothing_about_the_tracker(self):
        self.assertNotIn("internship tracker", self.run_loop())


@needs_discord
class AChangedFileIsNeverServedStale(_ContractCase):
    """A restore by rename, or in place, never leaves commands reading a file that is gone
    or no longer under the contract."""

    def pre_contract_copy(self):
        path = pathlib.Path(self.dir.name) / "pre-contract.db"
        conn = sqlite3.connect(path)
        conn.executescript(fixture.ddl())
        conn.executescript("DROP TABLE scraper_meta; DROP TABLE boards; DROP TABLE blocked_companies;")
        conn.commit()
        conn.close()
        return path

    def test_a_file_replaced_within_the_minute_is_refused_honestly(self):
        intern_ui._postings_tried_at = time.monotonic()       # a reopen just happened
        os.replace(fixture.contract_db(self.dir.name, "restored.db",
                                       meta={"db_path": os.path.realpath(self.path)}), self.path)

        self.assertFalse(intern_ui.ensure_postings())

        self.assertIsNone(intern_ui.pconn)                  # autocomplete and debug stop too
        self.assertIn("reopening shortly", intern_ui.pconn_error)

    def test_an_in_place_restore_of_a_pre_contract_copy_disables_the_tracker(self):
        src = sqlite3.connect(self.pre_contract_copy())
        dst = sqlite3.connect(self.path)
        src.backup(dst)                                     # what sqlite3 .restore does
        src.close()
        dst.close()

        self.assertFalse(intern_ui.ensure_postings(throttle=False))

        self.assertIsNone(intern_ui.pconn)
        self.assertIsNotNone(intern_ui.pconn_error)


@needs_discord
class TheWindowFollowsTheScrapersCommits(_ContractCase):
    """B4. No sweep runs here to invalidate the window; data_version says when one did."""

    def test_a_commit_by_the_scraper_is_seen_before_the_ttl(self):
        add_postings(self.path, ("n1", "Newco", "Software Engineer Intern", NOW - 3600))
        first = asyncio.run(intern_ui.window(now=NOW))
        add_postings(self.path, ("n2", "Laterco", "Software Engineer Intern", NOW - 1800))

        second = asyncio.run(intern_ui.window(now=NOW))

        self.assertEqual([c.company for c in first], ["Newco"])
        self.assertEqual(sorted(c.company for c in second), ["Laterco", "Newco"])

    def test_no_commit_keeps_the_cache(self):
        add_postings(self.path, ("n1", "Newco", "Software Engineer Intern", NOW - 3600))
        asyncio.run(intern_ui.window(now=NOW))
        with mock.patch.object(intern_ui.intern_match, "load_window",
                               side_effect=AssertionError("reloaded")):
            self.assertEqual(len(asyncio.run(intern_ui.window(now=NOW))), 1)


@needs_discord
class TheHeartbeat(_ContractCase):
    """B6. A scraper that stopped looks like a quiet week, unless somebody says so."""

    def swept(self, started) -> None:
        fixture.execute(self.path, "INSERT INTO sweeps VALUES (?, 1, 0, 0, 0, 0)", started)

    def test_a_stale_sweep_is_logged_at_most_once_an_hour(self):
        self.swept(NOW - 4 * 900)

        first = self.run_loop()
        second = self.run_loop()
        with mock.patch.object(intern_alert_views, "time",
                               types.SimpleNamespace(time=lambda: NOW + 3601)):
            third = self.run_loop()

        self.assertIn("no sweep recorded for 1.0h", first)
        self.assertNotIn("no sweep recorded", second)
        self.assertIn("no sweep recorded", third)

    def test_a_fresh_sweep_says_nothing(self):
        self.swept(NOW - 900)
        self.assertNotIn("no sweep recorded", self.run_loop())

    def test_debug_warns_and_names_the_sweeper(self):
        self.swept(NOW - 4 * 900)
        with mock.patch.object(intern_commands.time, "time", lambda: NOW):
            lines = "\n".join(intern_commands._store_lines(intern_ui.pconn, intern_ui.source))
        self.assertIn("DIAYN 1.0.0 sweeps every 15m", lines)
        self.assertIn("no sweep for 1.0h", lines)


@needs_discord
class CommandsReadOnlyTheContract(_ContractCase):
    """There is no poller in the bot: everything the commands read besides `postings`
    comes from `source`, the contract tables."""

    def setUp(self):
        super().setUp()
        add_postings(self.path, ("n1", "Newco", "Software Engineer Intern", NOW - 3600),
                     ("r1", "Rocket Lab", "Software Engineer Intern", NOW - 3600))

    def test_no_module_holds_a_poller(self):
        for module in (intern_ui, intern_alert_views, intern_commands):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "poller"))

    def test_the_info_autocomplete_still_suggests_and_still_blocks(self):
        access.grant(self.db, "user", 7, granted_by=None, now=NOW)
        labels = [c.name for c in intern_commands._role_choices(7, None, "intern")]
        self.assertEqual(labels, ["Newco — Software Engineer Intern"])

    def test_the_info_autocomplete_suggests_nothing_to_someone_without_access(self):
        self.assertEqual(intern_commands._role_choices(7, None, "intern"), [])

    def test_info_finds_a_posting_and_skips_a_blocked_one(self):
        self.assertEqual(intern_commands._find_posting("newco")[3], "Newco")
        self.assertIsNone(intern_commands._find_posting("rocket"))

    def test_debug_reports_the_tracker_rather_than_disabled(self):
        from test_intern_surface import fake_interaction
        interaction = fake_interaction(done=True)
        with mock.patch.object(intern_commands.access, "is_owner", lambda _uid: True):
            asyncio.run(intern_commands.internships_debug.callback(interaction))
        text = "\n".join(content for content, _ in interaction.followup.sent)
        self.assertIn("Gemini quota (today)", text)
        self.assertIn("model `gemini-3.5-flash-lite`", text)
        self.assertNotIn("disabled", text)

    def test_debug_says_the_quota_resets_at_midnight_in_the_quotas_own_zone(self):
        # The scraper's LLM_DAY_TZ, which the contract publishes; not DIAYN_TZ.
        berlin = types.SimpleNamespace(quota=lambda now=None: sources.Quota(
            "gemini-3.5-flash-lite", 250, 5, 250000, "2026-09-22", "Europe/Berlin"))
        published = "\n".join(intern_commands._gemini_lines(intern_ui.pconn, intern_ui.source))
        other = "\n".join(intern_commands._gemini_lines(intern_ui.pconn, berlin))
        self.assertIn("resets at midnight Los Angeles time", published)
        self.assertIn("resets at midnight Berlin time", other)
        self.assertNotIn("Pacific", published + other)

    def test_debug_names_the_finders_own_database(self):
        lines = "\n".join(intern_commands._store_lines(intern_ui.pconn, intern_ui.source))
        self.assertIn("`users.db`", lines)
        self.assertNotIn("stats.db", lines)

    def test_debug_counts_the_subscribers_import_legacy_brought_over(self):
        before = "\n".join(asyncio.run(intern_commands._finder_lines()))
        intern_store.set_meta(self.db, intern_store.LEGACY_IMPORT_KEY, 12.0)
        after = "\n".join(asyncio.run(intern_commands._finder_lines()))
        self.assertIn("legacy import: none", before)
        self.assertIn("legacy import: 12 subscribers imported", after)

    def test_details_are_fetched_with_the_boards_icims_hosts(self):
        seen = {}

        async def fetch(platform, url, external_id, *, icims_hosts):
            seen.update(platform=platform, hosts=icims_hosts)
            return {"salary": None, "description": "x"}

        (cand,) = intern_commands.intern_match.tag_rows([intern_commands._find_posting("newco")])
        with mock.patch.object(intern_commands.posting_details, "fetch_details", fetch):
            self.assertEqual(asyncio.run(intern_commands._details(cand))["description"], "x")
        self.assertIn("careers.rivian.com", seen["hosts"])

    def test_companies_come_from_the_published_registry(self):
        self.assertEqual(intern_ui.companies_watched(), 4)
        self.assertEqual(intern_ui.known_companies()["kimleyhorn"], "Kimley-Horn")


@needs_discord
class NoSourceIsNeverARaise(unittest.TestCase):
    """_notices quotes companies_watched() even with the tracker down; a raise there
    would stop the expiry warnings the privacy notice promises."""

    def test_no_source_is_zero_companies_and_an_empty_map(self):
        with mock.patch.object(intern_ui, "source", None):
            self.assertEqual(intern_ui.companies_watched(), 0)
            self.assertEqual(intern_ui.known_companies(), {})

    def test_a_source_whose_file_fails_is_zero_companies(self):
        broken = types.SimpleNamespace(boards_count=lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk I/O error")), board_companies=lambda: ())
        with mock.patch.object(intern_ui, "source", broken), redirect_stderr(io.StringIO()):
            self.assertEqual(intern_ui.companies_watched(), 0)


@needs_discord
class TheBotNeverSweeps(unittest.TestCase):
    """P5 from the bot's side: the scraper is the one process that writes postings.db,
    so the finder has no sweep loop, no sweep and no lock of its own to take. Nor does
    it answer the old tracker's buttons, or copy its subscribers on start-up: that is
    `diayn.py import-legacy`, run once by hand."""

    GONE = {intern_alert_views: ("internship_sweep", "SWEEP_MINUTES", "_sweep", "_sweep_locked",
                                 "_rollback", "_ledger_seeded", "_sweep_wait_ready",
                                 "LegacyDigestView", "migrate_once", "_intern_migrated"),
            intern_commands: ("internship_sweep", "SWEEP_MINUTES", "migrate_once")}

    def test_none_of_the_in_process_sweep_is_left(self):
        for module, names in self.GONE.items():
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertFalse(hasattr(module, name))

    def test_the_persistent_views_are_the_card_the_alerts_and_the_retry(self):
        kinds = [type(v).__name__ for v in asyncio.run(self._views())]
        self.assertEqual(kinds, ["ProfileCardView", "AlertControlsView", "DmCheckView"])

    @staticmethod
    async def _views():
        return intern_commands.persistent_views()

    def test_the_delivery_loop_is_the_only_loop(self):
        loops = [name for name in dir(intern_alert_views)
                 if type(getattr(intern_alert_views, name)).__name__ == "Loop"]
        self.assertEqual(loops, ["intern_delivery_loop"])


class TheContractDdlIsWhatTheContractChecks(unittest.TestCase):
    """The file these tests build passes the same open the bot does at start-up."""

    def test_a_fixture_file_opens_under_the_contract(self):
        with tempfile.TemporaryDirectory() as d:
            contract.open_readonly(fixture.contract_db(d)).close()


if __name__ == "__main__":
    unittest.main()
