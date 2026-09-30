"""
`diayn.py doctor`: everything setup set up, checked again, at any time.

    python3 -m unittest discover -s tests      # no install needed

doctor checks the Python version, the platform, the settings, the token and
the Server Members Intent, the data directory, postings.db and its last sweep,
whether anything holds the sweeper lock, POLL_CONTACT, and whether a Gemini
key is set. It exits 1 when anything is to fix, and 0 otherwise, warnings
included. What is pinned here:

- every check still runs after one fails, so one run shows everything;
- doctor makes nothing and changes nothing: no data directory, no database,
  no lock file, and a lock nobody held is free again afterwards;
- the token and the Gemini key are never printed.

Nothing reaches Discord: the portal is replaced. The scraper's .env is never
read, and boot() rebinds the scraper's globals, which each test puts back.
"""

import contextlib
import hashlib
import io
import os
import sqlite3
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
from test_setup import TOKEN, FakePortal, application  # noqa: E402

FAILED = 1
NOW = 1_790_000_000.0
GEMINI_KEY = "not-a-real-gemini-key"
CONTACT = "https://example.invalid/diayn-host"
SCRAPER_VARIABLES = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {"POLLER_ENV_FILE"}


def lines(out: str) -> dict:
    """{what: level} for every finding line doctor printed."""
    found = {}
    for line in out.splitlines():
        level, _, rest = line.partition(" ")
        if level in (host_checks.OK, host_checks.WARN, host_checks.NOTE, host_checks.FAIL):
            found[rest.strip().split(":", 1)[0]] = level
    return found


class _DoctorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = os.path.join(os.path.realpath(tmp.name), "data")
        self.db = os.path.join(self.data, "postings.db")
        self.env = {"DIAYN_DATA": self.data, "DISCORD_TOKEN": TOKEN, "POLL_CONTACT": CONTACT}
        self.portal = FakePortal()
        saved = poller.SETTINGS, poller.BOARDS, poller.STARTED_AT
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved):
        poller.SETTINGS, poller.BOARDS, poller.STARTED_AT = saved

    def healthy(self, swept_ago=300.0, interval=None):
        """A data directory at mode 700 with a postings.db whose last sweep began
        `swept_ago` seconds before NOW, and `interval` in scraper_meta if given."""
        os.mkdir(self.data, 0o700)
        v2_fixture(self.db)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO sweeps(started, duration, not_modified, errors, "
                         "new_rows, pruned) VALUES(?, 10, 0, 0, 0, 0)", (NOW - swept_ago,))
            if interval is not None:
                conn.execute("CREATE TABLE scraper_meta(key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO scraper_meta VALUES('sweep_interval_s', ?)",
                             (str(interval),))
            conn.commit()
        finally:
            conn.close()

    def doctor(self, *argv) -> tuple:
        """host_checks.cmd_doctor in process: (exit code, stdout, stderr)."""
        env = {k: v for k, v in os.environ.items() if k not in SCRAPER_VARIABLES}
        env.update(self.env)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(poller, "load_env_file", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = host_checks.cmd_doctor(poller, list(argv), fetch_application=self.portal,
                                          now=NOW)
        output = out.getvalue() + err.getvalue()
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(GEMINI_KEY, output)
        return code, out.getvalue(), err.getvalue()

    def doctor_holding_the_lock(self):
        with poller.sweeper_lock(self.db):
            return self.doctor()


class TheCommand(unittest.TestCase):
    def test_doctor_is_built_and_nothing_is_planned_any_more(self):
        self.assertIn("doctor", diayn.BOT_COMMANDS)
        self.assertEqual(diayn.PLANNED_COMMANDS, ())

    def test_main_hands_doctor_its_arguments(self):
        with mock.patch.object(host_checks, "cmd_doctor", return_value=0) as cmd_doctor:
            self.assertEqual(diayn.main(["doctor", "--anything"]), 0)
        cmd_doctor.assert_called_once_with(poller, ["--anything"])


class AHealthyHost(_DoctorCase):
    def test_every_check_is_ok_and_it_exits_0(self):
        self.healthy()
        code, out, err = self.doctor_holding_the_lock()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual(lines(out), {
            "Python": "ok", "platform": "ok", "settings": "ok", "DISCORD_TOKEN": "ok",
            "Server Members Intent": "ok", "data directory": "ok", "postings.db": "ok",
            "last sweep": "ok", "sweeper": "ok", "POLL_CONTACT": "ok",
            "GEMINI_API_KEY": "note"})
        self.assertIn("nothing to fix", out)

    def test_changes_nothing(self):
        self.healthy()
        with open(self.db, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        with poller.sweeper_lock(self.db):
            listed = sorted(os.listdir(self.data))
            self.doctor()
            self.assertEqual(sorted(os.listdir(self.data)), listed)
        with open(self.db, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), before)

    def test_the_gemini_key_is_optional_and_says_what_it_adds(self):
        self.healthy()
        _, out, _ = self.doctor_holding_the_lock()
        note = next(ln for ln in out.splitlines() if "GEMINI_API_KEY" in ln)
        self.assertIn("optional", note)
        self.assertIn("unchecked", note)

    def test_a_gemini_key_is_said_to_be_set_and_never_shown(self):
        self.healthy()
        self.env["GEMINI_API_KEY"] = GEMINI_KEY
        code, out, _ = self.doctor_holding_the_lock()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["GEMINI_API_KEY"], "ok")

    def test_a_sweep_interval_from_the_file_sets_what_counts_as_late(self):
        self.healthy(swept_ago=2 * 3600, interval=3600)
        _, out, _ = self.doctor_holding_the_lock()
        self.assertEqual(lines(out)["last sweep"], "ok")


class WhatIsToFix(_DoctorCase):
    def test_no_token_fails_without_asking_discord_and_the_rest_is_still_checked(self):
        self.healthy()
        del self.env["DISCORD_TOKEN"]
        code, out, err = self.doctor_holding_the_lock()
        self.assertEqual(code, FAILED)
        self.assertEqual(lines(err)["DISCORD_TOKEN"], "fail")
        self.assertEqual(self.portal.calls, [])
        self.assertEqual(lines(out)["postings.db"], "ok")
        self.assertIn("to fix", out)

    def test_a_refused_token_fails(self):
        self.healthy()
        self.portal.error = portal.PortalError("Discord refused DISCORD_TOKEN (HTTP 401)")
        code, _, err = self.doctor_holding_the_lock()
        self.assertEqual(code, FAILED)
        self.assertIn("HTTP 401", err)

    def test_the_members_intent_off_is_a_warning(self):
        self.healthy()
        self.portal.app = application(members_intent=False)
        code, out, _ = self.doctor_holding_the_lock()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["Server Members Intent"], "warn")

    def test_a_missing_data_directory_fails_and_is_not_made(self):
        code, out, err = self.doctor()
        self.assertEqual(code, FAILED)
        self.assertEqual(lines(err)["data directory"], "fail")
        self.assertEqual(lines(err)["postings.db"], "fail")
        self.assertIn(hints.command("setup"), err)
        self.assertFalse(os.path.exists(self.data))

    def test_a_loose_data_directory_is_a_warning(self):
        self.healthy()
        os.chmod(self.data, 0o755)
        code, out, _ = self.doctor_holding_the_lock()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["data directory"], "warn")

    def test_a_missing_postings_db_fails_and_nothing_is_made(self):
        os.mkdir(self.data, 0o700)
        code, out, err = self.doctor()
        self.assertEqual(code, FAILED)
        self.assertEqual(lines(err)["postings.db"], "fail")
        self.assertIn(hints.command("setup"), err)
        self.assertNotIn("sweeper", lines(out))
        self.assertEqual(os.listdir(self.data), [])

    def test_an_empty_ledger_fails(self):
        os.mkdir(self.data, 0o700)
        v2_fixture(self.db, rows=False)
        code, _, err = self.doctor()
        self.assertEqual(code, FAILED)
        self.assertIn("seen ledger is empty", err)

    def test_a_bad_setting_fails_and_nothing_that_needs_it_is_checked(self):
        self.env["DIAYN_TZ"] = "Mars/Olympus_Mons"
        code, out, err = self.doctor()
        self.assertEqual(code, FAILED)
        self.assertIn("DIAYN_TZ", err)
        self.assertEqual(lines(out)["Python"], "ok")
        self.assertEqual(lines(out)["platform"], "ok")
        self.assertEqual(self.portal.calls, [])


class WhatIsWorthAWarning(_DoctorCase):
    def test_no_lock_file_means_nothing_is_sweeping_and_none_is_made(self):
        self.healthy()
        code, out, _ = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["sweeper"], "warn")
        self.assertIn(hints.command("run"), out)
        self.assertFalse(os.path.exists(poller.lock_path(self.db)))

    def test_a_lock_nobody_holds_is_a_warning_and_is_free_again_after(self):
        self.healthy()
        with poller.sweeper_lock(self.db):
            pass
        code, out, _ = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["sweeper"], "warn")
        with poller.sweeper_lock(self.db):      # raises if doctor still held it
            pass

    def test_a_late_sweep_is_a_warning(self):
        self.healthy(swept_ago=4 * poller.DEFAULT_INTERVAL_S)
        code, out, _ = self.doctor_holding_the_lock()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["last sweep"], "warn")
        self.assertIn("1 hour", out)

    def test_no_sweep_at_all_is_a_warning(self):
        os.mkdir(self.data, 0o700)
        v2_fixture(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM sweeps")
        conn.commit()
        conn.close()
        code, out, _ = self.doctor_holding_the_lock()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["last sweep"], "warn")

    def test_no_contact_is_a_warning(self):
        self.healthy()
        del self.env["POLL_CONTACT"]
        code, out, _ = self.doctor_holding_the_lock()
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)["POLL_CONTACT"], "warn")
        self.assertIn("never a personal address", out)


class PythonAndPlatform(unittest.TestCase):
    def test_python_3_10_and_newer_is_ok_and_older_fails(self):
        self.assertEqual(host_checks.python_version((3, 10, 0)).level, "ok")
        self.assertEqual(host_checks.python_version((3, 14, 2)).level, "ok")
        old = host_checks.python_version((3, 9, 18))
        self.assertEqual(old.level, "fail")
        self.assertIn("3.10", old.said)

    def test_linux_and_macos_are_ok(self):
        for platform, name in (("linux", "Linux"), ("darwin", "macOS")):
            with self.subTest(platform=platform):
                found = host_checks.platform_support("posix", platform)
                self.assertEqual(found.level, "ok")
                self.assertIn(name, found.said)

    def test_windows_fails_and_says_why(self):
        found = host_checks.platform_support("nt", "win32")
        self.assertEqual(found.level, "fail")
        self.assertIn("Linux and macOS only", found.said)

    def test_a_posix_box_without_resource_fails(self):
        # The resume reader's limits are skipped where resource is missing, so the
        # promise that nothing it reads reaches a file would not hold.
        with mock.patch.object(host_checks, "_has_module", lambda name: name != "resource"):
            found = host_checks.platform_support("posix", "linux")
        self.assertEqual(found.level, "fail")
        self.assertIn("resource", found.said)


if __name__ == "__main__":
    unittest.main()
