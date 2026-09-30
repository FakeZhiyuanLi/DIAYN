"""
The internship finder's closed vocabularies, and the place tables behind "where".

    python3 -m unittest discover -s tests      # no install needed

Every id a profile may hold comes from `intern_vocab`: a field, a level, a
location preset, a degree. The card's selects are built from these tuples, the
worker's output is checked against them, and the store writes nothing else. So
most of what is pinned here is shape: counts that must fit a Discord select,
ids that must stay unique, tables that must agree with each other. The rest is
the two text cleaners every classifier and every typed keyword pass through.

`intern_places` is only data, ported verbatim from the measured reference; the
tests for it check that its tables are consistent with one another and with the
presets that point into them, since a preset naming a metro that has no cities
is a filter that silently matches nothing.

Both modules are pure, so this file needs nothing installed.
"""

import ast
import pathlib
import re
import unittest

import intern_places
import intern_vocab as vocab

TESTS_BOT = pathlib.Path(__file__).resolve().parent
BOT = TESTS_BOT.parents[1] / "bot"

#: Discord's ceilings: 25 options in one select, 100 characters in a label.
SELECT_OPTIONS = 25
OPTION_LABEL = 100

#: The fifteen metros section 4.3.4 names, which are also the metro presets.
METROS = ("oc", "la", "sd", "ie", "bay", "sac", "sea", "pdx", "phx", "den",
          "chi", "dc", "nyc", "bos", "atl")


def ids(pairs):
    return [first for first, _ in pairs]


class SelectVocabularies(unittest.TestCase):
    """What the card's selects are built from, and the limits Discord puts on them."""

    def test_there_are_24_fields_with_unique_ids(self):
        self.assertEqual(len(vocab.FIELDS), 24)
        self.assertEqual(len(set(ids(vocab.FIELDS))), 24)

    def test_there_are_7_selectable_levels(self):
        self.assertEqual(len(vocab.LEVELS), 7)
        self.assertEqual(len(set(ids(vocab.LEVELS))), 7)

    def test_there_are_23_unique_location_presets(self):
        self.assertEqual(len(vocab.LOCATION_PRESETS), 23)
        self.assertEqual(len(set(ids(vocab.LOCATION_PRESETS))), 23)

    def test_every_select_fits_in_one_discord_select(self):
        for name in ("FIELDS", "LEVELS", "LOCATION_PRESETS", "ALERT_CHOICES",
                     "MIN_SCORE_CHOICES", "DEGREES"):
            with self.subTest(vocabulary=name):
                self.assertLessEqual(len(getattr(vocab, name)), SELECT_OPTIONS)

    def test_every_label_fits_in_a_select_option(self):
        pairs = (vocab.FIELDS + vocab.LEVELS + vocab.LOCATION_PRESETS
                 + vocab.ALERT_CHOICES + vocab.MIN_SCORE_CHOICES + vocab.DEGREES)
        for value, label in pairs:
            with self.subTest(value=value):
                self.assertTrue(label)
                self.assertLessEqual(len(label), OPTION_LABEL)

    def test_the_id_tuples_follow_their_select_order(self):
        # The card preselects by id and renders by label; an id tuple that
        # drifted from its pairs would put a tick against the wrong option.
        self.assertEqual(vocab.FIELD_IDS, tuple(ids(vocab.FIELDS)))
        self.assertEqual(vocab.LEVEL_IDS, tuple(ids(vocab.LEVELS)))
        self.assertEqual(vocab.LOCATION_PRESET_IDS, tuple(ids(vocab.LOCATION_PRESETS)))
        self.assertEqual(vocab.DEGREE_IDS, tuple(ids(vocab.DEGREES)))

    def test_fields_start_with_software_and_end_with_legal_policy(self):
        self.assertEqual(vocab.FIELDS[0], ("software", "Software engineering"))
        self.assertEqual(vocab.FIELDS[-1], ("legal_policy", "Legal, policy & compliance"))

    def test_location_presets_start_with_the_us_and_end_with_unlisted(self):
        self.assertEqual(vocab.LOCATION_PRESETS[0], ("us", "Anywhere in the US"))
        self.assertEqual(vocab.LOCATION_PRESETS[-1],
                         ("unlisted", "Include roles that don't list a location"))

    def test_degrees_are_the_six_the_profile_table_allows(self):
        # intern_profiles.degree has a CHECK over exactly these ids; a seventh
        # here would be a draft the store refuses to write.
        self.assertEqual(set(vocab.DEGREE_IDS),
                         {"associate", "bachelor", "master", "mba", "pharmd", "phd"})


class Labels(unittest.TestCase):
    def test_every_field_has_a_label_and_so_does_engineering_general(self):
        for field in vocab.FIELD_IDS + ("engineering_general",):
            with self.subTest(field=field):
                self.assertIn(field, vocab.FIELD_LABELS)
        self.assertEqual(vocab.FIELD_LABELS["engineering_general"], "Engineering (general)")

    def test_engineering_general_is_never_selectable(self):
        self.assertNotIn("engineering_general", vocab.FIELD_IDS)

    def test_every_level_has_a_label_including_the_two_never_offered(self):
        for level in vocab.LEVEL_IDS + ("experienced", "excluded"):
            with self.subTest(level=level):
                self.assertIn(level, vocab.LEVEL_LABELS)

    def test_experienced_and_excluded_are_never_offered(self):
        self.assertNotIn("experienced", vocab.LEVEL_IDS)
        self.assertNotIn("excluded", vocab.LEVEL_IDS)

    def test_hourly_is_offered_under_its_own_label(self):
        self.assertEqual(vocab.LEVEL_LABELS["hourly"],
                         "Part-time & hourly (store, pharmacy tech)")


class Subsets(unittest.TestCase):
    """Every named group is drawn from the vocabulary it claims to belong to."""

    def test_engineering_fields_are_the_seven_disciplines(self):
        self.assertEqual(vocab.ENGINEERING_FIELDS, frozenset({
            "software", "electrical", "mechanical", "aerospace", "manufacturing",
            "civil", "chem_materials"}))

    def test_hourly_hint_fields_are_pharmacy_healthcare_and_biology(self):
        self.assertEqual(vocab.HOURLY_HINT_FIELDS,
                         frozenset({"pharmacy", "healthcare", "biology_lab"}))

    def test_group_members_all_exist(self):
        cases = (
            (vocab.ENGINEERING_FIELDS, vocab.FIELD_IDS),
            (vocab.HOURLY_HINT_FIELDS, vocab.FIELD_IDS),
            (vocab.EARLY_CAREER, vocab.LEVEL_IDS),
            (vocab.GRAD_DEGREES, vocab.DEGREE_IDS),
            (vocab.DEFAULT_LEVELS, vocab.LEVEL_IDS),
            (vocab.DEFAULT_LOCATIONS, vocab.LOCATION_PRESET_IDS),
        )
        for members, universe in cases:
            with self.subTest(members=members):
                self.assertTrue(set(members) <= set(universe))

    def test_early_career_leaves_out_hourly_and_unspecified(self):
        self.assertNotIn("hourly", vocab.EARLY_CAREER)
        self.assertNotIn("unspecified", vocab.EARLY_CAREER)

    def test_defaults_are_internships_and_anywhere_in_the_us(self):
        # D1: the default is never derived from the resume.
        self.assertEqual(vocab.DEFAULT_LEVELS, ("intern", "coop"))
        self.assertEqual(vocab.DEFAULT_LOCATIONS, ("us", "unlisted"))


class Adjacency(unittest.TestCase):
    def test_every_key_and_target_is_a_field(self):
        for source, targets in vocab.FIELD_ADJACENT.items():
            with self.subTest(source=source):
                self.assertIn(source, vocab.FIELD_IDS)
                for target in targets:
                    self.assertIn(target, vocab.FIELD_IDS)

    def test_nothing_is_adjacent_into_pharmacy(self):
        # D10: a Pharmacy Intern role needs pharmacy-school enrolment, so no
        # biology or healthcare profile may be lent weight toward one.
        for source, targets in vocab.FIELD_ADJACENT.items():
            with self.subTest(source=source):
                self.assertNotIn("pharmacy", targets)

    def test_every_weight_is_strictly_between_zero_and_one(self):
        for source, targets in vocab.FIELD_ADJACENT.items():
            for target, weight in targets.items():
                with self.subTest(source=source, target=target):
                    self.assertGreater(weight, 0)
                    self.assertLess(weight, 1)

    def test_no_field_is_adjacent_to_itself(self):
        for source, targets in vocab.FIELD_ADJACENT.items():
            with self.subTest(source=source):
                self.assertNotIn(source, targets)

    def test_every_field_lends_weight_somewhere(self):
        self.assertEqual(set(vocab.FIELD_ADJACENT), set(vocab.FIELD_IDS))

    def test_engineering_general_is_not_in_the_table(self):
        # It gets 1.0 from any engineering profile instead (4.2.3).
        self.assertNotIn("engineering_general", vocab.FIELD_ADJACENT)

    def test_two_measured_weights(self):
        self.assertEqual(vocab.FIELD_ADJACENT["mechanical"]["aerospace"], 0.7)
        self.assertEqual(vocab.FIELD_ADJACENT["business_ops"]["hr"], 0.3)


class ValidIds(unittest.TestCase):
    def test_unknown_non_string_and_duplicate_ids_are_dropped_in_order(self):
        # Arrange
        given = ["data_ml", "nope", 3, None, "software", "data_ml", ["software"]]

        # Act
        kept = vocab.valid_ids("field", given)

        # Assert
        self.assertEqual(kept, ("data_ml", "software"))

    def test_the_input_is_left_unchanged(self):
        given = ["software", "software", "nope"]
        snapshot = list(given)

        vocab.valid_ids("field", given)

        self.assertEqual(given, snapshot)

    def test_location_accepts_a_state_token(self):
        self.assertEqual(vocab.valid_ids("location", ["us", "st:CA", "st:DC"]),
                         ("us", "st:CA", "st:DC"))

    def test_location_rejects_an_unknown_or_lower_case_state(self):
        self.assertEqual(vocab.valid_ids("location", ["st:ZZ", "st:ca", "st:", "ca"]),
                         ("ca",))

    def test_each_kind_uses_its_own_vocabulary(self):
        cases = (
            ("field", ["software", "intern", "engineering_general"], ("software",)),
            ("level", ["intern", "hourly", "experienced", "software"], ("intern", "hourly")),
            ("location", ["oc", "socal", "st:WA", "hourly"], ("oc", "socal", "st:WA")),
            ("degree", ["bachelor", "phd", "none", "diploma"], ("bachelor", "phd")),
        )
        for kind, given, expected in cases:
            with self.subTest(kind=kind):
                self.assertEqual(vocab.valid_ids(kind, given), expected)

    def test_an_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            vocab.valid_ids("colour", ["software"])

    def test_something_that_is_not_a_list_keeps_nothing(self):
        # Read back from a JSON column, so it can hold anything. A string is
        # iterable, and "it" is a field id; neither may leak through.
        for given in (None, "software", b"software", 7):
            with self.subTest(given=given):
                self.assertEqual(vocab.valid_ids("field", given), ())

    def test_any_iterable_is_accepted(self):
        self.assertEqual(vocab.valid_ids("level", (i for i in ("coop", "entry"))),
                         ("coop", "entry"))


class CleanKeyword(unittest.TestCase):
    def test_spaces_are_stripped_collapsed_and_lower_cased(self):
        self.assertEqual(vocab.clean_keyword("  Urban   Planning "), "urban planning")

    def test_one_character_is_too_short(self):
        self.assertIsNone(vocab.clean_keyword("x"))

    def test_thirty_characters_fit_and_thirty_one_do_not(self):
        self.assertEqual(vocab.clean_keyword("a" * 30), "a" * 30)
        self.assertIsNone(vocab.clean_keyword("a" * 31))

    def test_control_characters_are_removed(self):
        self.assertEqual(vocab.clean_keyword("lab\x00 safety\x7f"), "lab safety")

    def test_a_tab_or_newline_reads_as_a_space(self):
        self.assertEqual(vocab.clean_keyword("lab\tsafety\n"), "lab safety")

    def test_the_punctuation_skills_use_is_kept(self):
        for word in ("c++", "c#", "node.js", "r&d", "tcp/ip", "gd-t"):
            with self.subTest(word=word):
                self.assertEqual(vocab.clean_keyword(word.upper()), word)

    def test_anything_outside_the_keyword_alphabet_is_refused(self):
        for text in ("<@123>", "drop;table", "naïve", "", "   ", "a\u200b"):
            with self.subTest(text=text):
                self.assertIsNone(vocab.clean_keyword(text))

    def test_something_that_is_not_text_is_refused(self):
        for value in (None, 12, ["python"]):
            with self.subTest(value=value):
                self.assertIsNone(vocab.clean_keyword(value))


class NormText(unittest.TestCase):
    def test_zero_width_characters_are_removed(self):
        self.assertEqual(vocab.norm_text("Soft\u200bware\u200c \u200dIn\u2060tern\ufeff"),
                         "Software Intern")

    def test_en_em_and_figure_dashes_become_hyphens(self):
        self.assertEqual(vocab.norm_text("Intern – Summer — 2027 ‒ CA"),
                         "Intern - Summer - 2027 - CA")

    def test_nfkc_is_applied(self):
        self.assertEqual(vocab.norm_text("ﬁnance ＩＴ"), "finance IT")

    def test_whitespace_is_collapsed_and_stripped(self):
        self.assertEqual(vocab.norm_text("  Data \tScience \n Intern  "),
                         "Data Science Intern")

    def test_none_is_the_empty_string(self):
        self.assertEqual(vocab.norm_text(None), "")

    def test_never_raises_on_something_that_is_not_text(self):
        for value in (0, 7, b"bytes", ["x"]):
            with self.subTest(value=value):
                self.assertEqual(vocab.norm_text(value), "")


class LegacyMap(unittest.TestCase):
    """The old tracker's categories, mapped onto fields for the one-off migration."""

    def test_every_legacy_category_is_covered(self):
        self.assertEqual(set(vocab.LEGACY_CATEGORY_FIELDS),
                         {"swe", "data-ml", "hardware", "quant", "pm", "other"})

    def test_all_tech_has_eleven_fields_in_select_order(self):
        self.assertEqual(len(vocab.LEGACY_ALL_TECH), 11)
        order = [vocab.FIELD_IDS.index(f) for f in vocab.LEGACY_ALL_TECH]
        self.assertEqual(order, sorted(order))

    def test_all_tech_keeps_quant_product_aerospace_and_manufacturing(self):
        # D20: the earlier draft of this map dropped these and silently shrank
        # what a migrated subscriber had signed up for.
        for field in ("quant", "product", "aerospace", "manufacturing"):
            with self.subTest(field=field):
                self.assertIn(field, vocab.LEGACY_ALL_TECH)

    def test_the_table_from_section_3_3(self):
        self.assertEqual(vocab.LEGACY_CATEGORY_FIELDS["swe"], ("software", "security", "it"))
        self.assertEqual(vocab.LEGACY_CATEGORY_FIELDS["data-ml"], ("data_ml", "analytics"))
        self.assertEqual(vocab.LEGACY_CATEGORY_FIELDS["hardware"],
                         ("electrical", "mechanical", "aerospace", "manufacturing"))
        self.assertEqual(vocab.LEGACY_CATEGORY_FIELDS["quant"], ("quant",))
        self.assertEqual(vocab.LEGACY_CATEGORY_FIELDS["pm"], ("product", "business_ops"))
        self.assertEqual(vocab.LEGACY_CATEGORY_FIELDS["other"], vocab.LEGACY_ALL_TECH)

    def test_every_mapped_field_exists(self):
        for category, fields in vocab.LEGACY_CATEGORY_FIELDS.items():
            with self.subTest(category=category):
                self.assertTrue(set(fields) <= set(vocab.FIELD_IDS))


class AlertChoices(unittest.TestCase):
    CADENCES = ("hourly", "daily", "weekly", "off")

    def test_a_value_is_the_cadence_and_the_hour(self):
        self.assertEqual(vocab.alert_choice_value("daily", 17), "daily:17")

    def test_every_value_round_trips(self):
        for cadence in self.CADENCES:
            for hour in range(24):
                with self.subTest(cadence=cadence, hour=hour):
                    value = vocab.alert_choice_value(cadence, hour)
                    self.assertEqual(vocab.parse_alert_choice(value), (cadence, hour))

    def test_every_listed_choice_parses(self):
        for value, _label in vocab.ALERT_CHOICES:
            with self.subTest(value=value):
                self.assertIsNotNone(vocab.parse_alert_choice(value))

    def test_anything_else_is_not_a_choice(self):
        # Only what alert_choice_value produces: a select value arrives from
        # the client, and a stored cadence has a CHECK behind it.
        for value in ("daily", "daily:24", "daily:-1", "monthly:9", "Daily:9",
                      "daily: 9", "daily:9:0", "daily:09", "daily:nine", "", None,
                      9, True):
            with self.subTest(value=value):
                self.assertIsNone(vocab.parse_alert_choice(value))

    def test_a_listed_choice_has_its_listed_label(self):
        for value, label in vocab.ALERT_CHOICES:
            with self.subTest(value=value):
                cadence, hour = vocab.parse_alert_choice(value)
                self.assertEqual(vocab.alert_choice_label(cadence, hour), label)

    def test_an_hour_set_with_ping_gets_a_label_of_its_own(self):
        self.assertEqual(vocab.alert_choice_label("daily", 7), "Daily at 7am Pacific")
        self.assertEqual(vocab.alert_choice_label("weekly", 19),
                         "Weekly, Mondays at 7pm Pacific")

    def test_midnight_and_noon_read_as_twelve(self):
        self.assertEqual(vocab.alert_choice_label("daily", 0), "Daily at 12am Pacific")
        self.assertEqual(vocab.alert_choice_label("daily", 12), "Daily at 12pm Pacific")

    def test_the_hour_means_nothing_to_hourly_or_off(self):
        self.assertEqual(vocab.alert_choice_label("hourly", 7),
                         "Hourly (at most one DM an hour)")
        self.assertEqual(vocab.alert_choice_label("off", 7), "Off")

    def test_an_impossible_cadence_or_hour_raises(self):
        for cadence, hour in (("monthly", 9), ("daily", 24), ("daily", -1), ("daily", "9")):
            with self.subTest(cadence=cadence, hour=hour):
                with self.assertRaises(ValueError):
                    vocab.alert_choice_label(cadence, hour)

    def test_min_score_choices_are_the_three_the_table_allows(self):
        # intern_profiles.min_score has CHECK (min_score IN (45, 60, 75)).
        self.assertEqual([score for score, _ in vocab.MIN_SCORE_CHOICES], [75, 60, 45])
        self.assertIn("recommended", dict(vocab.MIN_SCORE_CHOICES)[60])


class Caps(unittest.TestCase):
    def test_the_caps_section_6_fixes(self):
        self.assertEqual(
            (vocab.MAX_FIELDS, vocab.MAX_MAJORS, vocab.MAX_MINORS, vocab.MAX_SKILLS,
             vocab.MAX_KEYWORDS, vocab.MAX_TERMS, vocab.MAX_STATES,
             vocab.MAX_COMPANIES_ONLY, vocab.MAX_COMPANIES_HIDDEN),
            (6, 3, 2, 40, 10, 4, 10, 20, 30))

    def test_the_keyword_pattern(self):
        self.assertEqual(vocab.KEYWORD_RE.pattern, r"^[a-z0-9+#./& -]{2,30}$")


class PlaceTables(unittest.TestCase):
    """`intern_places` agrees with itself and with the presets that point into it."""

    def test_there_are_fifteen_metros(self):
        self.assertEqual(set(intern_places.METRO_CITIES), set(METROS))

    def test_every_metro_has_a_home_state(self):
        self.assertEqual(set(intern_places.METRO_STATE), set(METROS))
        for metro, state in intern_places.METRO_STATE.items():
            with self.subTest(metro=metro):
                self.assertIn(state, intern_places.US_STATES)

    def test_only_three_metros_span_states(self):
        self.assertEqual(intern_places.METRO_EXTRA_STATES,
                         {"pdx": {"WA"}, "dc": {"VA", "MD"}, "nyc": {"NJ", "CT"}})

    def test_every_metro_preset_names_a_metro(self):
        # A preset whose metro had no cities would match nothing and say nothing.
        presets = set(vocab.LOCATION_PRESET_IDS)
        self.assertTrue(set(METROS) <= presets)

    def test_southern_california_is_four_metros_plus_extra_cities(self):
        self.assertEqual(intern_places.SOCAL_METROS, frozenset({"oc", "la", "sd", "ie"}))
        self.assertIn("santa barbara", intern_places.SOCAL_EXTRA_CITIES)

    def test_metro_cities_are_lower_case_and_trimmed(self):
        for metro, cities in intern_places.METRO_CITIES.items():
            with self.subTest(metro=metro):
                self.assertIsInstance(cities, frozenset)
                for city in cities:
                    self.assertEqual(city, city.lower().strip())

    def test_states_include_dc_and_puerto_rico(self):
        self.assertEqual(len(intern_places.US_STATES), 52)
        self.assertEqual(intern_places.US_STATES["DC"], "district of columbia")
        self.assertEqual(intern_places.US_STATES["PR"], "puerto rico")

    def test_state_names_map_back_to_codes(self):
        for code, name in intern_places.US_STATES.items():
            with self.subTest(code=code):
                self.assertEqual(intern_places.STATE_BY_NAME[name], code)
        self.assertEqual(intern_places.STATE_BY_NAME["washington dc"], "DC")
        self.assertEqual(intern_places.STATE_BY_NAME["washington d.c."], "DC")

    def test_province_codes_are_the_province_values(self):
        self.assertEqual(intern_places.CA_PROV_CODES,
                         set(intern_places.CA_PROVINCES.values()))

    def test_country_codes_are_two_upper_case_letters(self):
        tables = (intern_places.ISO3, intern_places.COUNTRIES,
                  intern_places.NON_US_CITIES, intern_places.NON_US_REGIONS)
        for table in tables:
            for key, code in table.items():
                with self.subTest(key=key):
                    self.assertRegex(code, r"^[A-Z]{2}$")

    def test_us_cities_sit_in_real_states(self):
        for city, state in intern_places.US_CITIES.items():
            with self.subTest(city=city):
                self.assertIn(state, intern_places.US_STATES)

    def test_metro_phrases_compile_and_point_at_real_places(self):
        for pattern, metro, state in intern_places.METRO_PHRASES:
            with self.subTest(pattern=pattern):
                re.compile(pattern, re.I)
                self.assertIn(metro, set(METROS) | {""})
                self.assertIn(state, intern_places.US_STATES)
                if metro:
                    self.assertEqual(intern_places.METRO_STATE[metro], state)

    def test_irvine_is_orange_county(self):
        self.assertIn("irvine", intern_places.METRO_CITIES["oc"])
        self.assertEqual(intern_places.US_CITIES["irvine"], "CA")


def module_imports(name: str) -> set:
    tree = ast.parse((BOT / f"{name}.py").read_text(encoding="utf-8"))
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").split(".")[0])
    return found


class SourceHygiene(unittest.TestCase):
    """An invisible character in source makes a regex class look empty in review and in a
    diff; the finder writes every one of them as an escape."""

    INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff\uf0b7]")
    PATTERNS = ("intern_*.py", "resume_*.py", "message_pack.py", "test_intern_*.py",
                "test_resume_*.py", "test_message_pack.py")

    def test_no_finder_file_hides_an_invisible_character(self):
        paths = sorted({p for pattern in self.PATTERNS for folder in (BOT, TESTS_BOT)
                        for p in folder.glob(pattern)})
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(file=path.name):
                self.assertIsNone(self.INVISIBLE.search(path.read_text(encoding="utf-8")))


class Purity(unittest.TestCase):
    """Both modules import under bare `python3`: stdlib and each other only."""

    def test_places_imports_nothing(self):
        self.assertEqual(module_imports("intern_places"), set())

    def test_vocab_imports_only_the_stdlib_and_places(self):
        allowed = {"__future__", "collections", "re", "typing", "unicodedata",
                   "intern_places"}
        self.assertTrue(module_imports("intern_vocab") <= allowed,
                        module_imports("intern_vocab") - allowed)


if __name__ == "__main__":
    unittest.main()
