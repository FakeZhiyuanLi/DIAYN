"""
The Gemini fit check: what it sends, what it keeps, and how it fails.

    python3 -m unittest discover -s tests      # no install needed

Every request goes to FakeGemini, which answers generateContent the way the
real one does, through the same `llm.generate_json` the check uses, so the
tests see the exact request body that would have left the machine. Nothing
reaches the network, and every database is in memory.

The rules that matter most, because breaking them breaks nothing visible:

  * only labels and four posting fields are ever sent: never a Discord id, a
    name, an email address or a word of a resume;
  * whatever goes wrong (no key, a spent budget, an error, an answer that
    does not parse) the matches come back unchecked, never held back;
  * a verdict is asked for once per profile and role, and asked again when
    the profile changes;
  * a no_fit is never sent.
"""

import asyncio
import contextlib
import dataclasses
import io
import json
import sqlite3
import unittest
from datetime import date
from unittest import mock

import intern_delivery as delivery
import intern_fit as fit
import intern_match
import intern_profile as profile
import intern_store as store
import internship_poller as poller
import resume_parse
from test_intern_delivery import (ALICE, BOB, COMPANIES, DAY, HOUR, MINUTE, MONDAY,
                                  DeliveryTest, person, posting)
from test_llm import Response, gemini_body

KEY = "test-key-not-real"
NOW = MONDAY + HOUR
TITLES = ("Software Engineer Intern", "Backend Software Intern", "Frontend Engineering Intern")


def settings(**env):
    """The scraper's settings with a Gemini key, and whatever else `env` sets."""
    return poller.configure({"GEMINI_API_KEY": KEY, "LLM_DAY_TZ": "UTC", **env})


def with_settings(**env):
    return mock.patch.object(poller, "SETTINGS", settings(**env))


def verdicts_by_title(table):
    """A FakeGemini answer: each posting's verdict looked up by its title."""
    def answer(payload):
        return [{"i": p["i"], "verdict": table[p["title"]][0], "reason": table[p["title"]][1]}
                for p in payload["postings"]]
    return answer


def all_fit(payload):
    return [{"i": p["i"], "verdict": "fit", "reason": f"Your fields fit {p['title']}"}
            for p in payload["postings"]]


class FakeGemini:
    """generateContent, faked. Each request is answered by `answer(payload)`: a list or a
    string becomes the reply's text, a Response is sent as it is. Every body is kept."""

    def __init__(self, answer=all_fit, usage=None):
        self.answer, self.usage = answer, usage or {"promptTokenCount": 900,
                                                    "candidatesTokenCount": 80}
        self.bodies = []

    def post(self, url, json=None, headers=None):
        self.bodies.append({"url": url, "json": json, "headers": headers})
        reply = self.answer(self.payload(len(self.bodies) - 1))
        if isinstance(reply, Response):
            return reply
        text = reply if isinstance(reply, str) else _dumps(reply)
        return Response(200, gemini_body(text, self.usage))

    def prompt(self, n=-1) -> str:
        return self.bodies[n]["json"]["contents"][0]["parts"][0]["text"]

    def payload(self, n=-1) -> dict:
        prompt = self.prompt(n)
        self.test_prompt_shape(prompt)
        return json.loads(prompt[len(fit.FIT_PROMPT):])

    @staticmethod
    def test_prompt_shape(prompt):
        if not prompt.startswith(fit.FIT_PROMPT):
            raise AssertionError("the prompt is not FIT_PROMPT followed by the payload")

    @property
    def requests(self) -> int:
        return len(self.bodies)


def _dumps(value) -> str:
    return json.dumps(value)


class FitTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        store.init_db(self.db)
        fit.init_db(self.db)
        self.addCleanup(self.db.close)
        patch = with_settings()
        patch.start()
        self.addCleanup(patch.stop)
        self.pace = fit.Pace()
        self.slept, self.waited = [], 0.0
        self.addCleanup(setattr, fit, "last_error", None)
        self.addCleanup(setattr, fit, "quiet_until", 0.0)
        fit.last_error, fit.quiet_until = None, 0.0

    async def _sleep(self, seconds):
        """asyncio.sleep, faked: the clock `check` is given moves on by `seconds`."""
        self.slept.append(seconds)
        self.waited += seconds

    def matches(self, p=None, titles=TITLES):
        p = p or person()
        rows = [posting(title, MONDAY + n * MINUTE, company=f"Firm {n}")
                for n, title in enumerate(titles)]
        found = intern_match.rank(p, intern_match.tag_rows(rows), NOW)
        self.assertEqual(len(found), len(titles))
        return found

    def check(self, p, matches, gemini, now=NOW):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = asyncio.run(fit.check(self.db, p, matches, now, session=gemini,
                                           pace=self.pace, sleep=self._sleep,
                                           clock=lambda: now + self.waited))
        self.log = stderr.getvalue()
        return result

    def stored(self):
        return self.db.execute("SELECT profile_fp, role_hash, verdict, reason, model, at "
                               "FROM fit_verdicts ORDER BY role_hash").fetchall()


# ------------------------------------------------------------------ what is sent

class WhatIsSent(FitTest):
    def test_the_profile_is_sent_as_labels_under_exactly_these_keys(self):
        p = person(majors=("mechanical_engineering",), minors=("mathematics",), degree="bachelor",
                   grad_year=2027, grad_month=6, skills=("solidworks", "python"),
                   keywords=("turbomachinery",), fields=("software", "mechanical"),
                   levels=("intern", "coop"), locations=("oc", "st:WA", "unlisted"),
                   terms=("Summer 2027",))

        labels = fit.labels(p)

        self.assertEqual(set(labels), {"majors", "minors", "degree", "graduation", "levels",
                                       "fields", "skills", "keywords", "locations", "terms"})
        self.assertEqual(labels["majors"], ["Mechanical Engineering"])
        self.assertEqual(labels["minors"], ["Mathematics"])
        self.assertEqual(labels["degree"], "Bachelor's")
        self.assertEqual(labels["graduation"], "June 2027")
        self.assertIn("SolidWorks", labels["skills"])
        self.assertEqual(labels["keywords"], ["turbomachinery"])
        self.assertEqual(labels["terms"], ["Summer 2027"])
        self.assertIn("State: Washington", labels["locations"])
        self.assertNotIn("st:WA", json.dumps(labels))

    def test_each_posting_is_sent_as_its_number_title_company_location_and_term(self):
        gemini = FakeGemini()
        matches = self.matches(titles=("Summer 2027 Software Engineer Intern",))

        self.check(person(), matches, gemini)

        payload = gemini.payload()
        self.assertEqual(set(payload), {"profile", "postings"})
        (sent,) = payload["postings"]
        self.assertEqual(set(sent), {"i", "title", "company", "location", "term"})
        self.assertEqual(sent, {"i": 0, "title": "Summer 2027 Software Engineer Intern",
                                "company": "Firm 0", "location": "Minneapolis, MN",
                                "term": "Summer 2027"})
        self.assertEqual(payload, fit.request_payload(person(), matches))

    def test_the_prompt_is_the_instructions_then_the_payload_and_nothing_else(self):
        gemini, p = FakeGemini(), person()
        matches = self.matches(p)

        self.check(p, matches, gemini)

        self.assertEqual(gemini.prompt(), fit.FIT_PROMPT + json.dumps(
            fit.request_payload(p, matches), ensure_ascii=False))
        body = gemini.bodies[0]["json"]
        self.assertEqual(body["generationConfig"]["responseSchema"], fit.FIT_SCHEMA)
        self.assertEqual(gemini.bodies[0]["headers"], {"x-goog-api-key": KEY})

    def test_no_id_name_email_phone_school_or_resume_text_ever_leaves(self):
        resume = ("Jordan Q. Testperson\n"
                  "jordan.testperson@example.edu | (949) 555-0142 | linkedin.com/in/jqtest\n"
                  "EDUCATION\n"
                  "University of Nowhere, B.S. Mechanical Engineering, Expected June 2027\n"
                  "GPA 3.91\n"
                  "SKILLS\n"
                  "SolidWorks, MATLAB, Python\n"
                  "EXPERIENCE\n"
                  "Designed a regenerative braking rig for the Zephyr Racing club team.\n")
        draft = resume_parse.derive_draft(resume, date(2026, 9, 28))
        p = profile.from_draft(ALICE, draft, NOW, source="resume", cursor=NOW - 600,
                               today=date(2026, 9, 28))
        p = dataclasses.replace(p, fields=p.fields or ("mechanical",))
        gemini = FakeGemini()

        self.check(p, self.matches(p, titles=("Mechanical Engineering Intern",)), gemini)

        sent = json.dumps(gemini.bodies[0]["json"])
        self.assertIn("Mechanical Engineering", sent)             # the labels do go
        for private in (str(ALICE), "Jordan", "Testperson", "example.edu", "@", "555-0142",
                        "linkedin", "University of Nowhere", "3.91", "regenerative", "Zephyr"):
            with self.subTest(private=private):
                self.assertNotIn(private, sent)

    def test_the_fingerprint_is_the_labels_and_nothing_else(self):
        p = person()
        same_labels = dataclasses.replace(person(BOB), alerts="hourly", cursor=NOW, min_score=75)

        self.assertEqual(fit.profile_fp(p), fit.profile_fp(same_labels))
        self.assertRegex(fit.profile_fp(p), r"^[0-9a-f]{64}$")
        for change in ({"skills": ("python",)}, {"keywords": ("robotics",)},
                       {"terms": ("Summer 2027",)}, {"locations": ("oc",)},
                       {"grad_year": 2028}, {"degree": "master"}, {"levels": ("new_grad",)}):
            with self.subTest(change=change):
                self.assertNotEqual(fit.profile_fp(p),
                                    fit.profile_fp(dataclasses.replace(p, **change)))


# ------------------------------------------------------------------ the answer

class TheAnswer(unittest.TestCase):
    def test_verdicts_are_read_by_posting_number(self):
        found = fit.parse_verdicts([{"i": 1, "verdict": "no_fit", "reason": "A nursing role"},
                                    {"i": 0, "verdict": "fit", "reason": "Fits your major"}], 2)

        self.assertEqual(found, {0: fit.Verdict("fit", "Fits your major"),
                                 1: fit.Verdict("no_fit", "A nursing role")})

    def test_a_reason_is_one_line_of_at_most_120_characters(self):
        found = fit.parse_verdicts([{"i": 0, "verdict": "unsure",
                                     "reason": "  Two\nlines   and " + "x" * 300}], 1)

        reason = found[0].reason
        self.assertLessEqual(len(reason), fit.REASON_MAX)
        self.assertEqual(fit.REASON_MAX, 120)
        self.assertTrue(reason.startswith("Two lines and x"))
        self.assertNotIn("\n", reason)

    def test_rows_that_cannot_be_used_are_dropped_and_the_first_of_a_number_wins(self):
        found = fit.parse_verdicts([
            {"i": 0, "verdict": "maybe", "reason": "unknown verdict"},
            {"i": 5, "verdict": "fit", "reason": "no such posting"},
            {"i": True, "verdict": "fit", "reason": "a bool is not a number"},
            {"i": "1", "verdict": "fit", "reason": "a string is not a number"},
            {"i": 1, "verdict": "fit", "reason": 7},
            {"i": 1, "verdict": "fit"},
            "junk",
            {"i": 2, "verdict": "unsure", "reason": "first"},
            {"i": 2, "verdict": "fit", "reason": "second"},
        ], 3)

        self.assertEqual(found, {2: fit.Verdict("unsure", "first")})

    def test_an_answer_with_nothing_usable_is_unparseable(self):
        for data in ({"i": 0, "verdict": "fit", "reason": "not a list"}, [], ["junk"],
                     [{"i": 9, "verdict": "fit", "reason": "out of range"}], "text", None):
            with self.subTest(data=data), self.assertRaises(fit.Unparseable):
                fit.parse_verdicts(data, 1)


# ------------------------------------------------------------------ checking

class Checking(FitTest):
    def test_fit_comes_first_then_unsure_and_no_fit_is_dropped(self):
        table = {TITLES[0]: ("no_fit", "Needs a security clearance you did not list"),
                 TITLES[1]: ("unsure", "Backend work; your skills do not say"),
                 TITLES[2]: ("fit", "Frontend work fits your software field")}
        matches = self.matches()

        kept = self.check(person(), matches, FakeGemini(verdicts_by_title(table)))

        self.assertEqual([m.cand.title for m in kept], [TITLES[2], TITLES[1]])
        self.assertEqual([m.fit for m in kept], [table[TITLES[2]], table[TITLES[1]]])
        self.assertEqual([m.fit for m in matches], [None] * 3)      # the input is untouched

    def test_within_a_verdict_the_matchers_order_is_kept(self):
        matches = self.matches()

        kept = self.check(person(), matches, FakeGemini())

        self.assertEqual([m.cand.rowid for m in kept], [m.cand.rowid for m in matches])

    def test_every_verdict_is_cached_under_the_profile_and_the_role(self):
        p, matches = person(), self.matches()

        self.check(p, matches, FakeGemini())

        rows = self.stored()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r[0] for r in rows}, {fit.profile_fp(p)})
        self.assertEqual({r[1] for r in rows}, {m.cand.rk_hash for m in matches})
        self.assertEqual({(r[2], r[4], r[5]) for r in rows},
                         {("fit", poller.SETTINGS.gemini_model, NOW)})

    def test_a_cached_verdict_makes_no_request(self):
        p, matches, gemini = person(), self.matches(), FakeGemini()
        first = self.check(p, matches, gemini)

        again = self.check(p, matches, gemini, now=NOW + HOUR)

        self.assertEqual(gemini.requests, 1)
        self.assertEqual([m.fit for m in again], [m.fit for m in first])

    def test_someone_else_with_the_same_labels_shares_the_cache(self):
        matches, gemini = self.matches(), FakeGemini()
        self.check(person(ALICE), matches, gemini)

        self.check(person(BOB), matches, gemini)

        self.assertEqual(gemini.requests, 1)

    def test_only_the_roles_not_yet_checked_are_sent(self):
        p, gemini = person(), FakeGemini()
        matches = self.matches(p)
        self.check(p, matches[:1], gemini)

        self.check(p, matches, gemini)

        self.assertEqual([s["title"] for s in gemini.payload()["postings"]],
                         [m.cand.title for m in matches[1:]])

    def test_a_profile_edit_checks_again(self):
        p, gemini = person(), FakeGemini()
        matches = self.matches(p)
        self.check(p, matches, gemini)
        edited = profile.with_changes(p, NOW, skills=("python",))

        self.check(edited, matches, gemini)

        self.assertEqual(gemini.requests, 2)
        self.assertEqual(gemini.payload()["profile"]["skills"], ["Python"])

    def test_at_most_fit_batch_go_in_one_request_and_the_rest_follow_unchecked(self):
        with with_settings(FIT_BATCH="2"):
            p, gemini = person(), FakeGemini()
            matches = self.matches(p)
            kept = self.check(p, matches, gemini)

        self.assertEqual((gemini.requests, len(gemini.payload()["postings"])), (1, 2))
        self.assertEqual([s["title"] for s in gemini.payload()["postings"]],
                         [m.cand.title for m in matches[:2]])
        self.assertEqual([m.cand.title for m in kept], [m.cand.title for m in matches])
        self.assertEqual([m.fit is None for m in kept], [False, False, True])

    def test_the_request_is_counted_for_the_day_with_its_tokens(self):
        self.check(person(), self.matches(), FakeGemini())

        self.assertEqual(fit.usage(self.db, fit.quota_day(NOW)), fit.Usage(1, 900, 80))
        self.assertEqual(fit.quota_day(NOW), "2026-10-05")

    def test_the_day_is_the_one_in_llm_day_tz(self):
        # 23:30 on 4 October in Los Angeles is already 5 October in UTC.
        late = MONDAY - 9 * HOUR - 30 * MINUTE
        with with_settings(LLM_DAY_TZ="America/Los_Angeles"):
            self.assertEqual(fit.quota_day(late), "2026-10-04")
        with with_settings(LLM_DAY_TZ="UTC"):
            self.assertEqual(fit.quota_day(late), "2026-10-05")

    def test_requests_are_paced_to_fit_rpm(self):
        with with_settings(FIT_RPM="1"):
            gemini = FakeGemini()
            self.check(person(ALICE), self.matches(), gemini)
            self.check(profile.with_changes(person(ALICE), NOW, skills=("python",)),
                       self.matches(), gemini)

        self.assertEqual(gemini.requests, 2)
        self.assertEqual(len(self.slept), 1)
        self.assertGreater(self.slept[0], 59)


# ------------------------------------------------------------------ never held back

class Unchecked(FitTest):
    def assert_unchecked(self, kept, matches):
        self.assertEqual([m.cand.rowid for m in kept], [m.cand.rowid for m in matches])
        self.assertEqual([m.fit for m in kept], [None] * len(matches))

    def test_opting_out_makes_no_request(self):
        p, gemini = profile.with_changes(person(), NOW, fit_check=False), FakeGemini()
        matches = self.matches(p)

        kept = self.check(p, matches, gemini)

        self.assertEqual(gemini.requests, 0)
        self.assert_unchecked(kept, matches)

    def test_no_key_makes_no_request(self):
        gemini, matches = FakeGemini(), self.matches()
        with mock.patch.object(poller, "SETTINGS", poller.configure({})):
            self.assertFalse(fit.available())
            kept = self.check(person(), matches, gemini)

        self.assertEqual(gemini.requests, 0)
        self.assert_unchecked(kept, matches)
        self.assertIsNone(fit.last_error)

    def test_a_malformed_answer_sends_them_unchecked(self):
        for answer in ("this is not json", '{"verdict": "fit"}', "[]",
                       '[{"i": 0, "verdict": "perhaps", "reason": "?"}]'):
            with self.subTest(answer=answer):
                p = profile.with_changes(person(), NOW, keywords=(f"k{len(answer)}",))
                matches = self.matches(p)

                kept = self.check(p, matches, FakeGemini(lambda payload: answer))

                self.assert_unchecked(kept, matches)
                self.assertEqual(fit.last_error, ("unparseable response", NOW))
                self.assertEqual(self.stored(), [])

    def test_an_api_error_sends_them_unchecked(self):
        matches = self.matches()

        kept = self.check(person(), matches, FakeGemini(lambda payload: Response(403)))

        self.assert_unchecked(kept, matches)
        self.assertEqual(fit.last_error, ("HTTP 403", NOW))
        self.assertIn("HTTP 403", self.log)
        self.assertNotIn(KEY, self.log)

    def test_a_spent_budget_sends_them_unchecked_and_makes_no_request(self):
        with with_settings(FIT_RPD="3"):
            self.db.execute("INSERT INTO fit_usage (day, requests) VALUES (?, 3)",
                            (fit.quota_day(NOW),))
            gemini, matches = FakeGemini(), self.matches()
            kept = self.check(person(), matches, gemini)

        self.assertEqual(gemini.requests, 0)
        self.assert_unchecked(kept, matches)
        self.assertEqual(fit.last_error, ("budget spent", NOW))

    def test_yesterdays_spending_does_not_count_today(self):
        with with_settings(FIT_RPD="3"):
            self.db.execute("INSERT INTO fit_usage (day, requests) VALUES ('2026-10-04', 3)")
            gemini = FakeGemini()
            self.check(person(), self.matches(), gemini)

        self.assertEqual(gemini.requests, 1)

    def test_after_a_failed_request_it_stops_asking_for_a_while(self):
        # A timeout can take GEMINI_MAX_ATTEMPTS x GEMINI_HTTP_TIMEOUT to give up; were
        # every due user to wait that long in turn, an outage would hold every alert.
        failing, healthy = FakeGemini(lambda payload: Response(503)), FakeGemini()
        matches = self.matches()
        self.check(person(ALICE), matches, failing)
        tried = failing.requests

        soon = self.check(profile.with_changes(person(), NOW, skills=("python",)), matches,
                          healthy, now=NOW + fit.COOL_OFF_S - 1)
        later = self.check(profile.with_changes(person(), NOW, skills=("sql",)), matches,
                           healthy, now=NOW + fit.COOL_OFF_S)

        self.assertEqual(tried, 3)                       # GEMINI_MAX_ATTEMPTS, then it gave up
        self.assertEqual(healthy.requests, 1)            # not while cooling off; after, yes
        self.assertEqual([m.fit for m in soon], [None] * 3)
        self.assertTrue(all(m.fit for m in later))
        self.assertEqual(fit.COOL_OFF_S, 600)

    def test_a_spent_budget_or_a_bad_answer_does_not_stop_the_next_request(self):
        for first in (FakeGemini(lambda payload: "garbage"),):
            self.check(person(ALICE), self.matches(), first)
        with with_settings(FIT_RPD="1"):
            self.db.execute("DELETE FROM fit_usage")
            gemini = FakeGemini()
            self.check(profile.with_changes(person(), NOW, skills=("python",)), self.matches(),
                       gemini, now=NOW + 1)
            self.check(profile.with_changes(person(), NOW, skills=("sql",)), self.matches(),
                       gemini, now=NOW + 2)                   # the day's one request is spent
            self.db.execute("DELETE FROM fit_usage")
            self.check(profile.with_changes(person(), NOW, skills=("go",)), self.matches(),
                       gemini, now=NOW + 3)

        self.assertEqual(gemini.requests, 2)

    def test_a_failure_keeps_the_verdicts_already_cached(self):
        p, matches = person(), self.matches()
        self.check(p, matches[:1], FakeGemini(lambda payload: [
            {"i": 0, "verdict": "no_fit", "reason": "Not your field"}]))

        kept = self.check(p, matches, FakeGemini(lambda payload: "garbage"))

        self.assertEqual([m.cand.title for m in kept], [m.cand.title for m in matches[1:]])
        self.assertEqual([m.fit for m in kept], [None, None])

    def test_the_checker_never_raises(self):
        broken = sqlite3.connect(":memory:")            # no fit tables at all
        self.addCleanup(broken.close)
        matches = self.matches()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            kept = asyncio.run(fit.checker(broken)(person(), matches, NOW))

        self.assert_unchecked(kept, matches)
        self.assertIn("fit check failed: OperationalError", stderr.getvalue())
        self.assertEqual(fit.last_error, ("OperationalError", NOW))


# ------------------------------------------------------------------ browsing and housekeeping

class Browsing(FitTest):
    def test_matches_show_cached_verdicts_and_never_ask(self):
        p, matches = person(), self.matches()
        self.check(p, matches[:2], FakeGemini(lambda payload: [
            {"i": 0, "verdict": "no_fit", "reason": "Not your field"},
            {"i": 1, "verdict": "fit", "reason": "Your field"}]))

        with mock.patch.object(fit, "check", side_effect=AssertionError("no request")):
            shown = fit.with_cached(self.db, p, matches)

        # Nothing dropped and nothing moved: browsing shows every match, as ranked.
        self.assertEqual([m.cand.rowid for m in shown], [m.cand.rowid for m in matches])
        self.assertEqual([m.fit for m in shown], [("no_fit", "Not your field"),
                                                  ("fit", "Your field"), None])

    def test_an_opted_out_profile_is_shown_no_verdict(self):
        p, matches = person(), self.matches()
        self.check(p, matches, FakeGemini())

        shown = fit.with_cached(self.db, profile.with_changes(p, NOW, fit_check=False), matches)

        self.assertEqual([m.fit for m in shown], [None] * 3)


# ------------------------------------------------------------------ delivery

class AlertsAreChecked(DeliveryTest):
    """A delivery tick with the check wired in: Gemini decides what an alert carries,
    never whether one goes."""

    def setUp(self):
        super().setUp()
        fit.init_db(self.db)
        patch = mock.patch.object(poller, "SETTINGS", settings(DIAYN_TZ="America/Los_Angeles"))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(setattr, fit, "last_error", None)
        self.addCleanup(setattr, fit, "quiet_until", 0.0)
        self.gemini = FakeGemini()

    def checked_tick(self, now, gemini=None):
        gemini = gemini or self.gemini

        async def check_fit(p, matches, at):
            return await fit.check(self.db, p, matches, at, session=gemini, pace=fit.Pace())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            return asyncio.run(delivery.run_tick(
                self.db, load_window=self.load_window, send_dm=self.outbox, now=now,
                companies_watched=COMPANIES, allowed=self.allowed, check_fit=check_fit))

    def post_three(self):
        self.post(*(posting(title, MONDAY + (n + 1) * MINUTE, company=f"Firm {n}")
                    for n, title in enumerate(TITLES)))

    def test_no_fit_is_never_sent_and_the_cursor_still_moves_past_it(self):
        self.enrol(alerts="hourly")
        self.post_three()
        everything_no_fit = FakeGemini(lambda payload: [
            {"i": p["i"], "verdict": "no_fit", "reason": "Not a role for you"}
            for p in payload["postings"]])

        report = self.checked_tick(NOW, everything_no_fit)

        self.assertEqual((report.due, report.sent, report.empty), (1, 0, 1))
        self.assertEqual(self.outbox.calls, [])
        self.assertEqual(store.load(self.db, ALICE).cursor, delivery.horizon(NOW))
        self.assertEqual(self.seen(ALICE), {})        # nothing was sent, so nothing is recorded

    def test_fit_goes_first_then_unsure_each_with_its_reason_line(self):
        self.enrol(alerts="hourly")
        self.post_three()
        table = {TITLES[0]: ("unsure", "Backend skills are not on your profile"),
                 TITLES[1]: ("no_fit", "Needs a clearance you did not list"),
                 TITLES[2]: ("fit", "Frontend work fits your software field")}

        report = self.checked_tick(NOW, FakeGemini(verdicts_by_title(table)))

        (msg,) = self.outbox.to(ALICE)
        self.assertEqual(report.sent, 1)
        self.assertNotIn(TITLES[1], msg.text)
        self.assertLess(msg.text.index(TITLES[2]), msg.text.index(TITLES[0]))
        self.assertIn("Gemini: fits · Frontend work fits your software field", msg.text)
        self.assertIn("Gemini: not sure · Backend skills are not on your profile", msg.text)
        self.assertTrue(msg.text.startswith("**2 new roles for you**"))
        sent = {c.rk_hash for c in self.window if c.title != TITLES[1]}
        self.assertTrue(sent <= set(self.seen(ALICE)))
        dropped = next(c for c in self.window if c.title == TITLES[1])
        self.assertNotIn(dropped.rk_hash, self.seen(ALICE))

    def test_a_malformed_answer_sends_the_rule_based_matches_unchecked(self):
        self.enrol(alerts="hourly")
        self.post_three()

        report = self.checked_tick(NOW, FakeGemini(lambda payload: "no verdicts here"))

        (msg,) = self.outbox.to(ALICE)
        self.assertEqual(report.sent, 1)
        self.assertTrue(all(title in msg.text for title in TITLES))
        self.assertNotIn("Gemini", msg.text)
        self.assertEqual(fit.last_error, ("unparseable response", NOW))

    def test_a_spent_budget_sends_them_unchecked_and_asks_nothing(self):
        self.enrol(alerts="hourly")
        self.post_three()
        self.db.execute("INSERT INTO fit_usage (day, requests) VALUES (?, ?)",
                        (fit.quota_day(NOW), poller.SETTINGS.fit_rpd))

        report = self.checked_tick(NOW)

        self.assertEqual((report.sent, self.gemini.requests), (1, 0))
        self.assertNotIn("Gemini", self.outbox.to(ALICE)[0].text)

    def test_without_a_key_the_alert_is_the_one_sent_without_the_check(self):
        self.enrol(alerts="hourly")
        self.post_three()
        with mock.patch.object(poller, "SETTINGS",
                               poller.configure({"DIAYN_TZ": "America/Los_Angeles"})):
            self.checked_tick(NOW)
            self.enrol(BOB, alerts="hourly")
            self.tick(NOW)                            # BOB: no check wired in at all

        self.assertEqual(self.gemini.requests, 0)
        self.assertEqual(self.outbox.to(ALICE)[0].text, self.outbox.to(BOB)[0].text)

    def test_an_opted_out_user_is_sent_unchecked_and_asks_nothing(self):
        p = self.enrol(alerts="hourly")
        store.save(self.db, profile.with_changes(p, MONDAY, fit_check=False), now=MONDAY)
        self.post_three()

        report = self.checked_tick(NOW)

        self.assertEqual((report.sent, self.gemini.requests), (1, 0))
        self.assertNotIn("Gemini", self.outbox.to(ALICE)[0].text)

    def test_a_user_deleted_while_being_checked_is_sent_nothing(self):
        self.enrol(alerts="hourly")
        self.post_three()

        def deleting(payload):
            store.delete_user(self.db, ALICE)
            return all_fit(payload)

        report = self.checked_tick(NOW, FakeGemini(deleting))

        self.assertEqual((report.sent, self.outbox.calls), (0, []))
        self.assertIsNone(store.load(self.db, ALICE))

    def test_access_revoked_while_being_checked_stops_the_dm(self):
        self.enrol(alerts="hourly")
        self.post_three()

        def revoking(payload):
            self.revoked.add(ALICE)
            return all_fit(payload)

        report = self.checked_tick(NOW, FakeGemini(revoking))

        self.assertEqual((report.sent, self.outbox.calls), (0, []))

    def test_nothing_new_asks_nothing(self):
        self.enrol(alerts="hourly")

        report = self.checked_tick(NOW)

        self.assertEqual((report.empty, self.gemini.requests), (1, 0))

    def test_a_role_checked_once_is_not_asked_about_again(self):
        self.enrol(ALICE, alerts="hourly")
        self.enrol(BOB, alerts="hourly")              # the same labels as ALICE
        self.post_three()

        self.checked_tick(NOW)

        self.assertEqual(self.gemini.requests, 1)
        self.assertEqual(len(self.outbox.calls), 2)
        self.assertEqual(self.outbox.to(ALICE)[0].text, self.outbox.to(BOB)[0].text)


class Housekeeping(FitTest):
    def test_verdicts_older_than_45_days_are_pruned(self):
        p, matches = person(), self.matches()
        self.check(p, matches[:1], FakeGemini(), now=NOW - 46 * DAY)
        self.check(p, matches[1:], FakeGemini(), now=NOW - 44 * DAY)

        pruned = fit.prune(self.db, NOW)

        self.assertEqual(pruned, 1)
        self.assertEqual(len(self.stored()), 2)
        self.assertEqual(fit.RETAIN_S, store.SENT_RETAIN_S)

    def test_init_db_is_safe_to_run_again_and_names_no_user(self):
        fit.init_db(self.db)
        for table in ("fit_verdicts", "fit_usage"):
            with self.subTest(table=table):
                columns = [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]
                self.assertNotIn("user_id", columns)
        self.assertEqual([r[1] for r in self.db.execute("PRAGMA table_info(fit_verdicts)")],
                         ["profile_fp", "role_hash", "verdict", "reason", "model", "at"])
        self.assertEqual([r[1] for r in self.db.execute("PRAGMA table_info(fit_usage)")],
                         ["day", "requests", "prompt_tokens", "output_tokens"])


if __name__ == "__main__":
    unittest.main()
