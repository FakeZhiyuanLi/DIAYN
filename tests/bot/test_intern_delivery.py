"""
Delivery: who is due, what one tick sends them, and what it writes down.

    python3 -m unittest discover -s tests      # no install needed

`intern_delivery` is asyncio over sqlite3 and nothing else, so every test here
runs a real tick — `asyncio.run(run_tick(...))` — against an in-memory database
prepared by `intern_store.init_db` alone, with a fake `send_dm` that records
what it was given and can refuse, fail, or delete the user mid-send.
Candidates are built the way the bot builds them, synthetic `postings` rows
through `intern_match.tag_rows`. Nothing opens the real users.db or postings.db.

The rules that matter most, because breaking them breaks nothing visible:

  * a role reaches a user once — not again as a repost, as a late regional
    copy, or on the next tick — and a role inside the settle window is late,
    not lost;
  * a refused DM is counted and three of them stop the DMs; a transient
    failure changes nothing and is retried;
  * a user deleted while a DM to them is in flight stays deleted;
  * nobody the bot is not open to is DMed anything, and a revocation during a
    tick stops the DMs not yet sent;
  * a 9am slot is 9am on the wall clock, on both sides of a DST change.

Alert hours and the housekeeping day are in DIAYN_TZ, the scraper's
`SETTINGS.tz`. This module runs with it set to America/Los_Angeles, a zone
with a DST change, except where a test sets another. Times are written with
their UTC offset stated (PDT until 2026-11-01 02:00, PST after), so the DST
tests check the module against the calendar rather than against its own time
zone lookup.
"""

import asyncio
import contextlib
import dataclasses
import io
import itertools
import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import intern_delivery as delivery
import intern_match
import intern_profile as profile
import intern_store as store
import intern_text
import internship_poller as poller
from test_intern_match import CITIES     # the 56-place regional-clone fixture

PDT, PST = timezone(timedelta(hours=-7)), timezone(timedelta(hours=-8))
MINUTE, HOUR, DAY = 60, 3600, 86400
COMPANIES = 42
ALICE, BOB, CAROL = 111_111_111_111_111_111, 222_222_222_222_222_222, 333_333_333_333_333_333
DAVE = 444_444_444_444_444_444
_ROWIDS = itertools.count(1)
_IN_PACIFIC = mock.patch.object(poller, "SETTINGS",
                                poller.configure({"DIAYN_TZ": "America/Los_Angeles"}))


def setUpModule():
    _IN_PACIFIC.start()


def tearDownModule():
    _IN_PACIFIC.stop()


def in_zone(name: str):
    """DIAYN_TZ set to `name` for the length of a `with` block."""
    return mock.patch.object(poller, "SETTINGS", poller.configure({"DIAYN_TZ": name}))


def clock(month: int, day: int, hour: int = 9, minute: int = 0, tz=PDT) -> float:
    """A 2026 Pacific wall-clock time, its offset stated rather than looked up."""
    return datetime(2026, month, day, hour, minute, tzinfo=tz).timestamp()


MONDAY = clock(10, 5)            # Monday 2026-10-05, 09:00 PDT


def posting(title, seen, *, company="Acme", location="Minneapolis, MN"):
    """One `postings` row in WINDOW_SQL's column order, posted and first seen at `seen`."""
    rowid = next(_ROWIDS)
    return (rowid, "greenhouse", f"ext{rowid}", company, title, location,
            f"https://example.com/jobs/{rowid}", seen, seen)


def person(uid=ALICE, at=MONDAY, **fields) -> profile.Profile:
    """An unsaved software-engineering profile created at `at` (daily at 9am by default)."""
    base = profile.new_profile(uid, at, source="manual", cursor=delivery.horizon(at))
    return dataclasses.replace(base, **{"fields": ("software",), "degree": "bachelor", **fields})


class Outbox:
    """A fake `send_dm`: records every call; can refuse, fail, or act while the DM is in flight."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.calls = []              # (uid, DmMessage) for every attempt
        self.raises = {}             # uid -> exception class
        self.meanwhile = {}          # uid -> callable run mid-send
        self.open_transactions = []  # db.in_transaction at each call

    async def __call__(self, uid, msg):
        self.open_transactions.append(self.db.in_transaction)
        self.calls.append((uid, msg))
        self.meanwhile.get(uid, lambda: None)()
        if uid in self.raises:
            raise self.raises[uid]()

    def to(self, uid) -> list:
        return [msg for u, msg in self.calls if u == uid]

    def uids(self) -> list:
        return [u for u, _ in self.calls]


class Constants(unittest.TestCase):
    def test_the_spec_values(self):
        self.assertEqual((delivery.SETTLE_S, delivery.DM_MAX_LISTINGS, delivery.MAX_DMS_PER_TICK,
                          delivery.SEND_GAP_S, delivery.QUIET_AFTER_S, delivery.MAX_NOTICES_PER_TICK,
                          delivery.PAUSE_S), (600, 5, 50, 0.5, 14 * DAY, 20, 7 * DAY))
        self.assertEqual(delivery.horizon(MONDAY), MONDAY - 600)
        self.assertEqual(str(delivery.TZ), "America/Los_Angeles")


class Cadence(unittest.TestCase):
    def test_daily_is_due_from_its_hour_until_it_has_run(self):
        p = person(last_run_at=clock(10, 4, 9, 1))
        ran = dataclasses.replace(p, last_run_at=clock(10, 5, 9, 0))

        self.assertFalse(delivery.is_due(p, clock(10, 5, 8, 59)))
        self.assertTrue(delivery.is_due(p, clock(10, 5, 9, 0)))
        self.assertFalse(delivery.is_due(ran, clock(10, 5, 15, 0)))
        self.assertEqual(delivery.next_slot(ran, clock(10, 5, 15, 0)), clock(10, 6, 9, 0))

    def test_the_daily_slot_stays_at_nine_local_across_the_dst_change(self):
        p = person(last_run_at=clock(10, 31, 9, 1))

        # 08:59 PST is 09:59 PDT: a module stuck on daylight time would already be due.
        self.assertFalse(delivery.is_due(p, clock(11, 1, 8, 59, PST)))
        self.assertTrue(delivery.is_due(p, clock(11, 1, 9, 0, PST)))
        self.assertEqual(delivery.next_slot(p, clock(10, 31, 12, 0)), clock(11, 1, 9, 0, PST))

    def test_weekly_is_due_on_monday_at_its_hour_not_on_sunday(self):
        p = person(alerts="weekly", last_run_at=clock(10, 5, 9, 1))

        self.assertFalse(delivery.is_due(p, clock(10, 11, 9, 0)))      # Sunday
        self.assertFalse(delivery.is_due(p, clock(10, 12, 8, 59)))
        self.assertTrue(delivery.is_due(p, clock(10, 12, 9, 0)))       # Monday
        self.assertEqual(delivery.next_slot(p, clock(10, 7, 12, 0)), clock(10, 12, 9, 0))

    def test_hourly_waits_fifty_nine_minutes(self):
        p = person(alerts="hourly", last_run_at=MONDAY)

        self.assertFalse(delivery.is_due(p, MONDAY + 58 * MINUTE))
        self.assertTrue(delivery.is_due(p, MONDAY + 59 * MINUTE))
        self.assertEqual(delivery.next_slot(p, MONDAY), MONDAY + 59 * MINUTE)

    def test_a_pause_holds_until_it_passes_then_catches_up_whatever_the_cadence(self):
        p = person(last_run_at=MONDAY, paused_until=MONDAY + HOUR)     # ran at 9am today

        self.assertFalse(delivery.is_due(p, MONDAY + 30 * MINUTE))
        self.assertFalse(delivery.catching_up(p, MONDAY + 30 * MINUTE))
        self.assertEqual(delivery.next_slot(p, MONDAY), MONDAY + HOUR)
        self.assertTrue(delivery.is_due(p, MONDAY + HOUR))
        self.assertTrue(delivery.catching_up(p, MONDAY + HOUR))

    def test_off_left_and_refused_are_never_due(self):
        for fields in ({"alerts": "off"}, {"left_at": MONDAY - DAY}, {"dm_failures": 3},
                       {"left_at": MONDAY - DAY, "paused_until": MONDAY - HOUR}):
            p = person(**{"alerts": "hourly", "last_run_at": MONDAY - DAY, **fields})
            with self.subTest(fields=fields):
                self.assertFalse(delivery.is_due(p, MONDAY))
                self.assertFalse(delivery.is_due(p, MONDAY + 7 * DAY))
                self.assertIsNone(delivery.next_slot(p, MONDAY))


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        store.init_db(self.db)
        self.window, self.loads = [], 0
        self.outbox = Outbox(self.db)
        self.revoked = set()         # who this bot is not open to (access.allowed says no)
        no_gap = mock.patch.object(delivery, "SEND_GAP_S", 0)
        no_gap.start()
        self.addCleanup(no_gap.stop)

    def tearDown(self):
        self.db.close()

    def enrol(self, uid=ALICE, at=MONDAY, **fields) -> profile.Profile:
        """Saved as a first Save leaves it: cursor at the horizon, last run at `at`."""
        return store.save(self.db, person(uid, at, **fields), now=at, cursor=delivery.horizon(at))

    def post(self, *rows):
        self.window = self.window + intern_match.tag_rows(rows)

    async def load_window(self):
        self.loads += 1
        return list(self.window)

    def allowed(self, uid) -> bool:
        return uid not in self.revoked

    def run_async(self, entry, now):
        return asyncio.run(entry(self.db, load_window=self.load_window, send_dm=self.outbox,
                                 now=now, companies_watched=COMPANIES, allowed=self.allowed))

    def tick(self, now) -> delivery.TickReport:
        return self.run_async(delivery.run_tick, now)

    def notices(self, now) -> dict:
        return self.run_async(delivery.run_notices, now)

    def seen(self, uid) -> dict:
        rows = self.db.execute("SELECT role_hash, state FROM intern_seen WHERE user_id = ?", (uid,))
        return dict(rows.fetchall())


class Sending(DeliveryTest):
    def test_a_new_role_is_sent_once_and_the_cursor_moves_to_the_horizon(self):
        self.enrol(alerts="hourly")
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))
        now = MONDAY + HOUR

        report = self.tick(now)
        cursor = store.load(self.db, ALICE).cursor
        again = self.tick(now + HOUR)

        self.assertEqual(report, delivery.TickReport(due=1, sent=1, empty=0, forbidden=0,
                                                     transient=0, deferred=0))
        self.assertEqual(cursor, delivery.horizon(now))
        self.assertEqual((again.due, again.sent, again.empty), (1, 0, 1))
        (msg,) = self.outbox.to(ALICE)
        self.assertIn("Software Engineer Intern", msg.text)
        self.assertTrue(msg.with_controls and msg.hide_options)
        role = self.window[0]
        self.assertEqual(self.seen(ALICE), {role.rk_hash: "sent", role.ck_hash: "sent"})

    def test_a_role_inside_the_settle_window_waits_for_a_later_tick(self):
        self.enrol(alerts="hourly")
        now = MONDAY + HOUR
        self.post(posting("Software Engineer Intern", now - 5 * MINUTE))

        first, second = self.tick(now), self.tick(now + HOUR)

        self.assertEqual((first.empty, first.sent, second.sent), (1, 0, 1))

    def test_a_repost_of_a_sent_role_is_not_sent_again(self):
        self.enrol(alerts="hourly")
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))
        self.tick(MONDAY + HOUR)
        self.post(posting("Software Engineer Intern", MONDAY + HOUR + MINUTE))   # new rowid and id

        report = self.tick(MONDAY + 2 * HOUR)

        self.assertEqual((report.sent, report.empty, len(self.outbox.calls)), (0, 1, 1))

    def test_late_regional_copies_of_a_sent_role_are_not_sent(self):
        role = {"fields": ("business_ops",), "alerts": "hourly"}
        self.enrol(ALICE, **role)
        self.post(posting("Store Executive Intern - Duluth, MN", MONDAY + MINUTE, company="Target",
                          location="Duluth, MN"))
        self.tick(MONDAY + HOUR)
        self.enrol(BOB, MONDAY + HOUR, **role)          # never sent Duluth: the control
        self.post(*(posting(f"Store Executive Intern - {city}", MONDAY + HOUR + MINUTE,
                            company="Target", location="10 Locations")
                    for city in CITIES if city != "Duluth, MN"))

        self.tick(MONDAY + 3 * HOUR)

        self.assertEqual(len(self.outbox.to(ALICE)), 1)
        (folded,) = self.outbox.to(BOB)
        self.assertIn("more locations)", folded.text)

    def test_a_hidden_group_is_never_sent(self):
        self.enrol(alerts="hourly")
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))
        store.hide(self.db, ALICE, (self.window[0].rk_hash, self.window[0].ck_hash), MONDAY)

        report = self.tick(MONDAY + HOUR)

        self.assertEqual((report.sent, report.empty), (0, 1))

    def test_a_sibling_role_with_the_same_clone_key_is_still_sent(self):
        self.enrol(fields=("finance",), alerts="hourly")
        caterpillar = {"company": "Caterpillar", "location": "Peoria, IL"}
        self.post(posting("2027 Summer Corporate Intern - Accounting", MONDAY + MINUTE, **caterpillar))
        self.tick(MONDAY + HOUR)
        self.post(posting("2027 Summer Corporate Intern - Finance", MONDAY + HOUR + MINUTE, **caterpillar))

        report = self.tick(MONDAY + 2 * HOUR)

        self.assertEqual(self.window[0].ck, self.window[1].ck)
        self.assertEqual(report.sent, 1)
        self.assertIn("Corporate Intern - Finance", self.outbox.to(ALICE)[-1].text)

    def test_the_bootstrap_seed_never_becomes_alerts(self):
        self.enrol(alerts="hourly", at=MONDAY - DAY)
        self.post(*(posting(f"Software Engineer Intern {n}", MONDAY - MINUTE) for n in range(3)))
        store.advance_all_cursors(self.db, MONDAY)

        report = self.tick(MONDAY + HOUR)

        # Without the advance every seed row would qualify: all match, all after the old cursor.
        self.assertEqual(len(intern_match.rank(person(), self.window, MONDAY)), 3)
        self.assertEqual((report.due, report.sent, report.empty), (1, 0, 1))

    def test_a_profile_saved_just_after_the_bootstrap_never_gets_the_seed(self):
        # The sweep advances only the profiles there at the time. One saved three
        # minutes later starts at a settle horizon from before the seed was stored.
        swept = MONDAY - 10 * MINUTE
        self.post(*(posting(f"Software Engineer Intern {n}", swept) for n in range(3)))
        store.advance_all_cursors(self.db, swept + 1)
        self.enrol(alerts="hourly", at=swept + 3 * MINUTE)

        report = self.tick(MONDAY + HOUR)

        self.assertEqual((report.due, report.sent, report.empty), (1, 0, 1))
        self.assertEqual((self.outbox.calls, self.seen(ALICE)), ([], {}))

    def test_nobody_due_means_the_window_is_never_loaded(self):
        self.enrol()                          # daily at 9am, saved at 9am: next due tomorrow

        report = self.tick(MONDAY + HOUR)

        self.assertEqual(report, delivery.TickReport(0, 0, 0, 0, 0, 0))
        self.assertEqual(self.loads, 0)

    def test_the_digest_fits_one_message_and_records_every_group(self):
        self.enrol(fields=("mechanical",), alerts="hourly")
        self.post(*(posting(f"Mechanical Design Engineering Intern, Propulsion Systems {n:02d}",
                            MONDAY + MINUTE, company="Northrop Grumman", location="Irvine, CA")
                    for n in range(12)))

        self.tick(MONDAY + HOUR)

        (msg,) = self.outbox.to(ALICE)
        self.assertLessEqual(len(msg.text), intern_text.ALERT_MAX)
        self.assertEqual(msg.text.count("**Northrop Grumman**"), 5)
        self.assertIn("...and 7 more: `/internships matches`", msg.text)
        self.assertEqual(len(msg.hide_options), 6)             # five roles, the company once
        self.assertLessEqual(len(msg.hide_options), 25)
        self.assertEqual(len(self.seen(ALICE)), 24)             # all twelve groups, not the five shown

    def test_the_migrated_intro_is_sent_once(self):
        self.enrol(source="migrated", intro_pending=True, alerts="hourly", min_score=45)
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))
        self.tick(MONDAY + HOUR)
        self.post(posting("Backend Software Engineer Intern", MONDAY + HOUR + MINUTE))

        self.tick(MONDAY + 2 * HOUR)

        first, second = self.outbox.to(ALICE)
        self.assertTrue(first.text.startswith(intern_text.migrated_intro()))
        self.assertNotIn(intern_text.migrated_intro(), second.text)
        self.assertFalse(store.load(self.db, ALICE).intro_pending)

    def test_a_passed_pause_sends_a_catch_up_and_clears_it(self):
        self.enrol(ALICE, paused_until=MONDAY + 2 * HOUR)       # daily; already ran at 9am today
        self.enrol(BOB, paused_until=MONDAY + 2 * HOUR, fields=("civil",))   # nothing matches
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))

        paused, back = self.tick(MONDAY + HOUR), self.tick(MONDAY + 2 * HOUR)

        self.assertEqual((paused.due, back.due, back.sent, back.empty), (0, 2, 1, 1))
        self.assertTrue(self.outbox.to(ALICE)[0].text.startswith(
            "**Welcome back: 1 new role while you were paused.**"))
        for uid in (ALICE, BOB):
            self.assertIsNone(store.load(self.db, uid).paused_until)


class Refusals(DeliveryTest):
    def test_three_refusals_stop_alerts_until_they_are_turned_back_on(self):
        self.enrol(alerts="hourly")
        self.outbox.raises[ALICE] = delivery.DmForbidden
        for n in (1, 2, 3):
            now = MONDAY + n * HOUR
            self.post(posting("Software Engineer Intern", now - HOUR + MINUTE))
            report = self.tick(now)
            p = store.load(self.db, ALICE)
            self.assertEqual((report.forbidden, p.dm_failures, p.cursor), (1, n, delivery.horizon(now)))
        later = MONDAY + 10 * HOUR
        refused = store.load(self.db, ALICE)

        store.reset_dm_failures(self.db, ALICE)
        store.set_alerts(self.db, ALICE, "hourly", 9, later, cursor=delivery.horizon(later))

        self.assertFalse(delivery.is_due(refused, later))
        self.assertIsNone(delivery.next_slot(refused, later))
        self.assertTrue(delivery.is_due(store.load(self.db, ALICE), later + HOUR))
        self.assertEqual(self.seen(ALICE), {})

    def test_a_refused_catch_up_ends_the_pause_so_the_next_try_waits_for_a_slot(self):
        # The refusal moves the cursor past everything the pause held back, so the catch-up is
        # spent. Left paused, the user would be "catching up" and due on every five-minute tick.
        self.enrol(paused_until=MONDAY + 2 * HOUR)
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE),
                  posting("Backend Software Engineer Intern", MONDAY + 2 * HOUR + MINUTE))
        self.outbox.raises[ALICE] = delivery.DmForbidden

        refused = self.tick(MONDAY + 2 * HOUR)
        p = store.load(self.db, ALICE)
        again = self.tick(MONDAY + 2 * HOUR + 15 * MINUTE)

        self.assertEqual((refused.forbidden, p.dm_failures, p.paused_until), (1, 1, None))
        self.assertEqual((again.due, len(self.outbox.to(ALICE))), (0, 1))

    def test_a_transient_failure_changes_nothing_and_is_retried(self):
        self.enrol(at=MONDAY - DAY)
        self.post(posting("Software Engineer Intern", MONDAY - HOUR))
        self.outbox.raises[ALICE] = delivery.DmTransient
        before = store.load(self.db, ALICE)

        report = self.tick(MONDAY)
        after = store.load(self.db, ALICE)
        self.outbox.raises = {}
        retry = self.tick(MONDAY + 5 * MINUTE)

        self.assertEqual((report.transient, retry.sent), (1, 1))
        self.assertEqual(after, before)

    def test_one_user_s_error_never_stops_the_tick_or_names_them(self):
        for uid in (ALICE, BOB):
            self.enrol(uid, at=MONDAY - DAY)
        self.post(posting("Software Engineer Intern", MONDAY - HOUR))
        self.outbox.raises[ALICE] = RuntimeError
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            self.tick(MONDAY)

        self.assertEqual(self.outbox.uids(), [ALICE, BOB])
        self.assertEqual((len(self.seen(ALICE)), len(self.seen(BOB))), (0, 2))
        self.assertIn("RuntimeError", stderr.getvalue())
        self.assertNotIn(str(ALICE), stderr.getvalue())


class Races(DeliveryTest):
    def test_a_user_deleted_mid_send_leaves_nothing_behind(self):
        for uid in (ALICE, BOB):
            self.enrol(uid, at=MONDAY - DAY)
            self.outbox.meanwhile[uid] = lambda uid=uid: store.delete_user(self.db, uid)
        self.outbox.raises[BOB] = delivery.DmForbidden         # a refusal after the delete, too
        self.post(posting("Software Engineer Intern", MONDAY - HOUR))

        self.tick(MONDAY)

        self.assertEqual(self.outbox.uids(), [ALICE, BOB])
        for table in ("intern_profiles", "intern_seen"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_a_user_deleted_or_switched_off_before_their_turn_is_skipped(self):
        for uid in (ALICE, BOB, CAROL):
            self.enrol(uid, at=MONDAY - DAY)
        self.enrol(DAVE, at=MONDAY - DAY, fields=("civil",))      # nothing matches: no send to guard
        self.outbox.meanwhile[ALICE] = lambda: (
            store.delete_user(self.db, BOB),
            *(store.set_alerts(self.db, uid, "off", 9, MONDAY, cursor=MONDAY) for uid in (CAROL, DAVE)))
        self.post(posting("Software Engineer Intern", MONDAY - HOUR))
        before = store.load(self.db, DAVE)

        report = self.tick(MONDAY)

        self.assertEqual((report.due, report.sent, report.empty, self.outbox.uids()), (4, 1, 0, [ALICE]))
        self.assertEqual((self.seen(BOB), self.seen(CAROL)), ({}, {}))
        self.assertEqual(store.load(self.db, DAVE).last_run_at, before.last_run_at)

    def test_every_write_is_committed_before_the_next_await(self):
        for uid in (ALICE, BOB, CAROL):
            self.enrol(uid, at=MONDAY - DAY)
        self.outbox.raises[BOB] = delivery.DmForbidden
        self.post(posting("Software Engineer Intern", MONDAY - HOUR))

        self.tick(MONDAY)

        self.assertEqual(self.outbox.open_transactions, [False, False, False])
        self.assertFalse(self.db.in_transaction)


class Throttle(DeliveryTest):
    def test_the_per_tick_cap_leaves_the_rest_for_the_next_tick(self):
        for uid in (ALICE, BOB, CAROL):
            self.enrol(uid, at=MONDAY - DAY)
        self.post(posting("Software Engineer Intern", MONDAY - HOUR))

        with mock.patch.object(delivery, "MAX_DMS_PER_TICK", 2):
            first = self.tick(MONDAY)
            waiting = store.load(self.db, CAROL)
            second = self.tick(MONDAY + 5 * MINUTE)

        self.assertEqual((first.due, first.sent, first.deferred), (3, 2, 1))
        self.assertEqual(waiting.cursor, delivery.horizon(MONDAY - DAY))
        self.assertEqual((second.due, second.sent), (1, 1))
        self.assertEqual(self.outbox.uids(), [ALICE, BOB, CAROL])

    def test_sends_are_spaced_by_the_gap(self):
        stamps = []
        for uid in (ALICE, BOB):
            self.enrol(uid, at=MONDAY - DAY)
            self.outbox.meanwhile[uid] = lambda: stamps.append(time.monotonic())
        self.post(posting("Software Engineer Intern", MONDAY - HOUR))

        with mock.patch.object(delivery, "SEND_GAP_S", 0.05):
            self.tick(MONDAY)

        self.assertGreaterEqual(stamps[1] - stamps[0], 0.045)


class Notices(DeliveryTest):
    def test_a_quiet_note_after_fourteen_quiet_days_and_not_again_within_fourteen(self):
        self.enrol(at=MONDAY - 14 * DAY)

        first, soon, later = (self.notices(MONDAY), self.notices(MONDAY + 13 * DAY),
                              self.notices(MONDAY + 14 * DAY))

        self.assertEqual([first["quiet"], soon["quiet"], later["quiet"]], [1, 0, 1])
        self.assertEqual(self.outbox.uids(), [ALICE, ALICE])
        msg = self.outbox.calls[0][1]
        self.assertTrue(msg.text.startswith("**Still watching, nothing new for you yet.**"))
        self.assertIn(f"the {COMPANIES} companies I watch", msg.text)
        self.assertEqual((msg.hide_options, msg.with_controls), ((), False))
        self.assertEqual(store.load(self.db, ALICE).last_quiet_at, MONDAY + 14 * DAY)
        self.assertEqual(self.outbox.open_transactions, [False, False])

    def test_the_quiet_note_offers_what_would_add_roles_from_the_window(self):
        self.enrol(at=MONDAY - 20 * DAY, locations=("oc",))
        self.post(posting("Software Engineer Intern", MONDAY - 3 * DAY))      # Minneapolis

        self.notices(MONDAY)

        self.assertIn("- **Anywhere in the US** in `/internships profile` would add 1.",
                      self.outbox.calls[0][1].text)

    def test_an_expiry_warning_is_sent_once_at_351_days(self):
        self.enrol(ALICE, at=MONDAY - 351 * DAY, alerts="off")
        self.enrol(BOB, at=MONDAY - 350 * DAY, alerts="off")

        first, again = self.notices(MONDAY), self.notices(MONDAY + HOUR)

        self.assertEqual((first, again), ({"quiet": 0, "expiry": 1}, {"quiet": 0, "expiry": 0}))
        self.assertEqual(self.outbox.calls, [(ALICE, delivery.DmMessage(
            intern_text.expiry_note(MONDAY + 14 * DAY), (), False))])
        self.assertEqual(store.load(self.db, ALICE).expiry_warned_at, MONDAY)
        self.assertEqual(self.loads, 0)                         # a warning needs no window

    def test_a_refused_expiry_warning_is_recorded_and_not_retried(self):
        self.enrol(at=MONDAY - 351 * DAY, alerts="off")
        self.outbox.raises[ALICE] = delivery.DmForbidden

        self.notices(MONDAY)
        self.notices(MONDAY + HOUR)

        p = store.load(self.db, ALICE)
        self.assertEqual((len(self.outbox.calls), p.expiry_warned_at, p.dm_failures), (1, MONDAY, 1))

    def test_a_refused_quiet_note_leaves_the_alert_cursor_where_it_was(self):
        # The note offered no postings, so its refusal must not step past one it never
        # offered: a weekly user whose Monday digest has run gets a match that afternoon.
        self.enrol(at=MONDAY - 15 * DAY, alerts="weekly")
        self.tick(MONDAY + 5 * MINUTE)
        self.post(posting("Software Engineer Intern", MONDAY + 6 * HOUR))
        before = store.load(self.db, ALICE)
        self.outbox.raises[ALICE] = delivery.DmForbidden         # DMs shut on Wednesday...

        refused = self.notices(MONDAY + 2 * DAY)
        after, attempts = store.load(self.db, ALICE), len(self.outbox.calls)
        del self.outbox.raises[ALICE]                            # ...and open again by Monday
        report = self.tick(MONDAY + 7 * DAY + 5 * MINUTE)

        self.assertEqual((refused["quiet"], attempts), (0, 1))
        self.assertEqual((after.cursor, after.last_run_at, after.dm_failures),
                         (before.cursor, before.last_run_at, 1))
        self.assertEqual(report.sent, 1)
        self.assertIn("Software Engineer Intern", self.outbox.to(ALICE)[-1].text)

    def test_a_refused_expiry_warning_moves_no_alert_state(self):
        self.enrol(at=MONDAY - 351 * DAY, alerts="daily", last_quiet_at=MONDAY - DAY)
        before = store.load(self.db, ALICE)
        self.outbox.raises[ALICE] = delivery.DmForbidden

        self.notices(MONDAY)

        after = store.load(self.db, ALICE)
        self.assertEqual((after.cursor, after.last_run_at), (before.cursor, before.last_run_at))
        self.assertEqual((after.dm_failures, after.expiry_warned_at), (1, MONDAY))

    def test_a_user_who_came_back_is_warned_again_before_a_second_idle_year_ends(self):
        self.enrol(at=MONDAY - 351 * DAY, alerts="off")
        first = self.notices(MONDAY)
        store.touch(self.db, ALICE, MONDAY + DAY)                # "run any command to keep it"

        second = self.notices(MONDAY + DAY + 351 * DAY)

        self.assertEqual((first["expiry"], second["expiry"]), (1, 1))
        self.assertEqual(self.outbox.to(ALICE)[-1], delivery.DmMessage(
            intern_text.expiry_note(MONDAY + DAY + 365 * DAY), (), False))

    def test_the_notice_cap_leaves_the_rest_for_the_next_tick(self):
        for uid in (ALICE, BOB, CAROL):
            self.enrol(uid, at=MONDAY - 20 * DAY)

        with mock.patch.object(delivery, "MAX_NOTICES_PER_TICK", 2):
            first, second = self.notices(MONDAY), self.notices(MONDAY + 5 * MINUTE)

        self.assertEqual((first["quiet"], second["quiet"]), (2, 1))
        self.assertEqual(sorted(self.outbox.uids()), [ALICE, BOB, CAROL])

    def test_expiry_warnings_go_before_quiet_notes(self):
        self.enrol(ALICE, at=MONDAY - 20 * DAY)
        self.enrol(BOB, at=MONDAY - 352 * DAY, alerts="off")

        with mock.patch.object(delivery, "MAX_NOTICES_PER_TICK", 1):
            result = self.notices(MONDAY)

        self.assertEqual((result, self.outbox.uids()), ({"quiet": 0, "expiry": 1}, [BOB]))

    def test_a_user_active_since_the_list_was_read_is_not_warned(self):
        self.enrol(ALICE, at=MONDAY - 352 * DAY, alerts="off")
        self.enrol(BOB, at=MONDAY - 351 * DAY, alerts="off")
        self.outbox.meanwhile[ALICE] = lambda: store.touch(self.db, BOB, MONDAY)

        self.notices(MONDAY)

        self.assertEqual(self.outbox.uids(), [ALICE])
        self.assertIsNone(store.load(self.db, BOB).expiry_warned_at)

    def test_nothing_due_means_no_window_and_no_dm(self):
        self.enrol(ALICE, at=MONDAY - DAY)
        self.enrol(BOB, at=MONDAY - 20 * DAY, last_sent_at=MONDAY - 13 * DAY)   # heard from lately

        self.assertEqual(self.notices(MONDAY), {"quiet": 0, "expiry": 0})
        self.assertEqual((self.loads, self.outbox.calls), (0, []))

    def test_with_no_window_only_expiry_warnings_go_out(self):
        # postings.db is down: "still watching" would be untrue and there is nothing
        # to suggest, but the warning before a deletion must still arrive.
        self.enrol(ALICE, at=MONDAY - 20 * DAY)
        self.enrol(BOB, at=MONDAY - 351 * DAY, alerts="off")

        result = asyncio.run(delivery.run_notices(self.db, load_window=None, send_dm=self.outbox,
                                                  now=MONDAY, companies_watched=COMPANIES,
                                                  allowed=self.allowed))

        self.assertEqual((result, self.outbox.uids()), ({"quiet": 0, "expiry": 1}, [BOB]))

    def test_a_window_that_fails_to_load_does_not_hold_back_expiry_warnings(self):
        self.enrol(ALICE, at=MONDAY - 20 * DAY)
        self.enrol(BOB, at=MONDAY - 351 * DAY, alerts="off")

        async def locked():
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            asyncio.run(delivery.run_notices(self.db, load_window=locked, send_dm=self.outbox,
                                             now=MONDAY, companies_watched=COMPANIES,
                                             allowed=self.allowed))

        self.assertEqual(self.outbox.uids(), [BOB])
        self.assertEqual(store.load(self.db, BOB).expiry_warned_at, MONDAY)


class OnlyThoseWhoMayUseTheBotAreDmed(DeliveryTest):
    """Plan 3.3: revoking access stops alerts at the next tick, and a tick asks before
    every DM. Stop, Pause and delete need no access; being DMed does."""

    def test_someone_without_access_is_not_due_and_keeps_their_cursor(self):
        self.enrol(ALICE, alerts="hourly")
        self.enrol(BOB, alerts="hourly")
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))
        self.revoked.add(BOB)
        cursor = store.load(self.db, BOB).cursor

        report = self.tick(MONDAY + HOUR)

        self.assertEqual(self.outbox.uids(), [ALICE])
        self.assertEqual((report.due, report.sent), (1, 1))
        self.assertEqual(store.load(self.db, BOB).cursor, cursor)
        self.assertEqual(self.seen(BOB), {})

    def test_with_access_back_what_arrived_meanwhile_comes_in_one_digest(self):
        self.enrol(BOB, alerts="hourly")
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))
        self.revoked.add(BOB)
        self.tick(MONDAY + HOUR)
        self.revoked.clear()

        report = self.tick(MONDAY + 2 * HOUR)

        self.assertEqual((report.sent, len(self.outbox.to(BOB))), (1, 1))

    def test_a_revocation_during_a_tick_stops_the_dm_not_yet_sent(self):
        self.enrol(ALICE, alerts="hourly")
        self.enrol(BOB, alerts="hourly")
        self.post(posting("Software Engineer Intern", MONDAY + MINUTE))
        cursor = store.load(self.db, BOB).cursor
        self.outbox.meanwhile[ALICE] = lambda: self.revoked.add(BOB)

        report = self.tick(MONDAY + HOUR)

        self.assertEqual(self.outbox.uids(), [ALICE])
        self.assertEqual(report.due, 2)
        self.assertEqual(store.load(self.db, BOB).cursor, cursor)

    def test_no_quiet_note_or_expiry_warning_goes_to_someone_without_access(self):
        self.enrol(ALICE, at=MONDAY - 20 * DAY)                    # due a quiet note
        self.enrol(BOB, at=MONDAY - 351 * DAY, alerts="off")       # due its expiry warning
        self.post(posting("Accountant", MONDAY - DAY))
        self.revoked |= {ALICE, BOB}

        result = self.notices(MONDAY)

        self.assertEqual((result, self.outbox.calls), ({"quiet": 0, "expiry": 0}, []))
        self.assertIsNone(store.load(self.db, BOB).expiry_warned_at)
        self.assertIsNone(store.load(self.db, ALICE).last_quiet_at)

    def test_whoever_still_has_access_still_gets_theirs(self):
        self.enrol(ALICE, at=MONDAY - 20 * DAY)
        self.enrol(BOB, at=MONDAY - 351 * DAY, alerts="off")
        self.post(posting("Accountant", MONDAY - DAY))
        self.revoked.add(ALICE)

        self.assertEqual(self.notices(MONDAY), {"quiet": 0, "expiry": 1})
        self.assertEqual(self.outbox.uids(), [BOB])

    def test_neither_entry_point_can_be_run_without_being_told_who_may_be_dmed(self):
        for entry in (delivery.run_tick, delivery.run_notices):
            with self.subTest(entry=entry.__name__), self.assertRaises(TypeError):
                entry(self.db, load_window=self.load_window, send_dm=self.outbox, now=MONDAY,
                      companies_watched=COMPANIES)


class AccessLapses(DeliveryTest):
    """The 30 days start when someone loses access and stop when they get it back."""

    def test_losing_access_starts_the_clock_and_getting_it_back_stops_it(self):
        self.enrol(ALICE)
        self.enrol(BOB)
        self.revoked.add(BOB)

        first = delivery.track_access(self.db, self.allowed, MONDAY)
        again = delivery.track_access(self.db, self.allowed, MONDAY + DAY)
        lapsed_at = store.load(self.db, BOB).access_lapsed_at
        self.revoked.clear()
        back = delivery.track_access(self.db, self.allowed, MONDAY + 2 * DAY)

        self.assertEqual((first, again, back), ({"lapsed": 1, "restored": 0},
                                                {"lapsed": 0, "restored": 0},
                                                {"lapsed": 0, "restored": 1}))
        self.assertEqual(lapsed_at, MONDAY)
        self.assertIsNone(store.load(self.db, ALICE).access_lapsed_at)
        self.assertIsNone(store.load(self.db, BOB).access_lapsed_at)

    def test_thirty_days_without_access_deletes_the_profile(self):
        self.enrol(ALICE, at=MONDAY - 40 * DAY)
        self.enrol(BOB, at=MONDAY - 40 * DAY)
        self.revoked |= {ALICE, BOB}
        delivery.track_access(self.db, self.allowed, MONDAY - 31 * DAY)
        self.revoked.discard(BOB)                         # back on day 2
        delivery.track_access(self.db, self.allowed, MONDAY - 29 * DAY)

        counts = delivery.run_housekeeping(self.db, MONDAY)

        self.assertEqual(counts["access_deleted"], 1)
        self.assertIsNone(store.load(self.db, ALICE))
        self.assertIsNotNone(store.load(self.db, BOB))


class Housekeeping(DeliveryTest):
    def test_it_runs_once_per_day_in_diayn_tz(self):
        evening = clock(10, 5, 16, 30)                          # 23:30 UTC
        self.enrol(at=evening - 366 * DAY)
        due_before = delivery.housekeeping_due(self.db, evening)

        counts = delivery.run_housekeeping(self.db, evening)

        self.assertTrue(due_before)
        self.assertEqual(counts["expired"], 1)
        self.assertIsNone(store.load(self.db, ALICE))
        self.assertEqual(store.get_meta(self.db, "housekeeping_day"), 20261005)
        # The UTC date has turned by 18:00 Pacific; the Pacific one has not.
        self.assertFalse(delivery.housekeeping_due(self.db, clock(10, 5, 18, 0)))
        self.assertTrue(delivery.housekeeping_due(self.db, clock(10, 6, 0, 30)))

    def test_in_utc_the_day_turns_at_utc_midnight(self):
        evening = clock(10, 5, 16, 30)                          # 23:30 UTC
        with in_zone("UTC"):
            delivery.run_housekeeping(self.db, evening)

            self.assertEqual(store.get_meta(self.db, "housekeeping_day"), 20261005)
            self.assertTrue(delivery.housekeeping_due(self.db, clock(10, 5, 18, 0)))


class TheZoneIsDiaynTz(unittest.TestCase):
    """The slots follow whatever DIAYN_TZ says; America/Los_Angeles is only this module's."""

    def test_the_zone_is_the_one_set(self):
        with in_zone("UTC"):
            self.assertEqual(str(delivery.TZ), "UTC")
        self.assertEqual(str(delivery.TZ), "America/Los_Angeles")

    def test_a_daily_slot_is_its_hour_in_diayn_tz(self):
        nine_utc = datetime(2026, 10, 5, 9, tzinfo=timezone.utc).timestamp()
        p = person(last_run_at=nine_utc - DAY + 60)

        with in_zone("UTC"):
            self.assertFalse(delivery.is_due(p, nine_utc - 60))
            self.assertTrue(delivery.is_due(p, nine_utc))
            self.assertEqual(delivery.next_slot(p, nine_utc - 3600), nine_utc)


if __name__ == "__main__":
    unittest.main()
