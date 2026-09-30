"""
Where the finder asks who may use it: every callback that shows or stores
anything checks `access` first, and the ones that only remove data or reduce
contact never do.

    python3 -m unittest discover -s tests      # the source rules run; behaviour skips
    .venv/bin/python -m unittest discover -s tests

The bot is private (plan 3.3). The check lives in each callback, not in a
shared base view, so that the exemptions stay possible: `/internships help`
and `/internships delete`, the card's Delete button and its confirmation, and
the alert controls Stop and Pause answer anyone, so nobody is ever stuck with
their data or their alerts.

The first half reads the source, so a new command or button that nobody
classified fails here on a bare box too: every callback must be in one of the
tables below, and a gated one's first call must be the check. The second half
drives the callbacks with fakes and skips without discord.py.

The ids here are made up.
"""

import ast
import asyncio
import dataclasses
import functools
import io
import pathlib
import sqlite3
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

import access
import intern_profile
import intern_store
import intern_text
import internship_poller as poller

try:
    import discord
except ModuleNotFoundError as missing:  # pragma: no cover - depends on the environment
    if missing.name != "discord":
        raise
    discord = None

#: See test_intern_surface.REAL_DISCORD: a stub module has no __file__.
REAL_DISCORD = discord is not None and getattr(discord, "__file__", None) is not None
if REAL_DISCORD:
    import intern_commands
    import intern_ui
else:  # pragma: no cover - depends on the environment
    intern_commands = intern_ui = None

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

BOT = pathlib.Path(__file__).resolve().parents[2] / "bot"
GATE, MAY_USE, OWNER_CHECK = "intern_ui.need_access", "intern_ui.may_use", "access.is_owner"
REFUSAL = "This bot is private. Ask whoever runs it for access."

#: Every `/internships` subcommand: "gated" checks access first, "open" never does.
COMMANDS = {
    "internships_profile": "gated", "internships_matches": "gated",
    "internships_recent": "gated", "internships_ping": "gated", "internships_info": "gated",
    "internships_help": "open",       # how it works and what it keeps: for anyone deciding
    "internships_delete": "open",     # removes data: never withheld
    "internships_debug": "owner",
}
#: Every autocomplete. The field and place lists are the finder's fixed vocabulary;
#: the role list reads postings and the user's own matches, so `_role_choices` checks.
AUTOCOMPLETES = {"_role_autocomplete": "_role_choices", "_field_autocomplete": None,
                 "_where_autocomplete": None}

OWNER, GRANTED, STRANGER, MEMBER = 101, 202, 303, 404
SERVER, ELSEWHERE = 9001, 9002
NOW = 1_790_000_000.0


# ------------------------------------------------------------------ reading the source

@functools.lru_cache(maxsize=None)
def tree(name: str) -> ast.Module:
    return ast.parse((BOT / name).read_text(encoding="utf-8"), filename=name)


def chain(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = chain(node.value)
        return f"{head}.{node.attr}" if head else f"?.{node.attr}"
    if isinstance(node, ast.Call):
        return chain(node.func) + "()"
    return ""


def calls_in_order(fn: ast.AST) -> list:
    """Every call in the body of `fn`, in the order it appears in the source. Its
    decorators are not its body: they run once, when the def does."""
    found = [n for statement in fn.body for n in ast.walk(statement) if isinstance(n, ast.Call)]
    return sorted(found, key=lambda n: (n.lineno, n.col_offset))


def first_call(fn: ast.AST) -> str:
    ordered = calls_in_order(fn)
    return chain(ordered[0].func) if ordered else ""


def calls_to(fn: ast.AST, name: str) -> list:
    return [c for c in calls_in_order(fn) if chain(c.func) == name]


def decorated(module: ast.Module, prefix: str) -> dict:
    """Every def whose decorator is a call spelled `prefix...`, by name."""
    return {fn.name: fn for fn in ast.walk(module)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(d, ast.Call) and chain(d.func).startswith(prefix)
                    for d in fn.decorator_list)}


def function(module: ast.Module, name: str):
    (fn,) = [n for n in ast.walk(module)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    return fn


class TheRefusal(unittest.TestCase):
    def test_it_is_private_and_names_nobody(self):
        self.assertEqual(intern_text.no_access(), REFUSAL)


class EveryCommandIsClassified(unittest.TestCase):
    def commands(self) -> dict:
        return decorated(tree("intern_commands.py"), "internships.command")

    def test_the_table_is_every_subcommand(self):
        self.assertEqual(set(self.commands()), set(COMMANDS))

    def test_a_gated_command_checks_access_before_anything_else(self):
        for name, fn in self.commands().items():
            if COMMANDS[name] == "gated":
                with self.subTest(command=name):
                    self.assertEqual(first_call(fn), GATE)

    def test_an_open_command_never_asks(self):
        for name, fn in self.commands().items():
            if COMMANDS[name] == "open":
                with self.subTest(command=name):
                    self.assertEqual(calls_to(fn, GATE) + calls_to(fn, MAY_USE), [])

    def test_the_owners_command_asks_whether_this_is_the_owner_first(self):
        for name, fn in self.commands().items():
            if COMMANDS[name] == "owner":
                with self.subTest(command=name):
                    self.assertEqual(first_call(fn), OWNER_CHECK)


class EveryAutocompleteIsClassified(unittest.TestCase):
    def test_the_table_is_every_autocomplete(self):
        found = decorated(tree("intern_commands.py"), "internships_")
        self.assertEqual({n for n, fn in found.items() if any(
            chain(d.func).endswith(".autocomplete") for d in fn.decorator_list)}, set(AUTOCOMPLETES))

    def test_the_role_suggestions_check_access_before_reading_anything(self):
        module = tree("intern_commands.py")
        for name, checker in AUTOCOMPLETES.items():
            if checker is None:
                continue
            with self.subTest(autocomplete=name):
                self.assertTrue(calls_to(function(module, name), checker))
                self.assertEqual(first_call(function(module, checker)), MAY_USE)

    def test_it_asks_about_the_place_the_suggestions_are_for(self):
        (call,) = calls_to(function(tree("intern_commands.py"), "_role_autocomplete"), "_role_choices")
        self.assertIn("interaction.guild_id", [chain(a) for a in call.args])


# ------------------------------------------------------------------ behaviour (needs discord.py)

class Response:
    def __init__(self) -> None:
        self.sent, self.modals, self.edits, self.done = [], [], [], False

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))
        self.done = True

    async def send_modal(self, modal):
        self.modals.append(modal)
        self.done = True

    async def defer(self, **kw):
        self.done = True

    async def edit_message(self, **kw):
        self.edits.append(kw)
        self.done = True


class Followup:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


def interaction(uid, guild_id=None, *, component=False):
    kind = (discord.InteractionType.component if component
            else discord.InteractionType.application_command)
    i = types.SimpleNamespace(response=Response(), followup=Followup(), user=types.SimpleNamespace(id=uid),
                              guild_id=guild_id, type=kind, edited=[])

    async def edit_original_response(**kw):
        i.edited.append(kw)
    i.edit_original_response = edit_original_response
    return i


def client_with(members: dict):
    """A client whose member cache holds `members`: {server id: {user ids}}."""
    def get_guild(gid):
        if gid not in members:
            return None
        return types.SimpleNamespace(get_member=lambda uid: object() if uid in members[gid] else None)
    return types.SimpleNamespace(get_guild=get_guild)


def refused(i) -> bool:
    """Exactly one private reply, the refusal, and nothing else sent, opened or edited."""
    return (len(i.response.sent) == 1 and i.response.sent[0][0] == REFUSAL
            and i.response.sent[0][1].get("ephemeral") is True and "view" not in i.response.sent[0][1]
            and not i.followup.sent and not i.response.modals and not i.response.edits
            and not i.edited)


class _GateCase(unittest.TestCase):
    """users.db in memory with the finder's tables and the grants; the tracker down; the
    owner named by DIAYN_OWNER_IDS; no Discord client."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        intern_store.init_db(self.db)
        access.init_db(self.db)
        for target, name, value in (
                (intern_ui, "db", self.db), (intern_ui, "intern_error", None),
                (intern_ui, "pconn", None), (intern_ui, "source", None),
                (intern_ui, "pconn_error", "not started."), (intern_ui, "bot", None),
                (intern_ui, "ensure_postings", lambda **kw: False),
                (access, "_application_owners", frozenset()),
                (poller, "SETTINGS", poller.configure({"DIAYN_OWNER_IDS": str(OWNER)}))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def enrol(self, uid, **fields):
        base = intern_profile.new_profile(uid, NOW, source="manual", cursor=NOW)
        p = dataclasses.replace(base, fields=("software",), **fields)
        return intern_store.save(self.db, p, now=NOW, cursor=NOW)

    def grant(self, kind, target):
        access.grant(self.db, kind, target, granted_by=OWNER, now=NOW)

    @staticmethod
    def drive(coro):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            asyncio.run(coro)
        return stderr.getvalue()


def command_calls():
    """Each gated `/internships` subcommand, as a callable taking the interaction."""
    c = intern_commands
    return {"profile": lambda i: c.internships_profile.callback(i, None),
            "matches": lambda i: c.internships_matches.callback(i, None, 14),
            "recent": lambda i: c.internships_recent.callback(i, None, None, None, 7),
            "ping": lambda i: c.internships_ping.callback(i, None, None),
            "info": lambda i: c.internships_info.callback(i, "acme")}


@needs_discord
class TheCommandsAreGated(_GateCase):
    def test_every_gated_command_refuses_someone_without_access_and_changes_nothing(self):
        before = self.enrol(STRANGER, alerts="daily")         # revoked, profile still held
        for name, call in command_calls().items():
            with self.subTest(command=name):
                i = interaction(STRANGER, ELSEWHERE)
                self.drive(call(i))
                self.assertTrue(refused(i), (i.response.sent, i.followup.sent))
        self.assertEqual(intern_store.load(self.db, STRANGER), before)

    def test_a_granted_user_gets_past_it(self):
        self.grant("user", GRANTED)
        i = interaction(GRANTED)
        self.drive(intern_commands.internships_profile.callback(i, None))
        content, kw = i.response.sent[0]
        self.assertNotEqual(content, REFUSAL)
        self.assertEqual(type(kw["view"]).__name__, "StartView")

    def test_the_owner_needs_no_grant(self):
        i = interaction(OWNER)
        self.drive(intern_commands.internships_ping.callback(i, None, None))
        self.assertNotEqual(i.response.sent[0][0], REFUSAL)

    def test_anyone_inside_a_granted_server_gets_past_it(self):
        self.grant("guild", SERVER)
        for name, call in command_calls().items():
            with self.subTest(command=name):
                i = interaction(STRANGER, SERVER)
                self.drive(call(i))
                self.assertFalse(refused(i))

    def test_in_a_dm_only_a_member_of_a_granted_server_gets_past_it(self):
        self.grant("guild", SERVER)
        with mock.patch.object(intern_ui, "bot", client_with({SERVER: {MEMBER}})):
            member, stranger = interaction(MEMBER), interaction(STRANGER)
            self.drive(intern_commands.internships_ping.callback(member, None, None))
            self.drive(intern_commands.internships_ping.callback(stranger, None, None))
        self.assertFalse(refused(member))
        self.assertTrue(refused(stranger))

    def test_a_revoked_user_is_refused_again(self):
        self.grant("user", GRANTED)
        access.revoke(self.db, "user", GRANTED)
        i = interaction(GRANTED)
        self.drive(intern_commands.internships_ping.callback(i, None, None))
        self.assertTrue(refused(i))

    def test_grants_that_cannot_be_read_let_only_the_owner_in(self):
        self.db.execute("DROP TABLE access_grants")
        stranger, owner = interaction(STRANGER, SERVER), interaction(OWNER)
        log = self.drive(intern_commands.internships_ping.callback(stranger, None, None))
        self.drive(intern_commands.internships_ping.callback(owner, None, None))
        self.assertTrue(refused(stranger))
        self.assertFalse(refused(owner))
        self.assertIn("failed: OperationalError", log)
        self.assertNotIn("access_grants", log)


@needs_discord
class TheOpenCommandsAnswerAnyone(_GateCase):
    def test_delete_shows_everything_held_and_offers_to_erase_it(self):
        self.enrol(STRANGER)
        i = interaction(STRANGER, ELSEWHERE)
        self.drive(intern_commands.internships_delete.callback(i))
        views = [kw.get("view") for _, kw in i.response.sent + i.followup.sent]
        self.assertNotEqual(i.response.sent[0][0], REFUSAL)
        self.assertEqual(type(views[-1]).__name__, "DeleteConfirmView")

    def test_help_explains_the_finder(self):
        i = interaction(STRANGER)
        self.drive(intern_commands.internships_help.callback(i))
        self.assertNotEqual(i.response.sent[0][0], REFUSAL)
        self.assertIn("/internships", i.response.sent[0][0])


@needs_discord
class TheRoleSuggestionsAreGated(_GateCase):
    def test_someone_without_access_is_suggested_nothing_and_nothing_is_read(self):
        read = []
        pconn = types.SimpleNamespace(execute=lambda *a: read.append(a))
        with mock.patch.object(intern_ui, "pconn", pconn), \
                mock.patch.object(intern_ui, "source", types.SimpleNamespace()):
            self.assertEqual(intern_commands._role_choices(STRANGER, ELSEWHERE, "intern"), [])
        self.assertEqual(read, [])

    def test_inside_a_granted_server_the_check_passes(self):
        self.grant("guild", SERVER)
        self.assertTrue(intern_ui.may_use(STRANGER, SERVER))
        self.assertFalse(intern_ui.may_use(STRANGER, ELSEWHERE))


if __name__ == "__main__":
    unittest.main()
