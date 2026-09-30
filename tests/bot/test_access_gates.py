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
import intern_delivery
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
    import intern_alert_views
    import intern_commands
    import intern_ui
    import intern_upload
    import intern_views
else:  # pragma: no cover - depends on the environment
    intern_alert_views = intern_commands = intern_ui = intern_upload = intern_views = None

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

BOT = pathlib.Path(__file__).resolve().parents[2] / "bot"
GATE, MAY_USE = "intern_ui.need_access", "intern_ui.may_use"
REFUSAL = "This bot is private. Ask whoever runs it for access."

#: Every `/internships` subcommand: "gated" checks access first, "open" never does.
COMMANDS = {
    "internships_profile": "gated", "internships_matches": "gated",
    "internships_recent": "gated", "internships_ping": "gated", "internships_info": "gated",
    "internships_help": "open",       # how it works and what it keeps: for anyone deciding
    "internships_delete": "open",     # removes data: never withheld
}
#: Every autocomplete. The field and place lists are the finder's fixed vocabulary;
#: the role list reads postings and the user's own matches, so `_role_choices` checks.
AUTOCOMPLETES = {"_role_autocomplete": "_role_choices", "_field_autocomplete": None,
                 "_where_autocomplete": None}

#: Every button, select and modal submit, by file and `Class.method` (or a module
#: function wired in as one). The persistent ones answer messages sent long ago; the
#: short-lived ones hold a draft for up to 15 minutes, so someone revoked partway
#: through must not save it.
CALLBACKS = {
    "intern_views.py": {
        "ProfileCardView.matches": "gated", "ProfileCardView.details": "gated",
        "ProfileCardView.filters": "gated", "ProfileCardView.upload": "gated",
        "_edit_saved": "gated",                       # the card's four selects
        "ProfileCardView.delete": "open",
        "DeleteConfirmView.delete_all": "open", "DeleteConfirmView.keep": "open",
        "RelaxView._apply": "gated",
        "DraftCardView._changed": "gated", "DraftCardView.save": "gated",
        "DraftCardView.details": "gated", "DraftCardView.filters": "gated",
        "DraftCardView.cancel": "open",
    },
    "intern_upload.py": {
        "StartView.upload": "gated", "StartView.manual": "gated",
        "UploadModal.on_submit": "gated",
        "ConsentView.read_resume": "gated", "ConsentView.proceed": "gated",
        "ConsentView.pick": "gated", "ConsentView.cancel": "open",
        "ResumeFailView.paste": "gated", "ResumeFailView.manual": "gated",
        "DetailsModal.on_submit": "gated", "FiltersModal.on_submit": "gated",
    },
    "intern_alert_views.py": {
        "AlertControlsView._on_hide": "gated",
        "AlertControlsView.pause": "open", "AlertControlsView.stop_alerts": "open",
        "DmCheckView.retry": "gated",
        "ResumeNowView.resume": "gated",
    },
}
UI_DECORATORS = ("discord.ui.button", "discord.ui.select")

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

    def test_debug_is_the_owners_now_and_no_longer_here(self):
        # /diayn debug; test_diayn_commands pins that every /diayn command asks first.
        self.assertNotIn("internships_debug", self.commands())


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


def _classes(module: ast.Module) -> dict:
    return {n.name: n for n in module.body if isinstance(n, ast.ClassDef)}


def _methods(cls: ast.ClassDef) -> dict:
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _wired(cls: ast.ClassDef, value: ast.AST) -> set:
    """The callbacks an expression hands Discord: `self.m`, a module function by name, or a
    factory `self.f(...)` whose inner callback calls `self.m`."""
    if isinstance(value, ast.Attribute) and chain(value.value) == "self":
        return {f"{cls.name}.{value.attr}"}
    if isinstance(value, ast.Name):
        return {value.id}
    if isinstance(value, ast.Call) and chain(value.func).startswith("self."):
        factory = _methods(cls).get(chain(value.func).split(".", 1)[1])
        return {f"{cls.name}.{chain(c.func).split('.', 1)[1]}" for c in calls_in_order(factory or cls)
                if chain(c.func).startswith("self.")} if factory else set()
    return set()


def ui_callbacks(name: str) -> set:
    """Every callback in the file: decorated buttons and selects, modal submits, and what
    is assigned to an item's `.callback` or wired through `_wire`."""
    found = set()
    for cls in _classes(tree(name)).values():
        for method in _methods(cls).values():
            if method.name == "on_submit" or any(
                    isinstance(d, ast.Call) and chain(d.func) in UI_DECORATORS
                    for d in method.decorator_list):
                found.add(f"{cls.name}.{method.name}")
        for node in ast.walk(cls):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Attribute) and t.attr == "callback" for t in node.targets):
                found |= _wired(cls, node.value)
            if isinstance(node, ast.Call) and chain(node.func) == "_wire" and len(node.args) == 3:
                found |= _wired(cls, node.args[2])
    return found


def qualified(name: str, qualname: str):
    if "." not in qualname:
        return function(tree(name), qualname)
    cls, method = qualname.split(".")
    return _methods(_classes(tree(name))[cls])[method]


class EveryButtonIsClassified(unittest.TestCase):
    def test_the_table_is_every_button_select_and_submit(self):
        for name, table in CALLBACKS.items():
            with self.subTest(file=name):
                self.assertEqual(ui_callbacks(name), set(table))

    def test_a_gated_one_checks_access_before_anything_else(self):
        for name, table in CALLBACKS.items():
            for qualname, kind in table.items():
                if kind == "gated":
                    with self.subTest(callback=qualname):
                        self.assertEqual(first_call(qualified(name, qualname)), GATE)

    def test_the_way_out_never_asks(self):
        # Deleting your data, and stopping or pausing alerts, work for anyone, always.
        for name, table in CALLBACKS.items():
            for qualname, kind in table.items():
                if kind == "open":
                    with self.subTest(callback=qualname):
                        fn = qualified(name, qualname)
                        self.assertEqual(calls_to(fn, GATE) + calls_to(fn, MAY_USE), [])

    def test_no_base_view_or_modal_checks_access(self):
        # In a base class the check would reach the exemptions too.
        for cls in _classes(tree("intern_ui.py")).values():
            for method in _methods(cls).values():
                with self.subTest(method=f"{cls.name}.{method.name}"):
                    self.assertEqual(calls_to(method, "need_access") + calls_to(method, "may_use"), [])


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

    def test_a_member_of_a_granted_server_is_refused_inside_another_server(self):
        self.grant("guild", SERVER)
        with mock.patch.object(intern_ui, "bot", client_with({SERVER: {MEMBER}})):
            elsewhere = interaction(MEMBER, ELSEWHERE)
            self.drive(intern_commands.internships_ping.callback(elsewhere, None, None))
        self.assertTrue(refused(elsewhere))

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
class ARevocationStopsTheDmsNotYetSent(_GateCase):
    """A tick asks who may be DMed before each DM, against the grants as they are then,
    so `/diayn revoke` or `diayn.py revoke` stops the DMs a running tick has not sent."""

    def test_the_answer_is_the_grants_as_they_are_when_asked(self):
        self.grant("user", GRANTED)
        allowed = intern_ui.dm_access()
        self.assertTrue(allowed(GRANTED))

        access.revoke(self.db, "user", GRANTED)

        self.assertFalse(allowed(GRANTED))

    def test_revoked_between_two_sends_the_second_is_not_sent(self):
        for uid in (GRANTED, MEMBER):                  # delivered in this order, by id
            self.grant("user", uid)
            self.enrol(uid, alerts="hourly")
        rows = intern_delivery.intern_match.tag_rows([(
            1, "greenhouse", "ext1", "Acme", "Software Engineer Intern", "Irvine, CA",
            "https://example.com/jobs/1", NOW + 60, NOW + 60)])
        sent = []

        async def send_dm(uid, msg):
            sent.append(uid)
            if uid == GRANTED:
                access.revoke(self.db, "user", MEMBER)

        async def load_window():
            return list(rows)

        with mock.patch.object(intern_delivery, "SEND_GAP_S", 0):
            report = asyncio.run(intern_delivery.run_tick(
                self.db, load_window=load_window, send_dm=send_dm, now=NOW + 2 * 3600,
                companies_watched=0, allowed=intern_ui.dm_access()))

        self.assertEqual(sent, [GRANTED])
        self.assertEqual((report.due, report.sent), (2, 1))
        self.assertEqual(intern_store.load(self.db, MEMBER).cursor, NOW)   # left for access back


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


class Attachment:
    def __init__(self) -> None:
        self.filename, self.content_type, self.size, self.reads = "cv.pdf", "application/pdf", 10, 0

    async def read(self) -> bytes:
        self.reads += 1
        return b"%PDF-"


def on(build, act):
    """A press: builds its view inside the running loop, as discord.py needs, then acts."""
    async def press(i):
        await act(build(), i)
    return press


async def never(*args):
    raise AssertionError("a refused submit reached the step after it")


def gated_presses(p, attachment):
    """Each gated callback, pressed by the person `p` belongs to."""
    V, U, A = intern_views, intern_upload, intern_alert_views
    uid = p.user_id

    def draft():
        return V.DraftCardView(p, evidence=None, replacing=None, header="Draft")

    def consent(then_modal=False):
        return lambda: U.ConsentView(uid, None if then_modal else attachment, "pdf",
                                     then_modal=then_modal)
    return {
        "ProfileCardView.matches": on(V.ProfileCardView, lambda v, i: v.matches.callback(i)),
        "ProfileCardView.details": on(V.ProfileCardView, lambda v, i: v.details.callback(i)),
        "ProfileCardView.filters": on(V.ProfileCardView, lambda v, i: v.filters.callback(i)),
        "ProfileCardView.upload": on(V.ProfileCardView, lambda v, i: v.upload.callback(i)),
        "_edit_saved": lambda i: V._edit_saved(i, 3, ["off:9"]),
        "RelaxView._apply": on(lambda: V.RelaxView(uid, ()),
                               lambda v, i: v._apply(i, types.SimpleNamespace(changes={"fields": ()}))),
        "DraftCardView._changed": on(draft, lambda v, i: v._changed(i, 0, ["finance"])),
        "DraftCardView.save": on(draft, lambda v, i: v.save.callback(i)),
        "DraftCardView.details": on(draft, lambda v, i: v.details.callback(i)),
        "DraftCardView.filters": on(draft, lambda v, i: v.filters.callback(i)),
        "StartView.upload": on(lambda: U.StartView(uid), lambda v, i: v.upload.callback(i)),
        "StartView.manual": on(lambda: U.StartView(uid), lambda v, i: v.manual.callback(i)),
        "UploadModal.on_submit": on(U.UploadModal, lambda v, i: v.on_submit(i)),
        "ConsentView.read_resume": on(consent(), lambda v, i: v.read_resume.callback(i)),
        "ConsentView.proceed": on(consent(True), lambda v, i: v.proceed.callback(i)),
        "ConsentView.pick": on(consent(), lambda v, i: v.pick.callback(i)),
        "ResumeFailView.paste": on(lambda: U.ResumeFailView(uid), lambda v, i: v.paste.callback(i)),
        "ResumeFailView.manual": on(lambda: U.ResumeFailView(uid), lambda v, i: v.manual.callback(i)),
        "DetailsModal.on_submit": on(lambda: U.DetailsModal(p, on_done=never),
                                     lambda v, i: v.on_submit(i)),
        "FiltersModal.on_submit": on(lambda: U.FiltersModal(p, on_done=never),
                                     lambda v, i: v.on_submit(i)),
        "AlertControlsView._on_hide": on(A.AlertControlsView, lambda v, i: v._on_hide(i)),
        "DmCheckView.retry": on(A.DmCheckView, lambda v, i: v.retry.callback(i)),
        "ResumeNowView.resume": on(lambda: A.ResumeNowView(uid), lambda v, i: v.resume.callback(i)),
    }


@needs_discord
class TheButtonsAreGated(_GateCase):
    def setUp(self):
        super().setUp()
        self.dms = []

        async def send_dm(uid, msg):
            self.dms.append(uid)
        patch = mock.patch.object(intern_ui, "send_dm", send_dm)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_presses_cover_the_table(self):
        gated = {q for table in CALLBACKS.values() for q, kind in table.items() if kind == "gated"}
        self.assertEqual(set(gated_presses(self.enrol(STRANGER), Attachment())), gated)

    def test_each_refuses_someone_without_access_and_changes_nothing(self):
        before = self.enrol(STRANGER, alerts="daily", paused_until=NOW + 3600, dm_failures=3)
        attachment = Attachment()
        for name, press in gated_presses(before, attachment).items():
            with self.subTest(callback=name):
                i = interaction(STRANGER, ELSEWHERE, component=True)
                self.drive(press(i))
                self.assertTrue(refused(i), (i.response.sent, i.response.modals, i.edited))
        self.assertEqual(intern_store.load(self.db, STRANGER), before)
        self.assertEqual(intern_store.seen_hashes(self.db, STRANGER), frozenset())
        self.assertEqual((self.dms, attachment.reads), ([], 0))

    def test_a_refused_consent_drops_the_file_it_was_holding(self):
        attachment = Attachment()

        async def press():
            view = intern_upload.ConsentView(STRANGER, attachment, "pdf")
            await view.read_resume.callback(interaction(STRANGER, component=True))
            return view
        view = asyncio.run(press())
        self.assertIsNone(view.attachment)
        self.assertEqual(attachment.reads, 0)

    def test_someone_granted_gets_past_it(self):
        self.grant("user", GRANTED)
        i = interaction(GRANTED, component=True)
        self.drive(on(lambda: intern_upload.StartView(GRANTED),
                      lambda v, i: v.upload.callback(i))(i))
        self.assertEqual(type(i.response.modals[0]).__name__, "UploadModal")


@needs_discord
class TheWayOutIsAlwaysOpen(_GateCase):
    """Without access, anyone may still see and erase what is held, and stop or pause alerts."""

    def press(self, build, act, uid=STRANGER):
        i = interaction(uid, ELSEWHERE, component=True)
        self.drive(on(build, act)(i))
        self.assertFalse(any(content == REFUSAL for content, _ in i.response.sent))
        return i

    def test_the_cards_delete_button_shows_everything_and_offers_to_erase_it(self):
        self.enrol(STRANGER)
        i = self.press(intern_views.ProfileCardView, lambda v, i: v.delete.callback(i))
        views = [kw.get("view") for _, kw in i.response.sent + i.followup.sent]
        self.assertEqual(type(views[-1]).__name__, "DeleteConfirmView")

    def test_its_confirmation_erases_everything(self):
        self.enrol(STRANGER)
        intern_store.record_sent(self.db, STRANGER, ["abcd"], NOW)
        i = self.press(lambda: intern_views.DeleteConfirmView(STRANGER),
                       lambda v, i: v.delete_all.callback(i))
        self.assertIsNone(intern_store.load(self.db, STRANGER))
        self.assertEqual(intern_store.seen_hashes(self.db, STRANGER), frozenset())
        self.assertEqual(i.response.edits[0]["content"], intern_text.deleted_text())

    def test_keep_it_and_both_cancels_answer(self):
        self.enrol(STRANGER)
        p = intern_store.load(self.db, STRANGER)
        for build, act in (
                (lambda: intern_views.DeleteConfirmView(STRANGER), lambda v, i: v.keep.callback(i)),
                (lambda: intern_views.DraftCardView(p, evidence=None, replacing=None),
                 lambda v, i: v.cancel.callback(i)),
                (lambda: intern_upload.ConsentView(STRANGER, Attachment(), "pdf"),
                 lambda v, i: v.cancel.callback(i))):
            with self.subTest(view=build.__name__):
                self.assertEqual(len(self.press(build, act).response.edits), 1)
        self.assertEqual(intern_store.load(self.db, STRANGER), p)

    def test_stop_turns_alerts_off(self):
        self.enrol(STRANGER, alerts="hourly")
        self.press(intern_alert_views.AlertControlsView, lambda v, i: v.stop_alerts.callback(i))
        self.assertEqual(intern_store.load(self.db, STRANGER).alerts, "off")

    def test_pause_pauses_them_for_a_week(self):
        self.enrol(STRANGER, alerts="hourly")
        self.press(intern_alert_views.AlertControlsView, lambda v, i: v.pause.callback(i))
        self.assertIsNotNone(intern_store.load(self.db, STRANGER).paused_until)


if __name__ == "__main__":
    unittest.main()
