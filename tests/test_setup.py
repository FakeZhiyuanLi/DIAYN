"""
`diayn.py setup`: a new host, from a filled-in .env to an invite link.

    python3 -m unittest discover -s tests      # no install needed

setup checks the token with Discord and the Server Members Intent, makes the
data directory at mode 700, bootstraps postings.db with a first sweep that
records every open posting as seen, and prints the invite link. What is pinned
here:

- nothing is made until the token checks out;
- the intent being off is a warning, not a failure;
- an existing postings.db is never bootstrapped again, and one with an empty
  ledger is left alone and refused, pointing at `sweep --init`;
- a first sweep that records nothing, or fails, is a failure;
- an existing data directory keeps its mode, and a loose one is warned about;
- a held sweeper lock exits 3;
- the token never reaches the output.

Nothing reaches Discord or a job board: the portal and fetch_all are replaced.
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
        saved = poller.SETTINGS, poller.BOARDS, poller.STARTED_AT
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved):
        poller.SETTINGS, poller.BOARDS, poller.STARTED_AT = saved

    def setup(self, fetch_all=None, *argv) -> tuple:
        """host_checks.cmd_setup in process: (exit code, stdout, stderr)."""
        env = {k: v for k, v in os.environ.items() if k not in SCRAPER_VARIABLES}
        env.update(self.env)
        fetch_all = fetch_all or canned_fetch([posting("1"), posting("2")])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(poller, "load_env_file", return_value=None), \
                mock.patch.object(poller, "fetch_all", fetch_all), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = host_checks.cmd_setup(poller, list(argv), fetch_application=self.portal)
        output = out.getvalue() + err.getvalue()
        self.assertNotIn(TOKEN, output)
        return code, out.getvalue(), err.getvalue()

    def seen(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        finally:
            conn.close()


class TheCommand(unittest.TestCase):
    def test_setup_is_built_and_no_longer_planned(self):
        self.assertIn("setup", diayn.BOT_COMMANDS)
        self.assertNotIn("setup", diayn.PLANNED_COMMANDS)

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
    def test_the_members_intent_off_is_a_warning_that_says_where_to_turn_it_on(self):
        self.portal.app = application(members_intent=False)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"(?m)^warn\s+Server Members Intent")
        self.assertIn("Privileged Gateway Intents", out)
        self.assertEqual(self.seen(), 2)

    def test_the_members_intent_on_is_no_warning(self):
        _, out, _ = self.setup()
        self.assertNotIn("warn", out)

    def test_a_public_bot_is_pointed_out(self):
        self.portal.app = application(public=True)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertIn("Public Bot", out)


class TheDataDirectory(_SetupCase):
    def test_an_existing_one_keeps_its_mode_and_a_loose_one_is_warned_about(self):
        os.mkdir(self.data)
        os.chmod(self.data, 0o755)
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(self.data).st_mode), 0o755)
        self.assertRegex(out, r"(?m)^warn\s+data directory")
        self.assertIn(f"chmod 700 {self.data}", out)

    def test_a_file_where_the_directory_should_be_is_refused(self):
        os.makedirs(os.path.dirname(self.data), exist_ok=True)
        with open(self.data, "w", encoding="ascii") as f:
            f.write("not a directory")
        code, _, err = self.setup(fetch_nothing_allowed())
        self.assertEqual(code, FAILED)
        self.assertIn("not a directory", err)


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
