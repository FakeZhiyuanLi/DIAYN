"""
private_files.py: the data directory made at mode 700, and every database at 600.

    python3 -m unittest discover -s tests      # no install needed

users.db holds Discord ids and everyone's profile, so whichever command is first
to make the data directory or a database makes it through here. What is pinned:

- a directory, and any parent it lacks, is made at mode 700 whatever the umask;
- one already there is left as it is, and a file in its way is an OSError;
- a database sqlite makes inside private_umask() is made at mode 600, and its
  -wal, -shm and -journal take that mode from it, even once the block is over;
- the umask is put back after the block, however it ends, and blocks nest;
- tighten, which setup uses, takes every bit for the group and others off a
  directory or a file that has any, and says what mode it had; it never adds a
  bit, and leaves alone what is private already or not there.

Every test runs under umask 022, the usual default, which is what made the data
directory 755 and users.db 644 before, and puts the umask back after. The other
test files that check a mode use the fixtures here.
"""

import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import private_files  # noqa: E402

#: The usual default umask, under which a plain makedirs gives 755 and sqlite 644.
LOOSE_UMASK = 0o022
PRIVATE_DIRECTORY, PRIVATE_FILE = 0o700, 0o600
POSIX_MODES = unittest.skipUnless(os.name == "posix", "needs POSIX file modes")


def mode_of(path) -> int:
    """The permission bits of `path`."""
    return stat.S_IMODE(os.stat(path).st_mode)


def current_umask() -> int:
    """The process's umask, which can only be read by setting it."""
    umask = os.umask(0)
    os.umask(umask)
    return umask


def loose_umask(test: unittest.TestCase) -> None:
    """Runs the rest of `test` under umask 022, and puts back the one it had."""
    before = os.umask(LOOSE_UMASK)
    test.addCleanup(os.umask, before)


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        loose_umask(self)


@POSIX_MODES
class MakeDirectory(_Case):
    def test_a_missing_directory_is_made_at_700_under_a_loose_umask(self):
        path = os.path.join(self.tmp, "data")
        self.assertTrue(private_files.make_directory(path))
        self.assertEqual(mode_of(path), PRIVATE_DIRECTORY)

    def test_every_parent_it_makes_is_private_too(self):
        parent = os.path.join(self.tmp, "diayn")
        path = os.path.join(parent, "data")
        private_files.make_directory(path)
        self.assertEqual(mode_of(parent), PRIVATE_DIRECTORY)
        self.assertEqual(mode_of(path), PRIVATE_DIRECTORY)

    def test_one_already_there_is_left_as_it_is(self):
        path = os.path.join(self.tmp, "data")
        os.mkdir(path)
        os.chmod(path, 0o755)
        self.assertFalse(private_files.make_directory(path))
        self.assertEqual(mode_of(path), 0o755)

    def test_a_file_in_its_way_is_an_oserror(self):
        path = os.path.join(self.tmp, "data")
        with open(path, "w", encoding="ascii") as f:
            f.write("not a directory")
        with self.assertRaises(OSError):
            private_files.make_directory(path)

    def test_the_umask_is_put_back(self):
        private_files.make_directory(os.path.join(self.tmp, "data"))
        self.assertEqual(current_umask(), LOOSE_UMASK)


@POSIX_MODES
class PrivateUmask(_Case):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.tmp, "users.db")

    def connect(self) -> sqlite3.Connection:
        with private_files.private_umask():
            conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def test_a_database_sqlite_makes_in_the_block_is_600(self):
        self.connect()
        self.assertEqual(mode_of(self.db), PRIVATE_FILE)

    def test_its_wal_and_shm_take_its_mode_after_the_block(self):
        # Made by the first write, under the loose umask again: sqlite copies the
        # database's own mode onto them.
        conn = self.connect()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        for sidecar in ("-wal", "-shm"):
            with self.subTest(sidecar=sidecar):
                self.assertEqual(mode_of(self.db + sidecar), PRIVATE_FILE)

    def test_its_rollback_journal_takes_its_mode_too(self):
        conn = self.connect()
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(mode_of(self.db + "-journal"), PRIVATE_FILE)
        conn.rollback()

    def test_the_umask_is_put_back_after_the_block(self):
        self.connect()
        self.assertEqual(current_umask(), LOOSE_UMASK)

    def test_the_umask_is_put_back_when_the_block_raises(self):
        with self.assertRaises(ValueError):
            with private_files.private_umask():
                raise ValueError("a connect failed")
        self.assertEqual(current_umask(), LOOSE_UMASK)

    def test_blocks_nest(self):
        with private_files.private_umask():
            with private_files.private_umask():
                pass
            self.assertEqual(current_umask(), private_files.PRIVATE_UMASK)
        self.assertEqual(current_umask(), LOOSE_UMASK)


@POSIX_MODES
class Tighten(_Case):
    def made(self, name, mode) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="ascii"):
            pass
        os.chmod(path, mode)
        return path

    def test_a_file_others_can_read_becomes_600_and_its_old_mode_is_returned(self):
        path = self.made("users.db", 0o644)
        self.assertEqual(private_files.tighten(path), 0o644)
        self.assertEqual(mode_of(path), PRIVATE_FILE)

    def test_a_directory_others_can_enter_becomes_700(self):
        path = os.path.join(self.tmp, "data")
        os.mkdir(path)
        os.chmod(path, 0o755)
        self.assertEqual(private_files.tighten(path), 0o755)
        self.assertEqual(mode_of(path), PRIVATE_DIRECTORY)

    def test_it_only_takes_bits_away(self):
        path = self.made("users.db", 0o444)
        self.assertEqual(private_files.tighten(path), 0o444)
        self.assertEqual(mode_of(path), 0o400)

    def test_what_is_private_already_is_left_alone(self):
        path = self.made("users.db", 0o600)
        self.assertIsNone(private_files.tighten(path))
        self.assertEqual(mode_of(path), PRIVATE_FILE)

    def test_what_is_not_there_is_nothing_to_do(self):
        self.assertIsNone(private_files.tighten(os.path.join(self.tmp, "users.db")))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_a_refused_chmod_is_an_oserror(self):
        path = self.made("users.db", 0o644)
        refused = PermissionError(1, "Operation not permitted", path)
        with mock.patch.object(private_files.os, "chmod", side_effect=refused), \
                self.assertRaises(OSError):
            private_files.tighten(path)

    def test_a_chmod_the_filesystem_ignores_is_an_oserror_too(self):
        # Some mounts (vfat with `quiet`, some network and FUSE filesystems) accept a
        # chmod and change nothing; calling the file tightened then would be false.
        path = self.made("users.db", 0o644)
        with mock.patch.object(private_files.os, "chmod"), \
                self.assertRaises(OSError) as caught:
            private_files.tighten(path)
        self.assertIn("644", str(caught.exception))


class DatabaseFiles(unittest.TestCase):
    def test_a_database_and_the_files_sqlite_keeps_beside_it(self):
        self.assertEqual(private_files.database_files("/srv/diayn/users.db"),
                         ("/srv/diayn/users.db", "/srv/diayn/users.db-journal",
                          "/srv/diayn/users.db-wal", "/srv/diayn/users.db-shm"))


class ImportingDoesNothing(unittest.TestCase):
    def test_importing_it_leaves_the_umask_alone(self):
        before = current_umask()
        import importlib
        importlib.reload(private_files)
        self.assertEqual(current_umask(), before)


if __name__ == "__main__":
    unittest.main()
