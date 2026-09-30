"""
That DIAYN's Discord client wires the internship finder in, and wires it in safely.

    python3 -m unittest discover -s tests -p 'test_intern_wiring.py'   # no install needed
    .venv/bin/python -m unittest discover -s tests -p 'test_intern_wiring.py'

Everything the finder does lives in `bot/intern_*.py` and is tested there.
What those tests cannot see is the handful of lines in `bot/app.py` that
decide whether any of it runs: which loop starts when, what happens when the
finder's tables cannot be made, which databases are opened and how. Each is
one line somebody can delete or invert without a single other test noticing.

The first half reads `app.py` as source, so it needs nothing installed and
runs on a bare box too. The classes further down import it, build the client
without ever logging in, and drive it with fakes; they skip when discord.py is
missing, the only dependency that earns a skip.

Gates are *evaluated*, not searched for. A text search for `intern_error is
None` also matches `intern_error is not None`. Instead every `if` between a
call and its function is compiled and run against each combination of the
states in the spec's failure table (3.2), so an inverted or missing gate is a
failure rather than a match.
"""

import ast
import asyncio
import dataclasses
import functools
import io
import os
import pathlib
import sqlite3
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

import intern_delivery
import intern_match
import intern_profile
import intern_store
import internship_poller as poller
import postings_contract
import postings_source
import test_postings_contract

try:
    import discord
except ModuleNotFoundError as missing:  # pragma: no cover - depends on the environment
    if missing.name != "discord":
        raise
    discord = None

#: A stub `discord` another test installs has no `__file__` (see test_intern_surface).
REAL_DISCORD = discord is not None and getattr(discord, "__file__", None) is not None
if REAL_DISCORD:
    import app
    import intern_alert_views
    import intern_commands
    import intern_ui
else:  # pragma: no cover - depends on the environment
    app = intern_alert_views = intern_commands = intern_ui = None

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

APP_PATH = pathlib.Path(__file__).resolve().parents[2] / "bot" / "app.py"

#: What `intern_error` holds after `intern_store.init_db` failed at start-up.
BROKEN = "OperationalError: database is locked"
#: Any open postings connection; only `is not None` is ever asked of it.
CONNECTED = object()
#: 3.2's table: postings healthy or not, times finder healthy or not.
STATES = tuple({"pconn": pconn, "intern_error": error}
               for pconn in (None, CONNECTED) for error in (None, BROKEN))
#: What intern_ui is handed, each from the stores app.py opened, `bot` from the client.
SHARED = ("db", "pconn", "pconn_error", "source", "postings_path", "bot", "intern_error")


# ── Reading the source ────────────────────────────────────────────────────────
# Read on first use rather than at import, so a missing file is one test error
# with a traceback rather than a whole-suite import failure.

@functools.lru_cache(maxsize=None)
def _source() -> str:
    return APP_PATH.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _tree() -> ast.Module:
    return ast.parse(_source())


@functools.lru_cache(maxsize=None)
def _parents() -> dict:
    """Each node's parent, so a call can be walked back out to its function."""
    return {child: node for node in ast.walk(_tree())
            for child in ast.iter_child_nodes(node)}


def _dotted(node):
    """`a.b.c` for a chain of names and attributes, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _calls(dotted: str) -> list:
    """Every call in the file whose callee is spelled `dotted`."""
    return [node for node in ast.walk(_tree())
            if isinstance(node, ast.Call) and _dotted(node.func) == dotted]


def _function_of(node):
    """The function a node sits in, or None at module scope."""
    node = _parents().get(node)
    while node is not None and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        node = _parents().get(node)
    return node


def _branches(node) -> list:
    """The `if` tests between `node` and its function, each with the branch
    taken to reach it: True through the body, False through the `else`."""
    taken = []
    child, parent = node, _parents().get(node)
    while parent is not None and not isinstance(
            parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
        if isinstance(parent, ast.If):
            if any(child is s for s in parent.body):
                taken.append((parent.test, True))
            elif any(child is s for s in parent.orelse):
                taken.append((parent.test, False))
        child, parent = parent, _parents().get(parent)
    return taken


def _decide(condition: ast.expr, **names) -> bool:
    """What an `if` in app.py decides, with its free names bound to `names`."""
    return bool(eval(compile(ast.Expression(condition), "<condition>", "eval"), names))


def _world(*, pconn=CONNECTED, intern_error=None, delivery_running=False,
           member_is_bot=False, pconn_error=None) -> dict:
    """The names the start-up hook, the member events and the opens read, bound to one state."""
    loops = types.SimpleNamespace(
        intern_delivery_loop=types.SimpleNamespace(is_running=lambda: delivery_running))
    stores = types.SimpleNamespace(pconn=pconn, intern_error=intern_error, pconn_error=pconn_error)
    return {"self": types.SimpleNamespace(stores=stores), "intern_commands": loops,
            "member": types.SimpleNamespace(bot=member_is_bot), "pconn_error": pconn_error,
            "pconn": pconn, "intern_error": intern_error}


def _reached(node, **state) -> bool:
    """Whether control gets to `node` in the given state."""
    world = _world(**state)
    return all(_decide(test, **world) is branch for test, branch in _branches(node))


def _only(nodes: list, what: str):
    if len(nodes) != 1:
        raise AssertionError(f"expected exactly one {what}, found {len(nodes)}")
    return nodes[0]


def _named(name: str, kind=(ast.FunctionDef, ast.AsyncFunctionDef)):
    """The one def (or class) of that name, at any depth."""
    return _only([n for n in ast.walk(_tree()) if isinstance(n, kind) and n.name == name],
                 f"def {name}")


def _client_class() -> ast.ClassDef:
    return _named("DiaynBot", ast.ClassDef)


def _method(name: str):
    return _only([n for n in _client_class().body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name],
                 f"DiaynBot.{name}")


def _position(node) -> tuple:
    return node.lineno, node.col_offset


def _run_function(name: str, **names) -> dict:
    """Runs one module-level function of app.py, compiled on its own, with its free
    names bound to `names`; returns the namespace it ran in."""
    fn = _named(name)
    namespace = dict(names)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), f"<{name}>", "exec"), namespace)
    return namespace


def _open_users(init_db, grants_init=None) -> tuple:
    """Runs `_open_users` with `init_db` standing in for the finder's and `grants_init`
    (default: a healthy one) for the access grants'. Returns what it returned and what
    it printed to stderr."""
    stderr = io.StringIO()
    namespace = _run_function("_open_users", intern_store=types.SimpleNamespace(init_db=init_db),
                              access=types.SimpleNamespace(init_db=grants_init or _healthy),
                              sqlite3=sqlite3, sys=types.SimpleNamespace(stderr=stderr),
                              postings_source=postings_source)
    result = namespace["_open_users"](":memory:")
    if result[0] is not None:
        result[0].close()
    return result, stderr.getvalue()


def _healthy(db):
    return None


def _locked(db):
    raise sqlite3.OperationalError("database is locked")


def _module_names() -> set:
    """Every name bound by a module-level def, class or assignment."""
    bound = set()
    for s in _tree().body:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(s.name)
        targets = s.targets if isinstance(s, ast.Assign) else [getattr(s, "target", None)]
        for target in targets:
            for node in ast.walk(target) if target is not None else ():
                if isinstance(node, ast.Name):
                    bound.add(node.id)
    return bound


def _module_scope():
    """Nodes that run at import: everything outside a def body."""
    pending = list(_tree().body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


# ── The tests: reading app.py ─────────────────────────────────────────────────

class ImportingTheClientDoesNothing(unittest.TestCase):
    """The tests import app.py, and so may any tool. Importing it must open no
    database, build no client, log in nowhere and change nothing in discord.py."""

    MODULES = ("access", "intern_store", "intern_ui", "intern_commands", "postings_source")

    def test_the_finder_modules_it_wires_are_imported_at_module_scope(self):
        imported = {a.name for s in _tree().body if isinstance(s, ast.Import) for a in s.names}
        for name in self.MODULES:
            with self.subTest(module=name):
                self.assertIn(name, imported)

    def test_nothing_is_called_at_import_that_opens_builds_or_patches(self):
        banned = ("sqlite3.connect", "postings_source.open_from_env", "intern_store.init_db",
                  "access.init_db", "open_stores", "build", "DiaynBot", "install_send_defaults", "setattr",
                  "print", "wire")
        called = [_dotted(n.func) for n in _module_scope() if isinstance(n, ast.Call)]
        for name in banned:
            with self.subTest(call=name):
                self.assertNotIn(name, called)

    def test_the_scraper_is_never_imported_at_module_scope(self):
        # Its settings are read when the client is built, after boot() has bound them.
        top = {a.name.split(".")[0] for s in _tree().body
               if isinstance(s, (ast.Import, ast.ImportFrom)) for a in s.names}
        self.assertNotIn("internship_poller", top)


class TheFinderFailsAlone(unittest.TestCase):
    """W1. A finder whose tables cannot be made turns off; the bot starts anyway."""

    def block(self) -> ast.Try:
        (call,) = _calls("intern_store.init_db")
        node = _parents()[call]
        while not isinstance(node, ast.Try):
            node = _parents()[node]
        return node

    def test_init_db_is_called_once_and_only_inside_its_try_in_open_users(self):
        (call,) = _calls("intern_store.init_db")
        self.assertIs(_function_of(call), _named("_open_users"))
        self.assertTrue(any(call is n for s in self.block().body for n in ast.walk(s)))

    def test_the_access_grants_are_made_in_the_same_try_after_the_finders_tables(self):
        # Without them nobody but the owner could be let in, so they fail with the finder.
        (call,) = _calls("access.init_db")
        (finder,) = _calls("intern_store.init_db")
        self.assertTrue(any(call is n for s in self.block().body for n in ast.walk(s)))
        self.assertGreater(_position(call), _position(finder))

    def test_grants_that_cannot_be_made_turn_the_finder_off(self):
        (db, error), printed = _open_users(_healthy, _locked)
        self.assertIsNone(db)
        self.assertEqual(error, BROKEN)
        self.assertTrue(printed.startswith("internship finder disabled: "), printed)

    def test_the_try_catches_sqlite_errors_and_nothing_broader(self):
        # A broader catch would hide a typo in intern_store as "finder disabled".
        self.assertEqual([_dotted(h.type) for h in self.block().handlers], ["sqlite3.Error"])

    def test_the_connection_is_made_inside_the_same_try(self):
        # A users.db that cannot even be opened costs the finder, not the bot.
        (connect,) = _calls("sqlite3.connect")
        self.assertTrue(any(connect is n for s in self.block().body for n in ast.walk(s)))

    def test_a_healthy_init_leaves_the_finder_on(self):
        (db, error), printed = _open_users(_healthy)
        self.assertIsNotNone(db)
        self.assertIsNone(error)
        self.assertEqual(printed, "")

    def test_a_failed_init_turns_the_finder_off_and_names_the_error(self):
        (db, error), _ = _open_users(_locked)
        self.assertIsNone(db)
        self.assertEqual(error, BROKEN)

    def test_the_failure_is_logged_in_the_words_an_operator_looks_for(self):
        _, printed = _open_users(_locked)
        self.assertTrue(printed.startswith("internship finder disabled: "), printed)
        self.assertIn("OperationalError", printed)

    def test_it_follows_the_postings_open(self):
        opens = _named("open_stores")
        (postings,) = [c for c in _calls("postings_source.open_from_env") if _function_of(c) is opens]
        (users,) = [c for c in _calls("_open_users") if _function_of(c) is opens]
        self.assertGreater(_position(users), _position(postings))


class TheSharedStateIsHandedToTheFinder(unittest.TestCase):
    """W10. intern_ui never imports the client, so the client hands it what it needs."""

    def injections(self) -> dict:
        """attr -> [value] for each `intern_ui.<attr> = ...` in the file."""
        found = {}
        for node in ast.walk(_tree()):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = _dotted(node.targets[0]) or ""
                if target.startswith("intern_ui."):
                    found.setdefault(target.split(".", 1)[1], []).append(node)
        return found

    def test_each_is_injected_once_in_wire_from_the_stores_or_the_client(self):
        found = self.injections()
        self.assertEqual(set(found), set(SHARED))
        for attr in SHARED:
            with self.subTest(attr=attr):
                (assign,) = found[attr]
                self.assertIs(_function_of(assign), _named("wire"))
                self.assertEqual(_branches(assign), [])
                expected = "client" if attr == "bot" else f"stores.{attr}"
                self.assertEqual(_dotted(assign.value), expected)

    def test_no_poller_is_handed_over(self):
        self.assertNotIn("poller", self.injections())

    def test_the_client_is_wired_after_the_stores_are_open(self):
        build = _named("build")
        (opened,) = [c for c in _calls("open_stores") if _function_of(c) is build]
        (wired,) = [c for c in _calls("wire") if _function_of(c) is build]
        self.assertGreater(_position(wired), _position(opened))


class ThePostingsDbOpensReadOnly(unittest.TestCase):
    """The one open, at start-up: postings_source decides how, and never raises. The
    finder reopens the file later from `postings_path` (a restore, a failed first open)."""

    def call(self) -> ast.Call:
        return _only(_calls("postings_source.open_from_env"), "postings_source.open_from_env()")

    def test_it_is_opened_once_with_the_settings_it_was_given(self):
        call = self.call()
        self.assertIs(_function_of(call), _named("open_stores"))
        self.assertEqual(_branches(call), [])
        self.assertEqual([_dotted(a) for a in call.args], ["settings"])

    def test_it_sets_the_connection_the_source_and_the_error_together(self):
        assign = _parents()[self.call()]
        self.assertIsInstance(assign, ast.Assign)
        self.assertEqual([_dotted(e) for e in assign.targets[0].elts],
                         ["pconn", "source", "pconn_error"])

    def test_the_bot_never_creates_or_writes_it(self):
        for name in ("db_init", "cmd_sweep", "cmd_watch", "upgrade_db"):
            with self.subTest(call=name):
                self.assertNotIn(name, _source())

    def test_a_failure_is_logged_in_the_words_an_operator_looks_for(self):
        (call,) = [c for c in _calls("print") if isinstance(c.args[0], ast.JoinedStr)
                   and "internship tracker disabled" in ast.unparse(c.args[0])]
        self.assertIs(_function_of(call), _named("open_stores"))
        self.assertTrue(_reached(call, pconn_error="ContractError: postings.db lacks seen"))
        self.assertFalse(_reached(call, pconn_error=None))

    def test_the_path_to_reopen_is_the_one_the_settings_name(self):
        (keyword,) = [k for c in _calls("Stores") for k in c.keywords if k.arg == "postings_path"]
        self.assertEqual(_dotted(keyword.value), "settings.postings_db")


class TheCommandIsRegisteredOnce(unittest.TestCase):
    """W2."""

    def registration(self):
        return _only([c for c in _calls("self.tree.add_command")
                      if [_dotted(a) for a in c.args] == ["intern_commands.internships"]],
                     "self.tree.add_command(intern_commands.internships)")

    def test_the_finder_group_is_added_to_the_tree_exactly_once(self):
        self.registration()

    def test_it_is_added_whenever_the_client_is_built(self):
        # Added unconditionally before any sync, so the commands exist even with the
        # finder off: /internships then answers "switched off" rather than nothing.
        self.assertIs(_function_of(self.registration()), _method("__init__"))
        self.assertEqual(_branches(self.registration()), [])

    def test_no_internships_group_is_built_here(self):
        built = [c for c in _calls("app_commands.Group") if any(
            k.arg == "name" and isinstance(k.value, ast.Constant)
            and k.value.value == "internships" for k in c.keywords)]
        self.assertEqual(built, [])
        self.assertEqual(len([c for c in ast.walk(_tree()) if isinstance(c, ast.Call)
                              and (_dotted(c.func) or "").endswith("tree.add_command")]), 1)


class TheLoopStartsOnlyWhenItCanRun(unittest.TestCase):
    """W3 and W4, evaluated over 3.2's failure table."""

    def start(self):
        return _only(_calls("intern_commands.intern_delivery_loop.start"),
                     "intern_delivery_loop.start()")

    def test_it_is_started_from_exactly_one_place_in_the_start_up_hook(self):
        self.assertIs(_function_of(self.start()), _method("setup_hook"))

    def test_delivery_needs_the_finder_and_not_postings(self):
        # Without the finder's tables every tick would query tables that are not
        # there. Without postings there is nothing to match, but the loop still
        # runs the daily housekeeping the privacy notice promises; it skips the
        # alerts itself (DeliveryLoopKeepsTheRetentionPromise, below).
        for state in STATES:
            with self.subTest(**state):
                self.assertIs(_reached(self.start(), **state), state["intern_error"] is None)

    def test_a_running_loop_is_never_started_again(self):
        # start() on a running loop raises.
        self.assertFalse(_reached(self.start(), delivery_running=True))

    def test_there_is_no_other_loop_and_no_sweep(self):
        # serve()'s client.start() is the login, not a loop.
        starts = [c for c in ast.walk(_tree()) if isinstance(c, ast.Call)
                  and (_dotted(c.func) or "").endswith(".start")
                  and _function_of(c) is not _named("serve")]
        self.assertEqual(starts, [self.start()])
        self.assertNotIn("internship_sweep", _source())


class NothingIsMigratedOnStartUp(unittest.TestCase):
    """W5. The old tracker's subscribers are copied once, by hand, with
    `diayn.py import-legacy`. A start-up that copied them again could bring back
    someone who had since deleted their data."""

    def test_the_client_never_migrates(self):
        for name in ("migrate_once", "migrate_legacy", "read_legacy", "write_migrated"):
            with self.subTest(name=name):
                self.assertEqual([n for n in ast.walk(_tree()) if isinstance(n, ast.Call)
                                  and (_dotted(n.func) or "").split(".")[-1] == name], [])


class PersistentViewsAreReattached(unittest.TestCase):
    """W9. Buttons on messages sent before a restart stay clickable."""

    def adds(self):
        return _calls("self.add_view")

    def test_every_persistent_view_is_added_while_the_finder_is_healthy(self):
        loop = _only([n for n in ast.walk(_method("setup_hook")) if isinstance(n, ast.For)
                      and isinstance(n.iter, ast.Call)
                      and _dotted(n.iter.func) == "intern_commands.persistent_views"],
                     "for loop over intern_commands.persistent_views()")
        add = _only([c for c in self.adds() if [_dotted(a) for a in c.args]
                     == [_dotted(loop.target)] and _parents()[_parents()[c]] is loop],
                    "self.add_view(<loop item>) in that loop")
        for state in STATES:
            with self.subTest(**state):
                self.assertIs(_reached(add, **state), state["intern_error"] is None)

    def test_those_are_the_only_views_the_client_adds(self):
        self.assertEqual(len(self.adds()), 1)
        self.assertNotIn("LegacyDigestView", _source())


class LeavingAndRejoiningReachTheFinder(unittest.TestCase):
    """W8. Leaving pauses alerts and starts the 30-day clock; rejoining stops it."""

    HANDLERS = (("on_member_remove", "member_left"), ("on_member_join", "member_joined"))

    def test_both_handlers_are_the_clients_own_event_methods(self):
        # discord.Client dispatches an event to the method of the same name.
        for name, _ in self.HANDLERS:
            with self.subTest(handler=name):
                self.assertIsInstance(_method(name), ast.AsyncFunctionDef)

    def test_each_calls_the_finder_only_while_it_is_healthy(self):
        for name, callee in self.HANDLERS:
            call = _only(_calls(f"intern_commands.{callee}"), f"intern_commands.{callee}()")
            with self.subTest(handler=name):
                self.assertIs(_function_of(call), _method(name))
                self.assertEqual([_dotted(a) for a in call.args], ["member"])
                self.assertTrue(_reached(call, intern_error=None))
                self.assertFalse(_reached(call, intern_error=BROKEN))

    def test_bots_joining_or_leaving_are_ignored(self):
        for name, callee in self.HANDLERS:
            call = _only(_calls(f"intern_commands.{callee}"), f"intern_commands.{callee}()")
            with self.subTest(handler=name):
                self.assertFalse(_reached(call, member_is_bot=True))


class MessagePackingComesFromOneModule(unittest.TestCase):
    """W7. The finder packs its replies with message_pack; a copy here would drift."""

    def test_the_client_defines_no_packer_of_its_own(self):
        self.assertEqual(sorted(_module_names() & {"_pack", "pack", "MAX_CHUNK"}), [])


class TheOldTrackerIsGone(unittest.TestCase):
    """W6. None of the old channel tracker, and none of its tables, came along."""

    MARKERS = ("is_tech=1", "intern_pings", "InternshipDigestView", "internships:digest",
               "CREATE TABLE", "ALTER TABLE", "INTERN_SWEEP", "importlib")
    REMOVED = ("SWEEP_MINUTES", "ANNOUNCE_MINUTES", "ANNOUNCE_MAX", "RECENT_DAYS_DEFAULT",
               "CATEGORY_NAMES", "_format_role", "_get_prefs", "_set_ping", "_private",
               "_ledger_seeded", "_pending", "internship_sweep", "_tracker_disabled",
               "internships", "_find_posting", "poller", "SWEEP_MODE")

    def test_none_of_the_old_trackers_markers_remain(self):
        for marker in self.MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, _source())

    def test_none_of_its_module_level_names_survive(self):
        self.assertEqual(sorted(_module_names() & set(self.REMOVED)), [])


class TheClientAsksOnlyForWhatItNeeds(unittest.TestCase):
    """The members intent, which is privileged, and nothing else beyond the defaults:
    no message content and no presences, which the finder never reads."""

    def test_neither_message_content_nor_presences_is_ever_asked_for(self):
        for name in ("message_content", "presences"):
            with self.subTest(intent=name):
                granted = [n for n in ast.walk(_tree()) if isinstance(n, ast.Assign)
                           and any((_dotted(t) or "").endswith(f".{name}") for t in n.targets)
                           and not (isinstance(n.value, ast.Constant) and n.value.value is False)]
                self.assertEqual(granted, [])

    def test_there_are_no_prefix_commands(self):
        self.assertNotIn("command_prefix", _source())
        self.assertNotIn("commands.Bot", _source())


# ── The client, built and driven (needs discord.py) ───────────────────────────

class _Owner:
    """What Messageable.send, InteractionResponse.send_message and Webhook.send are
    called on, reduced to the one thing the send defaults read: whose it is."""

    def __init__(self, client) -> None:
        self._state = types.SimpleNamespace(_get_client=lambda: client)


class _ResponseOwner:
    """An InteractionResponse, which reaches its client through its interaction."""

    def __init__(self, client) -> None:
        self._parent = types.SimpleNamespace(_state=types.SimpleNamespace(_get_client=lambda: client))


def stores(**overrides):
    fields = {"db": None, "intern_error": None, "pconn": None, "source": None,
              "pconn_error": None, "postings_path": "/nowhere/postings.db"}
    return app.Stores(**{**fields, **overrides})


class _ClientCase(unittest.TestCase):
    """Builds DiaynBot instances that never log in, with discord.py's three send paths
    restored afterwards, whatever the client patched."""

    def setUp(self):
        for cls, name in app.SEND_PATHS:
            patch = mock.patch.object(cls, name, getattr(cls, name))
            patch.start()
            self.addCleanup(patch.stop)

    def client(self, **overrides):
        return app.DiaynBot(stores(**overrides))


@needs_discord
class TheIntents(_ClientCase):
    def test_the_defaults_plus_members_and_nothing_privileged_besides(self):
        intents = app.intents()
        self.assertTrue(intents.members)
        self.assertFalse(intents.message_content)
        self.assertFalse(intents.presences)
        expected = discord.Intents.default()
        expected.members = True
        self.assertEqual(intents.value, expected.value)

    def test_the_client_is_built_with_them(self):
        self.assertEqual(self.client().intents.value, app.intents().value)


@needs_discord
class TheSendDefaultsSuppressLinkPreviews(_ClientCase):
    """Job listings carry links, and Discord would draw a preview card for each,
    burying the text. Every message this client sends defaults to no previews; a
    caller can still ask for them, a message with an embed of its own is left alone,
    and nothing sent for any other client changes."""

    def setUp(self):
        super().setUp()
        self.sent = []

        async def recording(target, *args, **kwargs):
            self.sent.append(kwargs)

        for cls, name in app.SEND_PATHS:
            patch = mock.patch.object(cls, name, recording)
            patch.start()
            self.addCleanup(patch.stop)
        self.bot = self.client()             # installs the defaults over `recording`

    def send(self, owner, **kwargs):
        for cls, name in app.SEND_PATHS:
            asyncio.run(getattr(cls, name)(owner, "text", **kwargs))
        sent, self.sent = self.sent, []
        return sent

    def owners(self, client):
        return (_Owner(client), _ResponseOwner(client))

    def test_this_clients_sends_default_to_no_previews_on_all_three_paths(self):
        for owner in self.owners(self.bot):
            with self.subTest(owner=type(owner).__name__):
                sent = self.send(owner)
                self.assertEqual(len(sent), 3)
                self.assertTrue(all(kw.get("suppress_embeds") is True for kw in sent))

    def test_a_caller_may_still_ask_for_previews(self):
        sent = self.send(_Owner(self.bot), suppress_embeds=False)
        self.assertTrue(all(kw["suppress_embeds"] is False for kw in sent))

    def test_a_message_with_its_own_embed_is_left_alone(self):
        for key in ("embed", "embeds"):
            with self.subTest(key=key):
                sent = self.send(_Owner(self.bot), **{key: object()})
                self.assertTrue(all("suppress_embeds" not in kw for kw in sent))

    def test_another_clients_sends_are_untouched(self):
        other = object()
        for owner in (*self.owners(other), object()):
            with self.subTest(owner=type(owner).__name__):
                self.assertTrue(all("suppress_embeds" not in kw for kw in self.send(owner)))

    def test_a_second_client_does_not_wrap_the_paths_twice(self):
        before = [getattr(cls, name) for cls, name in app.SEND_PATHS]
        second = self.client()
        self.assertEqual([getattr(cls, name) for cls, name in app.SEND_PATHS], before)
        self.assertTrue(all(kw.get("suppress_embeds") for kw in self.send(_Owner(second))))


@needs_discord
class TheGlobalSyncKeepsTheEntryPoint(_ClientCase):
    """Discord creates an Entry Point command itself for an application with
    Activities. discord.py knows nothing of it, so a plain sync would leave it out,
    which Discord reads as a request to delete it and refuses the whole update."""

    def run_sync(self, existing):
        bot = self.client()
        bot._connection.application_id = 1
        calls = {}

        async def get_global_commands(application_id):
            calls["read"] = application_id
            return existing

        async def bulk_upsert_global_commands(application_id, *, payload):
            calls["written"] = payload

        bot.http.get_global_commands = get_global_commands
        bot.http.bulk_upsert_global_commands = bulk_upsert_global_commands
        asyncio.run(app.sync_global_commands(bot))
        return calls

    def test_the_entry_point_is_carried_over_and_nothing_else_is(self):
        entry = {"type": app.ENTRY_POINT_COMMAND_TYPE, "name": "launch"}
        calls = self.run_sync([{"type": 1, "name": "stale"}, entry])
        names = [c["name"] for c in calls["written"]]
        self.assertEqual(names, ["internships", "launch"])
        self.assertEqual(calls["read"], 1)

    def test_without_one_the_payload_is_the_tree(self):
        self.assertEqual([c["name"] for c in self.run_sync([])["written"]], ["internships"])


@needs_discord
class TheStartUpHook(_ClientCase):
    """What runs once, after login: the owners, the sync, the views and the loop."""

    def setUp(self):
        super().setUp()
        self.synced, self.started = [], []
        self.running = False

        async def sync(client):
            self.synced.append(client)

        loop = types.SimpleNamespace(is_running=lambda: self.running,
                                     start=lambda: self.started.append(True))
        for target, name, value in ((app, "sync_global_commands", sync),
                                    (intern_commands, "intern_delivery_loop", loop),
                                    (app.access, "_application_owners", frozenset())):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def start_up(self, bot):
        bot._application = types.SimpleNamespace(team=None, owner=types.SimpleNamespace(id=101))
        added = []
        bot.add_view = added.append
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            asyncio.run(bot.setup_hook())
        return added, stderr.getvalue()

    def test_a_healthy_finder_gets_its_views_and_its_loop(self):
        bot = self.client()
        added, _ = self.start_up(bot)
        self.assertEqual([type(v).__name__ for v in added],
                         ["ProfileCardView", "AlertControlsView", "DmCheckView"])
        self.assertEqual((self.synced, self.started), ([bot], [True]))

    def test_a_broken_finder_gets_neither_but_the_commands_still_sync(self):
        added, _ = self.start_up(self.client(intern_error=BROKEN))
        self.assertEqual((added, self.started), ([], []))
        self.assertEqual(len(self.synced), 1)

    def test_the_applications_owner_is_recorded_for_access(self):
        with mock.patch.object(poller, "SETTINGS", poller.configure({})):
            self.start_up(self.client())
            self.assertTrue(app.access.is_owner(101))
            self.assertFalse(app.access.is_owner(202))

    def test_a_failed_sync_is_logged_by_type_and_stops_nothing(self):
        async def failing(client):
            raise RuntimeError("canary 50240 message")

        with mock.patch.object(app, "sync_global_commands", failing):
            added, log = self.start_up(self.client())
        self.assertIn("command sync failed: RuntimeError", log)
        self.assertNotIn("canary", log)
        self.assertEqual((len(added), self.started), (3, [True]))


@needs_discord
class TheMemberEvents(_ClientCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        for name in ("member_left", "member_joined"):
            patch = mock.patch.object(intern_commands, name,
                                      lambda member, name=name: self.calls.append((name, member.id)))
            patch.start()
            self.addCleanup(patch.stop)

    def fire(self, bot, member):
        asyncio.run(bot.on_member_remove(member))
        asyncio.run(bot.on_member_join(member))

    def test_a_person_leaving_and_joining_reaches_the_finder(self):
        self.fire(self.client(), types.SimpleNamespace(id=5, bot=False))
        self.assertEqual(self.calls, [("member_left", 5), ("member_joined", 5)])

    def test_bots_and_a_broken_finder_do_not(self):
        self.fire(self.client(), types.SimpleNamespace(id=5, bot=True))
        self.fire(self.client(intern_error=BROKEN), types.SimpleNamespace(id=6, bot=False))
        self.assertEqual(self.calls, [])


@needs_discord
class BuildingTheClient(_ClientCase):
    """build() opens users.db and the scraper's postings.db, read-only, at the paths the
    scraper's bound settings name, and hands both to the finder. It never logs in."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.data = os.path.realpath(self.dir.name)
        for name in SHARED:
            patch = mock.patch.object(intern_ui, name, getattr(intern_ui, name))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(poller, "SETTINGS", poller.configure({"DIAYN_DATA": self.data}))
        patch.start()
        self.addCleanup(patch.stop)

    def build(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            bot = app.build()
        for conn in (bot.stores.db, bot.stores.pconn):
            if conn is not None:
                self.addCleanup(conn.close)
        return bot, stderr.getvalue()

    def test_both_databases_open_and_the_finder_is_handed_them(self):
        test_postings_contract.contract_db(self.data)
        bot, log = self.build()

        self.assertEqual(log, "")
        self.assertIs(intern_ui.bot, bot)
        for name in SHARED[:-2]:
            with self.subTest(name=name):
                self.assertIs(getattr(intern_ui, name), getattr(bot.stores, name))
        self.assertIsNone(intern_ui.intern_error)
        self.assertIsNone(intern_ui.pconn_error)
        self.assertEqual(intern_ui.postings_path, os.path.join(self.data, "postings.db"))
        self.assertTrue(os.path.exists(os.path.join(self.data, "users.db")))
        self.assertEqual(intern_store.summary(intern_ui.db)["profiles"], 0)

    def test_postings_are_read_only(self):
        test_postings_contract.contract_db(self.data)
        bot, _ = self.build()
        with self.assertRaises(sqlite3.OperationalError):
            bot.stores.pconn.execute("INSERT INTO seen VALUES ('greenhouse', 'x', 1)")
        self.assertEqual(postings_contract.WINDOW_DAYS, bot.stores.source.window_days)

    def test_no_postings_db_turns_off_the_tracker_and_leaves_the_finder_on(self):
        bot, log = self.build()
        self.assertIsNone(bot.stores.pconn)
        self.assertIn("internship tracker disabled:", log)
        self.assertIsNone(bot.stores.intern_error)
        self.assertFalse(os.path.exists(os.path.join(self.data, "postings.db")))   # never created

    def test_a_users_db_that_cannot_open_turns_off_the_finder_and_leaves_the_tracker_on(self):
        test_postings_contract.contract_db(self.data)
        os.mkdir(os.path.join(self.data, "users.db"))      # a directory where the file goes
        bot, log = self.build()
        self.assertIsNone(bot.stores.db)
        self.assertIn("internship finder disabled:", log)
        self.assertIsNotNone(bot.stores.pconn)

    def test_the_settings_are_the_scrapers_bound_ones(self):
        # DIAYN_TZ, POLL_CONTACT and the postings path must agree between the finder's
        # modules, which read the scraper's SETTINGS, and the files opened here.
        other = os.path.join(self.data, "elsewhere")
        os.mkdir(other)
        with mock.patch.object(poller, "SETTINGS", poller.configure({"DIAYN_DATA": other})):
            bot, _ = self.build()
        self.assertEqual(bot.stores.postings_path, os.path.join(other, "postings.db"))


@needs_discord
class ServingNeedsAToken(_ClientCase):
    def test_no_token_is_refused_before_anything_is_contacted(self):
        bot = self.client()
        for token in (None, "", "   "):
            with self.subTest(token=token):
                with self.assertRaises(ValueError) as caught:
                    asyncio.run(app.serve(bot, token))
                self.assertIn("DISCORD_TOKEN", str(caught.exception))
        self.assertIsNone(bot.user)


# ── What the loop does (needs discord.py) ─────────────────────────────────────

DAY = 86400
NOW = 1_790_000_000.0
IDLE, DUE, WARNED = 1, 2, 3
POSTED = (1, "greenhouse", "e1", "Acme", "Software Engineer Intern", "Irvine, CA",
          "https://example.com/1", NOW - 3600, NOW - 3600)


def fake_source(**overrides) -> types.SimpleNamespace:
    """A Source with no boards and no blocklist; what intern_ui and the loop read of one."""
    fields = {"window_days": 30, "sweep_interval_s": 900, "sweeper_label": "DIAYN",
              "db_path": ":memory:", "is_blocked": lambda _name: False,
              "drop_blocked": lambda rows, company_at=0: list(rows),
              "board_companies": lambda: (), "boards_count": lambda: 0,
              "icims_hosts": frozenset, "moved": lambda: False}
    return types.SimpleNamespace(**{**fields, **overrides})


def postings_memory_db() -> sqlite3.Connection:
    """An in-memory postings.db with the contract's schema. Its ledger began a year ago,
    so the bootstrap guard (B3) only records a floor and moves nothing."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(test_postings_contract.ddl())
    conn.executemany("INSERT INTO seen VALUES ('greenhouse', ?, ?)",
                     (("e0", NOW - 365 * DAY), ("e1", POSTED[-1])))
    conn.commit()
    return conn


@needs_discord
class DeliveryLoopKeepsTheRetentionPromise(unittest.TestCase):
    """Housekeeping deletes on its own schedule. A tick that fails, or a
    postings.db that is down, must never keep an idle or departed user's
    profile past the day the privacy notice promises, nor skip the warning."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        intern_store.init_db(self.db)
        self.enrol(IDLE, NOW - 366 * DAY, alerts="off")         # housekeeping deletes it
        self.enrol(DUE, NOW - 2 * 3600, alerts="hourly")         # a tick loads the window
        self.enrol(WARNED, NOW - 351 * DAY, alerts="off")        # due its expiry warning
        self.sent, self.loads = [], 0
        self.window_error = None

        async def send_dm(uid, msg):
            self.sent.append(uid)

        async def window(now=None):
            self.loads += 1
            if self.window_error is not None:
                raise self.window_error
            return intern_match.tag_rows([POSTED])

        self.pconn = postings_memory_db()
        self.addCleanup(self.pconn.close)
        for target, name, value in (
                (intern_ui, "db", self.db), (intern_ui, "intern_error", None),
                (intern_ui, "pconn", self.pconn), (intern_ui, "source", fake_source()),
                (intern_ui, "postings_path", None),
                (intern_ui, "send_dm", send_dm), (intern_ui, "window", window),
                (intern_alert_views, "time", types.SimpleNamespace(time=lambda: NOW)),
                (intern_delivery, "SEND_GAP_S", 0)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.db.close)

    def enrol(self, uid, at, **fields):
        base = intern_profile.new_profile(uid, at, source="manual",
                                          cursor=intern_delivery.horizon(at))
        p = dataclasses.replace(base, fields=("software",), degree="bachelor", **fields)
        intern_store.save(self.db, p, now=at, cursor=intern_delivery.horizon(at))

    def run_loop(self) -> str:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            asyncio.run(intern_alert_views.intern_delivery_loop.coro())
        return stderr.getvalue()

    def test_a_tick_whose_window_fails_still_deletes_and_warns(self):
        self.window_error = sqlite3.OperationalError("database is locked")

        log = self.run_loop()

        self.assertIn("the delivery tick failed: OperationalError", log)
        self.assertIsNone(intern_store.load(self.db, IDLE))
        self.assertEqual(intern_store.get_meta(self.db, intern_delivery.HOUSEKEEPING_KEY),
                         intern_delivery._local_day(NOW))
        self.assertEqual(self.sent, [WARNED])

    def test_with_postings_down_it_deletes_and_warns_and_never_reads_the_window(self):
        with mock.patch.object(intern_ui, "pconn", None):
            log = self.run_loop()

        self.assertNotIn("failed", log)
        self.assertIsNone(intern_store.load(self.db, IDLE))
        self.assertEqual((self.sent, self.loads), ([WARNED], 0))

    def test_a_housekeeping_failure_does_not_stop_the_alerts(self):
        def locked(db, now):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(intern_delivery, "run_housekeeping", locked):
            log = self.run_loop()

        self.assertIn("failed: OperationalError", log)
        self.assertIn(DUE, self.sent)
        self.assertEqual(intern_store.get_meta(self.db, "delivery_last_sent"), 1)


@needs_discord
class AnInvalidationWinsOverALoadInFlight(unittest.TestCase):
    """A window load reads its rows, then tags them in a thread for seconds.
    An invalidation meanwhile (a reopen of the file) must not be undone by that
    load finishing afterwards and caching what it read before."""

    def setUp(self):
        intern_ui.invalidate()
        self.rows, self.loads = [POSTED], 0
        self.read, self.proceed = None, None

        async def load_window(pconn, *, now, max_age_days, is_blocked):
            self.loads += 1
            snapshot = list(self.rows)            # read synchronously, as the real one does
            if self.read is not None:
                self.read.set()
                await self.proceed.wait()         # the tagging thread
            return intern_match.tag_rows(snapshot)

        pconn = postings_memory_db()
        self.addCleanup(pconn.close)
        for patch in (mock.patch.object(intern_ui, "pconn", pconn),
                      mock.patch.object(intern_ui, "source", fake_source()),
                      mock.patch.object(intern_ui.intern_match, "load_window", load_window)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(intern_ui.invalidate)

    def test_a_load_begun_before_invalidate_is_served_but_not_cached(self):
        async def straddle():
            self.read, self.proceed = asyncio.Event(), asyncio.Event()
            load = asyncio.create_task(intern_ui.window())
            await self.read.wait()
            self.rows.append((2, *POSTED[1:2], "e2", *POSTED[3:6], "https://example.com/2",
                              NOW, NOW))
            intern_ui.invalidate()                # a reopen, meanwhile
            self.read = None
            self.proceed.set()
            first = await load
            first_gmap = intern_ui.window_gmap()
            return first, first_gmap, await intern_ui.window()

        first, first_gmap, after = asyncio.run(straddle())

        self.assertEqual([c.rowid for c in first], [1])           # its caller still gets it
        self.assertEqual(set(first_gmap), {1})                    # with the matching group map
        self.assertEqual(sorted(c.rowid for c in after), [1, 2])
        self.assertEqual(self.loads, 2)


if __name__ == "__main__":
    unittest.main()
