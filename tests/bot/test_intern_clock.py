"""
The finder's time zone: DIAYN_TZ, which alert hours are kept in and shown in.

    python3 -m unittest discover -s tests      # no install needed

The scraper's configure() reads and checks DIAYN_TZ and main() binds it as
`internship_poller.SETTINGS.tz`. The finder reads it from there on every call,
so these tests change it the way a restart with a new .env would: by
replacing SETTINGS.
"""

import unittest
from datetime import timezone
from unittest import mock
from zoneinfo import ZoneInfo

import intern_clock as clock
import internship_poller as poller


def settings(**environ) -> "poller.Settings":
    return poller.configure(environ)


class TheZone(unittest.TestCase):
    def test_it_is_utc_until_a_zone_is_set(self):
        with mock.patch.object(poller, "SETTINGS", settings()):
            self.assertEqual(clock.zone_name(), "UTC")
            self.assertEqual(clock.zone_label(), "UTC")

    def test_it_is_diayn_tz_as_the_scraper_bound_it(self):
        with mock.patch.object(poller, "SETTINGS", settings(DIAYN_TZ="America/Los_Angeles")):
            self.assertEqual(clock.zone(), ZoneInfo("America/Los_Angeles"))
            self.assertEqual(clock.zone_label(), "Los Angeles time")

    def test_a_new_setting_is_read_on_the_next_call(self):
        with mock.patch.object(poller, "SETTINGS", settings(DIAYN_TZ="Europe/Berlin")):
            first = clock.zone()
        with mock.patch.object(poller, "SETTINGS", settings(DIAYN_TZ="Asia/Tokyo")):
            second = clock.zone()

        self.assertEqual((str(first), str(second)), ("Europe/Berlin", "Asia/Tokyo"))

    def test_it_is_not_the_gemini_quota_day_s_zone(self):
        with mock.patch.object(poller, "SETTINGS", settings(LLM_DAY_TZ="America/Los_Angeles")):
            self.assertEqual(clock.zone_name(), "UTC")

    def test_utc_works_on_a_box_without_tz_data(self):
        clock._zone.cache_clear()
        self.addCleanup(clock._zone.cache_clear)

        with mock.patch.object(clock, "ZoneInfo", side_effect=clock.ZoneInfoNotFoundError("UTC")):
            self.assertIs(clock._zone("UTC"), timezone.utc)


class TheLabel(unittest.TestCase):
    def test_a_zone_is_named_by_its_city(self):
        for name, label in (("America/Los_Angeles", "Los Angeles time"),
                            ("Europe/London", "London time"),
                            ("America/Argentina/Buenos_Aires", "Buenos Aires time"),
                            ("US/Pacific", "Pacific time")):
            with self.subTest(name=name):
                self.assertEqual(clock.zone_label(name), label)

    def test_a_zone_without_a_city_is_named_as_it_is(self):
        for name in ("UTC", "Etc/GMT+8", "Etc/UTC", "GMT"):
            with self.subTest(name=name):
                self.assertEqual(clock.zone_label(name), name)


if __name__ == "__main__":
    unittest.main()
