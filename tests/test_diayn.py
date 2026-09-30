"""
`diayn.py`, the one entry point: `python diayn.py <command> [options]`.

    python3 -m unittest discover -s tests      # no install needed

Every command of the scraper runs through it with the same arguments and the
same exit codes (0 done, 1 failed, 2 a usage error, 3 another sweeper holds the
lock), so a pm2 or systemd unit, or a log reader, cannot tell the two apart.
DIAYN's own commands are placeholders until they are built: each says so and
exits 2, having imported nothing and touched nothing.

The first tests call `diayn.main` in process with the scraper's `main`
replaced, so no .env is read. The rest run copies of both scripts in a child
process from a temporary checkout, as test_cli does, with the requests
replaced so that nothing is ever fetched.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()

import diayn  # noqa: E402
import internship_poller as poller  # noqa: E402
from test_cli import CHILD as SCRAPER_CHILD  # noqa: E402
from test_cli import SCRAPER_PREFIXES, SCRAPER_VARIABLES, copy_scraper, v2_fixture  # noqa: E402

PLANNED = ("doctor",)
BUILT = ("import-legacy", "grant", "revoke", "run", "setup")
USAGE_ERROR, FAILED, LOCK_HELD = 2, 1, 3

# The child: the temporary checkout first on the path, so `import diayn` and
# its `import internship_poller` find the copies. The requests are replaced
# before diayn runs, as in test_cli's child. BLOCK_AIOHTTP makes aiohttp
# impossible to import, as on a box that never installed requirements.txt.
CHILD = """
import os, sys
tests, checkout = sys.argv[1], sys.argv[2]
sys.path[:0] = [checkout, tests]
if os.environ.get("BLOCK_AIOHTTP"):
    sys.modules["aiohttp"] = None
else:
    from aiohttp_stub import stub_aiohttp
    stub_aiohttp()
    import internship_poller as poller

    async def fetch_all(*args, **kwargs):
        print("fetch_all was called", file=sys.stderr)
        raise SystemExit(99)

    def polite_session(**kwargs):
        print("polite_session was called", file=sys.stderr)
        raise SystemExit(98)

    poller.fetch_all = fetch_all
    poller.polite_session = polite_session
import diayn
sys.argv = [os.path.join(checkout, "diayn.py")] + sys.argv[3:]
sys.exit(diayn.main())
"""


def scraper_commands():
    """The commands the scraper's own parser accepts."""
    return next(a for a in poller.arguments()._actions if a.dest == "cmd").choices


def run_main(argv):
    """diayn.main(argv) in process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = diayn.main(argv)
    return code, out.getvalue(), err.getvalue()


class Planned(unittest.TestCase):
    def test_each_planned_command_says_it_is_not_built_and_exits_2(self):
        for command in PLANNED:
            with self.subTest(command=command), \
                    mock.patch.object(poller, "main") as scraper_main:
                code, out, err = run_main([command, "--anything"])
                self.assertEqual(code, USAGE_ERROR)
                self.assertIn("not built yet", out + err)
                scraper_main.assert_not_called()

    def test_the_planned_commands_are_not_scraper_commands(self):
        self.assertEqual(set(diayn.PLANNED_COMMANDS), set(PLANNED))
        self.assertEqual(set(PLANNED) & set(scraper_commands()), set())

    def test_diayn_s_built_commands_are_neither_planned_nor_the_scraper_s(self):
        self.assertEqual(set(diayn.BOT_COMMANDS), set(BUILT))
        self.assertEqual(set(BUILT) & (set(PLANNED) | set(scraper_commands())), set())


class PassThrough(unittest.TestCase):
    def test_every_scraper_command_reaches_the_scraper_with_its_arguments(self):
        for command in scraper_commands():
            argv = [command, "--interval", "900", "--init"]
            with self.subTest(command=command), \
                    mock.patch.object(poller, "main") as scraper_main:
                code, _, _ = run_main(argv)
                self.assertEqual(code, 0)
                scraper_main.assert_called_once_with(argv)

    def test_options_may_come_before_the_command_as_they_may_for_the_scraper(self):
        with mock.patch.object(poller, "main") as scraper_main:
            code, _, _ = run_main(["--us", "list"])
        self.assertEqual(code, 0)
        scraper_main.assert_called_once_with(["--us", "list"])

    def test_the_scraper_s_exit_code_is_diayn_s(self):
        for exit_code in (FAILED, USAGE_ERROR, LOCK_HELD):
            with self.subTest(exit_code=exit_code), \
                    mock.patch.object(poller, "main",
                                      side_effect=SystemExit(exit_code)), \
                    self.assertRaises(SystemExit) as caught:
                run_main(["prune"])
            self.assertEqual(caught.exception.code, exit_code)

    def test_the_scraper_lists_its_commands_in_one_place(self):
        self.assertEqual(tuple(scraper_commands()), tuple(poller.COMMANDS))


class Usage(unittest.TestCase):
    def test_help_lists_every_command_and_exits_0(self):
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag), \
                    mock.patch.object(poller, "main") as scraper_main:
                code, out, _ = run_main([flag])
                self.assertEqual(code, 0)
                for command in PLANNED + BUILT + tuple(scraper_commands()):
                    self.assertIn(command, out)
                scraper_main.assert_not_called()

    def test_no_command_prints_the_usage_and_exits_2(self):
        with mock.patch.object(poller, "main") as scraper_main:
            code, out, err = run_main([])
        self.assertEqual(code, USAGE_ERROR)
        self.assertEqual(out, "")
        self.assertIn("usage: diayn.py", err)
        scraper_main.assert_not_called()

    def test_an_unknown_command_is_refused_with_every_command_listed(self):
        with mock.patch.object(poller, "main") as scraper_main:
            code, _, err = run_main(["sweeep"])
        self.assertEqual(code, USAGE_ERROR)
        self.assertIn("sweeep", err)
        for command in PLANNED + BUILT + tuple(scraper_commands()):
            self.assertIn(command, err)
        scraper_main.assert_not_called()


class Script(unittest.TestCase):
    """Both scripts copied into `checkout/`, run with the data in `data/`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.checkout = os.path.join(self.tmp, "checkout")
        self.data = os.path.join(self.tmp, "data")
        os.mkdir(self.checkout)
        self.poller_script = copy_scraper(self.checkout)
        self.script = shutil.copy(diayn.__file__, self.checkout)
        self.db = os.path.join(self.data, "postings.db")

    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in SCRAPER_VARIABLES and not k.startswith(SCRAPER_PREFIXES)}
        env.update(PYTHONDONTWRITEBYTECODE="1", POSTINGS_DB=self.db,
                   BOARDS_FILE=os.path.join(self.data, "boards.json"))
        env.update(extra)
        return env

    def _subprocess(self, argv, **env):
        return subprocess.run(argv, cwd=self.tmp, env=self._env(**env),
                              capture_output=True, text=True, timeout=60)

    def _diayn(self, *args, **env):
        return self._subprocess(
            [sys.executable, "-B", "-c", CHILD, TESTS, self.checkout, *args], **env)

    def _scraper(self, *args):
        return self._subprocess(
            [sys.executable, "-B", "-c", SCRAPER_CHILD, TESTS, self.poller_script,
             *args])

    def test_config_prints_what_the_scraper_prints(self):
        through_diayn, direct = self._diayn("config"), self._scraper("config")
        self.assertEqual(through_diayn.returncode, 0, through_diayn.stderr)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(through_diayn.stdout, direct.stdout)
        self.assertIn("DISCORD_TOKEN", through_diayn.stdout)

    def test_a_usage_error_exits_2_with_the_scraper_s_message(self):
        result = self._diayn("prune", "--max-age", "1")
        self.assertEqual(result.returncode, USAGE_ERROR)
        self.assertIn("prune --max-age 1", result.stderr)
        self.assertIn("usage: diayn.py", result.stderr)

    def test_a_refused_database_exits_1(self):
        result = self._diayn("stats")
        self.assertEqual(result.returncode, FAILED, result.stderr)
        self.assertFalse(os.path.exists(self.db))

    def test_a_held_lock_exits_3(self):
        os.mkdir(self.data)
        v2_fixture(self.db)
        with poller.sweeper_lock(self.db):
            result = self._diayn("prune")
        self.assertEqual(result.returncode, LOCK_HELD, result.stderr)
        self.assertIn(self.db + ".lock", result.stderr)

    def test_a_planned_command_run_as_a_script_touches_nothing(self):
        # Run as pm2 would run it. The scraper is never imported, so this works
        # without aiohttp, and no data directory or lock file appears.
        for command in PLANNED:
            with self.subTest(command=command):
                result = self._subprocess([sys.executable, "-B", self.script, command])
                self.assertEqual(result.returncode, USAGE_ERROR)
                self.assertIn("not built yet", result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.data))
        self.assertEqual(sorted(os.listdir(self.checkout)),
                         ["diayn.py", "internship_poller.py", "llm.py"])

    def test_a_missing_dependency_names_it_and_the_fix(self):
        result = self._diayn("config", BLOCK_AIOHTTP="1")
        self.assertEqual(result.returncode, FAILED)
        self.assertIn("aiohttp", result.stderr)
        self.assertIn("requirements.txt", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
