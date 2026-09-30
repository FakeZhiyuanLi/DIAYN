"""
The Discord bot's tests.

A package, so that `python -m unittest discover -s tests` reaches this
directory; tests/test_layout.py fails if it does not. Discovery imports this
file before any test in it, so this is where the bot's modules in bot/, and
these tests themselves, go on sys.path: both use bare imports
(`import intern_store`, `from test_intern_store import ...`).

The checkout goes there too, because the bot reads the scraper's settings
(`internship_poller.SETTINGS`), and so does tests/, for its aiohttp stub: the
scraper imports aiohttp at module scope, and a bare `python3` has none.
"""

import os
import sys

TESTS_BOT = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(TESTS_BOT)
CHECKOUT = os.path.dirname(TESTS)
BOT = os.path.join(CHECKOUT, "bot")

for _path in (CHECKOUT, TESTS, TESTS_BOT, BOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()
