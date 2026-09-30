"""
Who runs this bot, `access.is_owner`, and who else may use it, `access.allowed`.

    python3 -m unittest discover -s tests      # no install needed

The owner is whoever DIAYN_OWNER_IDS names, or, when it names nobody, the
owner of the Discord application the bot logs in as: its single owner, or the
admins and developers of the team that owns it. Nobody else, and nobody at
all before either is known: an owner check that failed open would hand the
owner's commands to anyone.

Everyone else needs a grant, which only the owner gives: one user by id, or a
whole server. A server grant lets in anyone using the bot inside that server
and, in a DM, anyone who is a member of it; a member using the bot inside
another server that has no grant is refused (plan 3.3). Grants live in
users.db's `access_grants`; the policy itself is pure and reads a snapshot of
them.

The ids here are made up.
"""

import ast
import sqlite3
import types
import unittest
from unittest import mock

import access
import internship_poller as poller

OWNER, OTHER, TEAM_ADMIN, TEAM_DEV, TEAM_READER = 101, 202, 303, 404, 505
GRANTED, STRANGER, MEMBER = 606, 707, 808
CLUB_SERVER, OTHER_SERVER = 9001, 9002
NOW = 1_790_000_000.0


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


def nobody_asked(guild_ids):
    raise AssertionError("membership was looked up where it did not need to be")


def members_of(*servers, who=MEMBER):
    """A membership lookup: `who` is a member of `servers`, and nobody is a member of any other.
    Records the server ids it was asked about."""
    asked = []

    def lookup(user_id):
        def member_of(guild_ids):
            asked.append(frozenset(guild_ids))
            return user_id == who and bool(set(guild_ids) & set(servers))
        return member_of
    lookup.asked = asked
    return lookup


class _GrantsCase(_AccessCase):
    def setUp(self):
        super().setUp()
        self.bind(DIAYN_OWNER_IDS=str(OWNER))
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        access.init_db(self.db)

    def may(self, user_id, guild_id=None, member_of=nobody_asked):
        return access.allowed(access.grants(self.db), user_id, guild_id,
                              member_of(user_id) if member_of is not nobody_asked else nobody_asked)


class ThePolicy(_GrantsCase):
    """The table in plan 3.3: owner, user grant, server grant, a DM with and without
    membership, revoked."""

    def test_the_owner_may_use_it_anywhere_with_nothing_granted(self):
        for guild_id in (None, CLUB_SERVER):
            with self.subTest(guild_id=guild_id):
                self.assertTrue(self.may(OWNER, guild_id))

    def test_a_user_granted_by_id_may_use_it_anywhere(self):
        access.grant(self.db, "user", GRANTED, granted_by=OWNER, now=NOW)
        for guild_id in (None, OTHER_SERVER):
            with self.subTest(guild_id=guild_id):
                self.assertTrue(self.may(GRANTED, guild_id))
        self.assertFalse(self.may(STRANGER, None, members_of()))

    def test_anyone_inside_a_granted_server_may_with_no_lookup(self):
        access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW)
        self.assertTrue(self.may(STRANGER, CLUB_SERVER))

    def test_in_a_dm_a_member_of_a_granted_server_may(self):
        access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW)
        lookup = members_of(CLUB_SERVER)
        self.assertTrue(self.may(MEMBER, None, lookup))
        self.assertEqual(lookup.asked, [frozenset({CLUB_SERVER})])

    def test_in_a_dm_anyone_else_may_not(self):
        access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW)
        self.assertFalse(self.may(STRANGER, None, members_of(CLUB_SERVER)))

    def test_a_member_of_a_granted_server_is_refused_inside_another_server(self):
        # Plan 3.3: the membership fallback is for DMs. Inside a server, that server's
        # grant decides, and nobody's membership elsewhere is looked up.
        access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW)
        lookup = members_of(CLUB_SERVER)
        self.assertFalse(self.may(MEMBER, OTHER_SERVER, lookup))
        self.assertFalse(self.may(STRANGER, OTHER_SERVER, members_of(CLUB_SERVER)))
        self.assertEqual(lookup.asked, [])
        self.assertTrue(self.may(MEMBER, None, members_of(CLUB_SERVER)))     # in a DM, yes

    def test_with_no_server_granted_membership_is_never_looked_up(self):
        access.grant(self.db, "user", GRANTED, granted_by=OWNER, now=NOW)
        self.assertFalse(self.may(STRANGER, None))
        self.assertFalse(self.may(STRANGER, OTHER_SERVER))

    def test_a_revoked_user_may_not(self):
        access.grant(self.db, "user", GRANTED, granted_by=OWNER, now=NOW)
        self.assertTrue(access.revoke(self.db, "user", GRANTED))
        self.assertFalse(self.may(GRANTED, None, members_of()))

    def test_a_revoked_server_lets_nobody_in(self):
        access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW)
        self.assertTrue(access.revoke(self.db, "guild", CLUB_SERVER))
        self.assertFalse(self.may(STRANGER, CLUB_SERVER))
        self.assertFalse(self.may(MEMBER, None, members_of(CLUB_SERVER)))

    def test_revoking_a_grant_does_not_revoke_the_owner(self):
        access.grant(self.db, "user", OWNER, granted_by=OWNER, now=NOW)
        access.revoke(self.db, "user", OWNER)
        self.assertTrue(self.may(OWNER))

    def test_a_user_grant_and_a_server_grant_with_the_same_number_are_different_things(self):
        access.grant(self.db, "user", CLUB_SERVER, granted_by=OWNER, now=NOW)
        self.assertFalse(self.may(STRANGER, CLUB_SERVER))
        access.grant(self.db, "guild", GRANTED, granted_by=OWNER, now=NOW)
        self.assertFalse(self.may(GRANTED, None, members_of()))

    def test_anything_that_is_not_a_user_id_may_not(self):
        access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW)
        for value in (None, True, str(OWNER), float(OWNER)):
            with self.subTest(value=value):
                self.assertFalse(access.allowed(access.grants(self.db), value, CLUB_SERVER,
                                                nobody_asked))

    def test_without_users_db_only_the_owner_may(self):
        self.assertEqual(access.grants(None), access.Grants())
        self.assertTrue(access.allowed(access.grants(None), OWNER, None, nobody_asked))
        self.assertFalse(access.allowed(access.grants(None), STRANGER, CLUB_SERVER, nobody_asked))


class TheGrantsTable(_GrantsCase):
    def test_it_holds_the_kind_the_id_who_granted_it_and_when(self):
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(access_grants)")]
        self.assertEqual(columns, ["kind", "id", "granted_by", "granted_at"])

    def test_making_it_twice_keeps_what_it_holds(self):
        access.grant(self.db, "user", GRANTED, granted_by=OWNER, now=NOW)
        access.init_db(self.db)
        self.assertEqual(access.grants(self.db).users, frozenset({GRANTED}))

    def test_a_grant_records_who_gave_it_and_when(self):
        self.assertTrue(access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW))
        self.assertTrue(access.grant(self.db, "user", GRANTED, granted_by=None, now=NOW + 1))
        rows = self.db.execute("SELECT kind, id, granted_by, granted_at FROM access_grants "
                               "ORDER BY kind").fetchall()
        self.assertEqual(rows, [("guild", CLUB_SERVER, OWNER, NOW), ("user", GRANTED, None, NOW + 1)])

    def test_granting_again_changes_nothing_and_says_so(self):
        access.grant(self.db, "user", GRANTED, granted_by=OWNER, now=NOW)
        self.assertFalse(access.grant(self.db, "user", GRANTED, granted_by=None, now=NOW + 9))
        self.assertEqual(self.db.execute("SELECT granted_by, granted_at FROM access_grants").fetchall(),
                         [(OWNER, NOW)])

    def test_revoking_what_was_never_granted_says_so(self):
        self.assertFalse(access.revoke(self.db, "user", GRANTED))
        self.assertFalse(access.revoke(self.db, "guild", CLUB_SERVER))

    def test_whether_one_user_holds_a_grant_by_id(self):
        access.grant(self.db, "user", GRANTED, granted_by=OWNER, now=NOW)
        access.grant(self.db, "guild", STRANGER, granted_by=OWNER, now=NOW)   # a server's id
        self.assertTrue(access.user_granted(self.db, GRANTED))
        self.assertFalse(access.user_granted(self.db, STRANGER))
        self.assertFalse(access.user_granted(None, GRANTED))

    def test_the_snapshot_splits_users_from_servers(self):
        access.grant(self.db, "user", GRANTED, granted_by=OWNER, now=NOW)
        access.grant(self.db, "guild", CLUB_SERVER, granted_by=OWNER, now=NOW)
        access.grant(self.db, "guild", OTHER_SERVER, granted_by=OWNER, now=NOW)
        self.assertEqual(access.grants(self.db),
                         access.Grants(users=frozenset({GRANTED}),
                                       guilds=frozenset({CLUB_SERVER, OTHER_SERVER})))

    def test_a_kind_or_an_id_it_cannot_hold_is_refused(self):
        for kind, target in (("role", GRANTED), ("user", 0), ("user", -5), ("guild", "9001"),
                             ("guild", True), ("user", 2 ** 63)):
            with self.subTest(kind=kind, target=target):
                with self.assertRaises(ValueError):
                    access.grant(self.db, kind, target, granted_by=OWNER, now=NOW)
                with self.assertRaises(ValueError):
                    access.revoke(self.db, kind, target)
        with self.assertRaises(ValueError):
            access.grant(self.db, "user", GRANTED, granted_by="owner", now=NOW)
        self.assertEqual(access.grants(self.db), access.Grants())


class TheImportGuardReadsBothForms(unittest.TestCase):
    """The guard below, and test_intern_wiring's, read `from x import y` by its module:
    its alias names are what it imports from x, never x itself."""

    def test_every_form_of_import_that_runs_at_import_is_found_by_its_package(self):
        from module_imports import imported_at_module_scope
        cases = {"from discord import Client": {"discord"},
                 "from discord.ext import tasks": {"discord"},
                 "import internship_poller.settings as p": {"internship_poller"},
                 "try:\n    import aiohttp\nexcept ImportError:\n    aiohttp = None": {"aiohttp"},
                 "class C:\n    from discord import ui": {"discord"},
                 "def later():\n    import discord": set(),
                 "from . import sibling": set()}
        for source, found in cases.items():
            with self.subTest(source=source):
                self.assertEqual(imported_at_module_scope(ast.parse(source)), found)


class ImportingReadsNothing(unittest.TestCase):
    def test_the_module_imports_neither_discord_nor_the_scraper_at_module_scope(self):
        import pathlib
        from module_imports import imported_at_module_scope
        tree = ast.parse(pathlib.Path(access.__file__).read_text(encoding="utf-8"))
        top = imported_at_module_scope(tree)
        self.assertIn("sqlite3", top)                   # it does read the source it guards
        self.assertFalse(top & {"discord", "internship_poller", "aiohttp"})


if __name__ == "__main__":
    unittest.main()
