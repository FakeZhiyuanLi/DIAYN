"""
Where internship profiles live, and every rule about how they are written and
erased.

    python3 -m unittest discover -s tests      # no install needed

`intern_store` is sqlite3 and nothing else, so everything here runs against an
in-memory database built by `init_db` alone — never the real `users.db`. The
tests that need a file (did the deleted bytes leave the disk? what does a new
users.db hold?) make it in a temporary directory that is gone when the test
ends.

The rules that matter most, because breaking them breaks nothing visible:

  * a stale card can never overwrite delivery bookkeeping;
  * a deleted user stays deleted, even when a DM in flight tries to record
    something about them afterwards, and even when the import of the old
    tracker's subscribers is run again;
  * `/internships delete` erases every row that names the user, in every
    finder table, including ones added after this test was written.
"""

import dataclasses
import os
import sqlite3
import tempfile
import unittest

import intern_profile as profile
import intern_store as store
import intern_vocab

DAY = 86400
NOW = 1_790_000_000.0
CURSOR = NOW - 600
ALICE, BOB = 111_111_111_111_111_111, 222_222_222_222_222_222

#: The old tracker's subscriber table, as its bot created it in that bot's own
#: stats.db, with the three columns it gained later. A test that needs
#: `intern_pings` builds it exactly this way: `init_db` never creates it, and
#: DIAYN's users.db never holds it.
PINGS_DDL = """
    CREATE TABLE IF NOT EXISTS intern_pings (
        user_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL
    )
"""
PINGS_ADDED = (("categories", "TEXT"), ("us_only", "INTEGER"), ("days", "INTEGER"))


def create_pings(db: sqlite3.Connection) -> None:
    db.execute(PINGS_DDL)
    for column, decl in PINGS_ADDED:
        db.execute(f"ALTER TABLE intern_pings ADD COLUMN {column} {decl}")
    db.commit()


def add_legacy(db: sqlite3.Connection, uid: int, categories, us_only) -> None:
    """The old bot's own positional five-value write."""
    db.execute("INSERT OR REPLACE INTO intern_pings VALUES (?, ?, ?, ?, ?)",
               (uid, 999, categories, us_only, None))
    db.commit()


def make(uid: int, **fields) -> profile.Profile:
    base = profile.new_profile(uid, NOW, source="manual", cursor=CURSOR)
    return dataclasses.replace(base, **{"fields": ("software",), **fields})


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        store.init_db(self.db)

    def tearDown(self):
        self.db.close()

    def save(self, uid: int, **fields) -> profile.Profile:
        return store.save(self.db, make(uid, **fields), now=NOW, cursor=CURSOR)

    def set_columns(self, uid: int, **columns) -> None:
        """Bookkeeping written straight to the row, as delivery would leave it."""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self.db.execute(f"UPDATE intern_profiles SET {assignments} WHERE user_id = ?",
                        (*columns.values(), uid))
        self.db.commit()

    def count(self, table: str, uid: int) -> int:
        return self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid,)).fetchone()[0]

    def seen(self, uid: int) -> dict:
        rows = self.db.execute("SELECT role_hash, state, at FROM intern_seen WHERE user_id = ?", (uid,))
        return {h: (state, at) for h, state, at in rows}


class InitDb(StoreTest):
    def test_running_it_twice_is_harmless(self):
        store.init_db(self.db)

        self.assertIsNone(store.load(self.db, ALICE))

    def test_freed_pages_are_zeroed(self):
        self.assertEqual(self.db.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_meta_works_on_a_database_prepared_by_init_db_alone(self):
        store.set_meta(self.db, "delivery_last_at", 12.5)
        store.set_meta(self.db, "delivery_last_at", 13.0)

        self.assertEqual(store.get_meta(self.db, "delivery_last_at"), 13.0)
        self.assertIsNone(store.get_meta(self.db, "never_written"))

    def test_intern_meta_is_a_key_and_a_number(self):
        columns = [(name, kind, pk) for _, name, kind, _, _, pk
                   in self.db.execute("PRAGMA table_info(intern_meta)")]

        self.assertEqual(columns, [("key", "TEXT", 1), ("value", "REAL", 0)])

    def test_the_legacy_table_is_never_created_here(self):
        names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master")}

        self.assertNotIn("intern_pings", names)

    def test_the_legacy_fixture_has_the_old_tracker_s_columns_in_order(self):
        # The old bot wrote its rows positionally, five values (add_legacy).
        create_pings(self.db)

        columns = [row[1] for row in self.db.execute("PRAGMA table_info(intern_pings)")]

        self.assertEqual(columns, ["user_id", "channel_id", "categories", "us_only", "days"])


class ANewUsersDb(unittest.TestCase):
    """DIAYN's own users.db, as init_db leaves a file that did not exist."""

    def test_it_holds_the_finder_s_three_tables_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as folder:
            db = sqlite3.connect(os.path.join(folder, "users.db"))
            store.init_db(db)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            index = db.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                               "AND sql IS NOT NULL").fetchall()
            zeroed = db.execute("PRAGMA secure_delete").fetchone()[0]
            db.close()

        self.assertEqual(tables, {"intern_profiles", "intern_seen", "intern_meta"})
        self.assertEqual(index, [("idx_intern_seen_at",)])
        self.assertEqual(zeroed, 1)


class SaveAndLoad(StoreTest):
    def everything_set(self) -> profile.Profile:
        return make(ALICE, source="resume", consent_version=1,
                    majors=("mechanical_engineering", "physics"), minors=("mathematics",),
                    degree="bachelor", grad_year=2027, grad_month=6,
                    skills=("solidworks", "matlab"), keywords=("turbomachinery",),
                    fields=("mechanical", "aerospace"), fields_locked=True,
                    levels=("intern", "new_grad"), levels_locked=True,
                    locations=("oc", "unlisted", "st:WA"), terms=("Summer 2027",),
                    companies_only=("boeing",), companies_hidden=("cvshealth",),
                    alerts="weekly", alert_hour=17, min_score=75, paused_until=NOW + 5,
                    cursor=CURSOR, last_run_at=NOW, last_sent_at=NOW - 7,
                    last_quiet_at=NOW - 8, dm_failures=1, intro_pending=True,
                    left_at=NOW - 9, expiry_warned_at=NOW - 10, created_at=NOW - 11,
                    updated_at=NOW, active_at=NOW)

    def test_every_field_survives_a_round_trip(self):
        p = self.everything_set()

        saved = store.save(self.db, p, now=NOW, cursor=CURSOR)

        self.assertEqual(saved, p)
        self.assertEqual(store.load(self.db, ALICE), p)

    def test_a_malformed_json_column_loads_as_that_columns_default(self):
        defaults = profile.new_profile(ALICE, NOW, source="manual", cursor=CURSOR)
        for column in ("majors", "minors", "skills", "keywords", "fields", "levels",
                       "locations", "terms", "companies_only", "companies_hidden"):
            for junk in ("not json", '"a string"', '{"a": 1}'):
                with self.subTest(column=column, junk=junk):
                    self.save(ALICE, majors=("physics",), levels=("entry",),
                              locations=("oc",), terms=("Summer 2027",))
                    self.set_columns(ALICE, **{column: junk})

                    self.assertEqual(getattr(store.load(self.db, ALICE), column),
                                     getattr(defaults, column))
                    store.delete_user(self.db, ALICE)

    def test_a_list_holding_something_other_than_text_keeps_only_the_text(self):
        self.save(ALICE)
        self.set_columns(ALICE, fields='[1, "software", null]')

        self.assertEqual(store.load(self.db, ALICE).fields, ("software",))

    def test_a_stale_card_cannot_overwrite_delivery_bookkeeping(self):
        stale = self.save(ALICE)
        store.advance(self.db, ALICE, cursor=NOW + 100, now=NOW + 200, sent=True)
        store.mark_dm_failure(self.db, ALICE, NOW + 300, cursor=NOW + 250)
        store.set_alerts(self.db, ALICE, "hourly", 9, NOW + 400, cursor=NOW + 350)
        before = store.load(self.db, ALICE)

        after = store.save(self.db, dataclasses.replace(stale, fields=("finance",), alerts="off",
                                                        alert_hour=3, dm_failures=0),
                           now=NOW + 500, cursor=1.0)

        self.assertEqual(after.fields, ("finance",))
        for column in ("cursor", "last_run_at", "last_sent_at", "dm_failures", "alerts",
                       "alert_hour", "paused_until", "intro_pending", "created_at"):
            with self.subTest(column=column):
                self.assertEqual(getattr(after, column), getattr(before, column))
        self.assertEqual((after.updated_at, after.active_at), (NOW + 500, NOW + 500))

    def test_a_new_row_needs_a_cursor(self):
        with self.assertRaises(ValueError):
            store.save(self.db, make(ALICE), now=NOW)

        self.assertIsNone(store.load(self.db, ALICE))

    def test_a_new_row_takes_the_cursor_given_and_runs_from_now(self):
        saved = store.save(self.db, make(ALICE, cursor=1.0, last_run_at=2.0),
                           now=NOW + 60, cursor=NOW - 540)

        self.assertEqual((saved.cursor, saved.last_run_at), (NOW - 540, NOW + 60))

    def test_saving_over_a_warned_profile_rearms_the_expiry_warning(self):
        # Used again after the warning, the profile must be warned again before a
        # second idle year ends; neither save path may keep the old mark.
        stale = self.save(ALICE)
        for cursor in (None, CURSOR):                      # the update, and the upsert
            with self.subTest(cursor=cursor):
                self.set_columns(ALICE, expiry_warned_at=NOW + 1)

                after = store.save(self.db, dataclasses.replace(stale, expiry_warned_at=NOW + 1),
                                   now=NOW + 2, cursor=cursor)

                self.assertIsNone(after.expiry_warned_at)


class Touch(StoreTest):
    def test_touch_bumps_only_active_at(self):
        before = self.save(ALICE)

        store.touch(self.db, ALICE, NOW + 42)

        after = store.load(self.db, ALICE)
        self.assertEqual(after, dataclasses.replace(before, active_at=NOW + 42))

    def test_touching_nobody_creates_nobody(self):
        store.touch(self.db, ALICE, NOW)

        self.assertIsNone(store.load(self.db, ALICE))

    def test_touch_rearms_the_expiry_warning(self):
        self.save(ALICE)
        self.set_columns(ALICE, active_at=NOW - 352 * DAY, expiry_warned_at=NOW - DAY)

        store.touch(self.db, ALICE, NOW)

        self.assertIsNone(store.load(self.db, ALICE).expiry_warned_at)
        next_year = [p.user_id for p in store.expiring_profiles(self.db, NOW + 351 * DAY)]
        self.assertEqual(next_year, [ALICE])


class SetAlerts(StoreTest):
    def test_turning_alerts_back_on_restarts_delivery_from_now(self):
        self.save(ALICE, alerts="off")
        self.set_columns(ALICE, dm_failures=2, paused_until=NOW + DAY)

        p = store.set_alerts(self.db, ALICE, "daily", 17, NOW + 900, cursor=NOW + 300)

        self.assertEqual((p.alerts, p.alert_hour), ("daily", 17))
        self.assertEqual((p.cursor, p.last_run_at), (NOW + 300, NOW + 900))
        self.assertEqual((p.dm_failures, p.paused_until), (0, None))

    def test_a_change_of_cadence_keeps_the_cursor(self):
        before = self.save(ALICE, alerts="daily")

        p = store.set_alerts(self.db, ALICE, "weekly", 9, NOW + 900, cursor=NOW + 300)

        self.assertEqual(p.alerts, "weekly")
        self.assertEqual((p.cursor, p.last_run_at), (before.cursor, before.last_run_at))

    def test_a_user_whose_dms_were_refused_restarts_from_now(self):
        self.save(ALICE, alerts="daily")
        self.set_columns(ALICE, dm_failures=store.DM_FAILURE_LIMIT)

        p = store.set_alerts(self.db, ALICE, "daily", 9, NOW + 900, cursor=NOW + 300)

        self.assertEqual((p.cursor, p.dm_failures), (NOW + 300, 0))

    def test_turning_alerts_off_keeps_the_cursor(self):
        before = self.save(ALICE)

        p = store.set_alerts(self.db, ALICE, "off", 9, NOW + 900, cursor=NOW + 300)

        self.assertEqual((p.alerts, p.cursor), ("off", before.cursor))

    def test_the_cursor_never_moves_back(self):
        # A bootstrap sweep put the cursor ahead of the settle horizon; turning
        # alerts on must not reopen the seed rows it stepped over.
        self.save(ALICE, alerts="off")
        store.advance_all_cursors(self.db, NOW + 1000)

        p = store.set_alerts(self.db, ALICE, "daily", 9, NOW + 900, cursor=NOW + 300)

        self.assertEqual(p.cursor, NOW + 1000)

    def test_nobody_to_change_is_none_and_creates_nothing(self):
        self.assertIsNone(store.set_alerts(self.db, ALICE, "daily", 9, NOW, cursor=NOW))
        self.assertIsNone(store.load(self.db, ALICE))

    def test_an_unknown_cadence_is_refused(self):
        self.save(ALICE)

        with self.assertRaises(ValueError):
            store.set_alerts(self.db, ALICE, "sometimes", 9, NOW, cursor=NOW)


class PausesAndFailures(StoreTest):
    def test_a_pause_is_set_and_cleared(self):
        self.save(ALICE)

        store.set_paused_until(self.db, ALICE, NOW + 7 * DAY, NOW)
        paused = store.load(self.db, ALICE).paused_until
        store.set_paused_until(self.db, ALICE, None, NOW + 1)

        self.assertEqual(paused, NOW + 7 * DAY)
        self.assertIsNone(store.load(self.db, ALICE).paused_until)

    def test_dm_failures_count_up_and_advance_the_cursor(self):
        self.save(ALICE)

        counts = [store.mark_dm_failure(self.db, ALICE, NOW + n, cursor=NOW + n - 600)
                  for n in (10, 20, 30)]

        p = store.load(self.db, ALICE)
        self.assertEqual(counts, [1, 2, 3])
        self.assertEqual((p.cursor, p.last_run_at), (NOW - 570, NOW + 30))

    def test_a_failure_for_nobody_is_zero(self):
        self.assertEqual(store.mark_dm_failure(self.db, ALICE, NOW, cursor=NOW), 0)

    def test_a_refused_notice_is_counted_and_moves_nothing_else(self):
        # A notice carries no postings, so its refusal must not step the cursor past any.
        before = self.save(ALICE)

        counts = [store.count_dm_failure(self.db, ALICE) for _ in range(3)]

        self.assertEqual(counts, [1, 2, 3])
        self.assertEqual(store.load(self.db, ALICE), dataclasses.replace(before, dm_failures=3))

    def test_a_refused_notice_for_nobody_is_zero_and_creates_nobody(self):
        self.assertEqual(store.count_dm_failure(self.db, ALICE), 0)
        self.assertIsNone(store.load(self.db, ALICE))

    def test_resetting_failures_zeroes_them(self):
        self.save(ALICE)
        self.set_columns(ALICE, dm_failures=3)

        store.reset_dm_failures(self.db, ALICE)

        self.assertEqual(store.load(self.db, ALICE).dm_failures, 0)


class Advance(StoreTest):
    def test_a_sent_digest_records_the_send_and_clears_the_counters(self):
        self.save(ALICE, intro_pending=True)
        self.set_columns(ALICE, dm_failures=2, paused_until=NOW - 5)

        store.advance(self.db, ALICE, cursor=NOW + 100, now=NOW + 700, sent=True, clear_pause=True)

        p = store.load(self.db, ALICE)
        self.assertEqual((p.cursor, p.last_run_at, p.last_sent_at), (NOW + 100, NOW + 700, NOW + 700))
        self.assertEqual((p.dm_failures, p.intro_pending, p.paused_until), (0, False, None))

    def test_an_empty_tick_moves_the_cursor_and_nothing_it_did_not_do(self):
        self.save(ALICE, intro_pending=True)
        self.set_columns(ALICE, paused_until=NOW + DAY)

        store.advance(self.db, ALICE, cursor=NOW + 100, now=NOW + 700, sent=False)

        p = store.load(self.db, ALICE)
        self.assertEqual((p.cursor, p.last_run_at, p.last_sent_at), (NOW + 100, NOW + 700, None))
        self.assertEqual((p.intro_pending, p.paused_until), (True, NOW + DAY))

    def test_advance_never_lowers_the_cursor(self):
        self.save(ALICE)
        store.advance_all_cursors(self.db, NOW + 1000)

        store.advance(self.db, ALICE, cursor=NOW + 100, now=NOW + 700, sent=False)

        self.assertEqual(store.load(self.db, ALICE).cursor, NOW + 1000)

    def test_advance_all_cursors_raises_every_cursor_and_lowers_none(self):
        self.save(ALICE)
        self.save(BOB)
        self.set_columns(BOB, cursor=NOW + 5000)

        changed = store.advance_all_cursors(self.db, NOW + 1000)

        self.assertEqual(changed, 2)
        self.assertEqual(store.load(self.db, ALICE).cursor, NOW + 1000)
        self.assertEqual(store.load(self.db, BOB).cursor, NOW + 5000)

    def test_a_profile_saved_just_after_a_bootstrap_starts_past_the_seed(self):
        # The advance reaches only the rows there at the time. One saved three
        # minutes later would start at a settle horizon from before the seed.
        store.advance_all_cursors(self.db, NOW + 1000)

        saved = store.save(self.db, make(ALICE), now=NOW + 1180, cursor=NOW + 580)

        self.assertEqual(saved.cursor, NOW + 1000)

    def test_the_floor_is_the_latest_bootstrap_and_a_later_cursor_is_kept(self):
        store.advance_all_cursors(self.db, NOW + 1000)
        store.advance_all_cursors(self.db, NOW + 10)

        early = store.save(self.db, make(ALICE), now=NOW + 1100, cursor=NOW + 500)
        late = store.save(self.db, make(BOB), now=NOW + 5000, cursor=NOW + 4400)

        self.assertEqual((early.cursor, late.cursor), (NOW + 1000, NOW + 4400))


class Marks(StoreTest):
    def test_quiet_and_expiry_marks_record_when(self):
        self.save(ALICE)

        store.mark_quiet_sent(self.db, ALICE, NOW + 1)
        store.mark_expiry_warned(self.db, ALICE, NOW + 2)

        p = store.load(self.db, ALICE)
        self.assertEqual((p.last_quiet_at, p.expiry_warned_at), (NOW + 1, NOW + 2))

    def test_leaving_and_coming_back(self):
        self.save(ALICE)

        store.mark_left(self.db, ALICE, NOW + 1)
        store.mark_left(self.db, ALICE, NOW + 99)
        left = store.load(self.db, ALICE).left_at
        store.clear_left(self.db, ALICE)

        # The first departure starts the 30 days; a repeat does not restart them.
        self.assertEqual(left, NOW + 1)
        self.assertIsNone(store.load(self.db, ALICE).left_at)

    def test_alerting_profiles_leaves_out_alerts_off(self):
        self.save(ALICE, alerts="daily")
        self.save(BOB, alerts="off")

        self.assertEqual([p.user_id for p in store.alerting_profiles(self.db)], [ALICE])


class HideCompany(StoreTest):
    def test_a_company_is_added_normalised_and_only_once(self):
        self.save(ALICE)

        store.hide_company(self.db, ALICE, "CVS Health", NOW + 1)
        p = store.hide_company(self.db, ALICE, "cvshealth", NOW + 2)

        self.assertEqual(p.companies_hidden, ("cvshealth",))
        self.assertEqual(store.load(self.db, ALICE).companies_hidden, ("cvshealth",))

    def test_at_the_cap_the_oldest_hidden_company_makes_room(self):
        full = tuple(f"company{n}" for n in range(intern_vocab.MAX_COMPANIES_HIDDEN))
        self.save(ALICE, companies_hidden=full)

        p = store.hide_company(self.db, ALICE, "Boeing", NOW)

        self.assertEqual(p.companies_hidden, full[1:] + ("boeing",))

    def test_nobody_to_change_is_none(self):
        self.assertIsNone(store.hide_company(self.db, ALICE, "Boeing", NOW))


class Ledger(StoreTest):
    def test_seen_hashes_filters_by_state(self):
        self.save(ALICE)
        store.record_sent(self.db, ALICE, ["aaaa", "bbbb"], NOW)
        store.hide(self.db, ALICE, ["cccc"], NOW)

        self.assertEqual(store.seen_hashes(self.db, ALICE), frozenset({"aaaa", "bbbb", "cccc"}))
        self.assertEqual(store.seen_hashes(self.db, ALICE, states=("hidden",)), frozenset({"cccc"}))
        self.assertEqual(store.seen_hashes(self.db, BOB), frozenset())

    def test_hiding_upgrades_a_sent_role(self):
        self.save(ALICE)
        store.record_sent(self.db, ALICE, ["aaaa"], NOW)

        store.hide(self.db, ALICE, ["aaaa"], NOW + 5)

        self.assertEqual(self.seen(ALICE), {"aaaa": ("hidden", NOW + 5)})

    def test_sending_never_downgrades_a_hidden_role(self):
        self.save(ALICE)
        store.hide(self.db, ALICE, ["aaaa"], NOW)

        store.record_sent(self.db, ALICE, ["aaaa"], NOW + 5)

        self.assertEqual(self.seen(ALICE), {"aaaa": ("hidden", NOW)})

    def test_nothing_is_recorded_about_someone_without_a_profile(self):
        store.record_sent(self.db, ALICE, ["aaaa"], NOW)
        store.hide(self.db, ALICE, ["bbbb"], NOW)

        self.assertEqual(self.seen(ALICE), {})

    def test_a_send_recorded_after_deletion_leaves_nothing_behind(self):
        # The delete-during-await-send_dm race: the user erases everything
        # while their DM is in flight, then delivery records it as sent.
        self.save(ALICE)
        store.delete_user(self.db, ALICE)

        store.record_sent(self.db, ALICE, ["aaaa", "bbbb"], NOW)
        store.hide(self.db, ALICE, ["cccc"], NOW)

        self.assertEqual(self.count("intern_seen", ALICE), 0)
        self.assertIsNone(store.load(self.db, ALICE))


class DeleteUser(StoreTest):
    def populate(self):
        create_pings(self.db)
        for uid in (ALICE, BOB):
            self.save(uid)
            store.record_sent(self.db, uid, ["aaaa", "bbbb"], NOW)
            add_legacy(self.db, uid, "swe", 1)

    def user_tables(self) -> list:
        """Every finder table that names a user — found, not listed."""
        names = [r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        return [n for n in names if n.startswith("intern_")
                and "user_id" in {c[1] for c in self.db.execute(f"PRAGMA table_info({n})")}]

    def test_every_finder_table_forgets_the_user_and_only_that_user(self):
        self.populate()

        removed = store.delete_user(self.db, ALICE)

        tables = self.user_tables()
        self.assertEqual(set(tables), {"intern_profiles", "intern_seen", "intern_pings"})
        for table in tables:
            with self.subTest(table=table):
                self.assertEqual(self.count(table, ALICE), 0)
                self.assertGreater(self.count(table, BOB), 0)
        self.assertEqual(removed, 4)  # profile, two seen rows, legacy row

    def test_a_table_added_later_is_covered_without_a_code_change(self):
        self.populate()
        self.db.execute("CREATE TABLE intern_future (user_id INTEGER, note TEXT)")
        self.db.execute("INSERT INTO intern_future VALUES (?, 'x')", (ALICE,))

        store.delete_user(self.db, ALICE)

        self.assertEqual(self.count("intern_future", ALICE), 0)

    def test_it_works_before_the_legacy_table_exists(self):
        self.save(ALICE)

        self.assertEqual(store.delete_user(self.db, ALICE), 1)

    def test_nobody_to_delete_is_zero(self):
        self.assertEqual(store.delete_user(self.db, ALICE), 0)

    def test_a_failure_part_way_deletes_nothing(self):
        # One transaction: a user is either wholly erased or not touched.
        self.populate()
        self.db.execute("CREATE TRIGGER refuse BEFORE DELETE ON intern_seen "
                        "BEGIN SELECT RAISE(ABORT, 'refused'); END")

        with self.assertRaises(sqlite3.Error):
            store.delete_user(self.db, ALICE)

        self.assertIsNotNone(store.load(self.db, ALICE))
        self.assertEqual(self.count("intern_pings", ALICE), 1)


class SecureDelete(unittest.TestCase):
    CANARY = b"zqxcanaryzq"

    def test_deleted_profile_bytes_do_not_stay_in_the_database_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "stats.db")
            db = sqlite3.connect(path)
            store.init_db(db)
            store.save(db, make(ALICE, keywords=(self.CANARY.decode(),)), now=NOW, cursor=CURSOR)
            with open(path, "rb") as f:
                written = f.read()

            store.delete_user(db, ALICE)
            db.close()
            with open(path, "rb") as f:
                after = f.read()

        # The control: without it, a canary that never reached the file would
        # make the real assertion pass for the wrong reason.
        self.assertIn(self.CANARY, written)
        self.assertNotIn(self.CANARY, after)


def legacy_db(rows=()) -> sqlite3.Connection:
    """The old bot's stats.db, in memory: its subscriber table holding `rows`."""
    db = sqlite3.connect(":memory:")
    create_pings(db)
    for uid, categories, us_only in rows:
        add_legacy(db, uid, categories, us_only)
    return db


class ReadLegacy(unittest.TestCase):
    def test_every_subscriber_is_read_with_the_columns_the_mapping_needs(self):
        old = legacy_db(((1, "swe", 1), (2, None, None)))

        rows = store.read_legacy(old)

        self.assertEqual(sorted(rows), [(1, "swe", 1), (2, None, None)])
        old.close()

    def test_a_database_without_the_legacy_table_is_refused(self):
        other = sqlite3.connect(":memory:")
        other.execute("CREATE TABLE sticker_stats (user_id INTEGER PRIMARY KEY, count INTEGER)")

        with self.assertRaises(store.LegacyImportError) as caught:
            store.read_legacy(other)

        self.assertIn("intern_pings", str(caught.exception))
        other.close()


class WriteMigrated(StoreTest):
    ROWS = ((1, "swe", 1), (2, None, None), (3, "hardware", 0), (4, "pm,quant", 1))

    def write(self, rows=ROWS, now=NOW, cursor=CURSOR) -> store.ImportCounts:
        return store.write_migrated(self.db, list(rows), now, cursor=cursor)

    def test_every_legacy_subscriber_gets_a_migrated_profile(self):
        counts = self.write()

        self.assertEqual(counts, store.ImportCounts(legacy=4, written=4, already=0))
        expected = {
            1: (("software", "security", "it"), ("us", "unlisted", "remote_us")),
            2: (intern_vocab.LEGACY_ALL_TECH, ("us", "unlisted", "remote_us", "abroad")),
            3: (("electrical", "mechanical", "aerospace", "manufacturing"),
                ("us", "unlisted", "remote_us", "abroad")),
            4: (("product", "business_ops", "quant"), ("us", "unlisted", "remote_us")),
        }
        for uid, (fields, locations) in expected.items():
            with self.subTest(uid=uid):
                p = store.load(self.db, uid)
                self.assertEqual((p.fields, p.locations), (fields, locations))
                self.assertEqual((p.source, p.alerts, p.alert_hour, p.min_score),
                                 ("migrated", "hourly", 9, 45))
                self.assertEqual((p.levels, p.fields_locked, p.levels_locked),
                                 (("intern", "coop"), True, True))
                self.assertEqual((p.intro_pending, p.consent_version), (True, 0))
                self.assertEqual((p.cursor, p.last_run_at, p.created_at), (CURSOR, NOW, NOW))

    def test_the_import_is_recorded_with_how_many_it_wrote(self):
        self.write()

        self.assertEqual(store.get_meta(self.db, store.LEGACY_IMPORT_KEY), 4.0)

    def test_a_second_import_is_refused_and_changes_nothing(self):
        self.write()

        with self.assertRaises(store.LegacyImportError):
            self.write(now=NOW + DAY, cursor=NOW)

        self.assertEqual(store.load(self.db, 1).last_run_at, NOW)

    def test_a_deleted_user_is_not_brought_back_by_a_second_import(self):
        self.write()
        store.delete_user(self.db, 1)

        with self.assertRaises(store.LegacyImportError):
            self.write(now=NOW + DAY, cursor=NOW)

        self.assertIsNone(store.load(self.db, 1))

    def test_an_existing_profile_is_counted_and_never_overwritten(self):
        mine = self.save(3, fields=("finance",))

        counts = self.write()

        self.assertEqual(counts, store.ImportCounts(legacy=4, written=3, already=1))
        self.assertEqual(store.load(self.db, 3), mine)

    def test_counts_that_do_not_add_up_write_nothing(self):
        # Two rows for one user: one is written, the other is neither written
        # nor already there, so the import cannot vouch for every subscriber.
        with self.assertRaises(store.LegacyImportError) as caught:
            self.write(rows=((ALICE, "swe", 1), (ALICE, "quant", 0), (BOB, None, None)))

        self.assertIsNone(store.load(self.db, ALICE))
        self.assertIsNone(store.load(self.db, BOB))
        self.assertIsNone(store.get_meta(self.db, store.LEGACY_IMPORT_KEY))
        for uid in (ALICE, BOB):
            self.assertNotIn(str(uid), str(caught.exception))

    def test_nothing_to_import_is_still_recorded_as_done(self):
        counts = self.write(rows=())

        self.assertEqual(counts, store.ImportCounts(legacy=0, written=0, already=0))
        self.assertEqual(store.get_meta(self.db, store.LEGACY_IMPORT_KEY), 0.0)

    def test_a_subscriber_imported_after_a_bootstrap_starts_past_the_seed(self):
        store.advance_all_cursors(self.db, NOW + 1000)

        self.write(now=NOW + 1180, cursor=NOW + 580)

        self.assertEqual({store.load(self.db, uid).cursor for uid, *_ in self.ROWS}, {NOW + 1000})

    def test_the_old_migration_on_every_start_is_gone(self):
        self.assertFalse(hasattr(store, "migrate_legacy"))


class Housekeeping(StoreTest):
    def test_old_ledger_rows_are_pruned_by_state(self):
        self.save(ALICE)
        store.record_sent(self.db, ALICE, ["sent46"], NOW - 46 * DAY)
        store.record_sent(self.db, ALICE, ["sent44"], NOW - 44 * DAY)
        store.hide(self.db, ALICE, ["hide91"], NOW - 91 * DAY)
        store.hide(self.db, ALICE, ["hide89"], NOW - 89 * DAY)

        counts = store.housekeeping(self.db, NOW)

        self.assertEqual(set(self.seen(ALICE)), {"sent44", "hide89"})
        self.assertEqual(counts["seen_pruned"], 2)

    def test_an_idle_profile_is_removed_with_its_legacy_row(self):
        create_pings(self.db)
        for uid, idle_days in ((ALICE, 366), (BOB, 300)):
            self.save(uid)
            add_legacy(self.db, uid, "swe", 1)
            self.set_columns(uid, active_at=NOW - idle_days * DAY)

        counts = store.housekeeping(self.db, NOW)

        self.assertIsNone(store.load(self.db, ALICE))
        self.assertEqual(self.count("intern_pings", ALICE), 0)
        self.assertIsNotNone(store.load(self.db, BOB))
        self.assertEqual(counts["expired"], 1)

    def test_a_profile_is_removed_thirty_days_after_its_owner_left(self):
        for uid, days in ((ALICE, 31), (BOB, 29)):
            self.save(uid)
            self.set_columns(uid, left_at=NOW - days * DAY)

        counts = store.housekeeping(self.db, NOW)

        self.assertIsNone(store.load(self.db, ALICE))
        self.assertIsNotNone(store.load(self.db, BOB))
        self.assertEqual(counts, {"seen_pruned": 0, "expired": 0, "left_deleted": 1})


class NoticeQueries(StoreTest):
    def test_expiring_profiles_are_idle_351_days_and_not_yet_warned(self):
        for uid, idle, warned in ((1, 352, None), (2, 352, NOW - DAY), (3, 300, None)):
            self.save(uid)
            self.set_columns(uid, active_at=NOW - idle * DAY, expiry_warned_at=warned)

        self.assertEqual([p.user_id for p in store.expiring_profiles(self.db, NOW)], [1])

    def test_quiet_candidates_have_heard_nothing_for_two_weeks(self):
        cases = {
            1: {"created_at": NOW - 30 * DAY},                                 # quiet 30 days
            2: {"created_at": NOW - 60 * DAY, "last_sent_at": NOW - 20 * DAY},  # quiet 20 days
            3: {"created_at": NOW - 30 * DAY, "last_quiet_at": NOW - 3 * DAY},  # noted lately
            4: {"created_at": NOW - 3 * DAY},                                   # new
            5: {"created_at": NOW - 30 * DAY, "alerts": "off"},
            6: {"created_at": NOW - 30 * DAY, "paused_until": NOW + DAY},
            7: {"created_at": NOW - 30 * DAY, "dm_failures": 3},
            8: {"created_at": NOW - 30 * DAY, "left_at": NOW - DAY},
        }
        for uid, columns in cases.items():
            self.save(uid)
            self.set_columns(uid, **columns)

        quiet = [p.user_id for p in store.quiet_candidates(self.db, NOW)]

        self.assertEqual(quiet, [1, 2])  # oldest silence first


class ColumnsAndPrivacy(StoreTest):
    def test_stored_columns_are_the_table_in_order(self):
        table = [r[1] for r in self.db.execute("PRAGMA table_info(intern_profiles)")]

        self.assertEqual(list(store.STORED_COLUMNS), table)
        self.assertEqual(len(table), 33)
        self.assertEqual([f.name for f in dataclasses.fields(profile.Profile)], table)

    def test_no_description_names_a_zone_the_host_may_not_use(self):
        # The zone is DIAYN_TZ, shown with the value (intern_text), not fixed here.
        self.assertEqual(store.STORED_COLUMNS["alert_hour"], "Hour of day for alerts")
        self.assertFalse([d for d in store.STORED_COLUMNS.values() if "Pacific" in d])

    def test_leaving_is_described_without_a_club(self):
        self.assertEqual(store.STORED_COLUMNS["left_at"],
                         "When you left the last server you shared with this bot")

    def test_every_column_has_a_plain_english_description(self):
        for column, text in store.STORED_COLUMNS.items():
            with self.subTest(column=column):
                self.assertIsInstance(text, str)
                self.assertTrue(text.strip())
                self.assertNotIn("*", text)

    def test_privacy_rows_hold_every_column_and_both_counts(self):
        self.save(ALICE, keywords=("robotics",))
        store.record_sent(self.db, ALICE, ["aaaa", "bbbb"], NOW)
        store.hide(self.db, ALICE, ["cccc"], NOW)

        rows = store.privacy_rows(self.db, ALICE)

        self.assertEqual(list(rows), list(store.STORED_COLUMNS) + ["sent_count", "hidden_count"])
        self.assertEqual((rows["sent_count"], rows["hidden_count"]), (2, 1))
        self.assertEqual(rows["keywords"], ("robotics",))
        self.assertEqual(rows["user_id"], ALICE)

    def test_privacy_rows_for_nobody_is_none(self):
        self.assertIsNone(store.privacy_rows(self.db, ALICE))


class Summary(StoreTest):
    def test_summary_counts_and_names_nobody(self):
        self.save(ALICE, alerts="daily", fields=("software", "finance"))
        self.save(BOB, alerts="hourly", fields=("software",))
        self.save(333_333_333_333_333_333, alerts="off", fields=("design",))
        self.set_columns(BOB, dm_failures=3)
        self.set_columns(333_333_333_333_333_333, left_at=NOW)

        s = store.summary(self.db)

        self.assertEqual((s["profiles"], s["alerting"], s["hourly"], s["daily"], s["weekly"]),
                         (3, 2, 1, 1, 0))
        self.assertEqual((s["dm_blocked"], s["left"]), (1, 1))
        self.assertEqual((s["field:software"], s["field:finance"], s["field:civil"]), (2, 1, 0))
        self.assertEqual({k for k in s if k.startswith("field:")},
                         {f"field:{f}" for f in intern_vocab.FIELD_IDS})
        for uid in (ALICE, BOB):
            self.assertNotIn(str(uid), repr(s))


if __name__ == "__main__":
    unittest.main()
