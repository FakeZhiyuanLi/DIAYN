"""
llm.py: one Gemini request, shared by the scraper's --llm and the finder's fit check.

    python3 -m unittest discover -s tests      # no install needed

Every request here goes to a fake session that answers the way Gemini's
generateContent does, so nothing reaches the network. The scraper's own
`_llm_call` is driven through the same fake, which is what keeps --llm
behaving as it did before the request code moved out of it.
"""

import ast
import asyncio
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()

import internship_poller as poller  # noqa: E402
import llm  # noqa: E402

KEY = "test-key-not-real"
SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT"}}
USAGE = {"promptTokenCount": 120, "candidatesTokenCount": 30}


def gemini_body(payload, usage=USAGE) -> dict:
    """What generateContent returns: the answer as text inside the first candidate."""
    body = {"candidates": [{"content": {"parts": [{"text": payload}]}}]}
    if usage is not None:
        body["usageMetadata"] = usage
    return body


class Response:
    def __init__(self, status, body=None):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


class FakeSession:
    """Answers each post with the next of `answers`: a Response, or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.posts = []

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Gate:
    """A budget's `acquire`: says yes `allow` times, then no; counts every ask."""

    def __init__(self, allow=99):
        self.allow, self.asked = allow, 0

    async def __call__(self):
        self.asked += 1
        return self.asked <= self.allow


def ask(session, *, acquire=None, on_usage=None, attempts=3, **kwargs):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), \
            mock.patch.object(llm.asyncio, "sleep", mock.AsyncMock()) as slept:
        try:
            result = asyncio.run(llm.generate_json(
                session, key=KEY, model="gemini-test", prompt="Classify these.", schema=SCHEMA,
                max_attempts=attempts, acquire=acquire or Gate(), on_usage=on_usage, **kwargs))
        except llm.LlmError as error:
            result = error
    return result, stderr.getvalue(), slept


class TheRequest(unittest.TestCase):
    def test_it_asks_the_model_for_json_that_fits_the_schema(self):
        session = FakeSession(Response(200, gemini_body("[1, 2]")))

        result, _, _ = ask(session)

        self.assertEqual(result, [1, 2])
        (post,) = session.posts
        self.assertEqual(post["url"], llm.GEMINI_URL.format(model="gemini-test"))
        self.assertIn("generativelanguage.googleapis.com", post["url"])
        self.assertEqual(post["headers"], {"x-goog-api-key": KEY})
        self.assertEqual(post["json"], {
            "contents": [{"parts": [{"text": "Classify these."}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": SCHEMA, "temperature": 0}})

    def test_a_fenced_answer_is_read_through_its_fence(self):
        session = FakeSession(Response(200, gemini_body('```json\n[{"i": 0}]\n```')))

        result, _, _ = ask(session)

        self.assertEqual(result, [{"i": 0}])

    def test_the_token_counts_reach_the_caller(self):
        seen = []

        ask(FakeSession(Response(200, gemini_body("[]"))), on_usage=seen.append)

        self.assertEqual(seen, [USAGE])
        self.assertEqual(llm.usage_tokens(USAGE), (120, 30))

    def test_token_counts_that_are_missing_or_malformed_count_as_none(self):
        for meta in (None, {}, "junk", {"promptTokenCount": "many"}, {"promptTokenCount": None}):
            with self.subTest(meta=meta):
                self.assertEqual(llm.usage_tokens(meta), (0, 0))

    def test_the_estimate_is_four_characters_a_token_plus_headroom(self):
        self.assertEqual(llm.estimate_tokens("x" * 400), 164)
        self.assertEqual(llm.estimate_tokens(None), 64)


class TheBudget(unittest.TestCase):
    def test_a_budget_that_says_no_sends_nothing(self):
        session, gate = FakeSession(), Gate(allow=0)

        result, printed, _ = ask(session, acquire=gate)

        self.assertIsInstance(result, llm.LlmError)
        self.assertEqual(str(result), "budget spent")
        self.assertEqual((session.posts, printed), ([], ""))

    def test_every_attempt_asks_the_budget_first_a_retry_included(self):
        session = FakeSession(Response(503), Response(429), Response(200, gemini_body("[]")))
        gate = Gate()

        result, _, slept = ask(session, acquire=gate)

        self.assertEqual(result, [])
        self.assertEqual((gate.asked, len(session.posts)), (3, 3))
        self.assertEqual([c.args[0] for c in slept.await_args_list], [5, 10])

    def test_a_retry_the_budget_refuses_is_not_sent(self):
        session = FakeSession(Response(429))

        result, _, _ = ask(session, acquire=Gate(allow=1))

        self.assertEqual(str(result), "budget spent")
        self.assertEqual(len(session.posts), 1)


class Failures(unittest.TestCase):
    def test_a_refused_request_is_named_by_its_status_and_not_retried(self):
        session = FakeSession(Response(403))

        result, printed, _ = ask(session)

        self.assertEqual(str(result), "HTTP 403")
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(printed, "  llm: HTTP 403 — falling back to regex\n")

    def test_the_caller_names_itself_and_its_fallback(self):
        _, printed, _ = ask(FakeSession(Response(400)), label="fit check", fallback="unchecked")

        self.assertEqual(printed, "  fit check: HTTP 400 — falling back to unchecked\n")

    def test_rate_limits_on_every_attempt_end_in_that_status(self):
        session = FakeSession(Response(429), Response(429), Response(429))

        result, printed, _ = ask(session)

        self.assertEqual(str(result), "HTTP 429")
        self.assertEqual((len(session.posts), printed), (3, ""))

    def test_a_transient_failure_is_retried_until_the_attempts_run_out(self):
        session = FakeSession(asyncio.TimeoutError(), asyncio.TimeoutError())

        result, printed, slept = ask(session, attempts=2)

        self.assertEqual(str(result), "TimeoutError")
        self.assertEqual(printed.splitlines(), [
            "  llm: TimeoutError (attempt 1/2) — retrying in 5s",
            "  llm: TimeoutError (attempt 2/2) — falling back to regex"])
        self.assertEqual(slept.await_count, 1)

    def test_a_transient_failure_then_an_answer_is_the_answer(self):
        session = FakeSession(ConnectionResetError(), Response(200, gemini_body('[3]')))

        result, _, _ = ask(session)

        self.assertEqual(result, [3])

    def test_anything_else_that_goes_wrong_is_named_by_its_type(self):
        result, printed, _ = ask(FakeSession(ValueError("boom")))

        self.assertEqual(str(result), "ValueError")
        self.assertEqual(printed, "  llm: ValueError — falling back to regex\n")
        self.assertNotIn("boom", printed)

    def test_an_unparseable_answer_is_not_retried(self):
        for body in (gemini_body("not json at all"), {"candidates": []}, ["a", "list"]):
            session = FakeSession(Response(200, body), Response(200, gemini_body("[]")))
            with self.subTest(body=body):
                result, printed, _ = ask(session)
                self.assertEqual(str(result), "unparseable response")
                self.assertEqual(len(session.posts), 1)
                self.assertEqual(printed, "  llm: unparseable response — falling back to regex\n")

    def test_nothing_it_prints_carries_the_key_or_the_prompt(self):
        for answer in (Response(401), asyncio.TimeoutError(), Response(200, gemini_body("{"))):
            with self.subTest(answer=answer):
                _, printed, _ = ask(FakeSession(answer), attempts=1)
                self.assertNotIn(KEY, printed)
                self.assertNotIn("Classify these.", printed)


class TheScraperClassifiesThroughIt(unittest.TestCase):
    """`_llm_call` sends one batch through llm.generate_json and reads the answer by
    posting number, exactly as --llm did before the request code was shared."""

    class Budget:
        def __init__(self, allow=99):
            self.gate, self.usage = Gate(allow), []

        async def acquire(self, est_tokens=0):
            return await self.gate()

        def record_usage(self, meta):
            self.usage.append(meta)

        @staticmethod
        def estimate_tokens(text):
            return llm.estimate_tokens(text)

    def call(self, session, budget=None):
        posting = poller.Posting(platform="greenhouse", external_id="1", company="Acme",
                                 sector="tech", title="Software Engineer Intern",
                                 location="Irvine, CA", url="https://example.com/1",
                                 published=None)
        budget = budget or self.Budget()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                mock.patch.object(poller, "SETTINGS", poller.configure({"GEMINI_API_KEY": KEY})), \
                mock.patch.object(llm.asyncio, "sleep", mock.AsyncMock()):
            result = asyncio.run(poller._llm_call(session, budget, [(0, posting)]))
        return result, budget, stderr.getvalue()

    def test_an_answer_comes_back_by_posting_number(self):
        row = {"i": 0, "is_intern": True, "is_tech": True, "category": "swe", "region": "us"}
        session = FakeSession(Response(200, gemini_body(json.dumps([row, "junk", {"x": 1}]))))

        result, budget, printed = self.call(session)

        self.assertEqual((result, printed), ({0: row}, ""))
        self.assertEqual(budget.usage, [USAGE])
        (post,) = session.posts
        self.assertEqual(post["json"]["generationConfig"]["responseSchema"], poller.LLM_SCHEMA)
        text = post["json"]["contents"][0]["parts"][0]["text"]
        self.assertTrue(text.startswith(poller.LLM_PROMPT))
        self.assertIn("0. Software Engineer Intern — Irvine, CA", text)

    def test_any_failure_is_an_empty_answer_and_the_same_words_as_before(self):
        for answer, words in ((Response(403), "  llm: HTTP 403 — falling back to regex\n"),
                              (Response(200, gemini_body("nope")),
                               "  llm: unparseable response — falling back to regex\n"),
                              (Response(200, gemini_body("7")),
                               "  llm: unparseable response — falling back to regex\n")):
            with self.subTest(words=words):
                result, _, printed = self.call(FakeSession(answer))
                self.assertEqual((result, printed), ({}, words))

    def test_a_spent_budget_is_an_empty_answer_and_says_nothing(self):
        session = FakeSession()

        result, _, printed = self.call(session, self.Budget(allow=0))

        self.assertEqual((result, printed, session.posts), ({}, "", []))


class ImportingItDoesNothing(unittest.TestCase):
    def test_it_reads_no_setting_of_its_own(self):
        # The key, the model and the limits are the caller's, handed in per request.
        with open(os.path.join(ROOT, "llm.py"), encoding="utf-8") as f:
            module = ast.parse(f.read())
        imported = {alias.name.split(".")[0] for node in ast.walk(module)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in (node.names if isinstance(node, ast.Import)
                                  else [ast.alias(node.module or "")])}
        self.assertEqual(imported, {"asyncio", "json", "sys", "collections", "aiohttp"})
        names = {node.id for node in ast.walk(module) if isinstance(node, ast.Name)}
        self.assertFalse(names & {"SETTINGS", "os", "environ"})


if __name__ == "__main__":
    unittest.main()
