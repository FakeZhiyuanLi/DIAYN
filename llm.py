"""
llm.py
~~~~~~
One request to Gemini, shared by the scraper's `--llm` classifier
(`internship_poller.py`) and the finder's fit check (`bot/intern_fit.py`):
aiohttp to generativelanguage, asking for JSON that fits a schema, retried up
to the caller's GEMINI_MAX_ATTEMPTS, and the token counts a response reports.

**The caller owns every setting and every budget.** The key, the model, the
attempts and the session (whose timeout is GEMINI_HTTP_TIMEOUT) are handed in
per request, and so is `acquire`, the caller's budget. It is asked before
every attempt, a retry included, because each attempt is a real call against
the daily cap. Nothing here reads the environment or the scraper's settings,
and importing it does nothing.

**It fails in one way.** Whatever goes wrong, the caller gets LlmError, whose
text names the class of failure for a log or `/diayn debug`: "budget spent",
"HTTP 403", "TimeoutError", "unparseable response". What it prints goes to
stderr, in the scraper's words unless the caller names itself and its
fallback, and never carries the key, the prompt or the answer.

Retried: a timeout, a dropped connection, and HTTP 429, 500 and 503, after
`backoff(attempt)` seconds. Not retried: any other status, and an answer that
does not parse, which a model gives again for the same prompt.

The politeness gate does not apply: `polite_session` exempts this host, whose
limit is the caller's budget rather than someone else's server.
"""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable

import aiohttp

GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

# Failures worth retrying: the request never produced an answer, and the same
# request may well succeed moments later. Timeouts dominate here — a batch of 8
# postings is ~13k tokens, so a slow generation can exceed the client deadline
# even though nothing is wrong. Connection resets and aiohttp's generic
# ClientError cover transport-level flakiness.
TRANSIENT_ERRORS = (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError)
#: Rate limited, or the service is struggling: worth another attempt.
RETRY_STATUSES = frozenset({429, 500, 503})
#: Characters per token, Gemini's rule of thumb, and the response's overhead.
_CHARS_PER_TOKEN, _OVERHEAD_TOKENS = 4, 64

Acquire = Callable[[], Awaitable[bool]]
OnUsage = Callable[[object], None]


class LlmError(Exception):
    """No usable answer. The text is the class of failure, never the request or the reply."""


def backoff(attempt: int) -> float:
    """Seconds to wait before re-attempting a failed call: 5, 10, 20…, so a run
    can't stall for minutes on retries alone."""
    return 2 ** attempt * 5


def estimate_tokens(text: str | None) -> int:
    """A request's size in tokens, estimated generously: enough to stay under a
    tokens-per-minute ceiling, never a bill."""
    return len(text or "") // _CHARS_PER_TOKEN + _OVERHEAD_TOKENS


def usage_tokens(meta: object) -> tuple[int, int]:
    """(prompt, output) tokens from a response's usageMetadata: Gemini's own counts,
    for reporting. (0, 0) when they are missing or malformed."""
    if not isinstance(meta, dict):
        return 0, 0
    try:
        return int(meta.get("promptTokenCount") or 0), int(meta.get("candidatesTokenCount") or 0)
    except (TypeError, ValueError):
        return 0, 0


def request_body(prompt: str, schema: dict) -> dict:
    """A generateContent request for JSON matching `schema`, at temperature 0."""
    return {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": schema, "temperature": 0}}


def _say(label: str, detail: str) -> None:
    print(f"  {label}: {detail}", file=sys.stderr)


def _parse(body: object, on_usage: OnUsage | None) -> object:
    """The answer's JSON. The usage is recorded first: a reply that does not parse
    was still a call that counted."""
    if on_usage is not None:
        on_usage(body.get("usageMetadata"))
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text.strip().strip("`").removeprefix("json"))


async def generate_json(sess, *, key: str, model: str, prompt: str, schema: dict,
                        max_attempts: int, acquire: Acquire, on_usage: OnUsage | None = None,
                        label: str = "llm", fallback: str = "regex") -> object:
    """
    Asks `model` for JSON matching `schema` and returns it parsed. Raises LlmError.

    `sess` is an aiohttp session (or anything with its `post`); `acquire` is asked
    before each attempt and a False ends the request unsent; `on_usage` gets each
    parsed reply's usageMetadata. `label` and `fallback` fill the lines printed on
    a failure: "  {label}: HTTP 403 — falling back to {fallback}".
    """
    url, body = GEMINI_URL.format(model=model), request_body(prompt, schema)
    status = None
    for attempt in range(max_attempts):
        if not await acquire():
            raise LlmError("budget spent")
        try:
            async with sess.post(url, json=body, headers={"x-goog-api-key": key}) as r:
                status = r.status
                if status in RETRY_STATUSES:
                    await asyncio.sleep(backoff(attempt))
                    continue
                if status != 200:
                    _say(label, f"HTTP {status} — falling back to {fallback}")
                    raise LlmError(f"HTTP {status}")
                reply = await r.json(content_type=None)
        except LlmError:
            raise
        except TRANSIENT_ERRORS as e:
            last = attempt == max_attempts - 1
            _say(label, f"{type(e).__name__} (attempt {attempt + 1}/{max_attempts})"
                 + (f" — falling back to {fallback}" if last else
                    f" — retrying in {backoff(attempt):.0f}s"))
            if last:
                raise LlmError(type(e).__name__) from None
            await asyncio.sleep(backoff(attempt))
            continue
        except Exception as e:
            _say(label, f"{type(e).__name__} — falling back to {fallback}")
            raise LlmError(type(e).__name__) from None
        try:
            return _parse(reply, on_usage)
        except Exception:
            _say(label, f"unparseable response — falling back to {fallback}")
            raise LlmError("unparseable response") from None
    raise LlmError(f"HTTP {status}" if status is not None else "no attempt made")
