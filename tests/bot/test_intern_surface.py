"""
The Discord half of the internship finder, read as source and, where discord.py
is installed, driven with fakes.

    python3 -m unittest discover -s tests      # the source rules run; behaviour skips
    .venv/bin/python -m unittest discover -s tests

Most of this file reads `intern_ui.py`, `intern_views.py`, `intern_upload.py`,
`intern_alert_views.py`, `intern_commands.py` and `diayn_commands.py` with `ast`
rather than importing them. The rules it pins (spec 8.5, R1-R15) are about every call of a
kind, and a behaviour test only ever reaches the calls it happens to drive: a
reply that is public by accident, a resume read before its metadata was
checked, a `view=None` that crashes after the user already saw the message.
Reading the source needs nothing installed, so these run on a bare box too,
which is where a regression would otherwise go unnoticed.

The behaviour tests at the bottom import the modules and skip when discord.py
is missing — the only dependency that earns a skip here.
"""

import ast
import asyncio
import dataclasses
import functools
import io
import pathlib
import re
import sqlite3
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

import intern_profile
import internship_poller as poller
from club_wording import CLUB

try:
    import discord
except ModuleNotFoundError as missing:  # pragma: no cover - depends on the environment
    if missing.name != "discord":
        raise
    discord = None

#: Importable is not enough. On a box without discord.py, a test may install a
#: stub `discord` module (no `ui`, no `ext`) that stays in sys.modules for the
#: rest of the run, so `import discord` succeeds here and the finder's modules
#: then fail halfway. A stub has no `__file__`; the real package does.
#: When the real one is there the finder's modules are imported unguarded, so a
#: broken module fails loudly instead of reading as "not installed".
REAL_DISCORD = discord is not None and getattr(discord, "__file__", None) is not None
if REAL_DISCORD:
    from discord import app_commands
    import diayn_commands
    import intern_commands
    import intern_ui
else:  # pragma: no cover - depends on the environment
    discord = app_commands = diayn_commands = intern_commands = intern_ui = None

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

#: The bot's modules, which these tests read as source.
BOT = pathlib.Path(__file__).resolve().parents[2] / "bot"
SURFACE = ("intern_ui.py", "intern_views.py", "intern_upload.py", "intern_alert_views.py",
           "intern_commands.py", "diayn_commands.py")
PURE = ("message_pack", "intern_places", "intern_vocab", "intern_location", "intern_taxonomy",
        "resume_lexicon", "resume_parse", "resume_worker", "intern_profile", "intern_store",
        "intern_match", "intern_text", "intern_delivery", "postings_contract", "postings_source",
        "rate_limit", "access", "intern_fit")
#: Where the finder reads postings from, and the one request it makes to a job board.
POSTINGS = ("postings_source.py", "postings_contract.py", "posting_details.py")
PURE_FINDER = ("intern_vocab", "intern_places", "intern_location", "intern_taxonomy",
               "intern_profile", "intern_store", "intern_match", "intern_text", "intern_delivery")
#: Spec 1.5, the whole catalogue: a custom id outside it is either a typo that
#: breaks a persistent button after a restart or a component nobody designed.
CUSTOM_IDS = {
    "intern:card:fields", "intern:card:levels", "intern:card:where", "intern:card:alerts",
    "intern:card:matches", "intern:card:details", "intern:card:filters", "intern:card:upload",
    "intern:card:delete", "intern:alert:hide", "intern:alert:pause", "intern:alert:stop",
    "intern:dm:retry", "intern:upload:file", "intern:upload:text"}


# ------------------------------------------------------------------ reading the source

@functools.lru_cache(maxsize=None)
def source(name: str) -> str:
    return (BOT / name).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def tree(name: str) -> ast.Module:
    return ast.parse(source(name), filename=name)


def chain(node: ast.AST) -> str:
    """`interaction.response.send_message` for that attribute chain; '' otherwise."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = chain(node.value)
        return f"{head}.{node.attr}" if head else f"?.{node.attr}"
    if isinstance(node, ast.Call):
        return chain(node.func) + "()"
    return ""


def calls(module: ast.AST):
    return [n for n in ast.walk(module) if isinstance(n, ast.Call)]


def keyword(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def functions(module: ast.AST) -> dict:
    """Every def by name, nested ones included (the first of a name wins)."""
    found = {}
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, node)
    return found


def inside(module: ast.AST, target: ast.AST) -> list:
    """The names of the defs enclosing `target`, outermost first."""
    def walk(node, stack):
        if node is target:
            return stack
        named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            found = walk(child, stack + [node.name] if named else stack)
            if found is not None:
                return found
        return None
    return walk(module, []) or []


def module_scope(module: ast.Module):
    """Nodes that run at import: everything outside a def body."""
    pending = list(module.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def imported(nodes) -> set:
    names = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def surface_calls():
    return [(name, call) for name in SURFACE for call in calls(tree(name))]


# ------------------------------------------------------------------ R1-R15

class RepliesArePrivate(unittest.TestCase):
    def test_r1_every_send_message_is_ephemeral(self):
        for name, call in surface_calls():
            if chain(call.func).endswith("response.send_message"):
                with self.subTest(file=name, line=call.lineno):
                    self.assertTrue(is_true(keyword(call, "ephemeral")))

    def test_r2_every_defer_is_ephemeral(self):
        seen = 0
        for name, call in surface_calls():
            if chain(call.func).endswith("response.defer"):
                seen += 1
                with self.subTest(file=name, line=call.lineno):
                    self.assertTrue(is_true(keyword(call, "ephemeral")))
        self.assertGreater(seen, 0, "no defer found: the rule is checking nothing")

    def test_r3_every_send_and_edit_names_its_mentions(self):
        # A posting title can hold "<@id>" text; every reply passes NO_MENTIONS.
        kinds = {"send", "send_message", "edit_message", "edit_original_response"}
        for name, call in surface_calls():
            if isinstance(call.func, ast.Attribute) and call.func.attr in kinds:
                with self.subTest(file=name, line=call.lineno, call=chain(call.func)):
                    self.assertIsNotNone(keyword(call, "allowed_mentions"))

    def test_r4_followup_send_only_inside_private_send(self):
        seen = 0
        for name, call in surface_calls():
            if "followup.send" in chain(call.func):
                seen += 1
                with self.subTest(file=name, line=call.lineno):
                    self.assertEqual(name, "intern_ui.py")
                    self.assertIn("private_send", inside(tree(name), call))
        self.assertEqual(seen, 1)


class ResumesAreCheckedBeforeTheyAreRead(unittest.TestCase):
    @staticmethod
    def _reads():
        return [(name, call) for name, call in surface_calls()
                if isinstance(call.func, ast.Attribute) and call.func.attr == "read"
                and "attachment" in chain(call.func.value).split(".")[-1]]

    def test_r5_attachment_read_only_in_read_and_parse(self):
        reads = self._reads()
        self.assertEqual(len(reads), 1, "attachment.read() must happen in exactly one place")
        name, call = reads[0]
        self.assertEqual(name, "intern_upload.py")
        self.assertIn("read_and_parse", inside(tree(name), call))

    def test_r5_read_and_parse_requires_the_sniffed_kind(self):
        fn = functions(tree("intern_upload.py"))["read_and_parse"]
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        self.assertIn("kind", params)

    def test_r5_begin_upload_sniffs_before_it_answers(self):
        fn = functions(tree("intern_upload.py"))["begin_upload"]
        sniffs = [c.lineno for c in calls(fn) if chain(c.func).endswith("resume_parse.sniff")]
        answers = [c.lineno for c in calls(fn) if ".response." in chain(c.func)]
        self.assertTrue(sniffs, "begin_upload never calls resume_parse.sniff")
        self.assertTrue(all(min(sniffs) < line for line in answers))


class OwnerOnly(unittest.TestCase):
    def test_r6_is_owner_comes_before_any_defer(self):
        # /diayn debug, which was /internships debug; need_owner asks access.is_owner.
        module = tree("diayn_commands.py")
        fn = functions(module)["diayn_debug"]
        owner = [c.lineno for c in calls(fn) if chain(c.func) == "need_owner"]
        defers = [c.lineno for c in calls(fn)
                  if isinstance(c.func, ast.Attribute) and "defer" in c.func.attr]
        self.assertTrue(owner, "diayn_debug never checks need_owner")
        self.assertTrue(all(min(owner) < line for line in defers))
        self.assertTrue([c for c in calls(functions(module)["need_owner"])
                         if chain(c.func) == "access.is_owner"])

    def test_r6_no_module_names_a_list_of_officers(self):
        for name in SURFACE:
            with self.subTest(file=name):
                self.assertNotIn("puzzle_admins", imported(ast.walk(tree(name))))
                self.assertNotIn("puzzle-admins", source(name))


class NothingTouchesTheDisk(unittest.TestCase):
    def test_r7_no_file_writes_in_the_discord_modules(self):
        for name in SURFACE:
            for banned in ("open(", "write_bytes", "write_text", "tempfile", "NamedTemporaryFile"):
                with self.subTest(file=name, banned=banned):
                    self.assertNotIn(banned, source(name))

    def test_r8_nothing_imports_the_client(self):
        # app.py builds the client and hands the finder what it needs; importing
        # it back from the finder would be a cycle, and would build a client.
        for name in SURFACE:
            with self.subTest(file=name):
                self.assertNotIn("app", imported(ast.walk(tree(name))))

    def test_r10_pypdf_is_never_imported_at_module_scope(self):
        for path in sorted(BOT.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            with self.subTest(file=path.name):
                self.assertNotIn("pypdf", imported(module_scope(tree(path.name))))


class ComponentIds(unittest.TestCase):
    def test_r9_custom_ids_are_unique_and_in_the_catalogue(self):
        found = [keyword(call, "custom_id").value for _, call in surface_calls()
                 if isinstance(keyword(call, "custom_id"), ast.Constant)]
        self.assertEqual(len(found), len(set(found)), f"duplicated custom_id in {sorted(found)}")
        for custom_id in found:
            with self.subTest(custom_id=custom_id):
                self.assertTrue(custom_id.startswith("intern:"))
        self.assertEqual(set(found), CUSTOM_IDS)


def poller_reads(module: ast.AST) -> list:
    """Every read of a `poller` attribute, outermost only: `intern_ui.poller.cmd_sweep` once,
    not also the `intern_ui.poller` inside it."""
    reads = [n for n in ast.walk(module) if isinstance(n, ast.Attribute)
             and isinstance(n.ctx, ast.Load)
             and (n.attr == "poller" or chain(n.value).endswith("poller"))]
    inner = {id(n.value) for n in reads}
    return [n for n in reads if id(n) not in inner]


def scraper_reads(module: ast.AST) -> set:
    """The attributes read off the scraper, `internship_poller.<name>`."""
    return {n.attr for n in ast.walk(module) if isinstance(n, ast.Attribute)
            and isinstance(n.ctx, ast.Load) and chain(n.value) == "internship_poller"}


class NoAiPath(unittest.TestCase):
    def test_r11a_resume_and_pure_finder_modules_name_no_llm(self):
        names = [p.name for p in BOT.glob("resume_*.py")] + [f"{m}.py" for m in PURE_FINDER]
        for name in names:
            for banned in ("GEMINI", "generativelanguage", "LlmBudget", "llm_", "INTERN_RESUME_LLM"):
                with self.subTest(file=name, banned=banned):
                    self.assertNotIn(banned, source(name))

    def test_r11b_discord_modules_call_no_llm_function(self):
        banned = ("generativelanguage", "LlmBudget", "llm_classify", "llm_candidates", "_llm_call",
                  "GEMINI_API_KEY", "GEMINI_KEY", "GEMINI_URL", "INTERN_RESUME_LLM")
        for name in SURFACE + POSTINGS:
            for word in banned:
                with self.subTest(file=name, banned=word):
                    self.assertNotIn(word, source(name))

    def test_r11d_the_fit_check_is_the_finders_one_way_to_gemini(self):
        # The owner's decision (plan 3.5): Gemini checks whether a role suits someone.
        # It goes through llm.py, the scraper's own request code, from intern_fit and
        # nowhere else in the finder, and it never touches a resume.
        self.assertIn("llm.generate_json", source("intern_fit.py"))
        everything = imported(ast.walk(tree("intern_fit.py")))
        self.assertFalse(everything & {"resume_parse", "resume_worker", "intern_upload",
                                       "discord", "intern_ui"})
        for path in sorted(BOT.glob("*.py")):
            if path.name == "intern_fit.py":
                continue
            with self.subTest(file=path.name):
                self.assertNotIn("llm", imported(ast.walk(tree(path.name))))
                self.assertNotIn("generate_json", source(path.name))

    def test_r11b_nothing_reads_a_poller(self):
        # The finder reads a postings_source.Source. The scraper sweeps in its own
        # task, never inside the finder, so no module holds a loaded poller and
        # nothing may read one: there is none to read.
        found = [(name, chain(node)) for name in SURFACE for node in poller_reads(tree(name))]
        self.assertEqual(found, [])
        for name in SURFACE:
            names = [n for n in ast.walk(tree(name)) if isinstance(n, ast.Name)
                     and n.id == "poller" and isinstance(n.ctx, ast.Load)]
            with self.subTest(file=name):
                self.assertEqual(names, [])

    def test_r11b_postings_source_reads_only_the_scrapers_settings(self):
        # The file's path, from the settings the scraper's boot() bound; nothing
        # of the scraper's Gemini code, and none of its sweep.
        self.assertEqual(scraper_reads(tree("postings_source.py")), {"SETTINGS"})
        for name in SURFACE:
            with self.subTest(file=name):
                self.assertEqual(scraper_reads(tree(name)), set())

    def test_r11c_the_quota_display_is_the_only_llm_code_and_reads_only(self):
        # The quota comes from the contract's scraper_meta, by these keys alone.
        allowed = {"gemini_model", "llm_rpd", "llm_rpm", "llm_tpm", "llm_day_tz"}
        read = {n.value for n in ast.walk(tree("postings_source.py"))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and re.match(r"^(gemini|llm)_", n.value)}
        self.assertEqual(read, allowed)
        for name in SURFACE:
            for node in ast.walk(tree(name)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "llm_" in node.value:
                    with self.subTest(file=name, text=node.value[:40]):
                        self.assertEqual(name, "intern_commands.py")
                        self.assertTrue(node.value.lstrip().startswith("SELECT"))


class PureModulesStayPure(unittest.TestCase):
    def test_r12_pure_modules_import_no_bot_dependency_at_module_scope(self):
        banned = {"discord", "aiohttp", "internship_poller", "dotenv", "pypdf"}
        for module in PURE:
            with self.subTest(module=module):
                self.assertFalse(imported(module_scope(tree(f"{module}.py"))) & banned)

    def test_r13_nothing_in_the_finder_reads_the_old_tech_flag(self):
        for path in sorted(BOT.glob("intern_*.py")):
            with self.subTest(file=path.name):
                self.assertNotIn("is_tech", source(path.name))


class NeverViewNone(unittest.TestCase):
    """
    R14. `send_message(view=None)` sends and then raises; `followup.send(view=None)`
    raises before sending (discord.py 2.7.1). Either way the handler dies mid-reply.
    """

    @staticmethod
    def _none_like(node) -> bool:
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        return isinstance(node, ast.IfExp) and any(
            isinstance(b, ast.Constant) and b.value is None for b in (node.body, node.orelse))

    def _violations(self, name: str):
        module = tree(name)
        for fn in functions(module).values():
            if name == "intern_ui.py" and fn.name == "send_dm":
                continue                 # Messageable.send accepts None: the one allowed case
            nulled = {t.id for n in ast.walk(fn) if isinstance(n, (ast.Assign, ast.AnnAssign))
                      and self._none_like(n.value)
                      for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                      if isinstance(t, ast.Name)}
            senders = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                       and isinstance(n.value, ast.Call) and chain(n.value.func).endswith("private_send")
                       for t in n.targets if isinstance(t, ast.Name)}
            for call in calls(fn):
                func = call.func
                sends = ((isinstance(func, ast.Attribute) and func.attr in ("send", "send_message"))
                         or (isinstance(func, ast.Name) and func.id in senders)
                         or (isinstance(func, ast.Call) and chain(func.func).endswith("private_send")))
                view = keyword(call, "view")
                if sends and view is not None and (
                        self._none_like(view) or (isinstance(view, ast.Name) and view.id in nulled)):
                    yield fn.name, call.lineno

    def test_r14_no_send_is_handed_a_none_view(self):
        for name in SURFACE:
            with self.subTest(file=name):
                self.assertEqual(list(self._violations(name)), [])


class ErrorsNeverReachTheDefaultLoggers(unittest.TestCase):
    ROOTS = {"View", "Modal", "discord.ui.View", "discord.ui.Modal", "ui.View", "ui.Modal"}

    def _classes(self) -> dict:
        return {node.name: node for name in SURFACE for node in ast.walk(tree(name))
                if isinstance(node, ast.ClassDef)}

    @staticmethod
    def _local_bases(cls: ast.ClassDef, classes: dict) -> list:
        """Bases defined in the five files, however they are spelled (`intern_ui.OwnedView`)."""
        names = (chain(b).split(".")[-1] for b in cls.bases)
        return [classes[n] for n in names if n in classes]

    def _handles(self, cls: ast.ClassDef, classes: dict) -> bool:
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "on_error"
               for n in cls.body):
            return True
        local = self._local_bases(cls, classes)
        return bool(local) and all(self._handles(base, classes) for base in local)

    def test_r15_every_view_and_modal_overrides_on_error(self):
        classes = self._classes()
        checked = 0
        for cls in classes.values():
            bases = {chain(b) for b in cls.bases}
            if bases & self.ROOTS or self._local_bases(cls, classes):
                checked += 1
                with self.subTest(cls=cls.name):
                    self.assertTrue(self._handles(cls, classes))
        self.assertGreater(checked, 5)

    def test_r15_the_group_has_an_error_handler(self):
        decorated = [d for fn in functions(tree("intern_commands.py")).values()
                     for d in fn.decorator_list if chain(d) == "internships.error"]
        self.assertEqual(len(decorated), 1)

    def test_r15_no_traceback_is_ever_logged(self):
        for name in SURFACE:
            module = tree(name)
            with self.subTest(file=name):
                self.assertNotIn("traceback", imported(ast.walk(module)))
                for call in calls(module):
                    self.assertNotIn(chain(call.func).split(".")[-1], ("exception", "print_exc"))
                    self.assertIsNone(keyword(call, "exc_info"))


# ------------------------------------------------------------------ behaviour (needs discord.py)

class FakeResponse:
    """Mirrors discord.py 2.7.1: send_message(view=None) sends, *then* raises."""

    def __init__(self, done: bool) -> None:
        self.sent, self._done = [], done

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))
        self._done = True
        if "view" in kw and kw["view"] is None:
            raise AttributeError("'NoneType' object has no attribute 'is_finished'")


class FakeFollowup:
    """Mirrors Webhook.send: view=None raises TypeError before anything is sent."""

    def __init__(self) -> None:
        self.sent = []

    async def send(self, content=None, **kw):
        if "view" in kw and kw["view"] is None:
            raise TypeError("expected view parameter to be of type View or LayoutView, not NoneType")
        self.sent.append((content, kw))


def fake_interaction(done: bool = False):
    return types.SimpleNamespace(response=FakeResponse(done), followup=FakeFollowup(),
                                 user=types.SimpleNamespace(id=7))


@needs_discord
class RefuseAndPrivateSend(unittest.TestCase):
    def assert_one_private(self, sent, view=None):
        self.assertEqual(len(sent), 1)
        content, kw = sent[0]
        self.assertEqual(content, "x")
        self.assertIs(kw.get("ephemeral"), True)
        self.assertIs(kw.get("allowed_mentions"), intern_ui.NO_MENTIONS)
        if view is None:
            self.assertNotIn("view", kw)
        else:
            self.assertIs(kw["view"], view)

    def test_a_refusal_before_any_response_is_one_private_message_without_a_view(self):
        i = fake_interaction(done=False)
        asyncio.run(intern_ui.refuse(i, "x"))
        self.assert_one_private(i.response.sent)
        self.assertEqual(i.followup.sent, [])

    def test_a_refusal_after_a_defer_goes_through_the_followup(self):
        i = fake_interaction(done=True)
        asyncio.run(intern_ui.refuse(i, "x"))
        self.assert_one_private(i.followup.sent)
        self.assertEqual(i.response.sent, [])

    def test_a_view_is_forwarded_on_both_paths(self):
        view = object()
        for done in (False, True):
            with self.subTest(done=done):
                i = fake_interaction(done=done)
                asyncio.run(intern_ui.refuse(i, "x", view=view))
                self.assert_one_private(i.followup.sent if done else i.response.sent, view)

    def test_private_send_drops_a_none_view_and_defaults_the_mentions(self):
        i = fake_interaction(done=True)
        asyncio.run(intern_ui.private_send(i)("x", view=None))
        asyncio.run(intern_ui.private_send(i)("x"))
        self.assertEqual(len(i.followup.sent), 2)
        for content, kw in i.followup.sent:
            self.assertNotIn("view", kw)
            self.assertIs(kw["allowed_mentions"], intern_ui.NO_MENTIONS)
            self.assertIs(kw["ephemeral"], True)


@needs_discord
class TheCommandGroupBuilds(unittest.TestCase):
    """Built into a real CommandTree on a client that never logs in."""

    OPTIONS = {"profile": 1, "matches": 2, "recent": 4, "ping": 2, "info": 1, "delete": 0,
               "help": 0}

    def setUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self.client)
        self.tree.add_command(intern_commands.internships)

    def test_the_group_has_exactly_the_seven_subcommands(self):
        # debug is the owner's, in /diayn (test_diayn_commands).
        names = {c.name for c in intern_commands.internships.commands}
        self.assertEqual(names, set(self.OPTIONS))

    def test_every_payload_fits_discords_limits(self):
        payload = intern_commands.internships.to_dict(self.tree)
        self.assertLessEqual(len(payload["description"]), 100)
        for sub in payload["options"]:
            with self.subTest(command=sub["name"]):
                self.assertLessEqual(len(sub["description"]), 100)
                options = sub.get("options", [])
                self.assertEqual(len(options), self.OPTIONS[sub["name"]])
                for option in options:
                    self.assertLessEqual(len(option["description"]), 100)
                    self.assertLessEqual(len(option.get("choices", [])), 25)

    def test_the_three_free_text_options_autocomplete(self):
        payload = intern_commands.internships.to_dict(self.tree)
        flags = {(sub["name"], o["name"]): o.get("autocomplete", False)
                 for sub in payload["options"] for o in sub.get("options", [])}
        for key in (("recent", "field"), ("recent", "where"), ("info", "role")):
            with self.subTest(option=key):
                self.assertTrue(flags[key])

    def test_it_is_usable_in_servers_and_dms(self):
        contexts = intern_commands.internships.allowed_contexts
        self.assertTrue(contexts.guild)
        self.assertTrue(contexts.dm_channel)
        self.assertFalse(contexts.private_channel)

    def test_no_description_names_a_time_zone_the_host_may_not_use(self):
        # Alert hours are kept in DIAYN_TZ, which the host sets; a description is
        # synced once and cannot follow it, so it names no zone at all.
        payload = intern_commands.internships.to_dict(self.tree)
        for sub in payload["options"]:
            for option in sub.get("options", []):
                with self.subTest(command=sub["name"], option=option["name"]):
                    self.assertNotIn("Pacific", option["description"])

    def test_no_description_assumes_a_club(self):
        payload = intern_commands.internships.to_dict(self.tree)
        texts = [payload["description"]] + [
            text for sub in payload["options"]
            for text in (sub["description"], *(o["description"] for o in sub.get("options", [])))]
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(CLUB.search(text))


@needs_discord
class TheDebugCommandIsTheOwners(unittest.TestCase):
    """R6 driven, on /diayn debug: anyone but whoever runs this bot gets one private
    refusal, and nothing is read or deferred for them."""

    def run_debug(self, is_owner):
        i = fake_interaction(done=False)
        with mock.patch.object(diayn_commands.access, "is_owner", is_owner):
            asyncio.run(diayn_commands.diayn_debug.callback(i))
        return i

    def test_anyone_else_is_refused_privately(self):
        i = self.run_debug(lambda _uid: False)
        self.assertEqual(len(i.response.sent), 1)
        content, kw = i.response.sent[0]
        self.assertEqual(content, diayn_commands.intern_text.owner_only())
        self.assertIs(kw["ephemeral"], True)
        self.assertEqual(i.followup.sent, [])

    def test_it_asks_about_whoever_ran_the_command(self):
        asked = []
        self.run_debug(lambda uid: asked.append(uid) or False)
        self.assertEqual(asked, [7])


@needs_discord
class RecentResolvesWhatWasTyped(unittest.TestCase):
    def test_a_field_is_an_id_or_its_label_typed_exactly(self):
        cases = {"software": "software", "Software engineering": "software",
                 "  biology & LAB research ": "biology_lab", "biology": None, "": None}
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(intern_commands._resolve_field(typed), expected)

    def test_a_place_is_a_preset_a_state_or_a_code(self):
        cases = {"us": "us", "Anywhere in the US": "us", "ca": "ca", "st:CA": "st:CA",
                 "State: California": "st:CA", "california": "st:CA", "WA": "st:WA",
                 "SoCal": "socal", "Mars": None, "st:ZZ": None}
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(intern_commands._resolve_where(typed), expected)


@needs_discord
class BarePingNeverSilencesAPausedUser(unittest.TestCase):
    NOW = 1_790_000_000.0

    def profile(self, **changes):
        base = intern_profile.new_profile(7, self.NOW, source="manual", cursor=0.0)
        return dataclasses.replace(base, **changes)

    def test_alerts_off_come_back_daily_at_the_stored_hour_with_a_welcome(self):
        plan = intern_commands._ping_plan(self.profile(alerts="off", alert_hour=17), None, None, self.NOW)
        self.assertEqual((plan.action, plan.alerts, plan.alert_hour, plan.welcome), ("on", "daily", 17, True))

    def test_refused_dms_restart_the_same_cadence_with_a_welcome(self):
        plan = intern_commands._ping_plan(self.profile(alerts="weekly", dm_failures=3), None, None, self.NOW)
        self.assertEqual((plan.action, plan.alerts, plan.welcome, plan.unpause), ("on", "weekly", True, False))

    def test_a_pause_is_lifted_not_turned_into_off(self):
        plan = intern_commands._ping_plan(self.profile(paused_until=self.NOW + 60), None, None, self.NOW)
        self.assertEqual((plan.action, plan.alerts, plan.unpause, plan.welcome), ("on", None, True, False))

    def test_alerts_on_turn_off(self):
        plan = intern_commands._ping_plan(self.profile(), None, None, self.NOW)
        self.assertEqual((plan.action, plan.alerts), ("off", "off"))

    def test_an_hour_is_kept_only_for_daily_and_weekly(self):
        hourly = intern_commands._ping_plan(self.profile(alert_hour=9), "hourly", 20, self.NOW)
        daily = intern_commands._ping_plan(self.profile(alert_hour=9), None, 20, self.NOW)
        self.assertEqual((hourly.action, hourly.alerts, hourly.alert_hour), ("set", "hourly", 9))
        self.assertEqual((daily.alerts, daily.alert_hour), ("daily", 20))


@needs_discord
class TheWindowIsSharedAndRefreshed(unittest.TestCase):
    ROW = (1, "greenhouse", "e1", "Acme", "Software Engineer Intern", "Irvine, CA",
           "https://example.com/1", 1_790_000_000.0, 1_790_000_000.0)

    def setUp(self):
        intern_ui.invalidate()
        self.loads = []
        rows = [self.ROW]

        async def load_window(pconn, *, now, max_age_days, is_blocked):
            self.loads.append(max_age_days)
            await asyncio.sleep(0)
            return intern_ui.intern_match.tag_rows(rows)

        source = types.SimpleNamespace(window_days=30, is_blocked=lambda _c: False)
        pconn = sqlite3.connect(":memory:")
        self.addCleanup(pconn.close)
        for patch in (mock.patch.object(intern_ui, "pconn", pconn),
                      mock.patch.object(intern_ui, "source", source),
                      mock.patch.object(intern_ui.intern_match, "load_window", load_window)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(intern_ui.invalidate)

    def test_concurrent_callers_share_one_load(self):
        async def both():
            return await asyncio.gather(intern_ui.window(), intern_ui.window())
        first, second = asyncio.run(both())
        self.assertEqual(self.loads, [30])
        self.assertEqual([c.rowid for c in first], [1])
        self.assertEqual(first, second)
        self.assertEqual(intern_ui.window_gmap(), {1: first[0].rk})
        self.assertEqual(intern_ui.supply()["software"], 1)

    def test_invalidate_forces_the_next_caller_to_reload(self):
        asyncio.run(intern_ui.window())
        self.assertIsNotNone(intern_ui.cached_window())
        intern_ui.invalidate()
        self.assertIsNone(intern_ui.cached_window())
        asyncio.run(intern_ui.window())
        self.assertEqual(len(self.loads), 2)


class _HttpResponse:
    def __init__(self, status: int) -> None:
        self.status, self.reason = status, "canary-reason"


@needs_discord
class DirectMessagesMapToDeliveryOutcomes(unittest.TestCase):
    def send_raising(self, error):
        sent = []

        async def send(**kw):
            sent.append(kw)
            if error is not None:
                raise error

        user = types.SimpleNamespace(send=send)
        bot = types.SimpleNamespace(get_user=lambda _uid: user)
        msg = intern_ui.intern_delivery.DmMessage("hello", (), False)
        with mock.patch.object(intern_ui, "bot", bot):
            asyncio.run(intern_ui.send_dm(7, msg))
        return sent

    def test_a_plain_dm_carries_no_controls_and_no_mentions(self):
        (kw,) = self.send_raising(None)
        self.assertIsNone(kw["view"])
        self.assertIs(kw["allowed_mentions"], intern_ui.NO_MENTIONS)

    def test_closed_dms_and_gone_accounts_are_forbidden(self):
        for error in (discord.Forbidden(_HttpResponse(403), "closed"),
                      discord.NotFound(_HttpResponse(404), "gone")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(intern_ui.intern_delivery.DmForbidden):
                    self.send_raising(error)

    def test_discord_and_network_trouble_is_transient(self):
        for error in (discord.HTTPException(_HttpResponse(500), "boom"), asyncio.TimeoutError()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(intern_ui.intern_delivery.DmTransient):
                    self.send_raising(error)


@needs_discord
class FailuresAreLoggedByTypeOnly(unittest.TestCase):
    def test_the_message_of_an_exception_never_reaches_the_log(self):
        inner = ValueError("resume text canary-7731")
        wrapped = app_commands.CommandInvokeError(mock.Mock(name="cmd"), inner)
        for error in (inner, wrapped):
            with self.subTest(error=type(error).__name__):
                err = io.StringIO()
                with redirect_stderr(err):
                    intern_ui.log_failure("/internships profile", error)
                self.assertIn("ValueError", err.getvalue())
                self.assertNotIn("canary", err.getvalue())


@needs_discord
class AlertPromisesAreKept(unittest.TestCase):
    """Review findings 19 and 24: what the card and the welcome DM promise is what happens."""

    def setUp(self):
        import sqlite3
        import intern_store
        self.store = intern_store
        self.db = sqlite3.connect(":memory:")
        intern_store.init_db(self.db)
        for name, value in (("db", self.db), ("intern_error", None)):
            patcher = mock.patch.object(intern_ui, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def saved(self, **fields):
        p = dataclasses.replace(intern_profile.new_profile(7, 1.0, source="manual", cursor=0.0),
                                fields=("software",))
        self.store.save(self.db, p, now=1.0, cursor=0.0)
        if fields:
            columns = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE intern_profiles SET {columns} WHERE user_id = 7", tuple(fields.values()))
            self.db.commit()
        return self.store.load(self.db, 7)

    def test_choosing_off_on_the_card_keeps_the_hour_alerts_come_back_at(self):
        import intern_views
        p = self.saved(alerts="daily", alert_hour=17)

        changes = intern_views._select_changes(p, 3, ["off:9"])

        self.assertEqual(changes, {"alerts": "off", "alert_hour": 17})

    def test_a_welcome_dm_that_arrives_forgets_the_refused_ones(self):
        import intern_alert_views
        p = self.saved(dm_failures=self.store.DM_FAILURE_LIMIT)

        async def delivered(user_id, msg):
            return None
        with mock.patch.object(intern_ui, "send_dm", delivered):
            arrived = asyncio.run(intern_alert_views.send_welcome(fake_interaction(done=True), p))

        self.assertTrue(arrived)
        self.assertEqual(self.store.load(self.db, 7).dm_failures, 0)

    def test_a_refused_welcome_dm_leaves_alerts_stopped(self):
        import intern_alert_views
        import intern_delivery
        p = self.saved(dm_failures=self.store.DM_FAILURE_LIMIT)

        async def refused(user_id, msg):
            raise intern_delivery.DmForbidden()
        i = fake_interaction(done=True)
        with mock.patch.object(intern_ui, "send_dm", refused):
            arrived = asyncio.run(intern_alert_views.send_welcome(i, p))

        self.assertFalse(arrived)
        self.assertEqual(self.store.load(self.db, 7).dm_failures, self.store.DM_FAILURE_LIMIT)


@needs_discord
class TheCardsAlertHoursNameTheZone(unittest.TestCase):
    """Alert hours are kept in DIAYN_TZ, so every hour the card offers names it."""

    def setUp(self):
        patch = mock.patch.object(poller, "SETTINGS", poller.configure({"DIAYN_TZ": "Europe/Berlin"}))
        patch.start()
        self.addCleanup(patch.stop)
        import intern_views
        self.views = intern_views

    def profile(self, **changes):
        base = intern_profile.new_profile(7, 1.0, source="manual", cursor=0.0)
        return dataclasses.replace(base, **changes)

    def test_every_timed_option_names_diayn_tz(self):
        pairs, current = self.views._alert_pairs(None)
        timed = [label for value, label in pairs if value.startswith(("daily", "weekly"))]
        self.assertIsNone(current)
        self.assertEqual(len(timed), 3)
        for label in timed:
            with self.subTest(label=label):
                self.assertIn("Berlin time", label)
                self.assertNotIn("Pacific", label)

    def test_an_hour_set_with_ping_is_offered_in_the_zone_and_preselected(self):
        pairs, current = self.views._alert_pairs(self.profile(alerts="daily", alert_hour=6))
        self.assertEqual(current, "daily:6")
        self.assertEqual(dict(pairs)["daily:6"], "Daily at 6am Berlin time")

    def test_the_placeholder_card_builds(self):
        async def build():
            return self.views.ProfileCardView()
        view = asyncio.run(build())
        (alerts,) = [item for item in view.children if getattr(item, "custom_id", "") == "intern:card:alerts"]
        self.assertTrue(all("Pacific" not in option.label for option in alerts.options))


@needs_discord
class TodayIsTheHostsDay(unittest.TestCase):
    """The resume worker reads "Expected June 2028" against today, and the filters offer
    the terms that are still to come: both from today's date in DIAYN_TZ."""

    #: 03:00 UTC on 22 September 2026: still the 21st in Los Angeles.
    AT = 1_790_046_000.0

    def today_in(self, zone):
        with mock.patch.object(poller, "SETTINGS", poller.configure({"DIAYN_TZ": zone})):
            return intern_ui.today(self.AT)

    def test_it_is_the_date_in_diayn_tz(self):
        self.assertEqual(self.today_in("UTC").isoformat(), "2026-09-22")
        self.assertEqual(self.today_in("America/Los_Angeles").isoformat(), "2026-09-21")

    def test_without_a_moment_it_is_now(self):
        import datetime as dt
        with mock.patch.object(poller, "SETTINGS", poller.configure({"DIAYN_TZ": "UTC"})):
            before = dt.datetime.now(dt.timezone.utc).date()
            today = intern_ui.today()
            after = dt.datetime.now(dt.timezone.utc).date()
        self.assertIn(today, (before, after))

    def test_nothing_calls_it_pacific_any_more(self):
        self.assertFalse(hasattr(intern_ui, "today_pacific"))
        for name in SURFACE:
            with self.subTest(file=name):
                self.assertNotIn("today_pacific", source(name))


if __name__ == "__main__":
    unittest.main()
