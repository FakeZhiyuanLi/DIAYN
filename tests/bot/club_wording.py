"""
What nothing DIAYN says to a user may assume: a club, its officers, the file
that named them, or a command from another bot.

DIAYN is run by whoever hosts it, for whoever they let in. Help that needs
the host asks "whoever runs this bot". Shared by the tests of every module
that words a reply, so that each of them checks the same thing.
"""

import re

CLUB = re.compile(r"\bclub\b|\bofficers?\b|puzzle-admins|/report", re.I)
