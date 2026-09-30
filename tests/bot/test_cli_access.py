"""
`diayn.py grant` and `diayn.py revoke`: who may use the bot, changed from the
command line, with no Discord at all.

    python3 -m unittest discover -s tests      # no install needed

    python diayn.py grant --user <id>       python diayn.py revoke --user <id>
    python diayn.py grant --server <id>     python diayn.py revoke --server <id>

A host grants before the bot's first start this way (plan 7), and takes a grant
back without it. What is pinned: the grant lands in users.db's access_grants
with no granter (the command line is nobody's Discord account), `access.allowed`
then honours it, nothing but the grants changes, a revoke never creates
users.db, a usage error exits 2 having touched nothing, and no id is printed.

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

import access
import diayn
import intern_store
import internship_poller as poller
from test_intern_store import make

FAILED, USAGE_ERROR = 1, 2
SCRAPER_VARIABLES = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {"POLLER_ENV_FILE"}
AN_ID = re.compile(r"\d{15,}")
PERSON, SERVER, STRANGER = 111_111_111_111_111_111, 222_222_222_222_222_222, 333_333_333_333_333_333


def nobody(guild_ids):
    return False


class AccessFromTheCommandLine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.data = os.path.join(self.tmp, "data")
        self.users = os.path.join(self.data, "users.db")
        patch = mock.patch.object(access, "_application_owners", frozenset())
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv, data=None) -> tuple:
        """diayn.main(argv) in process: (exit code, stdout, stderr)."""
        env = {k: v for k, v in os.environ.items() if k not in SCRAPER_VARIABLES}
        env["DIAYN_DATA"] = self.data if data is None else data
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(poller, "load_env_file", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = diayn.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def rows(self) -> list:
        db = sqlite3.connect(self.users)
        try:
            return db.execute("SELECT kind, id, granted_by FROM access_grants ORDER BY kind").fetchall()
        finally:
            db.close()

    def grants(self) -> access.Grants:
        db = sqlite3.connect(self.users)
        try:
            return access.grants(db)
        finally:
            db.close()

    def test_a_user_grant_lands_in_users_db_with_no_granter(self):
        code, out, err = self.run_cli("grant", "--user", str(PERSON))

        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(), [("user", PERSON, None)])
        self.assertIn("may use this bot", out)
        self.assertTrue(access.allowed(self.grants(), PERSON, None, nobody))

    def test_a_server_grant_lets_its_members_in(self):
        code, out, err = self.run_cli("grant", "--server", str(SERVER))

        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(), [("guild", SERVER, None)])
        self.assertTrue(access.allowed(self.grants(), STRANGER, SERVER, nobody))
        self.assertFalse(access.allowed(self.grants(), STRANGER, None, nobody))

    def test_granting_twice_says_so_and_changes_nothing(self):
        self.run_cli("grant", "--user", str(PERSON))
        code, out, _ = self.run_cli("grant", "--user", str(PERSON))

        self.assertEqual(code, 0)
        self.assertIn("already", out)
        self.assertEqual(self.rows(), [("user", PERSON, None)])

    def test_a_revoke_takes_the_grant_away(self):
        self.run_cli("grant", "--user", str(PERSON))
        self.run_cli("grant", "--server", str(SERVER))

        code, out, err = self.run_cli("revoke", "--server", str(SERVER))

        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(), [("user", PERSON, None)])
        self.assertFalse(access.allowed(self.grants(), STRANGER, SERVER, nobody))
        self.assertIn("gone", out)

    def test_revoking_what_was_never_granted_says_so(self):
        self.run_cli("grant", "--server", str(SERVER))
        code, out, _ = self.run_cli("revoke", "--user", str(PERSON))

        self.assertEqual(code, 0)
        self.assertIn("no grant", out)
        self.assertEqual(self.rows(), [("guild", SERVER, None)])

    def test_a_revoke_never_creates_users_db(self):
        code, out, _ = self.run_cli("revoke", "--user", str(PERSON))

        self.assertEqual(code, 0)
        self.assertIn("no grant", out)
        self.assertFalse(os.path.exists(self.data))

    def test_the_finders_own_tables_are_left_alone(self):
        os.makedirs(self.data)
        db = sqlite3.connect(self.users)
        intern_store.init_db(db)
        intern_store.save(db, make(PERSON), now=1.0, cursor=0.0)
        db.close()

        code, _, err = self.run_cli("grant", "--user", str(PERSON))

        db = sqlite3.connect(self.users)
        self.addCleanup(db.close)
        self.assertEqual(code, 0, err)
        self.assertEqual(intern_store.load(db, PERSON).user_id, PERSON)

    def test_no_id_is_ever_printed(self):
        for argv in (("grant", "--user", str(PERSON)), ("grant", "--user", str(PERSON)),
                     ("revoke", "--user", str(PERSON)), ("revoke", "--user", str(PERSON)),
                     ("grant", "--server", str(SERVER))):
            with self.subTest(argv=argv):
                _, out, err = self.run_cli(*argv)
                self.assertIsNone(AN_ID.search(out + err))

    def test_a_usage_error_exits_2_and_touches_nothing(self):
        for argv in (("grant",), ("revoke",), ("grant", "--user", str(PERSON), "--server", str(SERVER)),
                     ("grant", "--user", "someone"), ("grant", "--user", "0"),
                     ("grant", "--server", "-5"), ("revoke", "--user", str(2 ** 63)),
                     ("grant", "--role", str(PERSON))):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as caught:
                    self.run_cli(*argv)
                self.assertEqual(caught.exception.code, USAGE_ERROR)
        self.assertFalse(os.path.exists(self.data))

    def test_a_users_db_that_cannot_be_opened_exits_1_and_says_which(self):
        os.makedirs(self.users)                 # a directory where the file goes

        code, _, err = self.run_cli("grant", "--user", str(PERSON))

        self.assertEqual(code, FAILED)
        self.assertIn("users.db", err)
        self.assertNotIn("Traceback", err)

    def test_a_bad_setting_exits_1_before_anything_is_made(self):
        code, _, err = self.run_cli("grant", "--user", str(PERSON), data="relative/data")

        self.assertEqual(code, FAILED)
        self.assertIn("DIAYN_DATA", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "relative")))


if __name__ == "__main__":
    unittest.main()
