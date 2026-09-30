"""
The Discord bot's tests.

A package, so that `python -m unittest discover -s tests` reaches this
directory; tests/test_layout.py fails if it does not. Discovery imports this
file before any test in it, so this is where the bot's modules in bot/, and
these tests themselves, go on sys.path: both use bare imports
(`import intern_store`, `from test_intern_store import ...`).
"""

import os
import sys

TESTS_BOT = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(os.path.dirname(os.path.dirname(TESTS_BOT)), "bot")

for _path in (TESTS_BOT, BOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)
