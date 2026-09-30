"""
`diayn.py`, the one entry point: `python diayn.py <command> [options]`.

    python3 -m unittest discover -s tests      # no install needed

Every command of the scraper runs through it with the same arguments and the
same exit codes (0 done, 1 failed, 2 a usage error, 3 another sweeper holds the
lock), so a pm2 or systemd unit, or a log reader, cannot tell the two apart.
DIAYN's own commands are its own, and none is the scraper's.

The first tests call `diayn.main` in process with the scraper's `main`
replaced, so no .env is read. The rest run copies of both scripts in a child
process from a temporary checkout, as test_cli does, with the requests
replaced so that nothing is ever fetched.
"""

import ast
import contextlib
import io
import os
import runpy
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
import hints  # noqa: E402
import internship_poller as poller  # noqa: E402
from test_cli import CHILD as SCRAPER_CHILD  # noqa: E402
from test_cli import SCRAPER_PREFIXES, SCRAPER_VARIABLES, copy_scraper, v2_fixture  # noqa: E402

BUILT = ("import-legacy", "grant", "revoke", "run", "setup", "doctor")
# The modules diayn.py imports from its checkout, besides the scraper's.
DIAYN_FILES = ("diayn.py", "hints.py", "host_checks.py", "discord_portal.py")
USAGE_ERROR, FAILED, LOCK_HELD = 2, 1, 3
# The scripts the README runs on their own, besides diayn.py, which check the Python
# version as diayn.py does, before importing what needs 3.10.
RUN_ALONE = ("internship_poller.py", "resolve_boards.py")
# What those scripts import that Python 3.9 cannot: aiohttp is not installed there,
# llm's annotations raise a TypeError, and resolve_boards imports the scraper.
NEEDS_3_10 = ("aiohttp", "llm", "internship_poller")

# The child: the temporary checkout first on the path, so `import diayn` and
# its `import internship_poller` find the copies. The requests are replaced
# before diayn runs, as in test_cli's child. BLOCK_AIOHTTP makes aiohttp
# impossible to import, as on a box that never installed requirements.txt, and
# BLOCK_FCNTL does the same for fcntl, as on Windows, which has none.
CHILD = """
import os, sys
tests, checkout = sys.argv[1], sys.argv[2]
sys.path[:0] = [checkout, tests]
if os.environ.get("BLOCK_FCNTL"):
    sys.modules["fcntl"] = None
    from aiohttp_stub import stub_aiohttp
    stub_aiohttp()
elif os.environ.get("BLOCK_AIOHTTP"):
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


class DiaynsOwn(unittest.TestCase):
    def test_diayn_s_commands_are_not_the_scraper_s(self):
        self.assertEqual(set(diayn.BOT_COMMANDS), set(BUILT))
        self.assertEqual(set(BUILT) & set(scraper_commands()), set())

    def test_every_one_is_built_and_the_placeholder_for_one_that_is_not_is_gone(self):
        self.assertFalse(hasattr(diayn, "PLANNED_COMMANDS"))


class AnOldPython(unittest.TestCase):
    """On Python 3.9, macOS's own python3, the scraper and host_checks die with a
    TypeError as they are imported, so diayn.py checks the version before either, and
    the scripts run on their own check it before their own imports."""

    #: What diayn.py imports before its check, so what must parse and run on 3.9.
    CHECKED_FIRST = ("diayn.py", "hints.py")

    def test_is_refused_first_naming_both_versions(self):
        for argv in (["doctor"], ["setup"], ["run"], ["config"], ["--help"], []):
            with self.subTest(argv=argv), \
                    mock.patch.object(sys, "version_info", (3, 9, 6, "final", 0)), \
                    mock.patch.object(diayn, "scraper",
                                      side_effect=AssertionError("imported the scraper")):
                code, out, err = run_main(argv)
            self.assertEqual(code, FAILED)
            self.assertEqual(err.splitlines()[0], "DIAYN needs Python 3.10 or newer; this is 3.9.6")
            self.assertIn(hints.interpreter(), err)
            self.assertEqual(out, "")

    def test_3_10_and_newer_pass(self):
        for version in ((3, 10, 0), (3, 12, 7), (3, 14, 2), (4, 0, 0)):
            with self.subTest(version=version):
                self.assertIsNone(hints.python_refusal(version))
        self.assertIsNone(hints.python_refusal())

    def sources(self):
        for name in self.CHECKED_FIRST:
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                yield name, f.read()

    def test_what_is_imported_before_the_check_parses_as_python_3_9(self):
        for name, source in self.sources():
            with self.subTest(file=name):
                ast.parse(source, filename=name, feature_version=(3, 9))

    def test_no_annotation_there_is_a_3_10_union(self):
        # `str | None` in an annotation is evaluated as the def runs: on 3.9, a TypeError.
        for name, source in self.sources():
            for node in ast.walk(ast.parse(source)):
                annotations = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    arguments = node.args
                    annotations.append(node.returns)
                    annotations += [a.annotation for a in (arguments.posonlyargs + arguments.args
                                                           + arguments.kwonlyargs)]
                    annotations += [a.annotation for a in (arguments.vararg, arguments.kwarg) if a]
                elif isinstance(node, ast.AnnAssign):
                    annotations.append(node.annotation)
                for annotation in filter(None, annotations):
                    with self.subTest(file=name, line=annotation.lineno):
                        self.assertFalse(any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                                             for n in ast.walk(annotation)))

    def test_the_scripts_run_alone_refuse_it_before_importing_what_needs_it(self):
        # aiohttp, llm and the scraper cannot be imported here, so a check that came
        # after any of them would fail with ModuleNotFoundError, not exit 1.
        for name in RUN_ALONE:
            err = io.StringIO()
            with self.subTest(script=name), \
                    mock.patch.object(sys, "version_info", (3, 9, 6, "final", 0)), \
                    mock.patch.dict(sys.modules, dict.fromkeys(NEEDS_3_10)), \
                    mock.patch.object(sys, "argv", [name, "config"]), \
                    contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as caught:
                runpy.run_path(os.path.join(ROOT, name), run_name="__main__")
            self.assertEqual(caught.exception.code, FAILED)
            self.assertEqual(err.getvalue().splitlines()[0],
                             "DIAYN needs Python 3.10 or newer; this is 3.9.6")

    def test_the_scripts_run_alone_parse_as_python_3_9(self):
        # A SyntaxError is raised before the first line runs, check and all.
        for name in RUN_ALONE:
            with self.subTest(file=name), \
                    open(os.path.join(ROOT, name), encoding="utf-8") as f:
                ast.parse(f.read(), filename=name, feature_version=(3, 9))

    def test_the_scripts_run_alone_import_only_what_3_9_runs_before_the_check(self):
        for name in RUN_ALONE:
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                body = ast.parse(f.read()).body
            checks = [i for i, node in enumerate(body) if isinstance(node, ast.If)
                      and "exit_if_old_python" in ast.unparse(node)]
            with self.subTest(script=name):
                self.assertEqual(len(checks), 1)
                self.assertIn("__name__ == '__main__'", ast.unparse(body[checks[0]].test))
                imported = set()
                for node in body[:checks[0]]:
                    if isinstance(node, ast.Import):
                        imported.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imported.add((node.module or "").split(".")[0])
                self.assertEqual(imported - set(sys.stdlib_module_names), {"hints"})

    def test_nothing_else_of_the_checkout_is_imported_at_the_top(self):
        # The scraper, host_checks and bot/ are imported only after the check.
        with open(os.path.join(ROOT, "diayn.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported - set(sys.stdlib_module_names), {"hints"})


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
                for command in BUILT + tuple(scraper_commands()):
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
        for command in BUILT + tuple(scraper_commands()):
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
        self.script = [shutil.copy(os.path.join(ROOT, name), self.checkout)
                       for name in DIAYN_FILES][0]
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

    def copied(self):
        return sorted(set(DIAYN_FILES) | {"internship_poller.py", "llm.py", "hints.py"})

    def test_doctor_on_a_box_with_nothing_set_up_makes_nothing(self):
        # No token, so Discord is never asked; no data directory and no database.
        result = self._diayn("doctor")
        self.assertEqual(result.returncode, FAILED, result.stdout + result.stderr)
        self.assertIn("DISCORD_TOKEN", result.stderr)
        # The child's checkout is a temporary one, with this Python outside it.
        self.assertIn(hints.command("setup", checkout=self.checkout), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(self.data))
        self.assertEqual(sorted(os.listdir(self.checkout)), self.copied())

    def test_without_fcntl_it_says_linux_and_macos_only(self):
        for command in ("config", "doctor", "run"):
            with self.subTest(command=command):
                result = self._diayn(command, BLOCK_FCNTL="1")
                self.assertEqual(result.returncode, FAILED)
                self.assertIn("Linux and macOS only", result.stderr)
                self.assertNotIn("requirements.txt", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_a_missing_dependency_names_it_and_the_fix(self):
        result = self._diayn("config", BLOCK_AIOHTTP="1")
        self.assertEqual(result.returncode, FAILED)
        self.assertIn("aiohttp", result.stderr)
        self.assertIn(hints.install_hint(checkout=self.checkout), result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
