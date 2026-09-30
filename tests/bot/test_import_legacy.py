"""
`diayn.py import-legacy --from <stats.db>`: the old tracker's subscribers, read
from its bot's stats.db and written into DIAYN's users.db as profiles.

    python3 -m unittest discover -s tests      # no install needed

What is pinned: the old file is opened read-only and left exactly as it was;
every subscriber becomes a profile or is counted as already having one, and
anything else refuses success; the import runs once, so a second run cannot
bring back someone who has since deleted their data; only counts are
printed, never a Discord id; the data directory and users.db it makes are
made at modes 700 and 600; and an old file or a users.db that cannot be
opened or made is refused in one line, "diayn.py import-legacy: <reason>",
exit 1, never with a traceback.

Every database is made in a temporary directory. The scraper's .env is never
read: load_env_file is replaced, and the scraper's variables are cleared from
the environment, with DIAYN_DATA pointing at the temporary directory.
"""

import contextlib
import io
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import diayn
import intern_store as store
import internship_poller as poller
from test_intern_store import add_legacy, create_pings, make
from test_private_files import (POSIX_MODES, PRIVATE_DIRECTORY, PRIVATE_FILE, loose_umask,
                                mode_of)

LEGACY = ((111_111_111_111_111_111, "swe", 1), (222_222_222_222_222_222, None, None),
          (333_333_333_333_333_333, "hardware", 0), (444_444_444_444_444_444, "pm,quant", 1))
FAILED, USAGE_ERROR = 1, 2
SETTLE_S = 600
SCRAPER_VARIABLES = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {"POLLER_ENV_FILE"}
AN_ID = re.compile(r"\d{15,}")
REFUSAL = "diayn.py import-legacy: "
# A directory this user may read but not write, and a file nobody may read.
READ_ONLY_DIRECTORY, UNREADABLE_FILE = 0o500, 0o000
# Permission bits hold only for a user they apply to: root reads and writes anything.
NEEDS_PERMISSION_BITS = unittest.skipUnless(
    os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
    "needs POSIX permission bits, and a user they apply to (not root)")


def old_bot_db(path, rows=LEGACY, *, legacy=True) -> str:
    """The old bot's stats.db: a table of its own, and its subscribers unless `legacy` is False."""
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE sticker_stats (user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0)")
        db.commit()
        if legacy:
            create_pings(db)
            for row in rows:
                add_legacy(db, *row)
    finally:
        db.close()
    return path


class ImportLegacy(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.data = os.path.join(self.tmp, "data")
        self.old = os.path.join(self.tmp, "stats.db")
        self.users = os.path.join(self.data, "users.db")

    def run_import(self, *argv, data=None) -> tuple:
        """diayn.main(["import-legacy", *argv]) in process: (exit code, stdout, stderr)."""
        env = {k: v for k, v in os.environ.items() if k not in SCRAPER_VARIABLES}
        env["DIAYN_DATA"] = self.data if data is None else data
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(poller, "load_env_file", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = diayn.main(["import-legacy", *argv])
        return code, out.getvalue(), err.getvalue()

    def users_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.users)
        self.addCleanup(db.close)
        return db

    def test_every_subscriber_becomes_a_profile_from_the_settled_past(self):
        old_bot_db(self.old)

        code, out, err = self.run_import("--from", self.old)

        self.assertEqual(code, 0, err)
        db = self.users_db()
        for uid, *_ in LEGACY:
            with self.subTest(uid=uid):
                p = store.load(db, uid)
                self.assertEqual((p.source, p.alerts, p.intro_pending), ("migrated", "hourly", True))
                self.assertEqual(p.cursor, p.last_run_at - SETTLE_S)

    @POSIX_MODES
    def test_the_data_directory_and_users_db_it_makes_are_private(self):
        old_bot_db(self.old)
        loose_umask(self)

        code, _, err = self.run_import("--from", self.old)

        self.assertEqual(code, 0, err)
        self.assertEqual(mode_of(self.data), PRIVATE_DIRECTORY)
        self.assertEqual(mode_of(self.users), PRIVATE_FILE)

    def test_only_counts_are_printed(self):
        old_bot_db(self.old)

        code, out, err = self.run_import("--from", self.old)

        self.assertEqual(code, 0, err)
        self.assertIn("4 subscribers", out)
        self.assertIn("4 imported, 0 already had a profile", out)
        self.assertIsNone(AN_ID.search(out + err))

    def test_the_old_database_is_left_exactly_as_it_was(self):
        old_bot_db(self.old)
        with open(self.old, "rb") as f:
            before = f.read()
        beside = sorted(os.listdir(self.tmp))

        self.run_import("--from", self.old)

        with open(self.old, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(sorted(n for n in os.listdir(self.tmp) if n != "data"), beside)

    def test_the_old_database_is_opened_read_only(self):
        old_bot_db(self.old)
        conn = diayn.open_legacy(self.old)
        self.addCleanup(conn.close)

        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM intern_pings")

    def test_a_second_run_is_refused_and_brings_nobody_back(self):
        old_bot_db(self.old)
        self.run_import("--from", self.old)
        gone = LEGACY[0][0]
        db = self.users_db()
        store.delete_user(db, gone)

        code, out, err = self.run_import("--from", self.old)

        self.assertEqual(code, FAILED)
        self.assertIn("already", err)
        self.assertIsNone(store.load(db, gone))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM intern_profiles").fetchone()[0], 3)
        self.assertIsNone(AN_ID.search(out + err))

    def test_a_profile_already_there_is_counted_and_kept(self):
        old_bot_db(self.old)
        os.mkdir(self.data)
        db = self.users_db()
        store.init_db(db)
        mine = store.save(db, make(LEGACY[2][0]), now=1_790_000_000.0, cursor=1_789_999_400.0)

        code, out, err = self.run_import("--from", self.old)

        self.assertEqual(code, 0, err)
        self.assertIn("3 imported, 1 already had a profile", out)
        self.assertEqual(store.load(db, LEGACY[2][0]), mine)

    def test_a_file_without_the_subscriber_table_is_refused_and_nothing_is_made(self):
        old_bot_db(self.old, legacy=False)

        code, out, err = self.run_import("--from", self.old)

        self.assertEqual(code, FAILED)
        self.assertIn("intern_pings", err)
        self.assertFalse(os.path.exists(self.users))

    def test_a_missing_file_is_refused_and_not_created(self):
        code, out, err = self.run_import("--from", self.old)

        self.assertEqual(code, FAILED)
        self.assertIn("no such file", err)
        self.assertFalse(os.path.exists(self.old))
        self.assertFalse(os.path.exists(self.users))

    def test_the_old_database_must_be_named(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_import()

        self.assertEqual(caught.exception.code, USAGE_ERROR)

    def test_a_setting_it_cannot_use_stops_it_naming_the_variable(self):
        old_bot_db(self.old)

        code, out, err = self.run_import("--from", self.old, data="relative/data")

        self.assertEqual(code, FAILED)
        self.assertIn("DIAYN_DATA", err)
        self.assertFalse(os.path.exists(self.users))

    def assert_refused(self, code, err, *said):
        """One line on stderr, "diayn.py import-legacy: <reason>", exit 1, no traceback."""
        self.assertEqual(code, FAILED, err)
        self.assertTrue(err.startswith(REFUSAL), err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertNotIn("Traceback", err)
        for words in said:
            self.assertIn(words, err)
        self.assertIsNone(AN_ID.search(err))

    def lock(self, path, mode):
        """`path` at `mode` for the test, and writable again for the clean-up."""
        os.chmod(path, mode)
        self.addCleanup(os.chmod, path, 0o700)

    def test_a_data_directory_that_is_a_file_is_refused_in_one_line(self):
        old_bot_db(self.old)
        with open(self.data, "w", encoding="ascii") as f:
            f.write("not a directory")

        code, out, err = self.run_import("--from", self.old)

        self.assert_refused(code, err, "users.db")
        self.assertEqual(out, "")

    @NEEDS_PERMISSION_BITS
    def test_a_users_db_that_cannot_be_made_is_refused_in_one_line(self):
        old_bot_db(self.old)
        os.mkdir(self.data)
        self.lock(self.data, READ_ONLY_DIRECTORY)

        code, out, err = self.run_import("--from", self.old)

        self.assert_refused(code, err, "users.db", "OperationalError")
        self.assertFalse(os.path.exists(self.users))

    @NEEDS_PERMISSION_BITS
    def test_a_data_directory_that_cannot_be_made_is_refused_in_one_line(self):
        old_bot_db(self.old)
        locked = os.path.join(self.tmp, "locked")
        os.mkdir(locked)
        self.lock(locked, READ_ONLY_DIRECTORY)

        code, out, err = self.run_import("--from", self.old,
                                         data=os.path.join(locked, "data"))

        self.assert_refused(code, err, "users.db", "PermissionError")
        self.assertFalse(os.path.exists(os.path.join(locked, "data")))

    @NEEDS_PERMISSION_BITS
    def test_an_old_database_that_cannot_be_opened_is_refused_in_one_line(self):
        old_bot_db(self.old)
        with open(self.old, "rb") as f:
            before = f.read()
        self.lock(self.old, UNREADABLE_FILE)

        code, out, err = self.run_import("--from", self.old)

        self.assert_refused(code, err, self.old, "OperationalError")
        self.assertFalse(os.path.exists(self.users))
        os.chmod(self.old, 0o600)
        with open(self.old, "rb") as f:
            self.assertEqual(f.read(), before)


class TheCommandIsBuilt(unittest.TestCase):
    def test_it_is_one_of_diayn_s_commands(self):
        self.assertIn("import-legacy", diayn.BOT_COMMANDS)


if __name__ == "__main__":
    unittest.main()
