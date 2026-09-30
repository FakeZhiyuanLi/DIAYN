"""
`diayn.py run`: the Discord bot and the sweep loop, in one process.

    python3 -m unittest discover -s tests      # no install needed
    .venv/bin/python -m unittest discover -s tests

`run` binds the scraper's settings (its boot()), takes <POSTINGS_DB>.lock for
as long as it runs, opens the writer connection the sweep loop needs, and then
runs two tasks in one event loop: bot/app.py's client, and the scraper's own
cmd_watch. What is pinned here:

- the settings are bound before the bot starts, so DIAYN_TZ reaches the finder;
- the lock is held while the bot runs, and let go when it stops;
- a second `run` exits 3 before anything logs in;
- a missing postings.db is refused, pointing at `diayn.py setup` run with the
  Python that is running, and nothing is made, not even the lock file;
- a sweep that raises is logged, and the bot goes on;
- a sweep loop that ends, however it ends, stops the bot and exits non-zero,
  so pm2 or systemd restarts the process and its sweeps with it;
- the bot reads postings.db through ContractSource on a mode=ro connection of
  its own, never the writer's.

Nothing logs in to Discord and nothing is fetched. Most tests hand `run` a fake
bot, a coroutine that records what it saw, and need nothing installed. The ones
that build the real client replace app.serve, and skip without discord.py.

The scraper's .env is never read: load_env_file is replaced, and the scraper's
variables are cleared from the environment, with DIAYN_DATA pointing at a
temporary directory. boot() rebinds the scraper's globals, which each test puts
back.
"""

import asyncio
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import diayn
import hints
import intern_clock
import internship_poller as poller
import postings_source
from test_cli import v2_fixture

try:
    import discord
except ModuleNotFoundError as missing:  # pragma: no cover - depends on the environment
    if missing.name != "discord":
        raise
    discord = None

#: A stub `discord` another test installs has no `__file__` (see test_intern_surface).
REAL_DISCORD = discord is not None and getattr(discord, "__file__", None) is not None
if REAL_DISCORD:
    import app
    import intern_ui
else:  # pragma: no cover - depends on the environment
    app = intern_ui = None

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

FAILED, USAGE_ERROR, LOCK_HELD = 1, 2, 3
INTERVAL = poller.DEFAULT_INTERVAL_S
#: The injected clock: the fixture's last sweep began long before it, so one is due.
NOW = 1_790_000_000.0
SCRAPER_VARIABLES = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {"POLLER_ENV_FILE"}
TOKEN = "not-a-real-token"
#: What intern_ui is handed when the real client is built.
SHARED = ("db", "pconn", "pconn_error", "source", "postings_path", "bot", "intern_error")


async def forever():
    """What a sweep loop does between sweeps, as far as the bot can tell."""
    await asyncio.Event().wait()


def idle(conn):
    """A sweep loop that never sweeps and never ends."""
    return forever()


class FakeBot:
    """
    What `run` starts in place of the Discord client. Calling it with the settings
    returns the coroutine `run` awaits; that records what it was started with, and runs
    until stop() or until it is cancelled, which it records too.
    """

    def __init__(self):
        self.started = []
        self.running = False
        self.cancelled = False
        self._stop = None

    async def __call__(self, settings):
        self.started.append(settings)
        self._stop = asyncio.Event()
        self.running = True
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.running = False

    def stop(self):
        self._stop.set()


class _RunCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = os.path.realpath(tmp.name)
        self.db = os.path.join(self.data, "postings.db")
        self.env = {"DIAYN_DATA": self.data, "DISCORD_TOKEN": TOKEN}
        saved = poller.SETTINGS, poller.BOARDS, poller.STARTED_AT
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved):
        poller.SETTINGS, poller.BOARDS, poller.STARTED_AT = saved

    @contextlib.contextmanager
    def environment(self):
        env = {k: v for k, v in os.environ.items() if k not in SCRAPER_VARIABLES}
        env.update(self.env)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(poller, "load_env_file", return_value=None):
            yield

    def run_diayn(self, *argv, bot=None, watch=None) -> tuple:
        """diayn.cmd_run in process: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with self.environment(), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = diayn.cmd_run(poller, list(argv), bot=bot, watch=watch)
        return code, out.getvalue(), err.getvalue()


class TheCommand(unittest.TestCase):
    def test_run_is_built_and_no_longer_planned(self):
        self.assertIn("run", diayn.BOT_COMMANDS)
        self.assertNotIn("run", diayn.PLANNED_COMMANDS)

    def test_main_hands_run_its_arguments(self):
        with mock.patch.object(diayn, "cmd_run", return_value=0) as cmd_run:
            self.assertEqual(diayn.main(["run", "--interval", "1200"]), 0)
        cmd_run.assert_called_once_with(poller, ["--interval", "1200"])


class BootingFirst(_RunCase):
    def test_the_settings_are_bound_before_the_bot_starts(self):
        v2_fixture(self.db)
        self.env["DIAYN_TZ"] = "America/New_York"
        seen = []

        async def bot(settings):
            seen.append((intern_clock.zone_name(), settings is poller.SETTINGS,
                         settings.postings_db))

        code, _, err = self.run_diayn(bot=bot, watch=idle)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [("America/New_York", True, self.db)])

    def test_no_token_is_refused_before_anything_is_locked_or_started(self):
        v2_fixture(self.db)
        del self.env["DISCORD_TOKEN"]
        bot = FakeBot()
        code, _, err = self.run_diayn(bot=bot, watch=idle)
        self.assertEqual(code, FAILED)
        self.assertIn("DISCORD_TOKEN", err)
        self.assertEqual(bot.started, [])
        self.assertFalse(os.path.exists(poller.lock_path(self.db)))

    def test_a_bad_setting_is_refused_with_the_scrapers_message(self):
        v2_fixture(self.db)
        self.env["DIAYN_TZ"] = "Mars/Olympus_Mons"
        bot = FakeBot()
        code, _, err = self.run_diayn(bot=bot, watch=idle)
        self.assertEqual(code, FAILED)
        self.assertIn("DIAYN_TZ", err)
        self.assertEqual(bot.started, [])

    def test_without_discord_py_it_is_refused_before_anything_is_locked_or_started(self):
        v2_fixture(self.db)
        watched = []

        def watch(conn):
            watched.append(conn)
            return forever()

        missing = ModuleNotFoundError("No module named 'discord'", name="discord")
        with mock.patch.object(diayn, "discord_bot", side_effect=missing):
            code, _, err = self.run_diayn(watch=watch)
        self.assertEqual(code, FAILED)
        self.assertIn("the bot needs discord", err)
        self.assertIn(hints.install_hint(), err)
        self.assertEqual(watched, [])
        self.assertEqual(os.listdir(self.data), ["postings.db"])     # no lock file

    def test_an_interval_below_the_floor_is_a_usage_error(self):
        v2_fixture(self.db)
        with self.assertRaises(SystemExit) as caught:
            self.run_diayn("--interval", str(poller.MIN_INTERVAL_S - 1),
                           bot=FakeBot(), watch=idle)
        self.assertEqual(caught.exception.code, USAGE_ERROR)


class TheLock(_RunCase):
    def test_the_lock_is_held_while_the_bot_runs_and_let_go_after(self):
        v2_fixture(self.db)
        seen = []

        async def bot(settings):
            try:
                with poller.sweeper_lock(self.db):
                    seen.append("free")
            except poller.LockHeld:
                seen.append("held")

        code, _, err = self.run_diayn(bot=bot, watch=idle)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, ["held"])
        with poller.sweeper_lock(self.db):      # raises if run still held it
            pass

    def test_a_second_run_exits_3_and_starts_neither_task(self):
        v2_fixture(self.db)
        bot, watched = FakeBot(), []

        def watch(conn):
            watched.append(conn)
            return forever()

        with poller.sweeper_lock(self.db), \
                mock.patch.object(poller, "open_for_sweeping") as opened:
            code, _, err = self.run_diayn(bot=bot, watch=watch)
        self.assertEqual(code, LOCK_HELD)
        self.assertIn(poller.lock_path(self.db), err)
        self.assertEqual((bot.started, watched), ([], []))
        opened.assert_not_called()


class TheDatabase(_RunCase):
    def test_a_missing_database_is_refused_with_the_setup_hint_and_nothing_made(self):
        bot = FakeBot()
        code, out, err = self.run_diayn(bot=bot, watch=idle)
        self.assertEqual(code, FAILED)
        self.assertIn(hints.command("setup"), err)
        self.assertIn(self.db, err)
        self.assertEqual(bot.started, [])
        self.assertEqual(os.listdir(self.data), [])     # no postings.db, no lock file

    def test_an_empty_ledger_is_refused_and_the_bot_never_starts(self):
        v2_fixture(self.db, rows=False)
        bot = FakeBot()
        code, _, err = self.run_diayn(bot=bot, watch=idle)
        self.assertEqual(code, FAILED)
        self.assertIn("seen ledger is empty", err)
        self.assertIn(hints.command("setup"), err)
        self.assertEqual(bot.started, [])

    def test_the_sweep_loop_gets_the_writer_and_the_options(self):
        v2_fixture(self.db)
        bot, calls = FakeBot(), []

        async def cmd_watch(conn, interval, use_llm=False):
            calls.append((interval, use_llm))
            conn.execute("INSERT INTO seen VALUES ('greenhouse', 'written', 1)")
            conn.commit()
            bot.stop()
            await forever()

        with mock.patch.object(poller, "cmd_watch", cmd_watch):
            code, _, err = self.run_diayn("--interval", "1200", "--llm", bot=bot)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [(1200, True)])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT value FROM scraper_meta "
                                          "WHERE key = 'sweep_interval_s'").fetchone(), ("1200",))
            self.assertIsNotNone(conn.execute("SELECT 1 FROM seen "
                                              "WHERE external_id = 'written'").fetchone())
        finally:
            conn.close()


class FetchFails(Exception):
    """What the canned fetch raises: a board that is down."""


async def fetch_fails(etags=None, on_status=None, sector=None):
    raise FetchFails("every board is down")


class TheSweepLoop(_RunCase):
    def watch(self, sleep=None):
        """The scraper's own cmd_watch, with the clock fixed and, if given, the sleep."""
        kwargs = {"clock": lambda: NOW}
        if sleep is not None:
            kwargs["sleep"] = sleep
        return lambda conn: poller.cmd_watch(conn, INTERVAL, **kwargs)

    def test_a_sweep_that_raises_is_logged_and_the_bot_keeps_running(self):
        v2_fixture(self.db)
        bot = FakeBot()
        waits, running = [], []

        async def sleep(seconds):
            waits.append(seconds)
            running.append(bot.running)
            if len(waits) == 2:
                bot.stop()
                await forever()

        with mock.patch.object(poller, "fetch_all", fetch_fails):
            code, _, err = self.run_diayn(bot=bot, watch=self.watch(sleep))
        self.assertEqual(code, 0, err)
        self.assertEqual(waits, [INTERVAL, INTERVAL])     # each a full interval later
        self.assertEqual(running, [True, True])
        self.assertEqual(err.count("sweep failed"), 2)
        self.assertIn("every board is down", err)
        self.assertFalse(bot.cancelled)

    def test_a_sweep_loop_that_returns_stops_the_bot_and_exits_non_zero(self):
        v2_fixture(self.db)
        bot = FakeBot()

        async def returns():
            return None

        code, _, err = self.run_diayn(bot=bot, watch=lambda conn: returns())
        self.assertEqual(code, FAILED)
        self.assertTrue(bot.cancelled)
        self.assertIn("sweep loop ended", err)

    def test_a_sweep_loop_that_raises_outside_a_sweep_stops_the_bot_and_exits_non_zero(self):
        # cmd_watch's start-up, seconds_until_due among it, runs outside its try.
        v2_fixture(self.db)
        bot = FakeBot()
        failure = sqlite3.OperationalError("disk I/O error")
        with mock.patch.object(poller, "seconds_until_due", side_effect=failure):
            code, _, err = self.run_diayn(bot=bot, watch=self.watch())
        self.assertEqual(code, FAILED)
        self.assertTrue(bot.cancelled)
        self.assertIn("sweep loop ended", err)
        self.assertIn("OperationalError: disk I/O error", err)

    def test_through_main_the_exit_code_is_the_processs(self):
        v2_fixture(self.db)
        bot = FakeBot()
        failure = sqlite3.OperationalError("disk I/O error")
        err = io.StringIO()
        with self.environment(), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(diayn, "discord_bot", return_value=bot), \
                mock.patch.object(poller, "seconds_until_due", side_effect=failure):
            code = diayn.main(["run"])
        self.assertEqual(code, FAILED, err.getvalue())
        self.assertTrue(bot.cancelled)

    def test_a_bot_that_stops_stops_the_sweep_loop_too(self):
        v2_fixture(self.db)
        cancelled = []

        async def watching():
            try:
                await forever()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async def bot(settings):
            await asyncio.sleep(0)

        code, _, err = self.run_diayn(bot=bot, watch=lambda conn: watching())
        self.assertEqual(code, 0, err)
        self.assertEqual(cancelled, [True])
        self.assertNotIn("sweep loop ended", err)


@needs_discord
class TheRealClient(_RunCase):
    """The default bot, bot/app.py's client, with app.serve replaced so nothing logs in."""

    def setUp(self):
        super().setUp()
        for cls, name in app.SEND_PATHS:
            patch = mock.patch.object(cls, name, getattr(cls, name))
            patch.start()
            self.addCleanup(patch.stop)
        for name in SHARED:
            patch = mock.patch.object(intern_ui, name, getattr(intern_ui, name))
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_bot_reads_postings_read_only_through_the_contract(self):
        v2_fixture(self.db)
        seen = {}

        async def serve(client, token):
            stores = client.stores
            for conn in (stores.db, stores.pconn):
                if conn is not None:
                    self.addCleanup(conn.close)
            seen.update(token=token, error=stores.pconn_error, source=stores.source,
                        handed=intern_ui.pconn is stores.pconn)
            if stores.pconn is None:
                return
            try:
                stores.pconn.execute("INSERT INTO seen VALUES ('greenhouse', 'x', 1)")
            except sqlite3.OperationalError as e:
                seen["write"] = str(e)

        with mock.patch.object(app, "serve", serve):
            code, _, err = self.run_diayn(watch=idle)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["token"], TOKEN)
        self.assertIsNone(seen["error"])
        self.assertIsInstance(seen["source"], postings_source.ContractSource)
        self.assertEqual(seen["source"].db_path, self.db)
        self.assertTrue(seen["handed"])
        self.assertIn("readonly", seen["write"])

    def test_a_second_run_exits_3_without_building_or_serving_the_client(self):
        v2_fixture(self.db)
        with poller.sweeper_lock(self.db), \
                mock.patch.object(app, "build") as build, \
                mock.patch.object(app, "serve") as serve:
            code, _, err = self.run_diayn(watch=idle)
        self.assertEqual(code, LOCK_HELD, err)
        build.assert_not_called()
        serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
