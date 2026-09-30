"""
`watch`, the process pm2 keeps running: it outlives a failed sweep, and a restart
does not sweep early.

    python3 -m unittest discover -s tests      # no install needed

pm2 restarts a process that exits. The loop used to sweep the moment it
started, so a crash loop would have swept every ATS host on every restart; its
first sweep now waits until one is due, an interval after the last sweep that
began. A sweep that raises is rolled back — none of its rows committed, and
none left pending for the next sweep's commit to carry in — then logged, and
the loop goes on. Each sweep is one line in the log, whatever happened.

A sweep that never finishes — the process killed mid-fetch — commits no sweeps
row, so each attempt is also written into the lock file before any request
goes out, and the wait counts from the later of the two.

Each test runs cmd_watch in process against a database in a temporary
directory, with a canned fetch and an injected sleep and clock. The sleep
records what it was asked for and ends the loop after a set number of calls, so
nothing waits and nothing is fetched.
"""

import asyncio
import contextlib
import io
import re
import sqlite3
import unittest
from unittest import mock

from test_contract import TempDirTest, posting, scraper

import internship_poller as poller

INTERVAL = 900
# The injected clock. Real sweeps stamp sweeps.started with the real time; the
# rows these tests plant are placed relative to this.
NOW = 1_790_000_000.0
STAMP = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ")


class Stop(Exception):
    """Raised by the injected sleep to end the loop."""


class Sleeper:
    """The injected sleep: records each wait, looks, and stops after `calls`.

    `look`, if given, is called with the number of this call before it returns:
    the moment between one sweep and the next.
    """

    def __init__(self, calls, look=None):
        self.calls, self.look, self.waits = calls, look, []

    async def __call__(self, seconds):
        self.waits.append(seconds)
        if self.look:
            self.look(len(self.waits))
        if len(self.waits) >= self.calls:
            raise Stop


def fetch_in_turn(*results):
    """A stand-in for fetch_all: each call returns (or raises) the next result.

    Also records how many times it was called, as `.calls`.
    """
    queue = list(results)

    async def fetch_all(etags=None, on_status=None, sector=None):
        fetch_all.calls += 1
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return list(result), {"ok": 1, "not_modified": 0, "error": 0,
                              "new_etags": {}}
    fetch_all.calls = 0
    return fetch_all


def fail_first(real, error):
    """`real`, except that its first call raises `error`."""
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise error
        return real(*args, **kwargs)
    return wrapper


class Watch(TempDirTest):
    def setUp(self):
        super().setUp()
        booted = scraper(self.dir)
        self.path = booted.__enter__()
        self.addCleanup(booted.__exit__, None, None, None)
        self.conn = poller.db_init(create=True)
        self.addCleanup(self.conn.close)
        # watch always runs holding the lock, and records its attempts there.
        held = poller.sweeper_lock(self.path)
        self.lock = held.__enter__()
        self.addCleanup(held.__exit__, None, None, None)
        self.out, self.err = io.StringIO(), io.StringIO()

    def _plant_sweep(self, started):
        self.conn.execute("INSERT INTO sweeps(started, duration, not_modified, "
                          "errors, new_rows, pruned) VALUES(?, 1, 0, 0, 0, 0)",
                          (started,))
        self.conn.commit()

    def _watch(self, fetch, sleeper, now=NOW, ends=Stop):
        """Run cmd_watch until it raises `ends`, with its output captured."""
        with mock.patch.object(poller, "fetch_all", fetch), \
                contextlib.redirect_stdout(self.out), \
                contextlib.redirect_stderr(self.err):
            with self.assertRaises(ends):
                asyncio.run(poller.cmd_watch(self.conn, INTERVAL, sleep=sleeper,
                                             clock=lambda: now))

    def _count(self, sql):
        other = sqlite3.connect(self.path)
        try:
            return other.execute(sql).fetchall()
        finally:
            other.close()


class FirstSweep(Watch):
    def test_a_new_ledger_is_swept_at_once(self):
        fetch, sleeper = fetch_in_turn([posting("1")]), Sleeper(1)
        self._watch(fetch, sleeper)
        self.assertEqual(fetch.calls, 1)
        self.assertEqual(sleeper.waits, [INTERVAL])

    def test_a_restart_waits_until_the_next_sweep_is_due(self):
        # The last sweep began 100 s ago, so the next is due in 800: a restart
        # sleeps that out before it fetches anything.
        self._plant_sweep(NOW - 100)
        fetch, sleeper = fetch_in_turn([posting("1")]), Sleeper(1)
        self._watch(fetch, sleeper)
        self.assertEqual(sleeper.waits, [INTERVAL - 100])
        self.assertEqual(fetch.calls, 0)

    def test_the_wait_counts_from_the_latest_sweep(self):
        self._plant_sweep(NOW - 5000)
        self._plant_sweep(NOW - 300)
        sleeper = Sleeper(1)
        self._watch(fetch_in_turn([posting("1")]), sleeper)
        self.assertEqual(sleeper.waits, [INTERVAL - 300])

    def test_an_overdue_sweep_runs_at_once(self):
        self._plant_sweep(NOW - 5000)
        fetch, sleeper = fetch_in_turn([posting("1")]), Sleeper(1)
        self._watch(fetch, sleeper)
        self.assertEqual(fetch.calls, 1)
        self.assertEqual(sleeper.waits, [INTERVAL])

    def test_a_last_sweep_in_the_future_waits_one_interval_at_most(self):
        # A clock set back must not stall the loop for longer than one gap.
        self._plant_sweep(NOW + 10 * INTERVAL)
        sleeper = Sleeper(1)
        self._watch(fetch_in_turn([posting("1")]), sleeper)
        self.assertEqual(sleeper.waits, [INTERVAL])


class Killed(BaseException):
    """The process dying mid-sweep: nothing the loop's `except Exception` catches."""


class KilledMidSweep(Watch):
    def test_the_next_start_waits_although_no_sweep_was_committed(self):
        # The OOM killer takes the first watch during its fetch. pm2 restarts
        # it 100 s later: the attempt counts, so it waits out the interval
        # instead of fetching every board again straight away.
        self._watch(fetch_in_turn(Killed()), Sleeper(1), ends=Killed)
        self.assertEqual(self._count("SELECT COUNT(*) FROM sweeps"), [(0,)])
        fetch, sleeper = fetch_in_turn([posting("1")]), Sleeper(1)
        self._watch(fetch, sleeper, now=NOW + 100)
        self.assertEqual(sleeper.waits, [INTERVAL - 100])
        self.assertEqual(fetch.calls, 0)

    def test_the_attempt_is_recorded_before_the_first_request(self):
        recorded = []

        async def fetch_all(etags=None, on_status=None, sector=None):
            recorded.append(poller.last_attempt(self.lock))
            raise Killed

        self._watch(fetch_all, Sleeper(1), ends=Killed)
        self.assertEqual(recorded, [NOW])

    def test_a_later_attempt_outweighs_an_older_success(self):
        self._plant_sweep(NOW - 5000)
        poller.note_attempt(self.lock, NOW - 300)
        sleeper = Sleeper(1)
        self._watch(fetch_in_turn([posting("1")]), sleeper)
        self.assertEqual(sleeper.waits, [INTERVAL - 300])

    def test_a_later_success_outweighs_an_older_attempt(self):
        poller.note_attempt(self.lock, NOW - 5000)
        self._plant_sweep(NOW - 300)
        sleeper = Sleeper(1)
        self._watch(fetch_in_turn([posting("1")]), sleeper)
        self.assertEqual(sleeper.waits, [INTERVAL - 300])

    def test_a_lock_file_that_records_nothing_readable_is_no_attempt(self):
        # A fresh lock file is empty; one from an older release, or cut short,
        # may hold anything. Neither delays the first sweep.
        for content in ("", "not a time\n"):
            with self.subTest(content=content):
                with open(self.lock, "w") as f:
                    f.write(content)
                self.assertIsNone(poller.last_attempt(self.lock))
        fetch = fetch_in_turn([posting("1")])
        self._watch(fetch, Sleeper(1))
        self.assertEqual(fetch.calls, 1)


class Failures(Watch):
    def test_a_failed_sweep_commits_nothing_and_the_loop_goes_on(self):
        # The failure comes after the sweep has written its rows, just before
        # its commit. Rolled back, the second sweep finds a clean slate; left
        # pending, posting 1 would ride in on the second sweep's commit.
        looked = {}

        def look(call):
            looked[call] = (self.conn.in_transaction, self._count(
                "SELECT external_id FROM seen ORDER BY 1"),
                self._count("SELECT COUNT(*) FROM sweeps")[0][0])

        fetch = fetch_in_turn([posting("1")], [posting("2")])
        flaky = fail_first(poller.publish_registry, RuntimeError("publish failed"))
        with mock.patch.object(poller, "publish_registry", flaky):
            self._watch(fetch, Sleeper(2, look))
        self.assertEqual(looked[1], (False, [], 0))
        self.assertEqual(looked[2], (False, [("2",)], 1))

    def test_a_failure_before_anything_is_written_is_survived_too(self):
        fetch = fetch_in_turn(RuntimeError("network down"), [posting("1")])
        self._watch(fetch, Sleeper(2))
        self.assertEqual(fetch.calls, 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM sweeps"), [(1,)])

    def test_a_failed_sweep_is_followed_by_a_full_interval(self):
        # Never an immediate retry: a failing sweep retried at once would hit
        # every ATS host in a tight loop.
        sleeper = Sleeper(2)
        self._watch(fetch_in_turn(RuntimeError("x"), RuntimeError("y")), sleeper)
        self.assertEqual(sleeper.waits, [INTERVAL, INTERVAL])


class Log(Watch):
    def test_each_sweep_logs_one_line(self):
        fetch = fetch_in_turn(
            [posting("1")],
            [posting("2"), posting("3", "Data Science Intern"),
             posting("4", "Hardware Engineering Intern")])
        flaky = fail_first(poller.publish_registry, RuntimeError("publish failed"))
        with mock.patch.object(poller, "publish_registry", flaky):
            self._watch(fetch, Sleeper(2))
        out = self.out.getvalue().splitlines()
        err = self.err.getvalue().splitlines()
        # The banner, then the one good sweep; its postings are the bot's to
        # announce, not the log's.
        self.assertEqual(len(out), 2, out)
        self.assertIn("3 new", out[1])
        self.assertEqual(len(err), 1, err)
        self.assertIn("rolled back", err[0])
        self.assertIn("RuntimeError: publish failed", err[0])
        for line in out + err:
            self.assertRegex(line, STAMP)

    def test_a_restart_says_when_it_will_sweep(self):
        self._plant_sweep(NOW - 100)
        self._watch(fetch_in_turn(), Sleeper(1))
        out = self.out.getvalue().splitlines()
        self.assertEqual(len(out), 2, out)
        self.assertIn("800s", out[1])


if __name__ == "__main__":
    unittest.main()
