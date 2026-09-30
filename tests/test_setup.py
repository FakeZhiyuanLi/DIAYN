"""
`diayn.py setup`: a new host, from a filled-in .env to an invite link.

    python3 -m unittest discover -s tests      # no install needed

setup checks the token with Discord and the Server Members Intent, makes the
data directory at mode 700, bootstraps postings.db with a first sweep that
records every open posting as seen, and prints the invite link. What is pinned
here:

- discord.py missing stops setup before anything is asked or made, and pypdf
  missing is a warning, each with the install command for this Python;
- nothing is made until the token checks out;
- the Server Members Intent being off is a failure, naming the portal toggle,
  and nothing is made: the bot cannot log in without it;
- an existing postings.db is never bootstrapped again, and one with an empty
  ledger is left alone and refused, pointing at `sweep --init`;
- a first sweep that records nothing, or fails, is a failure;
- a data directory others on the box can read is tightened to 700, and
  users.db and postings.db, with their -journal, -wal and -shm, to 600, and
  setup says what it tightened; what it cannot tighten stops it;
- a held sweeper lock exits 3;
- the token never reaches the output.

Nothing reaches Discord or a job board: the portal and fetch_all are replaced.
Which modules are installed is replaced too, so the suite says the same on a box
with requirements.txt installed and on a bare python3.
The scraper's .env is never read, and boot() rebinds the scraper's globals,
which each test puts back.
"""

import contextlib
import io
import os
import sqlite3
import stat
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
import discord_portal as portal  # noqa: E402
import hints  # noqa: E402
import host_checks  # noqa: E402
import internship_poller as poller  # noqa: E402
from test_cli import v2_fixture  # noqa: E402
from test_contract import canned_fetch, posting  # noqa: E402
from test_private_files import (POSIX_MODES, PRIVATE_DIRECTORY, PRIVATE_FILE,  # noqa: E402
                                loose_umask, mode_of)

FAILED, LOCK_HELD = 1, 3
TOKEN = "not-a-real-token.for-the-setup-tests"
APP_ID = "123456789012345678"
SCRAPER_VARIABLES = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {"POLLER_ENV_FILE"}


def application(members_intent=True, public=False) -> portal.Application:
    return portal.Application(id=APP_ID, name="DIAYN test", bot_name="diayn-test#0420",
                              members_intent=members_intent, public=public)


class FakePortal:
    """Stands in for discord_portal.fetch_application: answers `app`, or raises `error`."""

    def __init__(self, app=None, error=None):
        self.app, self.error, self.calls = app or application(), error, []

    async def __call__(self, token, *, user_agent):
        self.calls.append((token, user_agent))
        if self.error is not None:
            raise self.error
        return self.app


def fetch_nothing_allowed():
    """A fetch_all that fails the test if a sweep is attempted."""
    async def fetch_all(etags=None, on_status=None, sector=None):
        raise AssertionError("setup swept a postings.db it should have left alone")
    return fetch_all


def refusing_to_tighten(refused_path):
    """private_files.tighten, except that chmod is refused for `refused_path`."""
    tighten = host_checks.private_files.tighten

    def refuse(path):
        if path == refused_path:
            raise PermissionError(1, "Operation not permitted", path)
        return tighten(path)
    return refuse


def fetch_raising(error):
    async def fetch_all(etags=None, on_status=None, sector=None):
        raise error
    return fetch_all


class _SetupCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = os.path.join(os.path.realpath(tmp.name), "data")
        self.db = os.path.join(self.data, "postings.db")
        self.env = {"DIAYN_DATA": self.data, "DISCORD_TOKEN": TOKEN}
        self.portal = FakePortal()
        self.missing = set()        # the modules this box is to lack
        self.env_path = None        # the .env boot() reports, when a test makes one
        saved = poller.SETTINGS, poller.BOARDS, poller.STARTED_AT
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved):
        poller.SETTINGS, poller.BOARDS, poller.STARTED_AT = saved

    def make_env_file(self, mode) -> str:
        path = os.path.join(os.path.dirname(self.data), ".env")
        with open(path, "w", encoding="ascii") as f:
            f.write("# a test .env; the settings come from the environment\n")
        os.chmod(path, mode)
        self.env_path = path
        return path

    def setup(self, fetch_all=None, *argv) -> tuple:
        """host_checks.cmd_setup in process: (exit code, stdout, stderr)."""
        env = {k: v for k, v in os.environ.items() if k not in SCRAPER_VARIABLES}
        env.update(self.env)
        fetch_all = fetch_all or canned_fetch([posting("1"), posting("2")])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(poller, "load_env_file", return_value=self.env_path), \
                mock.patch.object(poller, "fetch_all", fetch_all), \
                mock.patch.object(host_checks, "_has_module", self.installed), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = host_checks.cmd_setup(poller, list(argv), fetch_application=self.portal)
        output = out.getvalue() + err.getvalue()
        self.assertNotIn(TOKEN, output)
        return code, out.getvalue(), err.getvalue()

    def installed(self, name) -> bool:
        return name not in self.missing

    def seen(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        finally:
            conn.close()


class TheCommand(unittest.TestCase):
    def test_setup_is_one_of_diayn_s_commands(self):
        self.assertIn("setup", diayn.BOT_COMMANDS)

    def test_main_hands_setup_its_arguments(self):
        with mock.patch.object(host_checks, "cmd_setup", return_value=0) as cmd_setup:
            self.assertEqual(diayn.main(["setup", "--help-me"]), 0)
        cmd_setup.assert_called_once_with(poller, ["--help-me"])


class ANewHost(_SetupCase):
    def test_gets_a_private_data_directory_a_ledger_and_an_invite_link(self):
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(self.data).st_mode), 0o700)
        self.assertEqual(self.seen(), 2)
        self.assertIn(portal.invite_url(APP_ID), out)
        self.assertIn(f"Then start it: {hints.command('run')}", out)
        self.assertEqual(err, "")

    @POSIX_MODES
    def test_the_directory_and_the_ledger_it_makes_are_private_whatever_the_umask(self):
        loose_umask(self)
        code, _, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertEqual(mode_of(self.data), PRIVATE_DIRECTORY)
        self.assertEqual(mode_of(self.db), PRIVATE_FILE)

    def test_says_the_first_sweep_announces_nothing(self):
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertIn("none of them is announced", " ".join(out.split()))

    def test_asks_discord_with_the_token_and_diayns_user_agent(self):
        self.setup()
        self.assertEqual(self.portal.calls,
                         [(TOKEN, portal.user_agent(poller.PROJECT_URL, poller.__version__))])

    def test_the_bot_and_its_application_are_named(self):
        _, out, _ = self.setup()
        self.assertIn("diayn-test#0420", out)
        self.assertIn("DIAYN test", out)


class TheDependencies(_SetupCase):
    def test_without_discord_py_nothing_is_asked_or_made(self):
        self.missing = {"discord"}
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertRegex(err, r"(?m)^fail\s+discord.py")
        self.assertIn(hints.install_hint(), err)
        self.assertEqual(self.portal.calls, [])
        self.assertFalse(os.path.exists(self.data))

    def test_without_pypdf_it_warns_and_carries_on(self):
        self.missing = {"pypdf"}
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"(?m)^warn\s+pypdf")
        self.assertIn("PDF", out)
        self.assertIn(hints.install_hint(), out)
        self.assertIn(portal.invite_url(APP_ID), out)

    def test_both_installed_are_ok(self):
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"(?m)^ok\s+discord.py")
        self.assertRegex(out, r"(?m)^ok\s+pypdf")


class TheToken(_SetupCase):
    def test_no_token_is_refused_before_discord_is_asked_or_anything_is_made(self):
        del self.env["DISCORD_TOKEN"]
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertIn("DISCORD_TOKEN", err)
        self.assertEqual(self.portal.calls, [])
        self.assertFalse(os.path.exists(self.data))

    def test_a_refused_token_is_refused_and_nothing_is_made(self):
        self.portal.error = portal.PortalError("Discord refused DISCORD_TOKEN (HTTP 401)")
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertIn("HTTP 401", err)
        self.assertFalse(os.path.exists(self.data))
        self.assertNotIn("oauth2/authorize", out)

    def test_a_bad_setting_is_refused_with_the_scrapers_message(self):
        self.env["DIAYN_TZ"] = "Mars/Olympus_Mons"
        code, _, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertIn("DIAYN_TZ", err)
        self.assertEqual(self.portal.calls, [])
        self.assertFalse(os.path.exists(self.data))


class TheIntent(_SetupCase):
    def test_the_members_intent_off_stops_setup_and_names_the_portal_toggle(self):
        # Discord refuses the login of a bot that asks for an intent the portal has
        # off, and the bot always asks for this one.
        self.portal.app = application(members_intent=False)
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertRegex(err, r"(?m)^fail\s+Server Members Intent")
        self.assertIn(hints.INTENT_HOW, err)
        self.assertFalse(os.path.exists(self.data))
        self.assertNotIn("oauth2/authorize", out)
        self.assertNotIn("Then start it", out)

    def test_the_members_intent_on_is_no_warning(self):
        _, out, _ = self.setup()
        self.assertNotIn("warn", out)

    def test_a_public_bot_is_pointed_out(self):
        self.portal.app = application(public=True)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertIn("Public Bot", out)


class TheDataDirectory(_SetupCase):
    @POSIX_MODES
    def test_a_loose_one_is_tightened_to_700_and_setup_says_so(self):
        # A grant before setup used to leave it 755, and setup only warned.
        os.mkdir(self.data)
        os.chmod(self.data, 0o755)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertEqual(mode_of(self.data), PRIVATE_DIRECTORY)
        self.assertRegex(out, r"(?m)^note\s+data directory: .*was mode 755.*now 700")
        self.assertNotIn("warn", out)

    @POSIX_MODES
    def test_a_private_one_is_left_as_it_is(self):
        os.mkdir(self.data, 0o700)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertEqual(mode_of(self.data), PRIVATE_DIRECTORY)
        self.assertRegex(out, r"(?m)^ok\s+data directory")

    @POSIX_MODES
    def test_one_it_cannot_tighten_stops_setup_before_anything_is_swept(self):
        os.mkdir(self.data)
        os.chmod(self.data, 0o755)
        with mock.patch.object(host_checks.private_files, "tighten",
                               refusing_to_tighten(self.data)):
            code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertRegex(err, r"(?m)^fail\s+data directory")
        self.assertIn(f"chmod 700 {self.data}", err)
        self.assertFalse(os.path.exists(self.db))
        self.assertNotIn("oauth2/authorize", out)

    def test_a_file_where_the_directory_should_be_is_refused(self):
        os.makedirs(os.path.dirname(self.data), exist_ok=True)
        with open(self.data, "w", encoding="ascii") as f:
            f.write("not a directory")
        code, _, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertIn("not a directory", err)


@POSIX_MODES
class TheDatabaseFiles(_SetupCase):
    """users.db and postings.db from before, made where nothing made them private."""

    def setUp(self):
        super().setUp()
        loose_umask(self)
        os.mkdir(self.data, 0o700)
        self.users = os.path.join(self.data, "users.db")

    def loose(self, path) -> str:
        with open(path, "a", encoding="ascii"):
            pass
        os.chmod(path, 0o644)
        return path

    def test_loose_ones_and_their_sidecars_are_tightened_to_600_and_named(self):
        v2_fixture(self.db)
        loose = [self.loose(self.db)] + [self.loose(self.users + suffix)
                                         for suffix in ("", "-journal", "-wal", "-shm")]
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, 0, err)
        for path in loose:
            with self.subTest(path=os.path.basename(path)):
                self.assertEqual(mode_of(path), PRIVATE_FILE)
                self.assertIn(path, out)
        self.assertRegex(out, r"(?m)^note\s+database files: ")
        self.assertIn(portal.invite_url(APP_ID), out)

    def test_private_ones_are_not_mentioned(self):
        v2_fixture(self.db)
        os.chmod(self.db, 0o600)
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, 0, err)
        self.assertNotIn("database files", out + err)

    def test_one_it_cannot_tighten_stops_setup(self):
        v2_fixture(self.db)
        self.loose(self.users)
        with mock.patch.object(host_checks.private_files, "tighten",
                               refusing_to_tighten(self.users)):
            code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertRegex(err, r"(?m)^fail\s+database files: ")
        self.assertIn(f"chmod 600 {self.users}", err)
        self.assertNotIn("oauth2/authorize", out)


@POSIX_MODES
class TheRealTighten(_SetupCase):
    """setup through private_files.tighten itself, not a stand-in for it."""

    def test_a_refused_chmod_on_the_data_directory_fails_setup(self):
        os.mkdir(self.data)
        os.chmod(self.data, 0o755)
        real = os.chmod

        def refuse(path, mode, *args, **kwargs):
            if os.path.realpath(path) == os.path.realpath(self.data):
                raise PermissionError(1, "Operation not permitted", path)
            return real(path, mode, *args, **kwargs)

        with mock.patch.object(host_checks.private_files.os, "chmod", refuse):
            code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, host_checks.FAILED_EXIT)
        self.assertIn("could not tighten", out + err)


@POSIX_MODES
class TheEnvFile(_SetupCase):
    """.env holds the bot's token and any Gemini key: nobody else on the box reads it."""

    def test_setup_makes_a_readable_env_file_private_and_says_so(self):
        path = self.make_env_file(0o644)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIn(".env", out)
        self.assertIn("644", out)

    def test_a_private_env_file_is_left_as_it_is(self):
        path = self.make_env_file(0o600)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertNotIn(".env", out)


class TheLedger(_SetupCase):
    def test_an_existing_ledger_is_never_bootstrapped_again(self):
        os.mkdir(self.data, 0o700)
        v2_fixture(self.db)
        before = self.seen()
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.seen(), before)
        self.assertIn("left as it is", out)
        self.assertIn(portal.invite_url(APP_ID), out)

    def test_an_existing_file_with_an_empty_ledger_is_left_alone_and_refused(self):
        os.mkdir(self.data, 0o700)
        v2_fixture(self.db, rows=False)
        code, out, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertEqual(self.seen(), 0)
        self.assertIn(hints.command("sweep", "--init"), err)
        self.assertNotIn("oauth2/authorize", out)

    def test_an_existing_file_that_is_not_a_postings_db_is_refused(self):
        os.mkdir(self.data, 0o700)
        sqlite3.connect(self.db).close()
        with open(self.db, "wb") as f:
            f.write(b"not a database at all, just bytes " * 8)
        code, _, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertIn("postings.db", err)

    def test_a_first_sweep_that_records_nothing_is_a_failure(self):
        code, out, err = self.setup(canned_fetch([]))
        self.assertEqual(code, FAILED)
        self.assertIn(hints.command("sweep", "--init"), err)
        self.assertNotIn("oauth2/authorize", out)

    def test_a_first_sweep_that_raises_is_a_failure_and_commits_nothing(self):
        code, _, err = self.setup(fetch_raising(RuntimeError("boards unreachable")))
        self.assertEqual(code, FAILED)
        self.assertIn("RuntimeError", err)
        self.assertIn(hints.command("sweep", "--init"), err)
        self.assertEqual(self.seen(), 0)

    def test_a_held_lock_exits_3_and_sweeps_nothing(self):
        os.mkdir(self.data, 0o700)
        with poller.sweeper_lock(self.db, create=True):
            code, _, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, LOCK_HELD)
        self.assertIn(poller.lock_path(self.db), err)
        self.assertFalse(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()
