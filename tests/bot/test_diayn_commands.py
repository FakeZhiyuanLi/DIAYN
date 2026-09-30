"""
`/diayn`, the commands of whoever runs this bot: grant and revoke access, see
who has it, and the debug report that used to be `/internships debug`.

    python3 -m unittest discover -s tests      # the text and source rules run
    .venv/bin/python -m unittest discover -s tests

Every subcommand answers only the owner, asked before anything else, and
tells anyone else so in words that name nobody. A grant or a revoke is a row
of users.db's access_grants, which the finder reads on every check, so the
effect is immediate. Replies are private and mention nobody; `/diayn access`
gives counts and server names, never a person or an id.

The ids here are made up.
"""

import ast
import asyncio
import functools
import io
import pathlib
import re
import sqlite3
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

import access
import intern_store
import intern_text
import internship_poller as poller
from club_wording import CLUB

try:
    import discord
except ModuleNotFoundError as missing:  # pragma: no cover - depends on the environment
    if missing.name != "discord":
        raise
    discord = None

#: See test_intern_surface.REAL_DISCORD: a stub module has no __file__.
REAL_DISCORD = discord is not None and getattr(discord, "__file__", None) is not None
if REAL_DISCORD:
    from discord import app_commands
    import diayn_commands
    import intern_ui
else:  # pragma: no cover - depends on the environment
    app_commands = diayn_commands = intern_ui = None

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

BOT = pathlib.Path(__file__).resolve().parents[2] / "bot"
OWNER, PERSON, STRANGER = 101_101_101_101_101_101, 202_202_202_202_202_202, 303_303_303_303_303_303
SERVER, OTHER_SERVER, GONE_SERVER = 900_900_900_900_900_901, 900_900_900_900_900_902, 900_900_900_900_900_903
NOW = 1_790_000_000.0
AN_ID = re.compile(r"\d{15,}")
#: Every subcommand, by the def that answers it.
SUBCOMMANDS = ("grant_user", "grant_server", "revoke_user", "revoke_server", "diayn_access",
               "diayn_debug")


@functools.lru_cache(maxsize=None)
def tree() -> ast.Module:
    return ast.parse((BOT / "diayn_commands.py").read_text(encoding="utf-8"))


def chain(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{chain(node.value)}.{node.attr}"
    return ""


def first_call(fn) -> str:
    found = sorted((n for s in fn.body for n in ast.walk(s) if isinstance(n, ast.Call)),
                   key=lambda n: (n.lineno, n.col_offset))
    return chain(found[0].func) if found else ""


def defs() -> dict:
    return {n.name: n for n in ast.walk(tree()) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


class TheOwnerIsAskedFirst(unittest.TestCase):
    def test_every_subcommand_asks_before_anything_else(self):
        commands = {name: fn for name, fn in defs().items() if any(
            isinstance(d, ast.Call) and chain(d.func).endswith(".command") for d in fn.decorator_list)}
        self.assertEqual(set(commands), set(SUBCOMMANDS))
        for name, fn in commands.items():
            with self.subTest(command=name):
                self.assertEqual(first_call(fn), "need_owner")

    def test_the_question_is_access_is_owner(self):
        self.assertEqual(first_call(defs()["need_owner"]), "access.is_owner")

    def test_the_group_has_an_error_handler(self):
        handlers = [fn for fn in defs().values()
                    if any(chain(d) == "diayn.error" for d in fn.decorator_list)]
        self.assertEqual(len(handlers), 1)


class WhatTheOwnerIsTold(unittest.TestCase):
    """The words, which need nothing installed."""

    def test_a_grant_and_a_repeat(self):
        self.assertEqual(intern_text.granted_user("<@1>", added=True), "<@1> may use this bot now.")
        self.assertIn("already", intern_text.granted_user("<@1>", added=False))
        self.assertIn("**Club**", intern_text.granted_server("Club", added=True))
        self.assertIn("DMs", intern_text.granted_server("Club", added=True))
        self.assertIn("already", intern_text.granted_server("Club", added=False))

    def test_a_revoke_says_when_alerts_stop_and_when_the_profile_goes(self):
        text = intern_text.revoked_user("<@1>", removed=True, still=False)
        self.assertIn("next delivery", text)
        self.assertIn("30 days", text)
        self.assertIn("still has access", intern_text.revoked_user("<@1>", removed=True, still=True))
        self.assertIn("no grant of their own",
                      intern_text.revoked_user("<@1>", removed=False, still=True))
        self.assertIn("had no access", intern_text.revoked_user("<@1>", removed=False, still=False))
        self.assertIn("next delivery", intern_text.revoked_server("Club", removed=True))
        self.assertIn("had no grant", intern_text.revoked_server("Club", removed=False))

    def test_a_server_name_cannot_carry_markdown_or_a_mention(self):
        text = intern_text.granted_server("**x** @everyone", added=True)
        self.assertNotIn("**x**", text)
        self.assertIn("\\*\\*x\\*\\*", text)

    def test_the_summary_is_counts_and_server_names(self):
        lines = intern_text.access_summary(owners=1, users=3, servers=("Beta", "Alpha"), gone=2)
        body = "\n".join(lines)
        self.assertIn("People granted by id: 3", body)
        self.assertIn("Servers granted: 4", body)
        self.assertIn("Alpha, Beta", body)
        self.assertIn("2 servers this bot is no longer in", body)
        self.assertIsNone(AN_ID.search(body))

    def test_nothing_it_says_assumes_a_club(self):
        for text in (intern_text.granted_server("x", added=True),
                     intern_text.revoked_server("x", removed=True),
                     intern_text.revoked_user("x", removed=True, still=True),
                     intern_text.needs_a_server("grant"),
                     *intern_text.access_summary(owners=2, users=0, servers=(), gone=0)):
            with self.subTest(text=text):
                self.assertIsNone(CLUB.search(text))


# ------------------------------------------------------------------ driven (needs discord.py)

class Response:
    def __init__(self) -> None:
        self.sent, self.deferred, self.done = [], [], False

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))
        self.done = True

    async def defer(self, **kw):
        self.deferred.append(kw)
        self.done = True


class Followup:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


def interaction(uid, guild_id=None, guild_name=None):
    guild = None if guild_id is None else types.SimpleNamespace(id=guild_id, name=guild_name or "Somewhere")
    return types.SimpleNamespace(response=Response(), followup=Followup(), user=types.SimpleNamespace(id=uid),
                                 guild_id=guild_id, guild=guild,
                                 type=discord.InteractionType.application_command)


def user(uid):
    return types.SimpleNamespace(id=uid, mention=f"<@{uid}>", bot=False)


def said(i) -> str:
    return "\n".join(content for content, _ in i.response.sent + i.followup.sent)


@needs_discord
class TheGroup(unittest.TestCase):
    def setUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self.client)
        self.tree.add_command(diayn_commands.diayn)

    def test_it_is_grant_and_revoke_each_for_a_user_or_this_server_then_access_and_debug(self):
        group = diayn_commands.diayn
        self.assertEqual({c.name for c in group.commands}, {"grant", "revoke", "access", "debug"})
        for name in ("grant", "revoke"):
            with self.subTest(group=name):
                sub = group.get_command(name)
                self.assertEqual({c.name for c in sub.commands}, {"user", "server"})
                (option,) = sub.get_command("user").parameters
                self.assertEqual(option.name, "user")
                self.assertEqual(sub.get_command("server").parameters, [])

    def test_every_payload_fits_discords_limits_and_assumes_no_club(self):
        payload = diayn_commands.diayn.to_dict(self.tree)
        texts = [payload["description"]]
        for option in payload["options"]:
            texts.append(option["description"])
            texts += [o["description"] for o in option.get("options", [])]
            texts += [p["description"] for o in option.get("options", []) for p in o.get("options", [])]
        for text in texts:
            with self.subTest(text=text):
                self.assertLessEqual(len(text), 100)
                self.assertIsNone(CLUB.search(text))

    def test_it_is_usable_in_servers_and_dms(self):
        contexts = diayn_commands.diayn.allowed_contexts
        self.assertTrue(contexts.guild)
        self.assertTrue(contexts.dm_channel)
        self.assertFalse(contexts.private_channel)


class _OwnerCase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        intern_store.init_db(self.db)
        access.init_db(self.db)
        guilds = {SERVER: types.SimpleNamespace(name="The Server", get_member=lambda uid: None),
                  OTHER_SERVER: types.SimpleNamespace(name="Another *Server*", get_member=lambda uid: None)}
        self.guilds = guilds
        client = types.SimpleNamespace(get_guild=lambda gid: guilds.get(gid))
        for target, name, value in (
                (intern_ui, "db", self.db), (intern_ui, "intern_error", None), (intern_ui, "bot", client),
                (access, "_application_owners", frozenset()),
                (poller, "SETTINGS", poller.configure({"DIAYN_OWNER_IDS": str(OWNER)}))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def rows(self) -> list:
        return self.db.execute("SELECT kind, id, granted_by FROM access_grants ORDER BY kind, id").fetchall()

    @staticmethod
    def drive(coro) -> str:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            asyncio.run(coro)
        return stderr.getvalue()


def presses():
    c = diayn_commands
    return {"grant_user": lambda i: c.grant_user.callback(i, user(PERSON)),
            "grant_server": lambda i: c.grant_server.callback(i),
            "revoke_user": lambda i: c.revoke_user.callback(i, user(PERSON)),
            "revoke_server": lambda i: c.revoke_server.callback(i),
            "diayn_access": lambda i: c.diayn_access.callback(i),
            "diayn_debug": lambda i: c.diayn_debug.callback(i)}


@needs_discord
class OnlyTheOwner(_OwnerCase):
    def test_anyone_else_is_refused_privately_by_every_subcommand_and_nothing_changes(self):
        access.grant(self.db, "user", PERSON, granted_by=OWNER, now=NOW)
        access.grant(self.db, "guild", SERVER, granted_by=OWNER, now=NOW)
        before = self.rows()
        self.assertEqual(set(presses()), set(SUBCOMMANDS))
        for who in (STRANGER, PERSON):                  # access to the bot is not the owner's commands
            for name, press in presses().items():
                with self.subTest(who=who, command=name):
                    i = interaction(who, SERVER)
                    self.drive(press(i))
                    self.assertEqual(len(i.response.sent), 1)
                    content, kw = i.response.sent[0]
                    self.assertEqual(content, intern_text.owner_only())
                    self.assertIs(kw["ephemeral"], True)
                    self.assertEqual((i.response.deferred, i.followup.sent), ([], []))
        self.assertEqual(self.rows(), before)

    def test_with_users_db_down_grants_are_refused_as_the_finder_being_off(self):
        with mock.patch.object(intern_ui, "intern_error", "OperationalError: locked"), \
                mock.patch.object(intern_ui, "db", None):
            i = interaction(OWNER, SERVER)
            self.drive(diayn_commands.grant_server.callback(i))
        self.assertIn("switched off", i.response.sent[0][0])
        self.assertEqual(self.rows(), [])


@needs_discord
class Granting(_OwnerCase):
    def test_a_user_is_granted_by_the_owner_and_told_without_a_ping(self):
        i = interaction(OWNER)
        self.drive(diayn_commands.grant_user.callback(i, user(PERSON)))
        again = interaction(OWNER)
        self.drive(diayn_commands.grant_user.callback(again, user(PERSON)))

        self.assertEqual(self.rows(), [("user", PERSON, OWNER)])
        content, kw = i.response.sent[0]
        self.assertEqual(content, intern_text.granted_user(f"<@{PERSON}>", added=True))
        self.assertIs(kw["allowed_mentions"], intern_ui.NO_MENTIONS)
        self.assertIs(kw["ephemeral"], True)
        self.assertIn("already", said(again))
        self.assertTrue(intern_ui.may_use(PERSON))

    def test_this_server_is_granted_and_named(self):
        i = interaction(OWNER, SERVER, "The Server")
        self.drive(diayn_commands.grant_server.callback(i))

        self.assertEqual(self.rows(), [("guild", SERVER, OWNER)])
        self.assertIn("**The Server**", said(i))
        self.assertTrue(intern_ui.may_use(STRANGER, SERVER))

    def test_in_a_dm_there_is_no_server_to_grant(self):
        i = interaction(OWNER)
        self.drive(diayn_commands.grant_server.callback(i))
        self.assertEqual(said(i), intern_text.needs_a_server("grant"))
        self.assertEqual(self.rows(), [])


@needs_discord
class Revoking(_OwnerCase):
    def test_a_users_grant_goes_and_the_owner_is_told_what_follows(self):
        access.grant(self.db, "user", PERSON, granted_by=OWNER, now=NOW)
        i = interaction(OWNER)
        self.drive(diayn_commands.revoke_user.callback(i, user(PERSON)))

        self.assertEqual(self.rows(), [])
        self.assertEqual(said(i), intern_text.revoked_user(f"<@{PERSON}>", removed=True, still=False))
        self.assertFalse(intern_ui.may_use(PERSON))

    def test_someone_still_in_a_granted_server_is_said_to_keep_access(self):
        access.grant(self.db, "user", PERSON, granted_by=OWNER, now=NOW)
        access.grant(self.db, "guild", SERVER, granted_by=OWNER, now=NOW)
        self.guilds[SERVER].get_member = lambda uid: object() if uid == PERSON else None
        i = interaction(OWNER)
        self.drive(diayn_commands.revoke_user.callback(i, user(PERSON)))
        self.assertEqual(said(i), intern_text.revoked_user(f"<@{PERSON}>", removed=True, still=True))

    def test_this_servers_grant_goes(self):
        access.grant(self.db, "guild", SERVER, granted_by=OWNER, now=NOW)
        i = interaction(OWNER, SERVER, "The Server")
        self.drive(diayn_commands.revoke_server.callback(i))

        self.assertEqual(self.rows(), [])
        self.assertEqual(said(i), intern_text.revoked_server("The Server", removed=True))
        self.assertFalse(intern_ui.may_use(STRANGER, SERVER))

    def test_in_a_dm_there_is_no_server_to_revoke(self):
        access.grant(self.db, "guild", SERVER, granted_by=OWNER, now=NOW)
        i = interaction(OWNER)
        self.drive(diayn_commands.revoke_server.callback(i))
        self.assertEqual(said(i), intern_text.needs_a_server("revoke"))
        self.assertEqual(len(self.rows()), 1)


@needs_discord
class WhoHasAccess(_OwnerCase):
    def test_counts_and_the_servers_by_name_never_a_person(self):
        for uid in (PERSON, STRANGER):
            access.grant(self.db, "user", uid, granted_by=OWNER, now=NOW)
        for gid in (SERVER, OTHER_SERVER, GONE_SERVER):
            access.grant(self.db, "guild", gid, granted_by=OWNER, now=NOW)
        i = interaction(OWNER)
        self.drive(diayn_commands.diayn_access.callback(i))

        body = said(i)
        self.assertEqual(body, "\n".join(intern_text.access_summary(
            owners=1, users=2, servers=("Another *Server*", "The Server"), gone=1)))
        self.assertIsNone(AN_ID.search(body))
        self.assertTrue(all(kw["ephemeral"] for _, kw in i.response.sent + i.followup.sent))


@needs_discord
class TheDebugReport(_OwnerCase):
    def test_the_owner_gets_it_privately_after_a_defer(self):
        async def lines():
            return ["**Internship finder**", "profiles: 0"]
        i = interaction(OWNER)
        with mock.patch.object(diayn_commands.intern_commands, "debug_report", lines):
            self.drive(diayn_commands.diayn_debug.callback(i))
        self.assertEqual(len(i.response.deferred), 1)
        self.assertIs(i.response.deferred[0]["ephemeral"], True)
        self.assertEqual(said(i), "**Internship finder**\nprofiles: 0")


if __name__ == "__main__":
    unittest.main()
