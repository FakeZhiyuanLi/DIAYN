"""
Who runs this bot: `access.is_owner`, which gates the owner's commands.

    python3 -m unittest discover -s tests      # no install needed

The owner is whoever DIAYN_OWNER_IDS names, or, when it names nobody, the
owner of the Discord application the bot logs in as: its single owner, or the
admins and developers of the team that owns it. Nobody else, and nobody at
all before either is known: an owner check that failed open would hand the
owner's commands to anyone.

The ids here are made up.
"""

import types
import unittest
from unittest import mock

import access
import internship_poller as poller

OWNER, OTHER, TEAM_ADMIN, TEAM_DEV, TEAM_READER = 101, 202, 303, 404, 505


def settings(**environ) -> "poller.Settings":
    return poller.configure(environ)


def member(uid, role):
    return types.SimpleNamespace(id=uid, role=types.SimpleNamespace(value=role))


class _AccessCase(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(access, "_application_owners", frozenset())
        patch.start()
        self.addCleanup(patch.stop)

    def bind(self, **environ):
        patch = mock.patch.object(poller, "SETTINGS", settings(**environ))
        patch.start()
        self.addCleanup(patch.stop)


class TheConfiguredOwners(_AccessCase):
    def test_diayn_owner_ids_are_the_owners(self):
        self.bind(DIAYN_OWNER_IDS=f"{OWNER}, {TEAM_ADMIN}")
        self.assertTrue(access.is_owner(OWNER))
        self.assertTrue(access.is_owner(TEAM_ADMIN))
        self.assertFalse(access.is_owner(OTHER))

    def test_they_win_over_the_application_owner(self):
        self.bind(DIAYN_OWNER_IDS=str(OWNER))
        access.set_application_owners({OTHER})
        self.assertTrue(access.is_owner(OWNER))
        self.assertFalse(access.is_owner(OTHER))

    def test_they_are_read_on_every_call(self):
        self.bind(DIAYN_OWNER_IDS=str(OWNER))
        self.assertTrue(access.is_owner(OWNER))
        self.bind(DIAYN_OWNER_IDS=str(OTHER))
        self.assertFalse(access.is_owner(OWNER))


class TheApplicationsOwners(_AccessCase):
    def test_with_none_configured_the_application_owner_is_the_owner(self):
        self.bind()
        access.set_application_owners({OWNER})
        self.assertTrue(access.is_owner(OWNER))
        self.assertFalse(access.is_owner(OTHER))

    def test_before_start_up_has_recorded_them_nobody_is_the_owner(self):
        self.bind()
        self.assertEqual(access.owner_ids(), frozenset())
        self.assertFalse(access.is_owner(OWNER))

    def test_a_single_owner(self):
        app = types.SimpleNamespace(team=None, owner=types.SimpleNamespace(id=OWNER))
        self.assertEqual(access.application_owners(app), frozenset({OWNER}))

    def test_a_teams_admins_and_developers_but_not_its_read_only_members(self):
        # The same members discord.py's own Bot.is_owner accepts.
        team = types.SimpleNamespace(members=[member(TEAM_ADMIN, "admin"),
                                              member(TEAM_DEV, "developer"),
                                              member(TEAM_READER, "read_only")])
        app = types.SimpleNamespace(team=team, owner=types.SimpleNamespace(id=OTHER))
        self.assertEqual(access.application_owners(app), frozenset({TEAM_ADMIN, TEAM_DEV}))

    def test_an_application_with_no_owner_to_read_has_none(self):
        app = types.SimpleNamespace(team=None, owner=None)
        self.assertEqual(access.application_owners(app), frozenset())


class WhatIsNotAnId(_AccessCase):
    def test_only_an_int_is_ever_an_owner(self):
        self.bind(DIAYN_OWNER_IDS="1")
        for value in (True, "1", 1.0, None):
            with self.subTest(value=value):
                self.assertFalse(access.is_owner(value))
        self.assertTrue(access.is_owner(1))

    def test_recorded_owners_that_are_not_ids_are_dropped(self):
        self.bind()
        access.set_application_owners({OWNER, "x", True, None})
        self.assertEqual(access.owner_ids(), frozenset({OWNER}))


class ImportingReadsNothing(unittest.TestCase):
    def test_the_module_imports_neither_discord_nor_the_scraper_at_module_scope(self):
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path(access.__file__).read_text(encoding="utf-8"))
        top = {alias.name.split(".")[0] for node in tree.body
               if isinstance(node, (ast.Import, ast.ImportFrom))
               for alias in getattr(node, "names", ())}
        self.assertFalse(top & {"discord", "internship_poller", "aiohttp"})


if __name__ == "__main__":
    unittest.main()
