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
  so pm2 or systemd restarts the process and its sweeps with it: the bot is
  cancelled before anything is logged, and a bot still stopping after
  BOT_SHUTDOWN_S is left behind;
- a stop from outside (Ctrl-C, or the SIGINT pm2 and systemd send) cancels each
  task once and waits for both, so a logout that takes several turns of the
  loop, as discord.py's does, finishes, and nothing is logged;
- Discord refusing the Server Members Intent exits 78, once, with one line
  naming the portal toggle: DEPLOY.md's units never restart on 78, since a
  loop of refused logins can get the token reset;
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
import signal
import sqlite3
import subprocess
import sys
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
#: sysexits.h's EX_CONFIG, which DEPLOY.md's pm2 and systemd units do not restart on.
CONFIG = 78
INTERVAL = poller.DEFAULT_INTERVAL_S
#: Seconds a stop from outside is given to finish before the test calls it hung.
STOPS_WITHIN_S = 5
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
    def test_run_is_one_of_diayn_s_commands(self):
        self.assertIn("run", diayn.BOT_COMMANDS)

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
        self.assertNotIn("--init", err)                 # run takes no --init
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
        self.assertNotIn("--init", err)                 # run takes no --init
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

    def test_the_bot_is_cancelled_even_when_logging_the_end_fails(self):
        # A log pipe that has gone away must not leave a bot running with no sweeps.
        v2_fixture(self.db)
        outcome = []

        async def bot(settings):
            try:
                await asyncio.sleep(2)
                outcome.append("never cancelled")
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        async def returns():
            return None

        with mock.patch.object(poller, "log", side_effect=BrokenPipeError("log pipe closed")), \
                self.assertLogs("asyncio", level="ERROR"):
            code, _, err = self.run_diayn(bot=bot, watch=lambda conn: returns())
        self.assertEqual(code, FAILED, err)
        self.assertEqual(outcome, ["cancelled"])

    def test_a_bot_that_does_not_stop_in_time_is_left_behind_and_it_exits_non_zero(self):
        v2_fixture(self.db)
        stages = []

        async def bot(settings):
            try:
                await forever()
            except asyncio.CancelledError:
                stages.append("cancelled")
                await asyncio.sleep(2)          # a logout that hangs
                stages.append("stopped")
                raise

        async def returns():
            return None

        with mock.patch.object(diayn, "BOT_SHUTDOWN_S", 0.05):
            code, _, err = self.run_diayn(bot=bot, watch=lambda conn: returns())
        self.assertEqual(code, FAILED, err)
        self.assertEqual(stages, ["cancelled"])
        self.assertIn("sweep loop ended", err)
        self.assertIn("the bot had not stopped", err)

    def test_a_bot_that_fails_as_it_stops_says_so(self):
        v2_fixture(self.db)

        async def bot(settings):
            try:
                await forever()
            except asyncio.CancelledError:
                raise RuntimeError("logging out failed") from None

        async def returns():
            return None

        code, _, err = self.run_diayn(bot=bot, watch=lambda conn: returns())
        self.assertEqual(code, FAILED, err)
        self.assertIn("the bot stopped with RuntimeError: logging out failed", err)
        self.assertNotIn("never retrieved", err)    # said by run, not left to asyncio

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


class Stopping(unittest.TestCase):
    """Ctrl-C, or the SIGINT pm2 and systemd send: asyncio.run cancels run_together."""

    def stop_from_outside(self, bot, sweep) -> str:
        """Runs `bot` and `sweep` through run_together, cancels it from outside as
        asyncio.run's SIGINT handler does, and returns what was written to stderr."""
        async def stop_it():
            task = asyncio.create_task(diayn.run_together(bot, sweep))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                # A stop that hangs fails here, with TimeoutError, rather than hanging.
                await asyncio.wait_for(task, STOPS_WITHIN_S)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            asyncio.run(stop_it())
        return err.getvalue()

    def test_a_stop_from_outside_cancels_both_tasks_and_waits_for_them(self):
        bot, cancelled = FakeBot(), []

        async def watching():
            try:
                await forever()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        err = self.stop_from_outside(bot(None), watching())
        self.assertTrue(bot.cancelled)
        self.assertEqual(cancelled, [True])
        self.assertNotIn("sweep loop ended", err)

    def test_a_stop_from_outside_lets_the_bot_finish_logging_out(self):
        # discord.py's close() awaits a task of its own, so the logout takes several
        # turns of the loop, and the sweep loop has finished stopping long before it.
        # A second cancel then would cut the websocket's close off halfway.
        stages = []

        async def bot():
            try:
                await forever()
            except asyncio.CancelledError:
                stages.append("logging out")
                closing = asyncio.create_task(asyncio.sleep(0.05))
                try:
                    await closing
                    stages.append("logged out")
                except asyncio.CancelledError:
                    stages.append("logout cut off")
                raise

        err = self.stop_from_outside(bot(), forever())
        self.assertEqual(stages, ["logging out", "logged out"])
        self.assertNotIn("sweep loop ended", err)
        self.assertEqual(err, "")


# A child process that runs run_together through diayn's own runner, with a bot whose
# logout takes several turns of the loop (as discord.py's close() does), prints "ready",
# and at the end prints what happened. The parent sends it a real signal.
SIGNAL_CHILD = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
import diayn
stages = []

async def bot():
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        stages.append("logging out")
        closing = asyncio.create_task(asyncio.sleep(0.05))
        try:
            await closing
            stages.append("logged out")
        except asyncio.CancelledError:
            stages.append("logout cut off")
        raise

async def sweep():
    await asyncio.Event().wait()

async def announce_then(coro):
    print("ready", flush=True)
    return await coro

try:
    diayn.run_until_stopped(announce_then(diayn.run_together(bot(), sweep())))
    print("returned")
except KeyboardInterrupt:
    print("stopped")
print(",".join(stages), flush=True)
"""


class Signals(unittest.TestCase):
    """The stop signals pm2 (SIGINT) and systemd (SIGTERM) send, on every Python.

    Before 3.11 asyncio.run has no SIGINT handler: a KeyboardInterrupt tears every task
    down at once and cuts the bot's logout off. And no version handles SIGTERM. diayn's
    own runner makes both cancel the main task, so run_together stops in order.
    """

    def stop_with(self, signum):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        child = subprocess.Popen([sys.executable, "-c", SIGNAL_CHILD, root],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), "ready")
        child.send_signal(signum)
        out, err = child.communicate(timeout=STOPS_WITHIN_S)
        return child.returncode, out.splitlines(), err

    def test_sigint_stops_in_order(self):
        code, out, err = self.stop_with(signal.SIGINT)
        self.assertEqual((code, out), (0, ["stopped", "logging out,logged out"]), err)
        self.assertNotIn("sweep loop ended", err)

    def test_sigterm_stops_in_order_too(self):
        code, out, err = self.stop_with(signal.SIGTERM)
        self.assertEqual((code, out), (0, ["stopped", "logging out,logged out"]), err)
        self.assertNotIn("sweep loop ended", err)


class TheRunnerIsUsed(unittest.TestCase):
    def test_run_goes_through_run_until_stopped_and_nothing_calls_asyncio_run(self):
        # asyncio.run would bring 3.10's teardown, and no SIGTERM handling, back.
        import ast
        tree = ast.parse(open(diayn.__file__, encoding="utf-8").read())
        calls = {(n.func.value.id if isinstance(n.func, ast.Attribute)
                  and isinstance(n.func.value, ast.Name) else None,
                  n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None))
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self.assertNotIn(("asyncio", "run"), calls)
        serve = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "_serve_and_sweep")
        self.assertIn("run_until_stopped", {n.func.id for n in ast.walk(serve)
                                            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)})


class ASweepCancelledFromOutside(unittest.TestCase):
    """A sweep task cancelled by something other than run_together, as an event loop
    tearing every task down does, is not a sweep that ended: nothing is logged, and the
    bot is cancelled once, so its logout finishes."""

    def test_the_bot_is_cancelled_once_and_nothing_is_logged(self):
        stages = []

        async def bot():
            try:
                await forever()
            except asyncio.CancelledError:
                stages.append("logging out")
                closing = asyncio.create_task(asyncio.sleep(0.05))
                try:
                    await closing
                    stages.append("logged out")
                except asyncio.CancelledError:
                    stages.append("logout cut off")
                raise

        async def go():
            task = asyncio.create_task(diayn.run_together(bot(), forever()))
            await asyncio.sleep(0.01)
            sweep = next(t for t in asyncio.all_tasks() if t.get_name() == "sweep loop")
            sweep.cancel()
            return await asyncio.wait_for(task, STOPS_WITHIN_S)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = asyncio.run(go())
        self.assertEqual(code, diayn.FAILED_EXIT)
        self.assertEqual(stages, ["logging out", "logged out"])
        self.assertNotIn("sweep loop ended", err.getvalue())


class TheIntent(_RunCase):
    """Discord refusing the Server Members Intent, as the bot logs in."""

    def test_a_refused_intent_exits_78_once_with_one_line_naming_the_toggle(self):
        v2_fixture(self.db)
        logins, cancelled = [], []

        async def bot(settings):
            logins.append(settings)
            raise diayn.IntentRefused()

        async def watching():
            try:
                await forever()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        code, out, err = self.run_diayn(bot=bot, watch=lambda conn: watching())
        self.assertEqual(code, CONFIG)
        self.assertEqual(diayn.CONFIG_EXIT, CONFIG)
        self.assertEqual(len(logins), 1)                # no second login
        self.assertEqual(cancelled, [True])
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn(hints.INTENT_HOW, err)
        self.assertNotIn("Traceback", err)
        with poller.sweeper_lock(self.db):              # raises if run still held it
            pass

    def test_through_main_the_exit_code_is_78(self):
        v2_fixture(self.db)

        async def bot(settings):
            raise diayn.IntentRefused()

        err = io.StringIO()
        with self.environment(), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(diayn, "discord_bot", return_value=bot):
            code = diayn.main(["run", "--interval", str(INTERVAL)])
        self.assertEqual(code, CONFIG, err.getvalue())


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

    def serving(self, raised, served):
        """An app.serve that records the token it was given and raises `raised`."""
        async def serve(client, token):
            for conn in (client.stores.db, client.stores.pconn):
                if conn is not None:
                    self.addCleanup(conn.close)
            served.append(token)
            raise raised()
        return serve

    def test_discord_refusing_the_intent_exits_78_after_one_login(self):
        v2_fixture(self.db)
        served = []
        refused = self.serving(lambda: discord.PrivilegedIntentsRequired(None), served)
        with mock.patch.object(app, "serve", refused):
            code, _, err = self.run_diayn(watch=idle)
        self.assertEqual(code, CONFIG, err)
        self.assertEqual(served, [TOKEN])
        self.assertIn(hints.INTENT_HOW, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(TOKEN, err)

    def test_a_failure_caused_by_the_refusal_is_the_refusal_too(self):
        def raised_from(how):
            def raise_it():
                try:
                    raise discord.PrivilegedIntentsRequired(None)
                except discord.PrivilegedIntentsRequired as refusal:
                    try:
                        if how == "cause":
                            raise RuntimeError("logging out failed") from refusal
                        raise RuntimeError("logging out failed")
                    except RuntimeError as wrapped:
                        return wrapped
            return raise_it

        def closed_4014():
            return discord.ConnectionClosed(mock.Mock(), shard_id=None, code=4014)

        for name, raised in (("cause", raised_from("cause")),
                             ("context", raised_from("context")),
                             ("gateway close 4014", closed_4014)):
            with self.subTest(raised=name):
                for leftover in (self.db, self.db + "-wal", self.db + "-shm"):
                    if os.path.exists(leftover):
                        os.remove(leftover)
                v2_fixture(self.db)
                served = []
                with mock.patch.object(app, "serve", self.serving(raised, served)):
                    code, _, err = self.run_diayn(watch=idle)
                self.assertEqual(code, CONFIG, err)
                self.assertEqual(served, [TOKEN])

    def test_any_other_failure_of_the_bot_is_raised_as_before(self):
        v2_fixture(self.db)
        served = []
        failing = self.serving(lambda: discord.ConnectionClosed(mock.Mock(), shard_id=None,
                                                                code=4000), served)
        with mock.patch.object(app, "serve", failing), \
                self.assertRaises(discord.ConnectionClosed):
            self.run_diayn(watch=idle)

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
