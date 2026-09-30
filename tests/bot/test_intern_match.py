"""
The internship scorer: which postings fit a profile, how well, and which of
them are one role listed many times.

    python3 -m unittest discover -s tests      # no install needed

Every candidate here is built the way the bot builds them — a synthetic
`postings` row through `intern_match.tag_rows` — so the classifiers under the
scorer are the shipped ones, not stand-ins. Nothing opens the real
postings.db; the window loader is run against an in-memory database built from
the contract's DDL, `contract/postings_v1.sql`.

The numbers pinned are spec 4.5: weights 55/20/10/10/5, a 25-point graduate
penalty, the keyword-only floor at 45, and bands at 45, 60 and 75.
"""

import asyncio
import dataclasses
import itertools
import pathlib
import sqlite3
import unittest
from unittest import mock

import intern_match as match
import intern_profile as profile
import intern_taxonomy

NOW = 1_790_000_000.0
DAY = 86400
_ROWIDS = itertools.count(1)

#: 56 real places for one Target store programme: the regional-clone fixture.
CITIES = (
    "Irvine, CA|Duluth, MN|Austin, TX|Denver, CO|Seattle, WA|Portland, OR|Phoenix, AZ|"
    "Chicago, IL|Boston, MA|Atlanta, GA|Miami, FL|Dallas, TX|Houston, TX|Omaha, NE|Tulsa, OK|"
    "Boise, ID|Reno, NV|Tucson, AZ|Fresno, CA|Madison, WI|Columbus, OH|Detroit, MI|"
    "Nashville, TN|Memphis, TN|Louisville, KY|Richmond, VA|Raleigh, NC|Charlotte, NC|Tampa, FL|"
    "Orlando, FL|Jacksonville, FL|Birmingham, AL|Jackson, MS|Little Rock, AR|Wichita, KS|"
    "Des Moines, IA|Fargo, ND|Sioux Falls, SD|Billings, MT|Cheyenne, WY|Albuquerque, NM|"
    "Salt Lake City, UT|Anchorage, AK|Honolulu, HI|Hartford, CT|Providence, RI|Newark, NJ|"
    "Pittsburgh, PA|Baltimore, MD|Wilmington, DE|Burlington, VT|Manchester, NH|Portland, ME|"
    "Charleston, SC|Buffalo, NY|Spokane, WA").split("|")


def row(title, location="Minneapolis, MN", company="Acme", *, age=10.0, published=True):
    """One `postings` row in WINDOW_SQL's column order, first seen `age` days ago."""
    rowid = next(_ROWIDS)
    seen = NOW - age * DAY
    return (rowid, "greenhouse", f"ext{rowid}", company, title, location,
            f"https://example.com/jobs/{rowid}", seen if published else None, seen)


def cand(*args, **kwargs):
    return match.tag_rows([row(*args, **kwargs)])[0]


def person(**fields):
    """A manual bachelor's profile, default levels and places, fields set as given."""
    base = profile.new_profile(7, NOW, source="manual", cursor=NOW - 600)
    return dataclasses.replace(base, **{"degree": "bachelor", **fields})


MECH = person(fields=("mechanical",))


def expected(F, K, P, R, T, G=0):
    return round(55 * F + 20 * K + 10 * P + 10 * R + 5 * T - 25 * G)


class ScoreParts(unittest.TestCase):
    def test_a_direct_field_fits_fully(self):
        F, why = match.field_fit(("mechanical",), cand("Mechanical Engineer Intern").fields)
        self.assertEqual((F, why), (1.0, "Mechanical engineering (your field)"))

    def test_an_adjacent_field_lends_its_weight(self):
        F, why = match.field_fit(("mechanical",), cand("Aerospace Engineering Intern").fields)
        self.assertEqual(F, 0.7)
        self.assertEqual(why, "Aerospace engineering (related to your Mechanical engineering)")

    def test_general_engineering_counts_only_for_an_engineering_profile(self):
        generic = cand("Engineering Intern")
        self.assertEqual(match.field_fit(("mechanical",), generic.fields)[0], 0.6)
        self.assertIsNone(match.score(person(fields=("biology_lab",)), generic, NOW))

    def test_one_keyword_hit_is_worth_less_than_two(self):
        one = person(fields=("mechanical",), keywords=("turbomachinery",))
        two = person(fields=("mechanical",), keywords=("turbomachinery", "cfd"))
        c = cand("Turbomachinery CFD Intern")
        self.assertEqual(match.score(one, c, NOW)[0], expected(1.0, 0.6, 0.8, 0, 0.5))
        self.assertEqual(match.score(two, c, NOW)[0], expected(1.0, 1.0, 0.8, 0, 0.5))
        self.assertIn("title mentions turbomachinery, cfd", match.score(two, c, NOW)[1])

    def test_place_tiers(self):
        cases = (("Irvine, CA", ("oc", "us", "unlisted"), 1.0),
                 ("Minneapolis, MN", ("us", "unlisted"), 0.8),
                 ("United States", ("us", "unlisted"), 0.6),
                 ("27 Locations", ("us", "unlisted"), 0.3))
        for location, places, tier in cases:
            with self.subTest(location=location):
                result = match.score(person(fields=("mechanical",), locations=places),
                                     cand("Mechanical Engineer Intern", location), NOW)
                self.assertEqual(result[2], tier)
                self.assertEqual(result[0], expected(1.0, 0, tier, 0, 0.5))

    def test_freshness_tiers(self):
        for age, fresh in ((2, 1.0), (5, 0.5), (10, 0.0)):
            with self.subTest(age=age):
                got = match.score(MECH, cand("Mechanical Engineer Intern", age=age), NOW)[0]
                self.assertEqual(got, expected(1.0, 0, 0.8, fresh, 0.5))

    def test_a_chosen_term_scores_higher_than_any_other(self):
        picked = person(fields=("mechanical",), terms=("Summer 2027",))
        c = cand("Mechanical Engineering Intern (Summer 2027)")
        self.assertEqual(match.score(picked, c, NOW)[0], expected(1.0, 0, 0.8, 0, 1.0))
        self.assertEqual(match.score(MECH, c, NOW)[0], expected(1.0, 0, 0.8, 0, 0.5))

    def test_published_is_preferred_over_first_seen_for_age(self):
        c = cand("Mechanical Engineer Intern", age=2)
        self.assertEqual(c.ts, c.published)
        unpublished = cand("Mechanical Engineer Intern", age=2, published=False)
        self.assertEqual(unpublished.ts, unpublished.first_seen)


class KeywordOnlyFloor(unittest.TestCase):
    SOFTWARE = person(fields=("software",), keywords=("turbomachinery",))

    def test_a_keyword_only_title_scores_exactly_the_floor(self):
        # raw 22.5 = 12 (one keyword) + 8 (US place) + 0 (10 days) + 2.5 (no term)
        c = cand("Turbomachinery Intern")
        self.assertEqual(match.score(self.SOFTWARE, c, NOW)[0], 45)
        ranked = match.rank(self.SOFTWARE, [c], NOW)
        self.assertEqual([m.band for m in ranked], ["Worth a look"])

    def test_the_graduate_penalty_applies_after_the_floor(self):
        c = cand("PhD Turbomachinery Intern")
        self.assertEqual(match.score(self.SOFTWARE, c, NOW)[0], 20)
        self.assertEqual(match.rank(self.SOFTWARE, [c], NOW), [])

    def test_a_title_with_a_field_is_never_floored(self):
        # Legal lends finance 0.3: 16.5 + 12 + 3 + 0 + 2.5 = 34, below the floor.
        legal = person(fields=("legal_policy",), keywords=("audit",))
        c = cand("Audit Intern", "27 Locations")
        self.assertEqual(match.score(legal, c, NOW)[0], expected(0.3, 0.6, 0.3, 0, 0.5))
        self.assertEqual(match.score(legal, c, NOW)[0], 34)


class GraduatePenalty(unittest.TestCase):
    TITLE = "Mechanical Engineering PhD Intern"

    def test_an_undergraduate_loses_25_and_is_told_why(self):
        score, why, *_ = match.score(MECH, cand(self.TITLE), NOW)
        self.assertEqual(score, expected(1.0, 0, 0.8, 0, 0.5, G=1))
        self.assertIn("asks for a PhD student", why)

    def test_a_masters_student_is_not_penalised(self):
        score, why, *_ = match.score(person(fields=("mechanical",), degree="master"),
                                     cand(self.TITLE), NOW)
        self.assertEqual(score, expected(1.0, 0, 0.8, 0, 0.5))
        self.assertFalse([w for w in why if w.startswith("asks for")])

    def test_a_bs_ms_role_is_open_to_undergraduates(self):
        score, *_ = match.score(MECH, cand("BS/MS Mechanical Engineering Intern"), NOW)
        self.assertEqual(score, expected(1.0, 0, 0.8, 0, 0.5))

    def test_a_migrated_profile_without_a_degree_sees_the_caveat_but_no_penalty(self):
        migrated = dataclasses.replace(MECH, source="migrated", degree=None)
        score, why, *_ = match.score(migrated, cand(self.TITLE), NOW)
        self.assertEqual(score, expected(1.0, 0, 0.8, 0, 0.5))
        self.assertIn("asks for a PhD student", why)
        with_degree = dataclasses.replace(migrated, degree="bachelor")
        self.assertEqual(match.score(with_degree, cand(self.TITLE), NOW)[0],
                         expected(1.0, 0, 0.8, 0, 0.5, G=1))

    def test_why_has_at_most_four_reasons_in_the_spec_order(self):
        # 4.5.5's fourth reason is "Caveats": both share it, so a clearance or citizenship
        # caveat is never the one cut (D16 shows it to everyone).
        keen = person(fields=("mechanical",), keywords=("thermal",))
        c = cand("PhD Thermal Mechanical Engineering Intern - Secret Clearance Required")
        _, why, *_ = match.score(keen, c, NOW)
        self.assertEqual(why, ("Mechanical engineering (your field)", "title mentions thermal",
                               "Minneapolis, MN",
                               "asks for a PhD student · title mentions a security clearance"))

    def test_a_clearance_caveat_alone_is_its_own_reason(self):
        c = cand("Mechanical Engineering Intern - US Citizenship Required")
        _, why, *_ = match.score(MECH, c, NOW)
        self.assertEqual(why[-1], "title mentions US citizenship")


class HardFilters(unittest.TestCase):
    def assertRefused(self, p, c, reason):
        self.assertIsNone(match.score(p, c, NOW))
        self.assertEqual(match.explain(p, c, NOW), (None, reason))

    def test_level(self):
        self.assertRefused(MECH, cand("Mechanical Engineer I"),
                           "level not in Looking for (Entry-level jobs (Associate, I, Junior))")

    def test_location(self):
        self.assertRefused(MECH, cand("Mechanical Engineer Intern", "London, UK"),
                           "outside your locations")

    def test_term_mismatch(self):
        summer = person(fields=("mechanical",), terms=("Summer 2027",))
        self.assertRefused(summer, cand("Mechanical Engineering Intern, Fall 2027"),
                           "term Fall 2027 isn't one you picked")
        # A year-only term passes when a chosen term is in that year.
        self.assertIsNotNone(match.score(summer, cand("Mechanical Engineering Intern 2027"), NOW))

    def test_starts_after_graduation(self):
        leaving = person(fields=("mechanical",), grad_year=2027, grad_month=6)
        self.assertRefused(leaving, cand("Mechanical Engineering Intern, Fall 2027"),
                           "starts after you graduate")
        self.assertIsNotNone(
            match.score(leaving, cand("Mechanical Engineering Intern (Summer 2027)"), NOW))

    def test_a_hidden_company_is_matched_as_a_prefix(self):
        hiding = person(fields=("mechanical",), companies_hidden=("boeing",))
        self.assertRefused(hiding, cand("Mechanical Engineer Intern", company="Boeing Defense"),
                           "company hidden")

    def test_only_these_companies(self):
        only = person(fields=("mechanical",), companies_only=("spacex",))
        self.assertRefused(only, cand("Mechanical Engineer Intern", company="Boeing"),
                           "not one of your only-these companies")
        self.assertIsNotNone(match.score(only, cand("Mechanical Engineer Intern",
                                                    company="SpaceX"), NOW))

    def test_no_evidence(self):
        self.assertRefused(person(fields=("biology_lab",)), cand("Software Engineer Intern"),
                           "no field or keyword of yours in the title")

    def test_biology_never_gets_pharmacy(self):
        bio = person(fields=("biology_lab", "healthcare"))
        self.assertIsNone(match.score(bio, cand("Pharmacy Intern"), NOW))

    def test_explain_returns_a_match_even_below_the_list_threshold(self):
        weak = person(fields=("civil",))
        found, reason = match.explain(weak, cand("Mechanical Engineer Intern", "27 Locations"), NOW)
        self.assertIsNone(reason)
        self.assertLess(found.score, match.SHOW_MIN)
        self.assertIsNone(found.band)


def target_rows():
    return [row(f"Store Executive Intern - {city}", "10 Locations", "Target") for city in CITIES]


class Grouping(unittest.TestCase):
    STORE = person(fields=("business_ops",), locations=("oc", "us", "unlisted"))

    def test_regional_clones_fold_into_their_clone_key(self):
        cands = match.tag_rows(target_rows())
        gmap = match.group_map(cands)
        self.assertEqual(set(gmap.values()), {cands[0].ck})

    def test_one_place_is_not_a_regional_clone(self):
        cands = match.tag_rows([row("Store Executive Intern - Duluth, MN", company="Target")
                                for _ in range(3)])
        self.assertEqual(set(match.group_map(cands).values()), {cands[0].rk})

    def test_two_postings_are_not_a_regional_clone(self):
        cands = match.tag_rows([row("Store Executive Intern - Duluth, MN", company="Target"),
                                row("Store Executive Intern - Austin, TX", company="Target")])
        gmap = match.group_map(cands)
        self.assertEqual([gmap[c.rowid] for c in cands], [c.rk for c in cands])

    def test_the_representative_is_the_nearest_member(self):
        ranked = match.rank(self.STORE, match.tag_rows(target_rows()), NOW)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].cand.title, "Store Executive Intern - Irvine, CA")
        self.assertEqual((ranked[0].more, ranked[0].place), (55, "Irvine, CA"))
        self.assertEqual(ranked[0].ledger, (ranked[0].cand.rk_hash, ranked[0].cand.ck_hash))


class Exclusion(unittest.TestCase):
    STORE = Grouping.STORE

    def test_any_member_s_role_hash_excludes_a_regional_group(self):
        cands = match.tag_rows(target_rows())
        duluth = next(c for c in cands if "Duluth" in c.title)
        self.assertEqual(match.rank(self.STORE, cands, NOW, exclude=frozenset({duluth.rk_hash})), [])

    def test_the_clone_hash_excludes_a_regional_group(self):
        cands = match.tag_rows(target_rows())
        self.assertEqual(match.rank(self.STORE, cands, NOW,
                                    exclude=frozenset({cands[0].ck_hash})), [])

    def test_the_clone_hash_never_excludes_a_sibling_role(self):
        accounting = cand("2027 Summer Corporate Intern - Accounting", "Peoria, IL", "Caterpillar")
        finance = cand("2027 Summer Corporate Intern - Finance", "Peoria, IL", "Caterpillar")
        self.assertEqual(accounting.ck, finance.ck)
        ranked = match.rank(person(fields=("finance",)), [accounting, finance], NOW,
                            exclude=frozenset({accounting.rk_hash, accounting.ck_hash}))
        self.assertEqual([m.cand.title for m in ranked], [finance.title])


class Ranking(unittest.TestCase):
    def test_best_first_and_newest_first(self):
        strong = cand("Mechanical Engineer Intern", "Irvine, CA", age=6)
        fresh = cand("Aerospace Engineering Intern", age=1)
        p = person(fields=("mechanical",), locations=("oc", "us", "unlisted"))
        self.assertEqual([m.cand for m in match.rank(p, [fresh, strong], NOW)], [strong, fresh])
        self.assertEqual([m.cand for m in match.rank(p, [strong, fresh], NOW, sort="newest")],
                         [fresh, strong])

    def test_min_score_is_respected(self):
        adjacent = cand("Aerospace Engineering Intern")      # 38.5 + 8 + 0 + 2.5 = 49
        self.assertEqual(len(match.rank(MECH, [adjacent], NOW)), 1)
        self.assertEqual(match.rank(MECH, [adjacent], NOW, min_score=60), [])

    def test_bands(self):
        cases = ((44, None), (45, "Worth a look"), (59, "Worth a look"), (60, "Good match"),
                 (74, "Good match"), (75, "Strong match"))
        for score, band in cases:
            with self.subTest(score=score):
                self.assertEqual(match.band_for(score), band)

    def test_the_candidate_list_is_not_modified(self):
        cands = match.tag_rows(target_rows())
        before = list(cands)
        match.rank(Grouping.STORE, cands, NOW)
        match.coverage(Grouping.STORE, cands, NOW, supply=match.field_supply(cands))
        self.assertEqual(cands, before)


class Coverage(unittest.TestCase):
    OC_ONLY = person(fields=("mechanical",), locations=("oc", "unlisted"))

    def test_us_extra_counts_roles_outside_the_chosen_metro(self):
        cands = [cand("Mechanical Engineer Intern", "Irvine, CA"),
                 cand("Mechanical Engineering Intern", "Minneapolis, MN")]
        got = match.coverage(self.OC_ONLY, cands, NOW, supply=match.field_supply(cands))
        self.assertEqual((got.total, got.us_extra), (1, 1))
        anywhere = match.coverage(MECH, cands, NOW, supply=match.field_supply(cands))
        self.assertIsNone(anywhere.us_extra)

    def test_a_field_nobody_posts_for_is_thin(self):
        cands = [cand("Mechanical Engineer Intern")]
        p = person(fields=("mechanical", "legal_policy"))
        got = match.coverage(p, cands, NOW, supply=match.field_supply(cands))
        self.assertIn(("legal_policy", 0), got.thin)
        self.assertIn(("mechanical", 1), got.thin)

    def test_hourly_hint_only_for_the_fields_that_need_it(self):
        cands = [cand("Pharmacy Technician", "Irvine, CA"), cand("Software Engineer Intern")]
        supply = match.field_supply(cands)
        pharmacy = match.coverage(person(fields=("pharmacy",)), cands, NOW, supply=supply)
        cs = match.coverage(person(fields=("software",)), cands, NOW, supply=supply)
        self.assertEqual((pharmacy.hourly_hint, cs.hourly_hint), (1, 0))

    def test_field_supply_counts_distinct_early_career_groups_in_the_us(self):
        cands = match.tag_rows(target_rows() + [row("Store Executive Intern - London, UK",
                                                    "London, UK", "Acme"),
                                                row("Pharmacy Technician")])
        supply = match.field_supply(cands)
        self.assertEqual((supply["business_ops"], supply["pharmacy"]), (1, 0))


class Relaxations(unittest.TestCase):
    P = person(fields=("mechanical",), locations=("oc",))

    def cands(self):
        return [cand("Mechanical Engineer Intern", "Irvine, CA"),
                cand("Mechanical Engineering Intern", "Minneapolis, MN"),
                cand("Mechanical Design Intern", "27 Locations"),
                cand("Mechanical Design Engineer, New Grad", "Irvine, CA"),
                *[cand(f"Civil Engineering Intern {n}", "Irvine, CA") for n in range(3)]]

    def test_only_gains_at_most_three_best_first(self):
        relax = match.relaxations(self.P, self.cands(), NOW)
        self.assertEqual([(r.id, r.gain) for r in relax], [("adjacent", 3), ("us", 2), ("levels", 1)])
        self.assertEqual(relax[0].label, "Add Civil & environmental engineering")

    def test_each_change_delivers_its_gain(self):
        cands = self.cands()
        base = len(match.rank(self.P, cands, NOW))
        for r in match.relaxations(self.P, cands, NOW):
            with self.subTest(relaxation=r.id):
                changed = profile.with_changes(self.P, NOW, **r.changes)
                self.assertEqual(len(match.rank(changed, cands, NOW)), base + r.gain)

    def test_adjacent_never_suggests_pharmacy(self):
        cands = [cand(f"Pharmacy Intern {n}", "Irvine, CA") for n in range(5)]
        for fields in (("biology_lab",), ("healthcare",), ("chem_materials", "biology_lab")):
            with self.subTest(fields=fields):
                relax = match.relaxations(person(fields=fields), cands, NOW)
                self.assertFalse([r for r in relax if "pharmacy" in str(r.changes)])

    def test_hourly_is_offered_to_a_pre_pharmacy_profile(self):
        cands = [cand("Pharmacy Technician", "Irvine, CA")]
        relax = match.relaxations(person(fields=("pharmacy",)), cands, NOW)
        self.assertEqual([(r.id, r.label, r.gain) for r in relax],
                         [("hourly", "Include part-time & hourly", 1)])


class Browse(unittest.TestCase):
    def test_field_level_days_and_order(self):
        cands = [cand("Mechanical Engineer Intern", age=1), cand("Mechanical Engineering Co-op", age=3),
                 cand("Mechanical Engineer I", age=2), cand("Software Engineer Intern", age=1),
                 cand("Mechanical Design Intern", age=9)]
        found = match.browse(cands, field="mechanical", levels=("intern", "coop"),
                             locations=("us", "unlisted"), days=7, now=NOW)
        self.assertEqual([m.cand.title for m in found],
                         ["Mechanical Engineer Intern", "Mechanical Engineering Co-op"])
        self.assertTrue(all(m.band is None and m.why == () for m in found))

    def test_at_most_twenty(self):
        cands = [cand(f"Software Engineer Intern {n}", age=n / 10) for n in range(25)]
        found = match.browse(cands, field=None, levels=("intern",), locations=("us",),
                             days=7, now=NOW)
        self.assertEqual(len(found), match.BROWSE_MAX)
        self.assertEqual(found[0].cand.title, "Software Engineer Intern 0")

    def test_no_limit_returns_every_role_so_the_header_can_count_them(self):
        cands = [cand(f"Software Engineer Intern {n}", age=n / 10) for n in range(25)]

        found = match.browse(cands, field=None, levels=("intern",), locations=("us",),
                             days=7, now=NOW, limit=None)

        self.assertEqual(len(found), 25)


class TitleTerms(unittest.TestCase):
    def test_strict_skills_are_skipped_and_slashes_split(self):
        p = person(keywords=("turbomachinery",), skills=("c", "go", "r", "html_css", "python"))
        self.assertEqual(match.title_terms(p), ("turbomachinery", "HTML", "CSS", "Python"))

    def test_a_skill_matches_whole_words_only(self):
        p = person(fields=("biology_lab",), skills=("python",))
        self.assertIsNotNone(match.score(p, cand("Python Developer Intern"), NOW))
        self.assertIsNone(match.score(p, cand("Pythonista Intern"), NOW))


#: The whole postings.db schema as DIAYN creates it (contract v1).
POSTINGS_DDL = (pathlib.Path(__file__).resolve().parents[2] / "contract" / "postings_v1.sql")


class LoadWindow(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(POSTINGS_DDL.read_text(encoding="utf-8"))
        rows = (("1", "SpaceX", "Propulsion Intern", 0, 2), ("2", "Rocket Lab", "Avionics Intern", 0, 2),
                ("3", "Astranis", "Payload Intern", 1, 2), ("4", "Anduril", "Old Intern", 0, 40))
        self.db.executemany(
            "INSERT INTO postings (platform, external_id, company, title, location, url, "
            "published, unbounded, first_seen) VALUES ('greenhouse', ?, ?, ?, 'Irvine, CA', "
            "'https://example.com', NULL, ?, ?)",
            [(ext, co, title, unbounded, NOW - age * DAY) for ext, co, title, unbounded, age in rows])
        self.addCleanup(self.db.close)

    def load(self):
        return asyncio.run(match.load_window(self.db, now=NOW, max_age_days=30,
                                             is_blocked=lambda company: company == "Rocket Lab"))

    def test_blocked_unbounded_and_old_rows_are_absent(self):
        self.assertEqual([c.company for c in self.load()], ["SpaceX"])

    def test_tagging_runs_in_a_worker_thread(self):
        real = asyncio.to_thread
        called = []

        async def spy(func, *args, **kwargs):
            called.append(func)
            return await real(func, *args, **kwargs)

        with mock.patch.object(match.asyncio, "to_thread", spy):
            cands = self.load()
        self.assertEqual(called, [match.tag_rows])
        self.assertEqual(cands[0].rk, intern_taxonomy.role_key("SpaceX", "Propulsion Intern"))


if __name__ == "__main__":
    unittest.main()
