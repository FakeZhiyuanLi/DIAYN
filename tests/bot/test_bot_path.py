"""
Where the bot's modules are found, for the tests beside this one.

    python3 -m unittest discover -s tests      # no install needed

The bot's modules use bare imports (`import intern_store`), with bot/ on
sys.path, and so do its tests between themselves. This package's __init__
puts both directories there.
"""

import os
import sys
import unittest

TESTS_BOT = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(os.path.dirname(os.path.dirname(TESTS_BOT)), "bot")


class BotPath(unittest.TestCase):
    def test_the_bot_s_directory_and_its_tests_are_on_the_path(self):
        self.assertIn(BOT, sys.path)
        self.assertIn(TESTS_BOT, sys.path)

    def test_bot_is_a_directory_of_modules_not_a_package(self):
        # Under discovery this test package is called `bot`. An __init__.py in
        # bot/ would make a second package of that name, and which of the two
        # an import found would depend on the order of sys.path.
        self.assertTrue(os.path.isdir(BOT))
        self.assertFalse(os.path.exists(os.path.join(BOT, "__init__.py")))


if __name__ == "__main__":
    unittest.main()
