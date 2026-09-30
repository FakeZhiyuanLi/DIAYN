"""
Every sentence the internship finder says, and the character budgets it says
them in.

    python3 -m unittest discover -s tests      # no install needed

`intern_text` is pure string building, so this runs under bare `python3`. Two
kinds of promise are pinned here:

  * **Budgets.** Discord refuses a message over 2,000 characters, and a refused
    DM or card is a reply the user never sees, with nothing in any log. Each
    renderer is fed the worst case the store allows — every list at its cap,
    every name at its longest — and must stay inside its limit.
  * **Copy.** The fixed sentences are the spec's (section 1.2) character for
    character, and text from outside (a company, a title, a filename) can
    never format, mention or break out of the line it is put on.

Hours and dates are in DIAYN_TZ, the scraper's `SETTINGS.tz`, named wherever
an hour is shown. This module runs with it set to America/Los_Angeles, except
where a test sets another.
"""

import dataclasses
import itertools
import re
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import intern_match
import intern_places
import intern_profile
import intern_store
import intern_text as text
import intern_vocab
import internship_poller as poller
import resume_lexicon
import resume_parse
from club_wording import CLUB

NOW = 1_790_000_000.0                  # 2026-09-21, a Monday, in Pacific time
DAY = 86400
PACIFIC = ZoneInfo("America/Los_Angeles")
_IDS = itertools.count(1)
_IN_PACIFIC = mock.patch.object(poller, "SETTINGS",
                                poller.configure({"DIAYN_TZ": "America/Los_Angeles"}))


def setUpModule():
    _IN_PACIFIC.start()


def tearDownModule():
    _IN_PACIFIC.stop()


def in_zone(name: str):
    """DIAYN_TZ set to `name` for the length of a `with` block."""
    return mock.patch.object(poller, "SETTINGS", poller.configure({"DIAYN_TZ": name}))

LONGEST_SKILLS = tuple(s.id for s in sorted(resume_lexicon.SKILLS, key=lambda s: -len(s.label))[:40])
LONGEST_MAJORS = tuple(m.id for m in sorted(resume_lexicon.MAJORS, key=lambda m: -len(m.label))[:5])
LONGEST_FIELDS = tuple(f for f, _ in sorted(intern_vocab.FIELDS, key=lambda fl: -len(fl[1]))[:11])
LONGEST_STATES = tuple(f"st:{code}" for code, _ in
                       sorted(intern_places.US_STATES.items(), key=lambda kv: -len(kv[1]))[:10])
KEYWORDS = tuple(f"{chr(97 + n)}{'x' * 28}{n}" for n in range(10))          # 30 characters each
COMPANIES = tuple(f"company{n:02d}{'z' * 21}" for n in range(30))          # 30 characters each


def person(**fields):
    base = intern_profile.new_profile(123, NOW, source="manual", cursor=NOW - 600)
    return dataclasses.replace(base, **fields)


def worst_profile(**extra):
    """Every list at its cap, every label at its longest (3.1)."""
    return person(**{
        "source": "migrated", "majors": LONGEST_MAJORS[:3], "minors": LONGEST_MAJORS[3:5],
        "degree": "pharmd", "grad_year": 2028, "grad_month": 12, "skills": LONGEST_SKILLS,
        "keywords": KEYWORDS, "fields": intern_vocab.LEGACY_ALL_TECH,
        "levels": intern_vocab.LEVEL_IDS, "locations": intern_vocab.LOCATION_PRESET_IDS + LONGEST_STATES,
        "terms": ("Winter 2027", "Spring 2027", "Summer 2027", "Fall 2027"),
        "companies_only": COMPANIES[:20], "companies_hidden": COMPANIES[:30],
        "alerts": "weekly", "alert_hour": 23, "min_score": 75, "paused_until": NOW + 3 * DAY,
        "dm_failures": 3, **extra})


def a_match(title="Mechanical Engineer Intern", company="Acme", url="https://example.com/1", *,
            location="Irvine, CA", place="Irvine, CA", why=("Mechanical engineering (your field)",),
            band="Good match", score=66, more=0, age=2.0):
    rowid = next(_IDS)
    seen = NOW - age * DAY
    cand = intern_match.tag_rows([(rowid, "greenhouse", f"x{rowid}", company, title, location,
                                   url, seen, seen)])[0]
    return intern_match.Match(cand=cand, score=score, band=band, why=why, place=place,
                              group_key=cand.rk, ledger=(cand.rk_hash, cand.ck_hash), more=more)


def worst_match(n=0):
    return a_match(title=f"*_~`|> {n} " + "T" * 400, company="C*_|" * 60,
                   url="https://example.com/" + "u" * 270, place="P" * 200,
                   why=tuple("W" * 200 for _ in range(4)), more=999, band="Strong match")


class Escaping(unittest.TestCase):
    def test_safe_inline_escapes_markdown_and_flattens_the_line(self):
        self.assertEqual(text.safe_inline("a*b_c~d`e|f>g\\h", 60), "a\\*b\\_c\\~d\\`e\\|f\\>g\\\\h")
        self.assertEqual(text.safe_inline("one\ntwo\u200b\x07  three", 60), "one two three")

    def test_safe_inline_never_passes_its_limit(self):
        for limit in (1, 5, 60, 150):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(text.safe_inline("*" * 500, limit)), limit)
                self.assertLessEqual(len(text.safe_inline("x" * 500, limit)), limit)

    def test_safe_url(self):
        self.assertEqual(text.safe_url("https://example.com/a?b=1"), "https://example.com/a?b=1")
        for bad in ("javascript:alert(1)", "https://x.com/" + "a" * 400, "https://a b", None,
                    "https://x.com/<script>", "ftp://example.com"):
            with self.subTest(url=bad):
                self.assertIsNone(text.safe_url(bad))

    def test_safe_filename_escapes_and_cuts_to_forty(self):
        name = text.safe_filename("my_*resume*_" + "x" * 80 + ".pdf")
        self.assertTrue(name.startswith("my\\_\\*resume\\*\\_"))
        self.assertLessEqual(len(name), 40)
        self.assertNotIn("`", text.safe_filename("evil`name.pdf"))


class Disclosure(unittest.TestCase):
    def test_it_says_what_it_must(self):
        disclosure = text.disclosure_text()
        for phrase in ("not sent to any AI service",
                       "Whoever runs this bot can read its database, users.db",
                       "on the computer this bot runs on",
                       "Discord keeps its own copy", "/internships delete"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, disclosure)
        self.assertIsNone(re.search(r"(sent|send|sends) to (gemini|openai|google|an ai)",
                                    disclosure, re.I))

    def test_the_start_card_and_consent_screen_reuse_it(self):
        for pdf_ok in (True, False):
            card = text.start_card(pdf_ok=pdf_ok)
            self.assertIn(text.disclosure_text(), card)
            self.assertIn("Just browsing? `/internships recent` needs no profile.", card)
        self.assertIn("PDF, Word (.docx) or .txt, up to 2 MB.", text.start_card(pdf_ok=True))
        self.assertIn("This bot can't read PDFs yet", text.start_card(pdf_ok=False))
        self.assertEqual(text.consent_text("cv_final.pdf"),
                         "**Before I read `cv\\_final.pdf`**\n" + text.disclosure_text())


class UploadErrors(unittest.TestCase):
    def test_every_reason_has_copy(self):
        for reason in resume_parse.REASONS:
            with self.subTest(reason=reason):
                self.assertTrue(text.upload_error(reason, size_mb=3.1, kind_label="PDF", minutes=12))

    def test_failures_after_the_metadata_check_say_nothing_was_kept(self):
        after = {"bad_magic", "encrypted", "too_many_pages", "no_text", "corrupt", "zip_bomb",
                 "xml_entity", "timeout", "worker_failed", "busy"}
        for reason in resume_parse.REASONS:
            with self.subTest(reason=reason):
                ends = text.upload_error(reason, kind_label="PDF").endswith(" Nothing was kept.")
                self.assertEqual(ends, reason in after)

    def test_placeholders_are_filled(self):
        self.assertEqual(text.upload_error("too_big", size_mb=3.4),
                         "That file is 3.4 MB and I take resumes up to 2 MB. Exporting it again "
                         "as a PDF without images usually shrinks it.")
        self.assertEqual(text.upload_error("rate_limited", minutes=12),
                         "You've uploaded 5 resumes in the last hour. Try again in 12 minutes.")
        self.assertIn("a real Word file", text.upload_error("bad_magic", kind_label="Word"))


class Card(unittest.TestCase):
    COVERAGE = intern_match.Coverage(total=9999, strong=9999, us_extra=9999,
                                     thin=tuple((f, 0) for f in intern_vocab.LEGACY_ALL_TECH),
                                     hourly_hint=9999)

    def test_the_worst_case_profile_fits_in_both_modes(self):
        p = worst_profile()
        header = text.draft_header(p, found_field=True, replacing=person(skills=("excel",)))
        feedback = text.feedback_line(9998, 9999) + " " + "I couldn't read 'x' as a date. " * 5
        for mode in ("draft", "saved"):
            with self.subTest(mode=mode):
                card = text.card_text(p, mode=mode, coverage=self.COVERAGE, feedback=feedback,
                                      evidence="Read from your Education section: " + "E" * 150,
                                      header=header, notices=("N" * 200,) * 3)
                self.assertLessEqual(len(card), text.CARD_MAX)
                for line in ("**Fields:**", "**Where:**", "**Alerts:**", "**Companies:**"):
                    self.assertIn(line, card)
                self.assertRegex(card, r"\(\+\d+ more\)")

    def test_the_example_card_reads_as_the_spec_shows(self):
        p = person(majors=("mechanical_engineering",), minors=("mathematics",), degree="bachelor",
                   grad_year=2028, grad_month=6, fields=("mechanical", "aerospace"),
                   locations=("us", "oc", "unlisted"), terms=("Summer 2027",),
                   companies_only=("boeing", "spacex"), companies_hidden=("cvshealth",))
        coverage = intern_match.Coverage(total=9, strong=3, us_extra=None, thin=(), hourly_hint=0)
        card = text.card_text(p, mode="saved", coverage=coverage,
                              companies={"boeing": "Boeing", "spacex": "SpaceX", "cvshealth": "CVS Health"})
        self.assertIn("**Studying:** Mechanical Engineering (Bachelor's) · minor Mathematics", card)
        self.assertIn("**Graduating:** June 2028", card)
        self.assertIn("**Fields:** Mechanical engineering · Aerospace engineering", card)
        self.assertIn("**Looking for:** Internships · Co-ops & placements", card)
        self.assertIn("**Where:** Anywhere in the US · Orange County / Irvine · roles that don't "
                      "list a location", card)
        self.assertIn("**Companies:** only Boeing, SpaceX · hiding CVS Health", card)
        self.assertIn("**Alerts:** Daily at 9am Los Angeles time · good and strong matches", card)
        self.assertIn("**Last 30 days:** 9 roles fit this (3 strong).", card)

    def test_coverage_notices(self):
        p = person(fields=("pharmacy",), locations=("oc", "unlisted"), dm_failures=3)
        coverage = intern_match.Coverage(total=0, strong=0, us_extra=12, thin=(("pharmacy", 1),),
                                         hourly_hint=17)
        card = text.card_text(p, mode="saved", coverage=coverage)
        self.assertIn("**Last 30 days:** nothing fit this yet. Anywhere in the US would add 12.", card)
        self.assertIn("*Heads up: the companies I watch posted only 1 Pharmacy role for students "
                      "in the last 30 days. They'll reach you when they appear.*", card)
        self.assertIn("*Tip: 17 part-time & hourly roles (like Pharmacy Technician) fit your "
                      "fields. Add **Part-time & hourly** under Looking for if you want them.*", card)
        self.assertTrue(card.startswith(text.dm_blocked_banner()))
        self.assertNotIn("Last 30 days", text.card_text(p, mode="saved", coverage=None))

    def test_headers_evidence_and_deltas(self):
        draft = {"majors": ["mechanical_engineering"], "degree": "bachelor", "grad_year": 2027,
                 "grad_month": 6, "evidence": {"study": "education", "grad": "education"}}
        self.assertEqual(text.evidence_line(draft), "Read from your Education section: Bachelor's "
                                                    "· Mechanical Engineering · June 2027")
        self.assertIsNone(text.evidence_line({"evidence": {"study": None, "grad": None}}))
        manual = person()
        self.assertEqual(text.draft_header(manual, found_field=False, replacing=None),
                         "**Let's set up your profile.** Pick at least one field below, then press **Save**.")
        found = person(source="resume")
        self.assertIn("Nothing is kept until you do.", text.draft_header(found, found_field=True, replacing=None))
        self.assertIn("I couldn't find your major; pick your fields below.",
                      text.draft_header(found, found_field=False, replacing=None))
        old = person(skills=("excel",), majors=("mechanical_engineering",))
        new = person(skills=("ansys", "python"), majors=("aerospace_engineering",))
        self.assertEqual(text.reparse_delta(old, new),
                         "+ ANSYS, Python · − Excel · major now Aerospace Engineering.")

    def test_feedback_line(self):
        self.assertEqual(text.feedback_line(6, 9), "Saved. **9 roles** match now (was 6).")
        self.assertEqual(text.feedback_line(9, 9), "Saved.")
        self.assertEqual(text.feedback_line(None, 9), "Saved.")


class MatchBlocks(unittest.TestCase):
    def test_external_text_is_escaped_and_bounded(self):
        m = a_match(title="*Bold* _it_ " + "t" * 300, company="Ac*me", url="https://x.com/" + "u" * 400)
        block = text.match_block(m, NOW)
        self.assertTrue(block.startswith("**Ac\\*me** — \\*Bold\\* \\_it\\_ "))
        self.assertNotIn("<https", block)
        self.assertLess(len(block), 1000)

    def test_the_spec_layout(self):
        m = a_match(title="Mechanical Engineering Intern (Summer 2027)", url="https://example.com/j",
                    why=("Mechanical engineering (your field)", "Irvine, CA"), more=3)
        self.assertEqual(text.match_block(m, NOW).split("\n"), [
            "**Acme** — Mechanical Engineering Intern (Summer 2027)",
            "Irvine, CA · Internship · Summer 2027 · posted 2d ago (+3 more locations)",
            "Good match · Why: Mechanical engineering (your field) · Irvine, CA",
            "<https://example.com/j>"])
        self.assertNotIn("Why", text.match_block(m, NOW, with_why=False))

    def test_the_worst_block_stays_under_a_thousand(self):
        self.assertLess(len(text.match_block(worst_match(), NOW)), 1000)


class Alerts(unittest.TestCase):
    def long(self, n):
        return a_match(title=f"Mechanical Design Engineering Intern, Propulsion Systems {n:02d}",
                       company="Northrop Grumman", url=f"https://careers.example.com/job/{n:04d}",
                       why=("Mechanical engineering (your field)", "Irvine, CA"))

    def test_twelve_long_matches_show_five_and_count_the_rest(self):
        body, shown = text.format_alert([self.long(n) for n in range(12)], NOW, cadence="daily",
                                        intro=False, catch_up=False, expiry_note=None, with_controls=True)
        self.assertLessEqual(len(body), text.ALERT_MAX)
        self.assertEqual(len(shown), 5)
        self.assertTrue(body.startswith("**12 new roles for you** · daily digest"))
        self.assertIn("...and 7 more: `/internships matches`", body)
        self.assertIn("Not quite right? Hide a role below, or change what you get with "
                      "`/internships profile`.", body)

    def test_worst_case_listings_are_dropped_until_the_message_fits(self):
        body, shown = text.format_alert([worst_match(n) for n in range(12)], NOW, cadence="weekly",
                                        intro=True, catch_up=False,
                                        expiry_note=text.expiry_note(NOW + 14 * DAY), with_controls=True)
        self.assertLessEqual(len(body), text.ALERT_MAX)
        self.assertGreaterEqual(len(shown), 1)
        self.assertIn(f"...and {12 - len(shown)} more", body)
        self.assertTrue(body.startswith(text.migrated_intro()))

    def test_catch_up_header(self):
        body, shown = text.format_alert([self.long(n) for n in range(12)], NOW, cadence="daily",
                                        intro=False, catch_up=True, expiry_note=None, with_controls=True)
        self.assertTrue(body.startswith(f"**Welcome back: 12 new roles while you were paused.** "
                                        f"Here are the best {len(shown)}."))

    def test_hide_options(self):
        shown = [self.long(n) for n in range(5)] + [a_match(company="SpaceX " + "x" * 200)]
        options = text.hide_options(shown)
        values = [v for v, _ in options]
        self.assertLessEqual(len(options), 25)
        self.assertEqual(len(values), len(set(values)))
        self.assertTrue(all(len(label) <= 100 and len(v) <= 100 for v, label in options))
        self.assertIn((f"r:{shown[0].cand.rk_hash}:{shown[0].cand.ck_hash}",
                       f"Hide this role: Northrop Grumman — {shown[0].cand.title}"), options)
        self.assertIn(("c:northropgrumman", "Hide everything from Northrop Grumman"), options)


class Lists(unittest.TestCase):
    def test_matches_header_and_packing(self):
        self.assertEqual(text.matches_header(9, days=14, sort="best"),
                         "**9 roles for you** (last 14 days · best first)")
        self.assertEqual(text.matches_header(40, days=7, sort="newest"),
                         "**40 roles for you** (last 7 days · newest first) — showing 15")
        chunks = text.matches_messages([worst_match(n) for n in range(15)], NOW, header="**h**")
        self.assertTrue(all(len(c) <= text.ALERT_MAX for c in chunks))
        self.assertTrue(chunks[0].startswith("**h**"))

    def test_browse_copy(self):
        self.assertEqual(text.browse_header(3, field="mechanical", level_label="Internships & co-ops",
                                            where_label="Anywhere in the US", days=7),
                         "**3 recent roles** in Mechanical engineering (Internships & co-ops · "
                         "Anywhere in the US · last 7 days)")
        self.assertEqual(text.browse_empty(days=7, companies=412),
                         "Nothing on record for that in the last 7 days. The tracker checks 412 "
                         "companies every 15 minutes.")

    def test_saved_followup(self):
        chunks = text.saved_followup([worst_match(n) for n in range(8)], NOW, person())
        self.assertTrue(chunks[0].startswith("**Saved.** Your best matches from the last 14 days:"))
        self.assertTrue(chunks[-1].endswith("New matches will reach you by DM every day at 9am "
                                            "Los Angeles time. `/internships matches` shows everything "
                                            "any time."))
        self.assertTrue(all(len(c) <= text.ALERT_MAX for c in chunks))
        self.assertEqual(sum(c.count("\n<https://example.com/") for c in chunks), 5)


class EmptyStates(unittest.TestCase):
    RELAX = (intern_match.Relaxation("us", "Anywhere in the US", 12, {}),
             intern_match.Relaxation("adjacent", "Add " + "L" * 200, 3, {}))

    def test_empty_state(self):
        body = text.empty_state(person(fields=("biology_lab",), locations=("oc",)), self.RELAX,
                                pool=17548, companies=412, days=14)
        self.assertIn("**No matches in the last 14 days for Biology & lab research.**", body)
        self.assertIn("I checked 17,548 postings from 412 companies; none were Biology & lab "
                      "research roles at your level in the places you picked.", body)
        self.assertIn("That's about which companies I watch, not about you.", body)
        self.assertIn("- **Anywhere in the US** would find 12.", body)
        self.assertIn("Ask whoever runs this bot to add it.", body)
        self.assertTrue(all(len(text.relax_button_label(r)) <= 80 for r in self.RELAX))
        self.assertEqual(text.relax_button_label(self.RELAX[0]), "Anywhere in the US (+12)")

    def test_quiet_note_and_expiry(self):
        note = text.quiet_note(person(fields=("mechanical",)), self.RELAX[:1], companies=412)
        self.assertTrue(note.startswith("**Still watching, nothing new for you yet.** In the last 14 "
                                        "days the 412 companies I watch posted no new Mechanical "
                                        "engineering roles at your level."))
        self.assertIn("- **Anywhere in the US** in `/internships profile` would add 12.", note)
        self.assertTrue(note.endswith("`/internships ping` turns these DMs off."))
        on = datetime(2026, 10, 5, 12, tzinfo=PACIFIC).timestamp()
        self.assertEqual(text.expiry_note(on), "I'll delete your internship profile on Oct 5 because "
                         "it hasn't been used in a year. Run any `/internships` command before then "
                         "to keep it.")


def stored_rows(p):
    return {**{column: getattr(p, column) for column in intern_store.STORED_COLUMNS},
            "sent_count": 9999, "hidden_count": 9999}


class DeleteScreen(unittest.TestCase):
    HEADER = "**This is everything the internship finder holds about you:**"
    QUESTION = "Delete all of it? Alerts stop and this can't be undone."

    def worst(self):
        stamps = dict.fromkeys(("paused_until", "last_run_at", "last_sent_at", "last_quiet_at",
                                "left_at", "expiry_warned_at"), NOW)
        return worst_profile(fields=LONGEST_FIELDS, companies_only=COMPANIES[:20],
                             fields_locked=True, levels_locked=True, intro_pending=True, **stamps)

    def test_every_column_is_listed(self):
        lines = text.privacy_text(stored_rows(person()))
        for description in intern_store.STORED_COLUMNS.values():
            with self.subTest(description=description):
                self.assertEqual(sum(line.startswith(f"**{description}:**") for line in lines), 1)

    def test_the_worst_case_is_split_whole_and_nothing_is_truncated(self):
        p = self.worst()
        chunks = text.delete_confirm(text.privacy_text(stored_rows(p)))
        joined = "\n".join(chunks)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= text.ALERT_MAX for c in chunks))
        self.assertTrue(chunks[0].startswith(self.HEADER))
        self.assertTrue(chunks[-1].endswith(self.QUESTION))
        for description in intern_store.STORED_COLUMNS.values():
            self.assertEqual(joined.count(f"**{description}:**"), 1)
        labels = ([resume_lexicon.SKILL_BY_ID[s].label for s in p.skills]
                  + [intern_vocab.FIELD_LABELS[f] for f in p.fields] + list(p.keywords)
                  + list(p.companies_hidden) + [intern_places.US_STATES[s[3:]] for s in LONGEST_STATES])
        for label in labels:
            self.assertIn(label.lower(), joined.lower())
        self.assertNotRegex(joined, r"\(\+\d+ more\)")
        self.assertIn("**Roles I've alerted you about:** 9999 (kept 45 days)", joined)
        self.assertIn("**Roles you hid:** 9999 (kept 90 days)", joined)

    def test_hours_and_times_name_their_zone(self):
        p = person(alert_hour=17, last_run_at=datetime(2026, 9, 21, 8, 5, tzinfo=PACIFIC).timestamp())

        lines = text.privacy_text(stored_rows(p))

        self.assertIn(f"**{intern_store.STORED_COLUMNS['alert_hour']}:** 5pm Los Angeles time", lines)
        self.assertIn(f"**{intern_store.STORED_COLUMNS['last_run_at']}:** 2026-09-21 08:05 "
                      "Los Angeles time", lines)

    def test_a_fresh_profile_fits_the_same_bounds(self):
        chunks = text.delete_confirm(text.privacy_text(stored_rows(person())))
        self.assertIn(len(chunks), (1, 2))
        self.assertTrue(all(len(c) <= text.ALERT_MAX for c in chunks))
        self.assertTrue(chunks[-1].endswith(self.QUESTION))


class TheZoneIsDiaynTz(unittest.TestCase):
    """Every hour shown is in DIAYN_TZ and says so; America/Los_Angeles is only this module's."""

    def test_in_utc_an_hour_says_utc(self):
        with in_zone("UTC"):
            self.assertEqual(text.cadence_phrase("daily", 9), "every day at 9am UTC")
            self.assertEqual(text.cadence_phrase("weekly", 17), "every Monday at 5pm UTC")
            card = text.card_text(person(), mode="saved", coverage=None)

        self.assertIn("**Alerts:** Daily at 9am UTC", card)

    def test_a_stored_time_is_shown_in_the_zone_it_names(self):
        at = datetime(2026, 9, 21, 8, 5, tzinfo=PACIFIC).timestamp()     # 15:05 UTC
        with in_zone("UTC"):
            lines = text.privacy_text(stored_rows(person(last_run_at=at)))

        self.assertIn(f"**{intern_store.STORED_COLUMNS['last_run_at']}:** 2026-09-21 15:05 UTC",
                      lines)

    def test_a_date_is_the_date_in_diayn_tz(self):
        until = datetime(2026, 9, 28, 23, 30, tzinfo=PACIFIC).timestamp()   # Sep 29 in UTC
        with in_zone("UTC"):
            reply = text.alert_reply("paused", until=until)

        self.assertTrue(reply.startswith("Paused until Sep 29."))


class NoClubWording(unittest.TestCase):
    """DIAYN is run by whoever hosts it, for whoever they let in: nothing it says assumes a
    club, its officers, its server or its other bots' commands."""

    def every_reply(self) -> list:
        p = person()
        return [text.disclosure_text(), text.start_card(pdf_ok=True), text.start_card(pdf_ok=False),
                text.upload_modal_note(), text.upload_error("no_pdf_support"),
                text.welcome_dm(p, now=NOW), text.migrated_intro(),
                text.empty_state(p, (), pool=10, companies=3, days=14, now=NOW),
                text.disabled_finder("ContractError: x"), text.owner_only(),
                *text.help_text(pdf_ok=False, companies=412),
                *text.privacy_text(stored_rows(p)),
                *text.debug_lines({}, {}, {}, pdf_ok=False, migrated=None, now=NOW)]

    def test_nothing_the_finder_says_names_a_club(self):
        for body in self.every_reply():
            with self.subTest(body=body[:60]):
                self.assertIsNone(CLUB.search(body))

    def test_help_that_needs_the_host_asks_whoever_runs_this_bot(self):
        self.assertIn("whoever runs this bot can install `pypdf`", text.start_card(pdf_ok=False))
        self.assertIn("whoever runs this bot can install `pypdf`",
                      text.upload_error("no_pdf_support"))
        self.assertTrue(text.disabled_finder("ContractError: x").endswith(
            "Whoever runs this bot can check its log."))
        self.assertEqual(text.owner_only(), "That one is only for whoever runs this bot.")

    def test_the_welcome_says_which_bot_this_is(self):
        self.assertTrue(text.welcome_dm(person(), now=NOW).startswith(
            "Hi! I'm DIAYN, an internship finder."))

    def test_the_migrated_intro_says_which_bot_this_is_and_why_it_writes(self):
        intro = text.migrated_intro()

        self.assertTrue(intro.startswith("**Hi, this is DIAYN, an internship finder bot.**"))
        self.assertIn("You were subscribed to internship alerts from another bot", intro)
        self.assertIn("a server you share with this bot", intro)
        self.assertIn("I copied your filters", intro)
        for command in ("/internships profile", "/internships ping", "/internships delete"):
            self.assertIn(command, intro)

    def test_the_old_tracker_s_button_reply_is_gone(self):
        # A button answers to the bot that posted it, so DIAYN can never be
        # sent a click on the old tracker's digest button.
        self.assertFalse(hasattr(text, "legacy_digest"))
        self.assertFalse(hasattr(text, "officer_only"))


class FixedCopy(unittest.TestCase):
    def test_short_replies_are_the_spec_s(self):
        cases = {
            text.upload_empty(): "Attach a file or paste some text, then submit again.",
            text.cancelled_consent(): "Cancelled. I never opened the file.",
            text.cancelled_draft(): "Cancelled. Nothing was kept.",
            text.dm_retry_ok(): "It worked, check your DMs.",
            text.nothing_held(): "I don't hold anything about you.",
            text.matches_no_profile(): "Meanwhile, `/internships recent` lists every field.",
            text.not_yours(): "That isn't yours.",
            text.generic_failure(): "Something went wrong on my side. Try again in a minute.",
            text.deleted_text(): "**Done. It's all gone.** Messages I already sent stay in your DMs "
                                 "until you delete them, and Discord keeps its own copy of files "
                                 "you uploaded.",
        }
        for got, want in cases.items():
            with self.subTest(want=want):
                self.assertEqual(got, want)

    def test_alert_replies(self):
        self.assertEqual(text.alert_reply("hidden_role"),
                         "Hidden. You won't be alerted about that role again.")
        self.assertEqual(text.alert_reply("hidden_company"),
                         "Hidden. Nothing from that company will reach you. `/internships profile` "
                         "-> More filters lists hidden companies.")
        self.assertEqual(text.alert_reply("stopped"), "Alerts are off. Your profile is saved; "
                                                      "`/internships ping` turns them back on.")
        self.assertEqual(text.alert_reply("resumed"), "Alerts are back on.")
        self.assertEqual(text.alert_reply("no_profile"), "I don't have a profile for you any more. "
                                                         "`/internships profile` sets one up.")
        until = datetime(2026, 9, 28, 23, 30, tzinfo=PACIFIC).timestamp()   # Sep 29 in UTC
        self.assertEqual(text.alert_reply("paused", until=until),
                         "Paused until Sep 28. You'll get a catch-up of the best ones then.")
        with self.assertRaises(ValueError):
            text.alert_reply("nonsense")

    def test_cadence_and_ping(self):
        phrases = {("hourly", 9): "hourly (at most one DM an hour)",
                   ("daily", 9): "every day at 9am Los Angeles time",
                   ("daily", 17): "every day at 5pm Los Angeles time",
                   ("weekly", 9): "every Monday at 9am Los Angeles time",
                   ("off", 9): "never, because alerts are off"}
        for (alerts, hour), phrase in phrases.items():
            self.assertEqual(text.cadence_phrase(alerts, hour), phrase)
        self.assertEqual(text.ping_reply(person(), action="on"),
                         "Alerts are on: every day at 9am Los Angeles time.")
        self.assertEqual(text.ping_reply(person(), action="set"),
                         "Alerts: every day at 9am Los Angeles time.")
        self.assertEqual(text.ping_reply(person(), action="off"), "Alerts are off. Your profile is "
                         "saved; run `/internships ping` again to turn them back on.")

    def test_unknown_choice_escapes_what_was_typed(self):
        reply = text.unknown_choice("field", "*So`Cal*" + "x" * 300)
        self.assertTrue(reply.startswith("I don't know the field '\\*So\\`Cal\\*"))
        self.assertLessEqual(len(reply), 200)
        self.assertIn("I don't know the place 'SoCal'.", text.unknown_choice("where", "SoCal"))

    def test_info_refuses_an_unknown_role_like_the_other_pickers(self):
        self.assertEqual(text.unknown_choice("role", "rocket*lab"),
                         "I don't know the role 'rocket\\*lab'. Start typing and pick one of the "
                         "suggestions.")

    def test_info_blocks_escape_the_salary_and_explain_a_missing_description(self):
        blocks = text.info_blocks("**Acme** — Intern", "$40/hr *plus* housing", "")
        bare = text.info_blocks("**Acme** — Intern", None, "Build things.")

        self.assertEqual(blocks, ["**Acme** — Intern", "**Salary:** $40/hr \\*plus\\* housing",
                                  "No description available — the posting may have closed."])
        self.assertEqual(bare[1:], ["**Salary:** not listed", "Build things."])

    def test_fit_line(self):
        found = a_match(why=("Mechanical engineering (your field)", "Irvine, CA"))
        self.assertEqual(text.fit_line(found, None),
                         "**For you:** Good match · Why: Mechanical engineering (your field) · Irvine, CA")
        self.assertEqual(text.fit_line(None, "company hidden"),
                         "**For you:** outside your filters (company hidden).")

    def test_no_emoji_anywhere(self):
        every = [text.start_card(pdf_ok=True), text.welcome_dm(person()), text.dm_blocked_text(),
                 text.migrated_intro(), *text.help_text(pdf_ok=False, companies=412)]
        for body in every:
            self.assertFalse([ch for ch in body if ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF])


class Debug(unittest.TestCase):
    SUMMARY = {"profiles": 1, "alerting": 2, "hourly": 0, "daily": 3, "weekly": 0,
               "dm_blocked": 0, "left": 2, "no_access": 4, "field:software": 7,
               "field:biology_lab": 1}
    REPORT = {"delivery_last_at": NOW - 120, "delivery_last_due": 2, "delivery_last_sent": 5,
              "delivery_last_empty": 0, "delivery_last_forbidden": 1}

    def test_small_counts_are_bucketed_and_no_ids_leak(self):
        lines = text.debug_lines(self.SUMMARY, self.REPORT, {"software": 48, "biology_lab": 0},
                                 pdf_ok=False, migrated=1.0, now=NOW)
        body = "\n".join(lines)
        self.assertIn("profiles: <3 · alerts on: <3 (hourly 0 · daily 3 · weekly 0) · DMs closed: 0 "
                      "· left every shared server: <3 · without access: 4", body)
        self.assertIn("last delivery tick: 2m ago · due <3 · sent 5 · nothing new 0 · DMs refused <3", body)
        self.assertIn("Software engineering 48 · 7", body)
        self.assertIn("Biology & lab research 0 · <3", body)
        self.assertIn("PDF unavailable (install pypdf)", body)
        self.assertIsNone(re.search(r"\d{15,}", body))
        self.assertTrue(all(len(line) <= text.ALERT_MAX for line in lines))

    def test_the_legacy_import_is_named_as_an_import(self):
        imported = "\n".join(text.debug_lines(self.SUMMARY, self.REPORT, {}, pdf_ok=True,
                                               migrated=4.0, now=NOW))
        never = "\n".join(text.debug_lines(self.SUMMARY, self.REPORT, {}, pdf_ok=True,
                                            migrated=None, now=NOW))

        self.assertIn("legacy import: 4 subscribers imported", imported)
        self.assertIn("legacy import: none", never)
        self.assertNotIn("start-up", imported + never)

    def test_help_chunks_fit(self):
        chunks = text.help_text(pdf_ok=True, companies=412)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= text.ALERT_MAX for c in chunks))
        self.assertIn(text.disclosure_text(), chunks[1])
        self.assertTrue(chunks[1].endswith("PDF reading: available."))


class PromisesMatchTheAlertState(unittest.TestCase):
    """Every "I'll DM you" is true for the person reading it (review findings 19, 22, 23)."""

    PAUSE_END = NOW + 5 * 86400

    def states(self):
        return {"on": person(fields=("software",)),
                "off": person(fields=("software",), alerts="off"),
                "blocked": person(fields=("software",), dm_failures=intern_store.DM_FAILURE_LIMIT),
                "paused": person(fields=("software",), paused_until=self.PAUSE_END)}

    def test_only_someone_whose_alerts_will_run_is_promised_a_dm_in_the_empty_state(self):
        for state, p in self.states().items():
            with self.subTest(state=state):
                body = text.empty_state(p, (), pool=10, companies=3, days=14, now=NOW)

                promised = "I'll DM you the moment one appears" in body
                self.assertEqual(promised, state == "on")

    def test_the_empty_state_tells_each_state_what_to_do(self):
        s = self.states()

        off = text.empty_state(s["off"], (), pool=10, companies=3, days=14, now=NOW)
        blocked = text.empty_state(s["blocked"], (), pool=10, companies=3, days=14, now=NOW)
        paused = text.empty_state(s["paused"], (), pool=10, companies=3, days=14, now=NOW)

        self.assertIn("/internships ping", off)
        self.assertIn("Direct Messages", blocked)
        self.assertIn("Sep 26", paused)

    def test_the_saved_followup_never_says_dms_are_coming_when_they_are_not(self):
        s = self.states()

        off = text.saved_followup((), NOW, s["off"])[-1]
        blocked = text.saved_followup((), NOW, s["blocked"])[-1]
        paused = text.saved_followup((), NOW, s["paused"])[-1]

        self.assertNotIn("will reach you by DM", off)
        self.assertNotIn("will reach you by DM", blocked)
        self.assertIn("Sep 26", paused)

    def test_a_paused_persons_welcome_dm_says_when_alerts_start(self):
        self.assertIn("Sep 26", text.welcome_dm(self.states()["paused"], now=NOW))

    def test_setting_a_cadence_while_paused_says_the_pause_still_holds(self):
        p = dataclasses.replace(self.states()["paused"], alerts="hourly")

        reply = text.ping_reply(p, action="set", now=NOW)

        self.assertIn("Sep 26", reply)
        self.assertIn("/internships ping", reply)

    def test_a_cadence_set_with_no_pause_reads_as_before(self):
        self.assertEqual(text.ping_reply(self.states()["on"], action="set", now=NOW),
                         f"Alerts: {text.cadence_phrase('daily', 9)}.")


class BrowseHeaderCountsEverything(unittest.TestCase):
    def test_more_than_fit_says_how_many_are_shown(self):
        header = text.browse_header(105, field=None, level_label="Internships", where_label="US", days=30)

        self.assertIn("105 recent roles", header)
        self.assertIn(f"showing {intern_match.BROWSE_MAX}", header)

    def test_a_list_that_fits_says_nothing_about_showing(self):
        header = text.browse_header(3, field=None, level_label="Internships", where_label="US", days=30)

        self.assertNotIn("showing", header)


if __name__ == "__main__":
    unittest.main()
