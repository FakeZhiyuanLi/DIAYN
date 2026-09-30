"""
The title classifiers: what level a posting is, what field it is in, what term
it names, and which postings are one role listed many times.

    python3 -m unittest discover -s tests      # no install needed

Every row the finder shows passes through these functions, and they only ever
see the stored `title` (and `company`) strings, so the tests are tables of real
titles and what they must come out as. The tables are the spec's (sections
4.1.5, 4.2.2 and 8.1), which were run against a month of real postings; a row
that fails here is a posting a student is now shown under the wrong level, or
not shown at all.

The module is pure, so this file needs nothing installed. The company key is
compared with the scraper's through the contract fixture,
`contract/company_norm_cases.json`, which the scraper's own tests hold its `_norm` to:
the two halves agree by both passing the same cases.
"""

import ast
import hashlib
import json
import pathlib
import unittest

import intern_places
import intern_taxonomy as tax
import intern_vocab

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "contract"

#: Section 4.1.5, one title per row, each exactly as the spec writes it. Real
#: titles contain " / " themselves (the long FLDP row is ONE title), so rows
#: are never joined with a separator and split back apart.
LEVEL_FIXTURES = (
    ("Operations Manager Intern", "intern"),
    ("Internal Audit Intern", "intern"),
    ("International Tax Intern", "intern"),
    ("Pharmacy Intern", "intern"),
    ("Foreign Pharmacy Graduate - International Pharmacy Intern", "excluded"),
    ("Engineering Co-op (Spring 2027)", "coop"),
    ("12 Month Placement - Finance", "coop"),
    ("Summer Analyst - Investment Banking", "intern"),
    ("Werkstudent Software", "intern"),
    ("Intern Program Manager", "experienced"),
    ("Associate Director, Finance", "experienced"),
    ("Software Engineer II", "experienced"),
    ("Technical Fellow", "experienced"),
    ("Senior FP&A Manager / Finance Leadership Development Program (FLDP) - "
     "Minneapolis, MN / Hybrid (2027)", "experienced"),
    ("Software Engineer, New Grad 2027", "new_grad"),
    ("Area Manager - New Grad", "new_grad"),
    ("Leadership Development Program - Finance", "new_grad"),
    ("FLDP", "new_grad"),
    ("Humana AI Fellow", "new_grad"),
    ("Grad Pharmacist", "new_grad"),
    ("Associate Engineer", "entry"),
    ("Mechanical Engineer I", "entry"),
    ("Research Associate I", "entry"),
    ("Associate Product Manager", "entry"),
    ("Java Software Engineer (Associate, Experienced or Senior) - Bixby", "entry"),
    ("Manufacturing Engineer Apprentice", "apprentice"),
    ("Apprentice - Sales Analytics", "apprentice"),
    ("Store Associate", "hourly"),
    ("Pharmacy Technician", "hourly"),
    ("Shift Supervisor", "hourly"),
    ("Store Manager in Training", "hourly"),
    ("Welder Apprentice", "hourly"),
    ("Customer Service Representative I", "hourly"),
    ("Medical Scribe", "hourly"),
    ("Target Security Specialist", "hourly"),
    ("Production Technician", "hourly"),
    ("Analyst", "unspecified"),
    ("Financial Analyst", "unspecified"),
    ("Associate", "unspecified"),
    ("University Recruiter", "unspecified"),
    ("Design Engineer", "unspecified"),
    ("Staff Pharmacist - Part-time", "experienced"),
    ("Talent Community - Engineering", "excluded"),
    ("Military SkillBridge Fellow", "excluded"),
)

#: Section 8.1's additions to that table.
MORE_LEVEL_FIXTURES = (
    ("PhD Intern - Machine Learning", "intern"),
    ("2027 Supply Chain Rotational Program", "new_grad"),
    ("Early Career Mechanical Engineer", "new_grad"),
    ("Data Analyst III", "experienced"),
    ("Engineering Manager", "experienced"),
    ("Senior Associate", "experienced"),
    ("Pharmacy Technician Per Diem", "hourly"),
    ("Summer Research Fellowship", "new_grad"),
    ("Distribution Center Operations Manager Intern", "intern"),
    ("", "unspecified"),
)

#: Section 4.2.2's field fixtures.
FIELD_FIXTURES = (
    ("Design Engineer", (("mechanical", 1.0),)),
    ("Target Security Specialist", ()),
    ("Space Communications Engineer", (("engineering_general", 0.6),)),
    ("ASIC Synthesis Engineer", (("electrical", 1.0),)),
    ("Turbomachinery Engineer", (("mechanical", 1.0),)),
    ("GNC Engineer", (("aerospace", 1.0),)),
    ("Data Scientist", (("data_ml", 1.0),)),
    ("Cybersecurity Analyst Intern", (("security", 1.0),)),
    ("Operations Intern", (("business_ops", 0.5),)),
    ("Engineering Intern", (("engineering_general", 0.6),)),
    ("Clinical Research Coordinator", (("biology_lab", 1.0), ("healthcare", 1.0))),
    ("Pharmacy Technician", (("pharmacy", 1.0),)),
)

#: Every function section 6.5 says is cached. The finder classifies the whole
#: 30-day window on each load; uncached, a cold load is seconds per user.
CACHED = ("classify_level", "is_hourly", "bucket", "title_fields", "term_of",
          "requires_grad", "clearance_note", "role_key", "clone_key")


def store_executive_titles(count):
    """Target's regional store programme, one title per city, as the board lists it."""
    cities = [(city.title(), state) for city, state in intern_places.US_CITIES.items()
              if "," not in city][:count]
    return [f"Store Executive Intern (Store Leadership Intern) - {city}, {state} "
            "(Starting Summer 2027)" for city, state in cities]


def norm_cases() -> list[dict]:
    """The contract's company-key cases (B7): input and the key both sides must produce."""
    return json.loads((FIXTURES / "company_norm_cases.json").read_text(encoding="utf-8"))["norm"]


class LevelTable(unittest.TestCase):
    def test_the_spec_table_has_44_single_title_rows(self):
        # Pinned so nobody "tidies" the table by joining rows with " / ",
        # which is how the long FLDP title was once misread as three titles.
        self.assertEqual(len(LEVEL_FIXTURES), 44)

    def test_every_spec_fixture_lands_in_its_bucket(self):
        for title, want in LEVEL_FIXTURES:
            with self.subTest(title=title):
                self.assertEqual(tax.bucket(title), want)

    def test_the_further_fixtures_land_in_their_bucket(self):
        for title, want in MORE_LEVEL_FIXTURES:
            with self.subTest(title=title):
                self.assertEqual(tax.bucket(title), want)

    def test_a_missing_title_is_unspecified(self):
        for title in (None, "", "   ", "\u200b"):
            with self.subTest(title=title):
                self.assertEqual(tax.classify_level(title), ("unspecified", None))
                self.assertEqual(tax.bucket(title), "unspecified")


class LevelEvidence(unittest.TestCase):
    def test_evidence_is_the_matched_text(self):
        tag = tax.classify_level("Operations Manager Intern")
        self.assertEqual(tag, tax.LevelTag("intern", "Intern"))

    def test_the_tag_names_its_two_parts(self):
        tag = tax.classify_level("Software Engineer II")
        self.assertEqual((tag.level, tag.evidence), ("experienced", "II"))

    def test_a_range_span_is_entry_level_with_the_span_as_evidence(self):
        # "(Associate, Experienced or Senior)" names a range whose bottom is
        # entry level; the "Senior" inside it must not make it experienced.
        tag = tax.classify_level(
            "Java Software Engineer (Associate, Experienced or Senior) - Bixby")
        self.assertEqual(tag, ("entry", "(Associate, Experienced or Senior)"))

    def test_an_intern_is_an_intern_whatever_seniority_word_it_carries(self):
        self.assertEqual(tax.classify_level("Senior Software Engineer Intern").level, "intern")

    def test_intern_staff_are_not_interns(self):
        for title in ("Intern Program Manager", "Internship Coordinator", "Internist"):
            with self.subTest(title=title):
                self.assertNotEqual(tax.classify_level(title).level, "intern")

    def test_a_recruiter_for_new_grads_is_not_a_new_grad_role(self):
        self.assertNotEqual(tax.classify_level("New Grad Recruiter").level, "new_grad")

    def test_text_is_normalised_before_it_is_read(self):
        # A zero-width space pasted into a word would otherwise hide "Intern"
        # from every pattern, and the role would drop out of the student view.
        self.assertEqual(tax.classify_level("Pharmacy In\u200btern").level, "intern")


class Hourly(unittest.TestCase):
    def test_professional_titles_with_a_soft_hourly_word_are_not_hourly(self):
        # Section 4.1.3's guard: "crew", a shift phrase and "security
        # specialist" also turn up in engineering and cyber titles.
        cases = (
            ("Mechanical Engineer, Cabin Structures (Crew Starship)", "unspecified"),
            ("MP&P Engineer 2nd shift (Composite)", "unspecified"),
            ("Cloud Security Specialist", "unspecified"),
            ("Security Specialist, SOC", "unspecified"),
            ("Test & Evaluation Engineer (Associate or Experienced) (2nd Shift)", "entry"),
        )
        for title, want in cases:
            with self.subTest(title=title):
                self.assertEqual(tax.bucket(title), want)

    def test_frontline_titles_stay_hourly_under_the_guard(self):
        for title in ("Target Security Specialist", "Crew Member",
                      "Warehouse Associate 2nd shift", "Maintenance Technician 2nd Shift",
                      "Production Technician - 2nd Shift", "Security Officer"):
            with self.subTest(title=title):
                self.assertEqual(tax.bucket(title), "hourly")

    def test_student_levels_are_never_hourly(self):
        for level in ("intern", "coop", "new_grad", "excluded"):
            with self.subTest(level=level):
                self.assertFalse(tax.is_hourly("Pharmacy Technician", level))

    def test_a_licensed_role_is_never_hourly(self):
        self.assertFalse(tax.is_hourly("Staff Pharmacist - Part-time", "experienced"))
        self.assertFalse(tax.is_hourly("Registered Nurse - Per Diem", "unspecified"))

    def test_licensed_clinicians_named_by_abbreviation_are_experienced_not_hourly(self):
        # D10. CVS lists its nurse practitioners as "NP or PA" and its triage
        # nurses without "registered"; the part-time and per-diem ones were
        # read as hourly and offered to biology majors as a Strong match.
        titles = (
            ": In-Home Health - NP or PA (Part Time) - Newark/ Sussex, NJ",
            "In-Home Health -  NP or PA (Per Diem)- Barnstable, MA",
            "In-Home NP/PA (Part Time) - Wise, VA",
            "Advanced Practice Provider - FNP/PA",
            "Clinical Call Center Triage Nurse",
            "Urgent Care PA-C - Per Diem",
            "CRNA - Part Time",
            "APRN - PRN",
            "Behavioral Health Specialist Requires LCSW LPC or LMFT - Part Time",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(tax.bucket(title), "experienced")

    def test_a_state_or_shift_abbreviation_is_not_read_as_a_licence(self):
        # "PA" alone is Pennsylvania, "PT" is part time and "RT" is
        # radiographic testing: none of them may lift a frontline role out
        # of hourly.
        for title in ("Store Associate - Pittsburgh, PA", "PT Pharmacy Technician",
                      "Nondestructive Test (NDT) Technician - UT & RT"):
            with self.subTest(title=title):
                self.assertEqual(tax.bucket(title), "hourly")

    def test_unlicensed_roles_that_say_nurse_are_not_licensed(self):
        # A nurse aide is the CNA job the hourly table already names; a
        # student nurse and a nurse recruiter hold no licence either.
        self.assertEqual(tax.bucket("Nurse Aide - Part Time"), "hourly")
        for title in ("Nurse Aide", "Student Nurse Extern", "Nurse Recruiter"):
            with self.subTest(title=title):
                self.assertNotEqual(tax.classify_level(title).level, "experienced")

    def test_a_technician_is_hourly_unless_the_title_says_engineer(self):
        self.assertTrue(tax.is_hourly("Maintenance Technician", "unspecified"))
        self.assertFalse(tax.is_hourly("Engineering Technician", "unspecified"))

    def test_the_bucket_examples_of_section_4_1_4(self):
        cases = (("Store Associate", "hourly"), ("Shift Supervisor", "hourly"),
                 ("Electrician Apprentice", "hourly"),
                 ("Manufacturing Engineer Apprentice", "apprentice"),
                 ("Staff Pharmacist - Part-time", "experienced"))
        for title, want in cases:
            with self.subTest(title=title):
                self.assertEqual(tax.bucket(title), want)


class Fields(unittest.TestCase):
    def test_every_spec_field_fixture(self):
        for title, want in FIELD_FIXTURES:
            with self.subTest(title=title):
                self.assertEqual(tax.title_fields(title), want)

    def test_an_engineering_word_beats_weak_evidence(self):
        self.assertEqual(tax.title_fields("2027 Summer Corporate Intern - Engineering"),
                         (("engineering_general", 0.6),))

    def test_business_operations_is_strong_and_operations_alone_is_weak(self):
        self.assertEqual(tax.title_fields("Business Operations Intern"), (("business_ops", 1.0),))
        self.assertEqual(tax.title_fields("Operations Intern"), (("business_ops", 0.5),))

    def test_results_are_sorted_by_confidence_then_id(self):
        for title in ("Business Data Associate", "Clinical Research Coordinator",
                      "Software Engineer Intern, Machine Learning", "Marketing Analytics Intern"):
            with self.subTest(title=title):
                got = tax.title_fields(title)
                self.assertTrue(got)
                self.assertEqual(list(got), sorted(got, key=lambda kv: (-kv[1], kv[0])))

    def test_several_weak_fields_come_back_in_id_order(self):
        self.assertEqual(tax.title_fields("Business Data Associate"),
                         (("analytics", 0.5), ("business_ops", 0.5), ("data_ml", 0.5)))

    def test_a_place_in_the_title_is_not_read_as_a_field(self):
        # "Plant" is a manufacturing word; "Plant City, FL" is a place.
        self.assertEqual(tax.title_fields("Store Intern - Plant City, FL"), ())

    def test_a_subsidiary_suffix_is_not_read_as_a_field(self):
        self.assertEqual(tax.title_fields("Mechanical Engineer - Starship"),
                         (("mechanical", 1.0),))

    def test_satellite_communications_are_not_marketing(self):
        self.assertNotIn("marketing",
                         dict(tax.title_fields("Satellite Communications Engineer")))

    def test_a_battery_electrolyte_is_not_biology(self):
        self.assertNotIn("biology_lab", dict(tax.title_fields("Electrolyte Scientist")))

    def test_no_title_has_no_fields(self):
        for title in (None, ""):
            with self.subTest(title=title):
                self.assertEqual(tax.title_fields(title), ())

    def test_every_field_produced_is_a_known_id(self):
        known = set(intern_vocab.FIELD_LABELS)
        for title, _ in LEVEL_FIXTURES + MORE_LEVEL_FIXTURES:
            for field, conf in tax.title_fields(title):
                with self.subTest(title=title, field=field):
                    self.assertIn(field, known)
                    self.assertIn(conf, (1.0, 0.6, 0.5))


class TermOf(unittest.TestCase):
    def test_a_season_and_year(self):
        self.assertEqual(tax.term_of("Engineering Co-op (Spring 2027)"), ("Spring 2027", 2027, 3))

    def test_autumn_is_written_fall(self):
        self.assertEqual(tax.term_of("Autumn 2026 Intern"), ("Fall 2026", 2026, 9))

    def test_a_bare_year_has_no_start_month(self):
        self.assertEqual(tax.term_of("Software Engineer, New Grad 2027"), ("2027", 2027, None))

    def test_no_term(self):
        for title in ("Intern", "", None):
            with self.subTest(title=title):
                self.assertEqual(tax.term_of(title), (None, None, None))


class RequiresGrad(unittest.TestCase):
    def test_graduate_only_titles_name_who_they_are_for(self):
        cases = (
            ("2027 Summer Intern, MS/PhD, Data Science", "an MS/PhD student"),
            ("2027 PhD Quantitative Research Intern", "a PhD student"),
            ("MBA Intern, Finance", "an MBA student"),
            ("Graduate Analytics Internship", "a graduate student"),
            ("Master's Intern - Supply Chain", "a master's student"),
        )
        for title, want in cases:
            with self.subTest(title=title):
                self.assertEqual(tax.requires_grad(title), want)

    def test_titles_open_to_undergraduates(self):
        for title in ("2027 Summer Intern, BS/MS, Software", "Software Engineer Intern", None):
            with self.subTest(title=title):
                self.assertIsNone(tax.requires_grad(title))


class ClearanceNote(unittest.TestCase):
    def test_a_clearance(self):
        self.assertEqual(tax.clearance_note("Systems Engineer Intern (Secret Clearance)"),
                         "title mentions a security clearance")

    def test_citizenship(self):
        self.assertEqual(tax.clearance_note("Intern - US Citizenship Required"),
                         "title mentions US citizenship")

    def test_a_clearance_is_named_before_citizenship(self):
        self.assertEqual(tax.clearance_note("TS/SCI Intern - US Citizen"),
                         "title mentions a security clearance")

    def test_nothing_to_note(self):
        for title in ("Software Engineer Intern", None):
            with self.subTest(title=title):
                self.assertIsNone(tax.clearance_note(title))


class Grouping(unittest.TestCase):
    def test_distribution_centre_kinds_in_different_cities_share_a_role_key(self):
        a = tax.role_key("Target", "Operation Manager Intern (Starting Summer 2027) "
                                   "Food Distribution Center - Thornton, CO")
        b = tax.role_key("Target", "Operation Manager Intern (Starting Summer 2027) "
                                   "Regional Distribution Center - Pueblo, CO")
        self.assertEqual(a, b)

    def test_a_store_number_does_not_split_a_role(self):
        self.assertEqual(tax.role_key("CVS Health", "Store #1234 Pharmacy Intern"),
                         tax.role_key("CVS Health", "Pharmacy Intern"))

    def test_a_role_key_starts_with_the_normalised_company(self):
        self.assertTrue(tax.role_key("CVS Health", "Pharmacy Intern").startswith("cvshealth|"))

    def test_different_companies_never_share_a_key(self):
        self.assertNotEqual(tax.role_key("Target", "Pharmacy Intern"),
                            tax.role_key("CVS Health", "Pharmacy Intern"))
        self.assertNotEqual(tax.clone_key("Target", "Pharmacy Intern"),
                            tax.clone_key("CVS Health", "Pharmacy Intern"))

    def test_56_regional_store_titles_share_one_clone_key(self):
        titles = store_executive_titles(56)
        self.assertEqual(len(set(titles)), 56)
        keys = {tax.clone_key("Target", title) for title in titles}
        self.assertEqual(len(keys), 1)

    def test_a_clone_key_never_joins_two_levels(self):
        a = tax.clone_key("Acme", "Finance Intern")
        b = tax.clone_key("Acme", "Finance Intern (Accounting) - 12 Month Placement")
        self.assertNotEqual(a, b)
        self.assertEqual((a.rsplit("|", 1)[1], b.rsplit("|", 1)[1]), ("intern", "coop"))

    def test_missing_company_and_title_still_give_a_key(self):
        self.assertIsInstance(tax.role_key(None, None), str)
        self.assertIsInstance(tax.clone_key(None, None), str)


class GroupHash(unittest.TestCase):
    def test_sixteen_lower_case_hex_characters(self):
        self.assertRegex(tax.group_hash("target|pharmacyintern"), r"^[0-9a-f]{16}$")

    def test_stable_across_processes_and_releases(self):
        # Stored in the sent/hidden ledger; a hash that changed would re-send
        # every role a user has already been told about or has hidden.
        self.assertEqual(tax.group_hash("abc"), hashlib.sha1(b"abc").hexdigest()[:16])
        self.assertEqual(tax.group_hash("abc"), "a9993e364706816a")


class CompanyNorm(unittest.TestCase):
    def test_matches_the_contract_cases_the_scraper_is_held_to(self):
        cases = norm_cases()
        self.assertGreaterEqual(len(cases), 10)
        for case in cases:
            with self.subTest(name=case["input"]):
                self.assertEqual(tax.company_norm(case["input"]), case["norm"])


class Caching(unittest.TestCase):
    def test_every_classifier_is_cached(self):
        for name in CACHED:
            with self.subTest(function=name):
                self.assertTrue(hasattr(getattr(tax, name), "cache_info"))

    def test_the_cache_is_the_size_the_spec_sets(self):
        for name in CACHED:
            with self.subTest(function=name):
                self.assertEqual(getattr(tax, name).cache_info().maxsize, 65536)


class Purity(unittest.TestCase):
    def test_imports_only_the_stdlib_and_the_modules_the_graph_allows(self):
        # Section 6: the resume worker and a bare `python3` both import this.
        allowed = {"hashlib", "re", "functools", "typing", "collections", "intern_vocab",
                   "intern_location", "intern_places"}
        tree = ast.parse(pathlib.Path(tax.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, allowed)


if __name__ == "__main__":
    unittest.main()
