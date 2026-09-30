"""
The sweeper lock: exactly one process writes postings.db (CONTRACT.md, P5).

    python3 -m unittest discover -s tests      # no install needed

Two sweepers double the traffic to every job board, and two writers race on the
ledger the bot reads. The lock is `<POSTINGS_DB>.lock`, taken with
flock(LOCK_EX|LOCK_NB): `watch` holds it for its whole life, every other command
that writes holds it while it runs, and a command that finds it held exits 3
having done nothing. The commands that only read never take it, so a sweeper
never makes them wait.

The first tests call sweeper_lock in process. The rest run the scraper in child
processes through test_cli's child, which fetches nothing: a writer that got
past the lock would reach the replaced fetch_all or polite_session and exit 99
or 98, never 3.
"""

import os
import select
import sqlite3
import subprocess
import tempfile
import time
import unittest

from test_cli import Cli, v2_fixture
from test_contract import TempDirTest
from test_private_files import POSIX_MODES, PRIVATE_DIRECTORY, loose_umask, mode_of

import internship_poller as poller

LOCK_HELD = 3

# Long enough that a watch under test never reaches its second sweep.
AN_HOUR = "3600"


class SweeperLock(TempDirTest):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.dir, "postings.db")
        open(self.db, "w").close()

    def test_the_lock_file_sits_beside_the_database(self):
        self.assertEqual(poller.lock_path(self.db), self.db + ".lock")

    def test_a_second_holder_is_refused_while_the_first_holds_it(self):
        # flock locks belong to an open file, not a process, so a second
        # holder in the same process is refused exactly as another would be.
        with poller.sweeper_lock(self.db):
            with self.assertRaises(poller.LockHeld) as caught:
                with poller.sweeper_lock(self.db):
                    self.fail("the lock was taken twice")
        self.assertIn(self.db + ".lock", str(caught.exception))

    def test_it_is_free_again_once_released(self):
        with poller.sweeper_lock(self.db):
            pass
        with poller.sweeper_lock(self.db):
            pass

    def test_it_is_released_when_the_block_raises(self):
        with self.assertRaises(ValueError):
            with poller.sweeper_lock(self.db):
                raise ValueError("a sweep failed")
        with poller.sweeper_lock(self.db):
            pass

    def test_a_missing_database_is_refused_before_the_lock_file_is_made(self):
        # A wrong POSTINGS_DB must leave nothing behind (P6), not even a lock.
        os.remove(self.db)
        with self.assertRaises(poller.DatabaseRefused) as caught:
            with poller.sweeper_lock(self.db):
                self.fail("locked a database that does not exist")
        self.assertIn("--init", str(caught.exception))
        self.assertEqual(os.listdir(self.dir), [])

    def test_taking_it_again_keeps_the_attempt_it_records(self):
        # The lock file carries the last sweep attempt across restarts, so
        # taking the lock must never empty it.
        with poller.sweeper_lock(self.db) as lock:
            poller.note_attempt(lock, 1_790_000_000.0)
        with poller.sweeper_lock(self.db) as lock:
            self.assertEqual(poller.last_attempt(lock), 1_790_000_000.0)

    def test_create_makes_the_lock_file_and_its_directory(self):
        db = os.path.join(self.dir, "new", "postings.db")
        with poller.sweeper_lock(db, create=True) as path:
            self.assertEqual(path, db + ".lock")
            self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.exists(db))

    @POSIX_MODES
    def test_the_directory_create_makes_is_private(self):
        # --init takes the lock first, so this is what makes a new data directory.
        loose_umask(self)
        db = os.path.join(self.dir, "new", "postings.db")
        with poller.sweeper_lock(db, create=True):
            pass
        self.assertEqual(mode_of(os.path.dirname(db)), PRIVATE_DIRECTORY)


class HeldByAnotherProcess(Cli):
    """The test holds the lock itself, and each command runs in a child."""

    def setUp(self):
        super().setUp()
        os.mkdir(self.data)
        v2_fixture(self.db)

    def test_every_command_that_writes_exits_3(self):
        writers = (("sweep",), ("watch", "--interval", AN_HOUR), ("prune",),
                   ("upgrade-db",), ("llm-diff",), ("discover",),
                   ("list", "--llm"))
        with poller.sweeper_lock(self.db):
            for args in writers:
                with self.subTest(command=" ".join(args)):
                    result = self._run(*args)
                    self.assertEqual(result.returncode, LOCK_HELD, result.stderr)
                    self.assertIn(self.db + ".lock", result.stderr)
                    self.assertNotIn("was called", result.stderr)

    def test_a_refused_command_changes_nothing(self):
        conn = sqlite3.connect(self.db)
        try:
            before = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        with poller.sweeper_lock(self.db):
            self.assertEqual(self._run("upgrade-db").returncode, LOCK_HELD)
        conn = sqlite3.connect(self.db)
        try:
            after = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertEqual((before, after), ("delete", "delete"))
        self.assertNotIn("scraper_meta", tables)

    def test_the_commands_that_only_read_never_wait_for_it(self):
        readers = (("stats",), ("verify",), ("list",), ("config",))
        with poller.sweeper_lock(self.db):
            for args in readers:
                with self.subTest(command=" ".join(args)):
                    result = self._run(*args, canned=True)
                    self.assertEqual(result.returncode, 0, result.stderr)


class AgainstARunningWatch(Cli):
    """A real `watch`, started the way pm2 starts it, and a second process."""

    def _start_watch(self):
        """A `watch --init` child, returned once its first sweep has committed."""
        log = tempfile.TemporaryFile(dir=self.tmp)
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            self._command("watch", "--init", "--interval", AN_HOUR),
            cwd=self.tmp, env=self._env(canned=True),
            stdout=subprocess.PIPE, stderr=log)
        self.addCleanup(self._stop, proc)
        self._wait_for(proc, log, b"sweep:")
        return proc

    @staticmethod
    def _stop(proc):
        proc.terminate()
        proc.wait(timeout=30)
        proc.stdout.close()

    def _wait_for(self, proc, log, text, timeout=30):
        """Read the child's stdout until `text` appears; fail if it never does."""
        out, deadline = b"", time.monotonic() + timeout
        fd = proc.stdout.fileno()
        while text not in out:
            left = deadline - time.monotonic()
            chunk = (os.read(fd, 4096)
                     if left > 0 and select.select([fd], [], [], left)[0] else b"")
            if not chunk:
                log.seek(0)
                self.fail(f"watch never logged {text!r}: {out!r} {log.read()!r}")
            out += chunk

    def _sweeps(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0]
        finally:
            conn.close()

    def test_a_second_watch_exits_3(self):
        self._start_watch()
        result = self._run("watch", "--interval", AN_HOUR, canned=True)
        self.assertEqual(result.returncode, LOCK_HELD, result.stderr)
        self.assertIn(self.db + ".lock", result.stderr)
        self.assertEqual(self._sweeps(), 1)

    def test_a_one_shot_sweep_refuses_while_watch_holds_the_lock(self):
        self._start_watch()
        result = self._run("sweep")
        self.assertEqual(result.returncode, LOCK_HELD, result.stderr)
        self.assertNotIn("fetch_all was called", result.stderr)
        self.assertEqual(self._sweeps(), 1)

    def test_stats_still_runs_while_watch_holds_the_lock(self):
        self._start_watch()
        result = self._run("stats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recent sweeps", result.stdout)

    def test_the_lock_is_free_once_watch_is_gone(self):
        # The kernel drops a flock when its holder dies, however it dies, so a
        # crashed watch never leaves a stale lock for pm2's restart to trip on.
        proc = self._start_watch()
        self._stop(proc)
        with poller.sweeper_lock(self.db):
            pass


if __name__ == "__main__":
    unittest.main()
