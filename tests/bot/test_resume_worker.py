"""
The resume worker, driven exactly as the bot drives it: a real child.

    python3 -m unittest discover -s tests      # PDF round trip skips without pypdf
    .venv/bin/python -m unittest discover -s tests

Every test here spawns a process with the interpreter running the suite. That
is deliberate. The worker's worst known bug was invisible in-process: under
`python -I` the script's own directory is not on `sys.path`, so its sibling
imports failed the moment it ran as a child, and every unit test that imported
it directly still passed. The round trips are what prove the fix.

Most failure cases replace the command with a one-line `python -c` script via
`run_worker(argv=...)`, so the parent's handling of a hung, noisy or lying
child is tested without having to make the real worker misbehave. `main()` is
never called in this process: it lowers resource limits for good.
"""

import ast
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from unittest import mock

import resume_parse
import resume_worker
from test_resume_parse import MECH_LINES, RefusalCase, make_docx, make_pdf

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOT = ROOT / "bot"
TODAY = date(2026, 9, 28)
TIMEOUT_SECONDS = int(resume_worker.TIMEOUT_S)

needs_pypdf = unittest.skipUnless(resume_parse.pdf_supported(), "pypdf is not installed")


def script(source: str) -> list:
    """A stand-in child: the same interpreter running one line of Python."""
    return [sys.executable, "-c", source]


def replying_after(seconds: float) -> list:
    """A child that holds its slot for `seconds`, then sends an empty, valid draft."""
    return script(f"import json, time; time.sleep({seconds}); "
                  "print(json.dumps({'ok': True, 'draft': {}}))")


def run(data: bytes, kind: str = "txt", **options) -> dict:
    return asyncio.run(resume_worker.run_worker(data, kind, TODAY, **options))


class LegacyTimeoutError(Exception):
    """asyncio.TimeoutError as Python 3.10 has it: a class apart from the builtin."""


@contextlib.contextmanager
def python_310_timeouts():
    """
    `asyncio.wait_for` as 3.10 runs it, on any interpreter. The bot still starts
    on 3.10, where a deadline raises asyncio's own TimeoutError; only 3.11 made
    it the builtin, so an `except TimeoutError` that passes here would miss it.
    """
    original, deadline = asyncio.wait_for, (TimeoutError, asyncio.exceptions.TimeoutError)

    async def wait_for(awaitable, timeout):
        try:
            return await original(awaitable, timeout)
        except deadline:
            raise LegacyTimeoutError from None

    with mock.patch.object(asyncio, "TimeoutError", LegacyTimeoutError), \
            mock.patch.object(asyncio, "wait_for", wait_for):
        yield


class RoundTrip(RefusalCase):
    """The real worker, spawned under `-I -B` with a scrubbed environment."""

    def test_a_text_resume_comes_back_as_a_draft(self):
        # Proves the child imports its siblings under -I (the judges' bug).
        draft = run(b"EDUCATION\nB.S. Biological Sciences, Expected June 2027\nSkills: PCR")
        self.assertEqual(draft["majors"], ["biological_sciences"])
        self.assertEqual((draft["grad_year"], draft["grad_month"]), (2027, 6))

    def test_a_docx_resume_comes_back_as_a_draft(self):
        draft = run(make_docx(MECH_LINES), "docx")
        self.assertEqual(draft["majors"], ["mechanical_engineering"])
        self.assertIn("c", draft["skills"])

    @needs_pypdf
    def test_a_pdf_resume_comes_back_as_a_draft(self):
        draft = run(make_pdf(MECH_LINES), "pdf")
        self.assertEqual(draft["majors"], ["mechanical_engineering"])

    def test_the_childs_refusal_reaches_the_parent(self):
        with self.refused("bad_magic"):
            run(b"not a word document", "docx")

    def test_more_than_the_cap_on_stdin_is_too_big(self):
        with self.refused("too_big"):
            run(b"x" * (resume_parse.MAX_BYTES + 1))

    def test_bad_arguments_are_a_failed_worker_not_a_bad_file(self):
        argv = [sys.executable, "-I", "-B", str(resume_worker.WORKER_PATH), "txt", "someday"]
        with self.refused("worker_failed"):
            run(b"EDUCATION\nB.S. Biological Sciences", argv=argv)


class ChildFailures(RefusalCase):
    """What the parent makes of a child that hangs, crashes or lies (2.2)."""

    def test_a_hung_child_is_killed_at_the_timeout(self):
        self.assert_a_hung_child_is_killed()

    def test_a_hung_child_is_killed_at_the_timeout_on_python_3_10(self):
        with python_310_timeouts():
            self.assert_a_hung_child_is_killed()

    def assert_a_hung_child_is_killed(self):
        spawned = []
        original = asyncio.create_subprocess_exec

        async def recording(*args, **kwargs):
            process = await original(*args, **kwargs)
            spawned.append(process)
            return process

        started = time.monotonic()
        with mock.patch.object(asyncio, "create_subprocess_exec", recording):
            with self.refused("timeout"):
                run(b"x", argv=script("import time; time.sleep(30)"), timeout=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(spawned[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(spawned[0].pid, 0)

    def test_output_that_is_not_json_is_a_failed_worker(self):
        with self.refused("worker_failed"):
            run(b"x", argv=script("print('not json')"))

    def test_output_over_the_cap_is_a_failed_worker(self):
        with self.refused("worker_failed"):
            run(b"x", argv=script("import sys; sys.stdout.write('x' * 70000)"))

    def test_a_non_zero_exit_is_a_failed_worker(self):
        with self.refused("worker_failed"):
            run(b"x", argv=script("print('{\"ok\": false, \"reason\": \"encrypted\"}'); exit(3)"))

    def test_a_known_reason_is_passed_on(self):
        with self.refused("encrypted"):
            run(b"x", argv=script("print('{\"ok\": false, \"reason\": \"encrypted\"}')"))

    def test_an_unknown_reason_is_a_failed_worker(self):
        with self.refused("worker_failed"):
            run(b"x", argv=script("print('{\"ok\": false, \"reason\": \"nonsense\"}')"))

    def test_a_reply_of_the_wrong_shape_is_a_failed_worker(self):
        for reply in ('[1, 2]', '{"ok": "yes"}', '{"ok": true}'):
            with self.subTest(reply=reply), self.refused("worker_failed"):
                run(b"x", argv=script(f"print({reply!r})"))

    def test_the_parent_rebuilds_the_childs_draft(self):
        reply = json.dumps({"ok": True, "draft": {
            "majors": ["physics", "astrology"], "name": "Jane Canaryperson"}})
        draft = run(b"x", argv=script(f"print({reply!r})"))
        self.assertEqual(draft["majors"], ["physics"])
        self.assertNotIn("name", draft)


class Environment(unittest.TestCase):
    def test_the_child_gets_path_and_lang_and_nothing_else(self):
        with mock.patch.dict(os.environ, {"DISCORD_TOKEN": "canary-token"}):
            env = resume_worker.worker_env()
        self.assertEqual(set(env), {"PATH", "LANG"})
        self.assertEqual(env, {"PATH": os.defpath, "LANG": "C.UTF-8"})

    def test_the_childs_limits_keep_every_byte_out_of_files_and_cores(self):
        # The privacy note promises exactly this, so it is proved in a child
        # rather than read off the source. The empty file is allowed: opening
        # one writes no byte of the resume.
        with tempfile.TemporaryDirectory() as scratch:
            target = os.path.join(scratch, "leak")
            source = "\n".join((
                "import resource, signal, sys",
                f"sys.path.insert(0, {str(BOT)!r})",
                "import resume_worker",
                "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)",
                "resume_worker._limit_resources()",
                f"sink = open({target!r}, 'wb', buffering=0)",
                "try:\n    sink.write(b'x'); print('wrote')",
                "except OSError:\n    print('refused')",
                "print(resource.getrlimit(resource.RLIMIT_CORE)[0])",
            ))
            child = subprocess.run([sys.executable, "-I", "-B", "-c", source],
                                   env=resume_worker.worker_env(), capture_output=True,
                                   text=True, timeout=10, check=True)
            self.assertEqual(os.path.getsize(target), 0)
        self.assertEqual(child.stdout.split(), ["refused", "0"])


class Documentation(unittest.TestCase):
    """What README.md tells users about this file, held to what it enforces."""

    def privacy_note(self) -> str:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        start = text.index("\n## Privacy\n")
        end = text.find("\n## ", start + 1)
        return " ".join(text[start:end if end != -1 else len(text)].split())

    def test_the_privacy_note_promises_a_scrubbed_environment_not_confinement(self):
        # The child runs as the bot's user, free to read files (`.env` among
        # them) and open sockets. The note may promise the scrubbed environment,
        # the limits and the kill; it must not promise the child is walled off.
        note = self.privacy_note()
        self.assertNotIn("no access", note)
        self.assertIn("environment variables", note)
        self.assertIn("read files", note)
        self.assertIn(f"killed after {TIMEOUT_SECONDS} seconds", note)


class Concurrency(unittest.TestCase):
    """At most MAX_CONCURRENT children, one semaphore per loop and limit (2.2)."""

    async def three_at_once(self) -> list:
        calls = [resume_worker.run_worker(b"x", "txt", TODAY, argv=replying_after(1.0))
                 for _ in range(3)]
        return await asyncio.gather(*calls, return_exceptions=True)

    def outcomes(self, results: list) -> list:
        return sorted("draft" if isinstance(r, dict) else r.reason for r in results)

    def test_a_third_worker_waits_then_gives_up_as_busy(self):
        with mock.patch.object(resume_worker, "MAX_CONCURRENT", 2), \
                mock.patch.object(resume_worker, "BUSY_WAIT_S", 0.2):
            results = asyncio.run(self.three_at_once())
        self.assertEqual(self.outcomes(results), ["busy", "draft", "draft"])

    def test_a_third_worker_gives_up_as_busy_on_python_3_10(self):
        with mock.patch.object(resume_worker, "MAX_CONCURRENT", 2), \
                mock.patch.object(resume_worker, "BUSY_WAIT_S", 0.2), python_310_timeouts():
            results = asyncio.run(self.three_at_once())
        self.assertEqual(self.outcomes(results), ["busy", "draft", "draft"])

    def test_a_new_loop_or_a_new_limit_gets_a_new_semaphore(self):
        # The first run leaves a semaphore that has had waiters, which binds it
        # to that loop; reusing it in another loop would raise.
        with mock.patch.object(resume_worker, "MAX_CONCURRENT", 2), \
                mock.patch.object(resume_worker, "BUSY_WAIT_S", 0.2):
            asyncio.run(self.three_at_once())
            again = asyncio.run(self.three_at_once())
        self.assertEqual(self.outcomes(again), ["busy", "draft", "draft"])
        with mock.patch.object(resume_worker, "MAX_CONCURRENT", 1):
            draft = run(b"x", argv=replying_after(0))
        self.assertIsInstance(draft, dict)

    def test_the_semaphore_is_shared_within_one_loop(self):
        async def twice():
            return resume_worker._semaphore(), resume_worker._semaphore()

        first, second = asyncio.run(twice())
        self.assertIs(first, second)


class SourceRules(unittest.TestCase):
    """The containment rules of 2.2, read off `resume_worker.py` with `ast`."""

    NETWORK = {"socket", "urllib", "http", "aiohttp", "requests", "discord"}

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((BOT / "resume_worker.py").read_text(encoding="utf-8"))
        cls.parents = {child: node for node in ast.walk(cls.tree)
                       for child in ast.iter_child_nodes(node)}

    def calls_to(self, attribute: str) -> list:
        return [node for node in ast.walk(self.tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == attribute]

    def function(self, name: str):
        return next(node for node in ast.walk(self.tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name)

    def enclosing_try(self, node):
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.Try):
                return node
        return None

    def test_the_child_is_spawned_isolated_quiet_and_with_a_scrubbed_env(self):
        (call,) = self.calls_to("create_subprocess_exec")
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        self.assertEqual(keywords["stderr"], "asyncio.subprocess.DEVNULL")
        self.assertEqual(keywords["env"], "worker_env()")
        constants = {node.value for arg in call.args for node in ast.walk(arg)
                     if isinstance(node, ast.Constant)}
        self.assertLessEqual({"-I", "-B"}, constants)

    def test_main_puts_its_own_directory_on_the_path_before_importing(self):
        main = self.function("main")
        inserts = [c.lineno for c in ast.walk(main) if isinstance(c, ast.Call)
                   and ast.unparse(c.func) == "sys.path.insert"]
        imports = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Import)
                   and any(a.name == "resume_parse" for a in n.names)]
        self.assertTrue(inserts and imports)
        self.assertLess(min(inserts), min(imports))

    def test_module_scope_imports_only_the_standard_library(self):
        functions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        pending, names = list(self.tree.body), []
        while pending:
            node = pending.pop()
            if isinstance(node, functions):
                continue
            if isinstance(node, ast.Import):
                names += [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append((node.module or "").split(".")[0])
            pending.extend(ast.iter_child_nodes(node))
        self.assertTrue(names)
        for name in names:
            with self.subTest(module=name):
                self.assertIn(name, sys.stdlib_module_names)
        self.assertFalse({"resume_parse", "resume_lexicon", "intern_vocab"} & set(names))

    def test_run_worker_imports_resume_parse_inside_the_function(self):
        body = self.function("run_worker")
        self.assertTrue(any(isinstance(n, ast.Import)
                            and any(a.name == "resume_parse" for a in n.names)
                            for n in ast.walk(body)))

    def test_each_resource_limit_is_set_in_its_own_try(self):
        calls = self.calls_to("setrlimit")
        tries = [self.enclosing_try(call) for call in calls]
        self.assertNotIn(None, tries)
        self.assertEqual(len(set(map(id, tries))), len(calls))
        limits = {ast.unparse(call.args[0]).split(".")[-1]: ast.unparse(call.args[1])
                  for call in calls}
        self.assertEqual(set(limits), {"RLIMIT_FSIZE", "RLIMIT_CORE", "RLIMIT_CPU", "RLIMIT_AS"})
        self.assertEqual(limits["RLIMIT_CORE"], "(0, 0)")
        self.assertEqual(limits["RLIMIT_FSIZE"], "(0, 0)")

    def test_resource_is_imported_lazily_inside_a_try(self):
        imports = [n for n in ast.walk(self.tree) if isinstance(n, ast.Import)
                   and any(a.name == "resource" for a in n.names)]
        self.assertTrue(imports)
        for node in imports:
            self.assertIsNotNone(self.enclosing_try(node))

    def test_nothing_is_written_to_disk_and_no_network_module_is_imported(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                self.assertNotIn(name, {"open", "write_bytes", "write_text"})
        self.assertFalse(imported & (self.NETWORK | {"tempfile"}))


if __name__ == "__main__":
    unittest.main()
