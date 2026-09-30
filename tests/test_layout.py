"""
The layout the Discord bot's code relies on.

    python3 -m unittest discover -s tests      # no install needed

The bot's modules live in bot/ and their tests in tests/bot/. The suite is
run with one command, and that command must reach tests/bot/: a test file
that discovery skips passes on every run and checks nothing.
"""

import os
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))

# A test that must be found under tests/bot/, by the id discovery gives it.
BOT_TEST_PREFIX = "bot.test_bot_path."
# How discovery names a test file it found but could not import.
FAILED_IMPORT_PREFIX = "unittest.loader._FailedTest."


def test_ids(suite):
    """Every test id in `suite`, however deeply its suites are nested."""
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


class Discovery(unittest.TestCase):
    def test_discovery_from_tests_reaches_tests_bot(self):
        suite = unittest.TestLoader().discover(TESTS, top_level_dir=TESTS)
        ids = list(test_ids(suite))
        self.assertTrue(any(i.startswith(BOT_TEST_PREFIX) for i in ids),
                        "no test from tests/bot/ was discovered")
        self.assertEqual([i for i in ids if i.startswith(FAILED_IMPORT_PREFIX)], [])


if __name__ == "__main__":
    unittest.main()
