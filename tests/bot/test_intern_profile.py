"""
The internship profile: what a person asked the finder for, and every rule for
changing it.

    python3 -m unittest discover -s tests      # no install needed

`intern_profile` is pure: no database, no Discord. Everything a user can type
or pick reaches a stored profile through `with_changes` or one of the two form
parsers here, so most of what is pinned is that a bad value is dropped or
refused rather than stored, and that a change never reaches back into the
profile it was made from.

Dates are fixed at the spec's reference day, 2026-09-28, so the level and term
tables below read the same on any day the suite runs.
"""

import copy
import dataclasses
import unittest
from datetime import date

import intern_profile as profile
import intern_vocab
import resume_lexicon
from intern_profile import Profile

TODAY = date(2026, 9, 28)
#: Fixed rather than time.time(), so a failure repeats.
NOW = 1_790_000_000.0
CURSOR = NOW - 600

ALL_FOUR = ("intern", "coop", "new_grad", "entry")


def fresh(**fields) -> Profile:
    """A new manual profile with `fields` set as-is (no normalisation)."""
    return dataclasses.replace(profile.new_profile(7, NOW, source="manual", cursor=CURSOR), **fields)


def mech_student(**fields) -> Profile:
    """A saved mechanical engineer graduating June 2028, fields not locked."""
    base = fresh(majors=("mechanical_engineering",), degree="bachelor", grad_year=2028,
                 grad_month=6, skills=("solidworks",), fields=("mechanical", "manufacturing"))
    return dataclasses.replace(base, **fields)


DRAFT = {
    "majors": ["mechanical_engineering"], "minors": ["mathematics"], "degree": "bachelor",
    "grad_year": 2027, "grad_month": 6, "skills": ["solidworks", "matlab", "python"],
    "fields": ["mechanical", "manufacturing"],
    "evidence": {"study": "education", "grad": "education"},
}


class NewProfile(unittest.TestCase):
    def test_a_new_profile_starts_from_the_documented_defaults(self):
        p = profile.new_profile(7, NOW, source="manual", cursor=CURSOR)

        self.assertEqual(p.levels, intern_vocab.DEFAULT_LEVELS)
        self.assertEqual(p.locations, intern_vocab.DEFAULT_LOCATIONS)
        self.assertEqual((p.alerts, p.alert_hour, p.min_score), ("daily", 9, 60))
        self.assertEqual(p.consent_version, 0)
        self.assertEqual((p.majors, p.skills, p.fields, p.keywords), ((), (), (), ()))
        self.assertEqual((p.created_at, p.updated_at, p.active_at, p.last_run_at), (NOW,) * 4)
        self.assertEqual(p.cursor, CURSOR)
        self.assertEqual((p.dm_failures, p.intro_pending, p.paused_until), (0, False, None))

    def test_an_unknown_source_is_refused(self):
        # The column has a CHECK; a typo here should fail where it was made,
        # not at the first save.
        with self.assertRaises(ValueError):
            profile.new_profile(7, NOW, source="upload", cursor=CURSOR)

    def test_a_profile_cannot_be_changed_in_place(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            fresh().fields = ("software",)

    def test_editable_is_the_user_columns_plus_the_draft_alert_settings(self):
        self.assertEqual(profile.EDITABLE, frozenset({
            "source", "consent_version", "majors", "minors", "degree", "grad_year",
            "grad_month", "skills", "keywords", "fields", "fields_locked", "levels",
            "levels_locked", "locations", "terms", "companies_only", "companies_hidden",
            "min_score", "alerts", "alert_hour"}))


class DefaultLevels(unittest.TestCase):
    def test_levels_follow_the_months_left_until_graduation(self):
        table = (
            ((2028, 6), ("intern", "coop")),     # 21 months out
            ((2027, 6), ALL_FOUR),               # 9 months out
            ((2025, 12), ALL_FOUR),              # graduated 9 months ago
            ((2024, 6), ("new_grad", "entry")),  # 27 months ago
            ((None, None), ("intern", "coop")),
        )
        for (year, month), expected in table:
            with self.subTest(year=year, month=month):
                self.assertEqual(profile.default_levels(year, month, TODAY), expected)

    def test_a_year_without_a_month_counts_as_june(self):
        self.assertEqual(profile.default_levels(2027, None, TODAY), ALL_FOUR)


class FromDraft(unittest.TestCase):
    def test_a_new_draft_takes_its_fields_from_the_resume_and_levels_from_the_date(self):
        p = profile.from_draft(7, DRAFT, NOW, source="resume", cursor=CURSOR, today=TODAY)

        self.assertEqual(p.fields, ("mechanical", "manufacturing"))
        self.assertEqual(p.levels, ALL_FOUR)
        self.assertEqual(p.consent_version, profile.DISCLOSURE_VERSION)
        self.assertEqual((p.source, p.cursor, p.fields_locked), ("resume", CURSOR, False))
        self.assertEqual(p.minors, ("mathematics",))

    def test_a_replacement_keeps_locked_choices_filters_and_alerts(self):
        existing = mech_student(fields=("aerospace",), fields_locked=True, levels=("new_grad",),
                                levels_locked=True, locations=("oc", "unlisted"),
                                alerts="weekly", companies_hidden=("cvshealth",),
                                skills=("excel",), keywords=("turbomachinery",), dm_failures=2)

        p = profile.from_draft(7, DRAFT, NOW + 50, source="resume", cursor=NOW,
                               today=TODAY, existing=existing)

        self.assertEqual(p.fields, ("aerospace",))
        self.assertEqual(p.levels, ("new_grad",))
        self.assertEqual(p.locations, ("oc", "unlisted"))
        self.assertEqual(p.alerts, "weekly")
        self.assertEqual(p.companies_hidden, ("cvshealth",))
        self.assertEqual(p.skills, ("solidworks", "matlab", "python"))
        self.assertEqual(p.keywords, ("turbomachinery",))
        # Bookkeeping belongs to the stored row, not to the new resume.
        self.assertEqual((p.cursor, p.created_at, p.dm_failures), (CURSOR, NOW, 2))

    def test_a_replacement_rederives_what_the_user_never_set_by_hand(self):
        existing = mech_student(fields=("aerospace",), levels=("intern",))

        p = profile.from_draft(7, DRAFT, NOW, source="pasted", cursor=NOW, today=TODAY,
                               existing=existing)

        self.assertEqual(p.fields, ("mechanical", "manufacturing"))
        self.assertEqual(p.levels, ALL_FOUR)
        self.assertEqual(p.source, "pasted")

    def test_a_replacement_that_finds_no_field_keeps_the_fields_it_had(self):
        # Emptying them would leave a replacement card whose Save stays disabled.
        existing = mech_student(fields=("aerospace",))
        fieldless = {**DRAFT, "fields": []}

        p = profile.from_draft(7, fieldless, NOW, source="resume", cursor=NOW, today=TODAY,
                               existing=existing)

        self.assertEqual((p.fields, p.fields_locked), (("aerospace",), False))
        self.assertTrue(profile.can_save(p))

    def test_anything_outside_the_vocabulary_is_dropped_from_the_draft(self):
        # The draft came out of a subprocess; its keys are rebuilt, never trusted.
        junk = {"majors": ["mechanical_engineering", "wizardry", 5], "degree": "wizard",
                "grad_year": True, "grad_month": 6, "skills": ["python", "juggling"],
                "fields": ["mechanical", "sorcery"], "name": "Jane Doe"}

        p = profile.from_draft(7, junk, NOW, source="resume", cursor=CURSOR, today=TODAY)

        self.assertEqual(p.majors, ("mechanical_engineering",))
        self.assertEqual((p.degree, p.grad_year, p.grad_month), (None, None, None))
        self.assertEqual(p.skills, ("python",))
        self.assertEqual(p.fields, ("mechanical",))

    def test_the_draft_is_not_modified(self):
        draft = copy.deepcopy(DRAFT)

        profile.from_draft(7, draft, NOW, source="resume", cursor=CURSOR, today=TODAY)

        self.assertEqual(draft, DRAFT)


class WithChanges(unittest.TestCase):
    def test_a_change_returns_a_new_profile_and_leaves_the_original_alone(self):
        original = mech_student()
        saved_copy = copy.deepcopy(original)

        changed = profile.with_changes(original, NOW + 5, fields=("software",))

        self.assertIsNot(changed, original)
        self.assertEqual(original, saved_copy)
        self.assertEqual(changed.fields, ("software",))

    def test_a_key_that_is_not_editable_is_refused(self):
        for key in ("cursor", "user_id", "dm_failures", "nonsense"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    profile.with_changes(fresh(), NOW, **{key: 1})

    def test_ids_outside_the_vocabulary_are_dropped(self):
        p = profile.with_changes(fresh(), NOW, fields=("software", "sorcery"),
                                 levels=("intern", "wizard"), locations=("us", "st:ZZ", "mars"))

        self.assertEqual(p.fields, ("software",))
        self.assertEqual(p.levels, ("intern",))
        self.assertEqual(p.locations, ("us",))

    def test_fields_are_capped_at_six(self):
        eight = intern_vocab.FIELD_IDS[:8]

        p = profile.with_changes(fresh(), NOW, fields=eight)

        self.assertEqual(p.fields, eight[:6])

    def test_a_migrated_profile_keeps_its_eleven_fields_but_cannot_grow(self):
        migrated = fresh(source="migrated", fields=intern_vocab.LEGACY_ALL_TECH)

        grown = profile.with_changes(migrated, NOW, fields=intern_vocab.LEGACY_ALL_TECH + ("civil",))
        shrunk = profile.with_changes(migrated, NOW, fields=("software", "quant"))

        self.assertEqual(grown.fields, intern_vocab.LEGACY_ALL_TECH)
        self.assertEqual(shrunk.fields, ("software", "quant"))

    def test_keywords_are_cleaned_filtered_and_capped_at_ten(self):
        typed = ("Turbomachinery", "<script>", "x", "  Urban   Planning ") + tuple(
            f"keyword {n}" for n in range(12))

        p = profile.with_changes(fresh(), NOW, keywords=typed)

        self.assertEqual(p.keywords[:2], ("turbomachinery", "urban planning"))
        self.assertEqual(len(p.keywords), intern_vocab.MAX_KEYWORDS)
        for word in p.keywords:
            self.assertRegex(word, intern_vocab.KEYWORD_RE)

    def test_setting_fields_or_levels_locks_them_unless_told_otherwise(self):
        locked = profile.with_changes(fresh(), NOW, fields=("software",), levels=("entry",))
        derived = profile.with_changes(fresh(), NOW, fields=("software",), fields_locked=False)

        self.assertTrue(locked.fields_locked)
        self.assertTrue(locked.levels_locked)
        self.assertFalse(derived.fields_locked)
        self.assertFalse(derived.levels_locked)

    def test_a_min_score_that_is_not_a_choice_leaves_it_unchanged(self):
        self.assertEqual(profile.with_changes(fresh(), NOW, min_score=50).min_score, 60)
        self.assertEqual(profile.with_changes(fresh(), NOW, min_score=75).min_score, 75)

    def test_a_location_change_that_leaves_no_preset_falls_back_to_the_default(self):
        with_state = fresh(locations=("us", "unlisted", "st:WA"))

        only_state = profile.with_changes(with_state, NOW, locations=("st:WA",))
        emptied = profile.with_changes(with_state, NOW, locations=())

        self.assertEqual(only_state.locations, intern_vocab.DEFAULT_LOCATIONS)
        self.assertEqual(emptied.locations, intern_vocab.DEFAULT_LOCATIONS)

    def test_states_are_capped_at_ten_and_presets_are_kept(self):
        states = tuple(f"st:{code}" for code in ("WA", "OR", "NV", "AZ", "UT", "ID", "MT",
                                                "WY", "CO", "NM", "TX", "OK"))

        p = profile.with_changes(fresh(), NOW, locations=("us",) + states)

        self.assertEqual(p.locations, ("us",) + states[:intern_vocab.MAX_STATES])

    def test_every_change_bumps_updated_and_active(self):
        p = profile.with_changes(fresh(), NOW + 99, min_score=45)

        self.assertEqual((p.updated_at, p.active_at), (NOW + 99, NOW + 99))
        self.assertEqual(p.created_at, NOW)

    def test_the_alert_hour_is_clamped_to_the_day(self):
        self.assertEqual(profile.with_changes(fresh(), NOW, alert_hour=30).alert_hour, 23)
        self.assertEqual(profile.with_changes(fresh(), NOW, alert_hour=-2).alert_hour, 0)

    def test_companies_are_stored_normalised(self):
        p = profile.with_changes(fresh(), NOW, companies_hidden=("Boeing", "CVS Health", " - "),
                                 companies_only=("SpaceX",))

        self.assertEqual(p.companies_hidden, ("boeing", "cvshealth"))
        self.assertEqual(p.companies_only, ("spacex",))

    def test_a_minor_is_never_also_a_major(self):
        p = profile.with_changes(fresh(), NOW, majors=("mathematics",),
                                 minors=("mathematics", "physics"))

        self.assertEqual(p.minors, ("physics",))

    def test_clearing_the_year_clears_the_month(self):
        p = profile.with_changes(mech_student(), NOW, grad_year=None)

        self.assertEqual((p.grad_year, p.grad_month), (None, None))

    def test_an_unknown_degree_or_cadence_leaves_it_unchanged(self):
        p = profile.with_changes(mech_student(), NOW, degree="wizard", alerts="sometimes")

        self.assertEqual((p.degree, p.alerts), ("bachelor", "daily"))
        self.assertIsNone(profile.with_changes(mech_student(), NOW, degree=None).degree)


class CanSave(unittest.TestCase):
    def test_a_profile_needs_at_least_one_field_to_be_saved(self):
        self.assertFalse(profile.can_save(fresh()))
        self.assertTrue(profile.can_save(fresh(fields=("software",))))


class ParseGrad(unittest.TestCase):
    def test_the_formats_a_person_types(self):
        table = {
            "June 2028": (2028, 6), "Jun 2028": (2028, 6), "jun. 2028": (2028, 6),
            "Spring 2027": (2027, 6), "Fall 2027": (2027, 12), "06/2028": (2028, 6),
            "6/2028": (2028, 6), "2028": (2028, 6), "  December 2026 ": (2026, 12),
        }
        for text, expected in table.items():
            with self.subTest(text=text):
                self.assertEqual(profile.parse_grad(text, TODAY), expected)

    def test_what_is_not_a_date_or_is_out_of_range_is_none(self):
        for text in ("next year", "2040", "2015", "13/2028", "", "June", "sometime 2028"):
            with self.subTest(text=text):
                self.assertIsNone(profile.parse_grad(text, TODAY))


class ParseDetailsForm(unittest.TestCase):
    def parse(self, majors="Mechanical Engineering", degree=None, grad="June 2028",
              skills="SolidWorks", keywords="", current=None):
        return profile.parse_details_form(majors, degree, grad, skills, keywords, today=TODAY,
                                          current=current or mech_student())

    def test_an_unknown_major_becomes_a_keyword_and_says_so(self):
        changes, problems = self.parse(majors="Viticulture")

        self.assertIn("I didn't recognise 'Viticulture' as a major; kept it as a keyword.", problems)
        self.assertEqual(changes["majors"], ())
        self.assertIn("viticulture", changes["keywords"])

    def test_an_unknown_skill_becomes_a_keyword_and_says_so(self):
        changes, problems = self.parse(skills="SolidWorks, lab safety")

        self.assertIn("Not in my skills list, so kept as keywords: lab safety.", problems)
        self.assertEqual(changes["skills"], ("solidworks",))
        self.assertIn("lab safety", changes["keywords"])

    def test_text_that_cannot_be_a_keyword_is_reported_rather_than_claimed_kept(self):
        changes, problems = self.parse(majors="*bold* stuff", skills="Excel (pivot tables)")

        self.assertIn("I didn't recognise '\\*bold\\* stuff' as a major.", problems)
        self.assertIn("Not in my skills list, so left out: Excel (pivot tables).", problems)
        self.assertEqual(changes["keywords"], ())

    def test_changed_majors_rederive_fields_when_they_are_not_locked(self):
        changes, _ = self.parse(majors="Computer Science")

        self.assertEqual(changes["fields"], resume_lexicon.fields_for(("computer_science",),
                                                                      ("solidworks",)))
        self.assertIs(changes["fields_locked"], False)

    def test_changed_majors_leave_locked_fields_alone(self):
        changes, _ = self.parse(majors="Computer Science",
                                current=mech_student(fields_locked=True))

        self.assertNotIn("fields", changes)

    def test_unchanged_majors_do_not_touch_fields(self):
        changes, _ = self.parse()

        self.assertNotIn("fields", changes)

    def test_a_date_that_cannot_be_read_says_how_to_write_it(self):
        changes, problems = self.parse(grad="next year")

        self.assertIn("I couldn't read 'next year' as a date. Try 'June 2028'.", problems)
        self.assertNotIn("grad_year", changes)

    def test_a_new_date_rederives_levels_unless_they_are_locked(self):
        free, _ = self.parse(grad="June 2027")
        locked, _ = self.parse(grad="June 2027", current=mech_student(levels_locked=True))

        self.assertEqual((free["grad_year"], free["grad_month"]), (2027, 6))
        self.assertEqual(free["levels"], ALL_FOUR)
        self.assertIs(free["levels_locked"], False)
        self.assertNotIn("levels", locked)

    def test_degree_none_means_prefer_not_to_say(self):
        self.assertIsNone(self.parse(degree="none")[0]["degree"])
        self.assertEqual(self.parse(degree="master")[0]["degree"], "master")
        self.assertNotIn("degree", self.parse(degree=None)[0])

    def test_blank_boxes_keep_their_values_except_keywords_which_clear(self):
        current = mech_student(keywords=("turbomachinery",))

        changes, problems = self.parse(majors="", grad="", skills="  ", current=current)

        self.assertEqual(problems, ())
        self.assertEqual(changes, {"keywords": ()})

    def test_the_changes_apply_cleanly(self):
        changes, _ = self.parse(majors="Computer Science; minor Mathematics", degree="bachelor",
                                grad="June 2027", skills="Python, Git", keywords="robotics")

        p = profile.with_changes(mech_student(), NOW, **changes)

        self.assertEqual((p.majors, p.minors), (("computer_science",), ("mathematics",)))
        self.assertEqual(p.skills, ("python", "git"))
        self.assertEqual(p.keywords, ("robotics",))
        self.assertFalse(p.fields_locked)


class UpcomingTerms(unittest.TestCase):
    def test_the_four_seasons_after_this_month(self):
        self.assertEqual(profile.upcoming_terms(TODAY),
                         ("Winter 2027", "Spring 2027", "Summer 2027", "Fall 2027"))

    def test_a_season_later_this_year_comes_first(self):
        self.assertEqual(profile.upcoming_terms(date(2026, 2, 1))[0], "Spring 2026")

    def test_n_limits_the_list(self):
        self.assertEqual(profile.upcoming_terms(TODAY, n=2), ("Winter 2027", "Spring 2027"))


class ParseFiltersForm(unittest.TestCase):
    KNOWN = {"boeing": "Boeing", "spacex": "SpaceX", "cvshealth": "CVS Health"}

    def parse(self, states="", terms=(), hide="", only="", min_score="", current=None):
        return profile.parse_filters_form(
            states, terms, hide, only, min_score, today=TODAY,
            current=current or fresh(locations=("oc", "unlisted", "st:CA")), known_companies=self.KNOWN)

    def test_typed_states_replace_the_old_ones_and_keep_the_presets(self):
        changes, problems = self.parse(states="WA, Oregon")

        self.assertEqual(changes["locations"], ("oc", "unlisted", "st:WA", "st:OR"))
        self.assertEqual(problems, ())

    def test_something_that_is_not_a_state_is_named(self):
        _, problems = self.parse(states="WA, SoCal")

        self.assertIn("Not a US state: 'SoCal'.", problems)

    def test_a_near_miss_company_gets_a_suggestion(self):
        _, problems = self.parse(hide="Boing")

        self.assertIn("I don't track 'Boing'. Did you mean Boeing?", problems)

    def test_an_untracked_company_points_at_report(self):
        changes, problems = self.parse(hide="Pfizer")

        self.assertIn("I don't track 'Pfizer' yet. Suggest it with `/report`.", problems)
        self.assertEqual(changes["companies_hidden"], ())

    def test_companies_are_stored_normalised(self):
        changes, _ = self.parse(hide="Boeing, CVS Health\nboeing", only="SpaceX")

        self.assertEqual(changes["companies_hidden"], ("boeing", "cvshealth"))
        self.assertEqual(changes["companies_only"], ("spacex",))

    def test_a_stored_company_no_longer_tracked_survives_a_resubmit(self):
        current = fresh(companies_hidden=("oldco",))

        changes, problems = self.parse(hide="oldco", current=current)

        self.assertEqual(changes["companies_hidden"], ("oldco",))
        self.assertEqual(problems, ())

    def test_only_upcoming_or_already_chosen_terms_are_kept(self):
        current = fresh(terms=("Summer 2026",))

        changes, _ = self.parse(terms=("Summer 2027", "Summer 2031", "Summer 2026"),
                                current=current)

        self.assertEqual(changes["terms"], ("Summer 2027", "Summer 2026"))

    def test_min_score_must_be_one_of_the_three_choices(self):
        self.assertEqual(self.parse(min_score="75")[0]["min_score"], 75)
        self.assertNotIn("min_score", self.parse(min_score="50")[0])
        self.assertNotIn("min_score", self.parse(min_score="")[0])

    def test_blank_states_remove_every_state_but_keep_the_presets(self):
        changes, _ = self.parse()

        self.assertEqual(changes["locations"], ("oc", "unlisted"))

    def test_the_changes_apply_cleanly(self):
        changes, _ = self.parse(states="NY", terms=("Summer 2027",), hide="CVS Health",
                                only="Boeing", min_score="45")

        p = profile.with_changes(fresh(), NOW, **changes)

        self.assertEqual(p.locations, ("oc", "unlisted", "st:NY"))
        self.assertEqual((p.terms, p.min_score), (("Summer 2027",), 45))
        self.assertEqual((p.companies_hidden, p.companies_only), (("cvshealth",), ("boeing",)))


class Legacy(unittest.TestCase):
    ALL = intern_vocab.LEGACY_ALL_TECH

    def test_every_row_of_the_legacy_category_table(self):
        table = {
            None: self.ALL, "": self.ALL, "other": self.ALL, "swe,other": self.ALL,
            "swe": ("software", "security", "it"),
            "data-ml": ("data_ml", "analytics"),
            "hardware": ("electrical", "mechanical", "aerospace", "manufacturing"),
            "quant": ("quant",),
            "pm": ("product", "business_ops"),
            "pm,quant": ("product", "business_ops", "quant"),
            "bogus": self.ALL,
            "bogus,quant": ("quant",),
            " swe , ,": ("software", "security", "it"),
            "data-ml,swe,data-ml": ("data_ml", "analytics", "software", "security", "it"),
        }
        for categories, expected in table.items():
            with self.subTest(categories=categories):
                self.assertEqual(profile.legacy_fields(categories), expected)

    def test_all_tech_is_eleven_fields_in_field_order(self):
        self.assertEqual(len(self.ALL), 11)
        self.assertEqual(list(self.ALL), sorted(self.ALL, key=intern_vocab.FIELD_IDS.index))

    def test_us_only_drops_only_abroad(self):
        self.assertEqual(profile.legacy_locations(1), ("us", "unlisted", "remote_us"))
        self.assertEqual(profile.legacy_locations(0), ("us", "unlisted", "remote_us", "abroad"))
        self.assertEqual(profile.legacy_locations(None), ("us", "unlisted", "remote_us", "abroad"))


class TermAfterGraduation(unittest.TestCase):
    def test_a_term_that_starts_after_graduating_is_flagged(self):
        june_2027 = mech_student(grad_year=2027, grad_month=6)

        self.assertFalse(profile.term_after_graduation(june_2027, 2027, 6))   # Summer 2027
        self.assertTrue(profile.term_after_graduation(june_2027, 2027, 9))    # Fall 2027
        self.assertFalse(profile.term_after_graduation(june_2027, 2027, None))  # "2027"

    def test_without_a_full_graduation_date_nothing_is_flagged(self):
        self.assertFalse(profile.term_after_graduation(fresh(), 2030, 9))
        self.assertFalse(profile.term_after_graduation(fresh(grad_year=2025), 2030, 9))


if __name__ == "__main__":
    unittest.main()
