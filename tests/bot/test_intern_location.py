"""
Where a posting is, and whether that is somewhere the student asked for.

    python3 -m unittest discover -s tests      # no install needed

A posting's location is a free-text string from whichever board listed it:
`CA - Dublin`, `USA - El Segundo, CA`, `02551 - CVS Albany, L.L.C.`, `27
Locations`. `parse_location` turns that into places and a work mode;
`location_for` falls back to the title when the string says nothing (a Target
store programme lists `10 Locations` and puts the city in the title);
`location_fit` decides whether one of the student's location presets accepts
it, and says so in the words the Why line shows.

Most of this file is the spec's fixture table (section 8.1). The hard cases are
the ones a naive parser gets backwards — `Dublin, CA` is California and a bare
`Dublin` is Ireland; `IN-Bengaluru` is India and not Indiana — and each is a
student in one country being shown roles in another.

The module is pure, so this file needs nothing installed.
"""

import ast
import dataclasses
import pathlib
import unittest

import intern_location as loc
from intern_location import LocInfo, Place

#: The Where select's labels, as `describe_locations` joins them.
SEP = " · "


def us(city="", state=""):
    return Place(city, state, "US")


class ParseLocationTable(unittest.TestCase):
    """Section 8.1's table: input -> us, mode, unlisted, places, metros."""

    def check(self, raw, *, places=None, us_=None, mode="unknown", unlisted=0, metros=()):
        info = loc.parse_location(raw)
        if places is not None:
            self.assertEqual(info.places, tuple(places))
        if us_ is not None:
            self.assertEqual(info.us, us_)
        self.assertEqual(info.mode, mode)
        self.assertEqual(info.unlisted, unlisted)
        self.assertEqual(info.metros, frozenset(metros))

    def test_state_prefix_with_a_city_that_is_also_foreign(self):
        self.check("CA - Dublin", places=[us("Dublin", "CA")], us_="yes", metros={"bay"})
        self.check("TX - Paris", places=[us("Paris", "TX")], us_="yes")

    def test_street_address_takes_the_city_not_the_street(self):
        self.check("1800 State Hwy 5S, Amsterdam,NY", places=[us("Amsterdam", "NY")], us_="yes")
        self.check("1000 Nicollet Mall, Minneapolis,MN 55403-2542",
                   places=[us("Minneapolis", "MN")], us_="yes")

    def test_iso3_prefix(self):
        self.check("GBR - Bristol, UK", places=[Place("Bristol", "", "GB")], us_="no")
        self.check("USA - El Segundo, CA", places=[us("El Segundo", "CA")], us_="yes",
                   metros={"la"})

    def test_remote_in_two_countries_is_mixed(self):
        self.check("Remote, Canada; Remote, United States",
                   places=[Place("", "", "CA"), us()], us_="mixed", mode="remote")

    def test_a_workday_location_count(self):
        self.check("27 Locations", places=[], us_="unknown", unlisted=27)

    def test_remote_nationwide_is_the_us(self):
        self.check("Remote Nationwide", places=[us()], us_="yes", mode="remote")

    def test_city_and_country(self):
        self.check("Rayong, Thailand", places=[Place("Rayong", "", "TH")], us_="no")

    def test_a_state_prefix_with_work_from_home(self):
        self.check("AZ - Work from home", places=[us("", "AZ")], us_="yes", mode="remote")

    def test_city_and_state_finds_the_metro(self):
        self.check("Irvine, CA", places=[us("Irvine", "CA")], us_="yes", metros={"oc"})
        self.check("Seattle, WA, United States", places=[us("Seattle", "WA")], us_="yes",
                   metros={"sea"})

    def test_a_cvs_legal_entity(self):
        self.check("02551 - CVS Albany, L.L.C.", places=[us("Albany", "NY")], us_="yes")

    def test_a_bare_dublin_is_ireland(self):
        self.check("Dublin", places=[Place("Dublin", "", "IE")], us_="no")

    def test_a_metro_phrase(self):
        info = loc.parse_location("Greater Los Angeles Area")
        self.assertEqual(info.us, "yes")
        self.assertEqual(info.states, frozenset({"CA"}))
        self.assertEqual(info.metros, frozenset({"la"}))

    def test_hybrid_alone_has_no_place(self):
        self.check("Hybrid", places=[], us_="unknown", mode="hybrid")

    def test_two_segments_give_two_places_and_two_metros(self):
        self.check("San Francisco, CA; New York, NY",
                   places=[us("San Francisco", "CA"), us("New York", "NY")], us_="yes",
                   metros={"bay", "nyc"})

    def test_a_country_code_prefix_is_not_a_state(self):
        # IN is Indiana and India; Bengaluru settles which.
        self.check("IN-Bengaluru", places=[Place("Bengaluru", "", "IN")], us_="no")

    def test_placeholders_are_empty(self):
        for raw in ("N/A", "n/a", "TBD", "Various", "Multiple Locations", "Location", "", None):
            with self.subTest(raw=raw):
                self.assertEqual(loc.parse_location(raw), LocInfo())


class ParseLocationRules(unittest.TestCase):
    def test_a_us_state_suffix_beats_the_foreign_city_table(self):
        for raw, state in (("Dublin, CA", "CA"), ("Paris, TX", "TX"), ("Amsterdam, NY", "NY")):
            with self.subTest(raw=raw):
                self.assertEqual(loc.parse_location(raw).places, (us(raw.split(",")[0], state),))

    def test_a_canadian_province_code_is_canada(self):
        self.assertEqual(loc.parse_location("Waterloo, ON").places, (Place("Waterloo", "", "CA"),))
        self.assertEqual(loc.parse_location("CA-Toronto").places, (Place("Toronto", "", "CA"),))

    def test_a_foreign_city_before_a_state_name_is_a_list(self):
        info = loc.parse_location("London, New York")
        self.assertEqual(info.places, (Place("London", "", "GB"), us("New York", "NY")))
        self.assertEqual(info.us, "mixed")

    def test_a_foreign_province_is_foreign(self):
        self.assertEqual(loc.parse_location("Mannheim, Baden-Wurttemberg").places,
                         (Place("Mannheim", "", "XX"),))

    def test_global_is_the_us_and_abroad(self):
        self.assertEqual(loc.parse_location("Global").places, (us(), Place("", "", "XX")))

    def test_hybrid_beats_remote_beats_onsite(self):
        self.assertEqual(loc.parse_location("Remote; San Jose, CA (Hybrid)").mode, "hybrid")
        self.assertEqual(loc.parse_location("Onsite - Irvine, CA; Remote").mode, "remote")
        self.assertEqual(loc.parse_location("Onsite - Irvine, CA").mode, "onsite")

    def test_places_are_deduplicated_in_order(self):
        info = loc.parse_location("Irvine, CA; Austin, TX; Irvine, CA")
        self.assertEqual(info.places, (us("Irvine", "CA"), us("Austin", "TX")))

    def test_odd_strings_parse_to_something_without_raising(self):
        for raw in (" ; ; ", "|", "- , -", "•", "Remote or Hybrid", "12345", "(Remote)", 12345):
            with self.subTest(raw=raw):
                self.assertIsInstance(loc.parse_location(raw), LocInfo)


class NamedForms(unittest.TestCase):
    """One example of each form section 4.3.2 lists, as the reference reads it."""

    FORMS = (
        ("00469 - Rhode Island CVS Pharmacy, L.L.C.", (us("", "RI"),)),
        ("12345 - CVS Pharmacy, Inc.", (us(),)),
        ("US - AZ - Camelback Office", (us("Camelback Office", "AZ"),)),
        ("US- AMER", (us(),)),
        ("USA - IN - Lebanon - Warehouse", (us("Lebanon", "IN"),)),
        ("PA-Kennett Square-Tangent Energy", (us("Kennett Square", "PA"),)),
        ("Moody Air Force Base - GA", (us("Moody Air Force Base", "GA"),)),
        ("Toronto, Ontario, CA", (Place("Toronto", "", "CA"),)),
        ("Suzhou, Jiangsu", (Place("Suzhou", "", "CN"),)),
        ("Pune, IND", (Place("Pune", "", "IN"),)),
        ("Seattle, United States", (us("Seattle", "WA"),)),
        ("Texas, United States", (us("", "TX"),)),
        ("United States", (us(),)),
        ("Mountain View, California", (us("Mountain View", "CA"),)),
        ("Boston, Chicago", (us("Boston", "MA"), us("Chicago", "IL"))),
        ("Field-Florida", (us("", "FL"),)),
        ("Virginia - Remote", (us("", "VA"),)),
        ("APAC", (Place("", "", "XX"),)),
        ("Worldwide", (us(), Place("", "", "XX"))),
    )

    def test_each_form_reads_as_the_reference_reads_it(self):
        for raw, places in self.FORMS:
            with self.subTest(raw=raw):
                self.assertEqual(loc.parse_location(raw).places, places)


class ValueObjects(unittest.TestCase):
    def test_place_and_locinfo_are_frozen(self):
        info = loc.parse_location("Irvine, CA")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            info.mode = "remote"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            info.places[0].city = "Tustin"

    def test_places_is_a_tuple_and_metros_a_frozenset(self):
        info = loc.parse_location("San Francisco, CA; New York, NY")
        self.assertIsInstance(info.places, tuple)
        self.assertIsInstance(info.metros, frozenset)

    def test_countries_us_and_states(self):
        info = LocInfo(places=(us("Irvine", "CA"), Place("London", "", "GB"), Place("", "", "")))
        self.assertEqual(info.countries, {"US", "GB"})
        self.assertEqual(info.us, "mixed")
        self.assertEqual(info.states, frozenset({"CA"}))
        self.assertEqual(LocInfo().us, "unknown")
        self.assertEqual(LocInfo(places=(Place("Paris", "", "FR"),)).us, "no")


class LocationFor(unittest.TestCase):
    def test_a_city_tail_in_the_title(self):
        info = loc.location_for("Store Executive Intern (Store Leadership Intern) - Duluth, MN "
                                "(Starting Summer 2027)", "10 Locations")
        self.assertEqual(info.places, (us("Duluth", "MN"),))
        self.assertTrue(info.from_title)
        self.assertEqual(info.unlisted, 10)

    def test_a_region_in_the_title(self):
        info = loc.location_for("Retail Store Management Internship Summer 2027 - "
                                "Northern California", "27 Locations")
        self.assertEqual(info.places, (us("", "CA"),))
        self.assertTrue(info.from_title)

    def test_a_foreign_language_title_is_hinted_not_placed(self):
        info = loc.location_for("Practicante de Ingeniería", "3 Locations")
        self.assertEqual(info.places, ())
        self.assertTrue(info.foreign_hint)
        self.assertFalse(info.from_title)

    def test_the_location_field_wins_when_it_names_a_place(self):
        info = loc.location_for("Intern - Pueblo, CO", "Irvine, CA")
        self.assertEqual(info.places, (us("Irvine", "CA"),))
        self.assertFalse(info.from_title)

    def test_a_metro_phrase_in_the_title_adds_its_metro(self):
        info = loc.location_for("Software Engineer Intern (Greater Los Angeles)", "Irvine, CA")
        self.assertEqual(info.metros, frozenset({"oc", "la"}))

    def test_a_remote_role_is_not_placed_from_its_title(self):
        info = loc.location_for("Software Engineer Intern - Pueblo, CO", "Remote")
        self.assertEqual((info.places, info.mode, info.from_title), ((), "remote", False))

    def test_a_city_and_state_anywhere_in_the_title(self):
        info = loc.location_for("Engineering Intern (Irvine, CA)", "")
        self.assertEqual(info.places, (us("Irvine", "CA"),))
        self.assertEqual(info.metros, frozenset({"oc"}))
        self.assertTrue(info.from_title)

    def test_a_metro_phrase_alone_gives_its_state(self):
        info = loc.location_for("Summer Intern - Orange County", "")
        self.assertEqual((info.places, info.metros), ((us("", "CA"),), frozenset({"oc"})))

    def test_a_state_name_after_the_first_separator(self):
        info = loc.location_for("Retail Intern - North & Central Alabama", "5 Locations")
        self.assertEqual((info.places, info.unlisted), ((us("", "AL"),), 5))

    def test_nothing_to_go_on(self):
        self.assertEqual(loc.location_for("Software Engineer Intern", "12 Locations"),
                         LocInfo(unlisted=12))
        self.assertEqual(loc.location_for(None, None), LocInfo())


class LocationFit(unittest.TestCase):
    def fit(self, raw, *prefs):
        return loc.location_fit(loc.parse_location(raw), prefs)

    def test_the_six_fixtures_of_section_4_3_5(self):
        self.assertEqual(self.fit("Irvine, CA", "us", "unlisted", "oc"), (1.0, "Irvine, CA"))
        self.assertEqual(self.fit("Irvine, CA", "us", "unlisted"), (0.8, "Irvine, CA"))
        self.assertEqual(self.fit("27 Locations", "us", "unlisted"),
                         (0.3, "location not listed (27 locations), check the posting"))
        self.assertIsNone(self.fit("London, UK", "us", "unlisted")[0])
        self.assertEqual(self.fit("Remote", "us", "unlisted"), (0.8, "Remote (US)"))
        self.assertEqual(self.fit("CA - Remote", "oc", "unlisted"),
                         (0.3, "somewhere in California, city not listed"))

    def test_abroad_accepts_a_foreign_city(self):
        self.assertEqual(self.fit("London, UK", "abroad"), (0.8, "London"))

    def test_remote_us_alone_rejects_an_onsite_role(self):
        self.assertEqual(self.fit("Irvine, CA", "remote_us"), (None, "outside your locations"))

    def test_remote_us_takes_a_remote_role(self):
        self.assertEqual(self.fit("Remote", "remote_us"), (1.0, "Remote (US)"))

    def test_a_state_token(self):
        self.assertEqual(self.fit("Seattle, WA", "st:WA"), (1.0, "Seattle, WA"))
        self.assertEqual(self.fit("Seattle, WA", "st:OR"), (None, "outside your locations"))

    def test_the_state_presets(self):
        for raw, preset in (("Houston, TX", "tx"), ("Miami, FL", "fl"), ("Fresno, CA", "ca")):
            with self.subTest(preset=preset):
                self.assertEqual(self.fit(raw, preset)[0], 1.0)

    def test_socal_takes_a_city_outside_the_four_metros(self):
        self.assertEqual(self.fit("Santa Barbara, CA", "socal"), (1.0, "Santa Barbara, CA"))

    def test_a_us_place_with_no_state(self):
        self.assertEqual(self.fit("USA", "us"), (0.6, "in the US (city not listed)"))

    def test_an_unlisted_posting_needs_unlisted(self):
        self.assertEqual(self.fit("27 Locations", "us"), (None, "location not listed"))
        self.assertEqual(self.fit("", "us", "unlisted"),
                         (0.3, "location not listed, check the posting"))

    def test_a_foreign_hint_unlisted_posting_is_rejected_even_with_unlisted(self):
        info = loc.location_for("Practicante de Ingeniería", "3 Locations")
        self.assertEqual(loc.location_fit(info, ("us", "unlisted")), (None, "location not listed"))

    def test_a_metro_preset_does_not_take_another_metro(self):
        self.assertEqual(self.fit("Irvine, CA", "bay"), (None, "outside your locations"))

    def test_any_iterable_of_presets_is_accepted_and_left_unchanged(self):
        prefs = ["us", "unlisted"]
        self.assertEqual(loc.location_fit(loc.parse_location("Irvine, CA"), iter(prefs)),
                         (0.8, "Irvine, CA"))
        self.assertEqual(prefs, ["us", "unlisted"])

    def test_the_first_matching_preset_names_the_state(self):
        # Two state-only places, two metro presets: the answer follows the
        # order the presets were given in, never set iteration order.
        state_only = LocInfo(places=(us("", "CA"), us("", "WA")))
        self.assertEqual(loc.location_fit(state_only, ("sea", "oc", "unlisted")),
                         (0.3, "somewhere in Washington, city not listed"))
        self.assertEqual(loc.location_fit(state_only, ("oc", "sea", "unlisted")),
                         (0.3, "somewhere in California, city not listed"))


class ParseStateList(unittest.TestCase):
    def test_the_spec_example(self):
        self.assertEqual(loc.parse_state_list("WA, Oregon, ny and SoCal"),
                         (("st:WA", "st:OR", "st:NY"), ("SoCal",)))

    def test_duplicates_are_dropped_in_order(self):
        self.assertEqual(loc.parse_state_list("wa; Texas / WA"), (("st:WA", "st:TX"), ()))

    def test_nothing_typed(self):
        for text in (None, "", " , ; "):
            with self.subTest(text=text):
                self.assertEqual(loc.parse_state_list(text), ((), ()))

    def test_an_unknown_token_is_cut_to_thirty_characters(self):
        _, bad = loc.parse_state_list("x" * 50)
        self.assertEqual(bad, ("x" * 30,))


class DescribeLocations(unittest.TestCase):
    def test_the_spec_example(self):
        self.assertEqual(loc.describe_locations(("us", "unlisted", "st:WA")),
                         SEP.join(("Anywhere in the US", "State: Washington",
                                   "roles that don't list a location")))

    def test_presets_follow_select_order_whatever_order_they_are_given_in(self):
        want = SEP.join(("Anywhere in the US", "Orange County / Irvine",
                         "roles that don't list a location"))
        self.assertEqual(loc.describe_locations(("unlisted", "oc", "us")), want)
        self.assertEqual(loc.describe_locations(["us", "oc", "unlisted"]), want)

    def test_unknown_tokens_are_left_out(self):
        self.assertEqual(loc.describe_locations(("mars", "st:ZZ", "tx")), "Texas")

    def test_nothing_chosen(self):
        self.assertEqual(loc.describe_locations(()), "")

    def test_district_of_columbia_is_written_as_its_name(self):
        self.assertEqual(loc.describe_locations(("st:DC",)), "State: District of Columbia")
        self.assertIn(("State: District of Columbia", "st:DC"), loc.state_choices("columbia"))


class StateChoices(unittest.TestCase):
    def test_a_partial_word_finds_the_preset_and_the_state(self):
        got = loc.state_choices("cal")
        self.assertIn(("State: California", "st:CA"), got)
        self.assertIn(("Anywhere in California", "ca"), got)

    def test_presets_come_before_states(self):
        got = loc.state_choices("cal")
        self.assertLess(got.index(("Anywhere in California", "ca")),
                        got.index(("State: California", "st:CA")))

    def test_every_typed_token_must_appear(self):
        got = loc.state_choices("new york")
        self.assertIn(("New York City area", "nyc"), got)
        self.assertIn(("State: New York", "st:NY"), got)
        self.assertNotIn(("State: New Jersey", "st:NJ"), got)

    def test_matching_ignores_case(self):
        self.assertEqual(loc.state_choices("CAL"), loc.state_choices("cal"))

    def test_at_most_25_and_never_unlisted(self):
        for query in ("", "a", None):
            with self.subTest(query=query):
                got = loc.state_choices(query)
                self.assertLessEqual(len(got), 25)
                self.assertNotIn("unlisted", [value for _, value in got])


class Caching(unittest.TestCase):
    def test_the_parsers_are_cached_at_the_spec_size(self):
        for fn in (loc.parse_location, loc.location_for):
            with self.subTest(function=fn.__name__):
                self.assertEqual(fn.cache_info().maxsize, 65536)


class Purity(unittest.TestCase):
    def test_imports_only_the_stdlib_and_the_modules_the_graph_allows(self):
        allowed = {"re", "functools", "dataclasses", "collections", "typing",
                   "intern_vocab", "intern_places"}
        tree = ast.parse(pathlib.Path(loc.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, allowed)


if __name__ == "__main__":
    unittest.main()
