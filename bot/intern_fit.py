"""
intern_fit.py
~~~~~~~~~~~~~
The Gemini fit check. Before an alert is sent, Gemini is asked whether each
role the rule-based matcher picked suits this person, and says why in one
short line: `fit`, `unsure` or `no_fit`. A no_fit is not sent; fit goes
first, then unsure, each with its reason.

**What is sent, and nothing else.** The profile as vocabulary labels
(`labels`: majors, minors, degree, graduation, kinds of role, fields, skills,
keywords, places and terms) and, for each role, its number in the request,
title, company, location and term (`request_payload`). Never the Discord id,
a name, an email address or a word of a resume: the profile holds none of
those, and `labels` reads only the fields listed. The prompt is FIT_PROMPT
followed by that payload as JSON.

**Never before the notice.** A profile is checked only once its owner has
been shown what is sent (`fit_notice_at`): on the start card or the consent
screen a new profile is made behind, in `/internships help`, or in a DM that
says so, the migrated subscribers' introduction or the one line
(`intern_text.fit_notice_line`) that `intern_delivery` puts on the next alert
of a profile from before the host had a key (`notice_due`). That alert goes
out unchecked; the check starts with the one after. Until then `enabled` is
False, so no request is made and no cached verdict is shown.

**Asked once.** Verdicts are kept in users.db's `fit_verdicts`, keyed by
(`profile_fp`, `role_hash`): the sha256 of the labels sent, so an edit to any
of them asks again, and the role's taxonomy key, the one `intern_seen` keeps.
Two people with the same labels share verdicts; no row names anyone. Rows go
after 45 days, as sent roles do (`prune`, from the daily housekeeping).

**Its own budget.** At most FIT_BATCH roles a request. FIT_RPD requests a
day, the day being the date in LLM_DAY_TZ as for the scraper's --llm, counted
in `fit_usage` with the tokens each reply reports; FIT_RPM a minute, paced in
memory (`Pace`), so a busy alert hour waits its turn, within the tick's
deadline, rather than going unchecked. It shares GEMINI_API_KEY and
GEMINI_MODEL with --llm, and the two budgets together must fit the key's quota.

**Its own deadlines.** A request, its waits for the minute's budget and its
retries included, gets REQUEST_DEADLINE_S, and every request of one delivery
tick together get TICK_DEADLINE_S (`checker`). They hold whatever
GEMINI_HTTP_TIMEOUT says, where 0 means no HTTP deadline at all: an alert never
waits on Gemini for longer than these.

**It never holds an alert back.** No key, the profile's owner turned it off,
the day's budget is spent, the request fails, runs out of time or the answer
does not parse: the matches come back as the matcher ranked them, unchecked
and without a reason line. Verdicts already cached still apply. A request that
fails or times out has cost a wait, so after one the check asks nothing for
COOL_OFF_S: in an outage one alert waits, not every alert in turn. A tick that
has spent its time is not an outage, and the next tick asks again. The last
failure's class is kept in `last_error` for `/diayn debug`.

The request goes through `llm.generate_json`, the code the scraper's --llm
uses. Importing this module reads nothing and imports neither aiohttp nor the
scraper: both are imported when a check first needs them, as `intern_clock`
reads the scraper's settings.
"""

import asyncio
import calendar
import dataclasses
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import intern_vocab as vocab
from intern_location import describe_locations
from intern_match import Candidate, Match
from intern_profile import Profile
from resume_lexicon import MAJOR_BY_ID, SKILL_BY_ID

VERDICTS = ("fit", "unsure", "no_fit")
REASON_MAX = 120
DAY_S = 86400
#: How long a verdict is kept: as long as a sent role is (intern_store.SENT_RETAIN_S).
RETAIN_S = 45 * DAY_S
#: The payload's keys, pinned by tests: labels only, and four fields of each role.
PROFILE_KEYS = ("majors", "minors", "degree", "graduation", "levels", "fields", "skills",
                "keywords", "locations", "terms")
POSTING_KEYS = ("i", "title", "company", "location", "term")
#: A role's fields are cut to these lengths before they are sent.
_TITLE_MAX, _COMPANY_MAX, _LOCATION_MAX = 200, 100, 120
_MINUTE_S = 60.0
#: How long the check asks nothing after a request failed: two delivery ticks.
COOL_OFF_S = 600
#: The longest one request may take, its budget waits and retries included, whatever
#: GEMINI_HTTP_TIMEOUT says; after it, that alert goes out unchecked.
REQUEST_DEADLINE_S = 90
#: The longest every request of one delivery tick may take together, well inside the
#: five-minute tick; after it, the tick's other alerts go out unchecked.
TICK_DEADLINE_S = 180
_TIMED_OUT, _OUT_OF_TIME = "timed out", "out of time this tick"
#: Failures that say nothing about the service, so start no cool-off.
_NOT_AN_OUTAGE = frozenset({"budget spent", "unparseable response", _OUT_OF_TIME})
#: How many times a request waits for the minute's budget before it goes unchecked.
_MAX_WAITS = 12
_ORDER = {"fit": 0, "unsure": 1}
_UNCHECKED = len(_ORDER)
_SPACES = re.compile(r"\s+")
_DEGREE_LABELS = dict(vocab.DEGREES)
_LOG = "internship finder: fit check"

FIT_PROMPT = """You check job postings for one student, for an internship alert bot. \
Before a role is sent to them, you say whether it suits them.

Below is one JSON object. "profile" holds the labels the student chose: majors, minors, \
degree, graduation, the kinds of role they want (levels), fields, skills, keywords, the \
places they want to work (locations) and terms. "postings" holds the roles, each with a \
number "i", a title, a company, a location and a term.

For each posting return one object:
  i        the posting's number
  verdict  "fit" when the student could apply and the role suits their studies or interests;
           "no_fit" when it clearly does not: another field entirely, a kind of role they are
           not looking for, or a requirement stated in the posting they cannot meet;
           "unsure" when these fields cannot tell.
  reason   one sentence for the student, at most 120 characters, naming what fits or what
           does not, e.g. "Your mechanical engineering major fits this manufacturing role".

Judge only from the labels and the posting fields given. When in doubt, say "unsure": a \
no_fit is never shown to the student. Return ONLY a JSON array, one object per posting.

"""

FIT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "verdict": {"type": "STRING", "enum": list(VERDICTS)},
            "reason": {"type": "STRING"},
        },
        "required": ["i", "verdict", "reason"],
    },
}

_VERDICTS_DDL = """
    CREATE TABLE IF NOT EXISTS fit_verdicts (
        profile_fp TEXT NOT NULL,
        role_hash  TEXT NOT NULL,
        verdict    TEXT NOT NULL CHECK (verdict IN ('fit','unsure','no_fit')),
        reason     TEXT NOT NULL,
        model      TEXT NOT NULL,
        at         REAL NOT NULL,
        PRIMARY KEY (profile_fp, role_hash)
    )
"""
_VERDICTS_INDEX = "CREATE INDEX IF NOT EXISTS idx_fit_verdicts_at ON fit_verdicts(at)"
_USAGE_DDL = """
    CREATE TABLE IF NOT EXISTS fit_usage (
        day           TEXT PRIMARY KEY,
        requests      INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0
    )
"""

#: The class of the last failure and when it happened, for /diayn debug; None since start.
last_error: tuple[str, float] | None = None
#: No request is made before this moment (COOL_OFF_S after a failed one).
quiet_until: float = 0.0

FitCheck = Callable[[Profile, Sequence[Match], float], Awaitable[list[Match]]]


class Unparseable(Exception):
    """An answer that holds no usable verdict."""


@dataclass(frozen=True)
class Verdict:
    verdict: str        # fit | unsure | no_fit
    reason: str         # one line, at most REASON_MAX characters


@dataclass(frozen=True)
class Usage:
    """One day's row of fit_usage."""
    requests: int
    prompt_tokens: int
    output_tokens: int


# ------------------------------------------------------------------ settings and tables

def _settings():
    """The scraper's bound settings (intern_clock's rule): the key, the model, the limits."""
    import internship_poller
    return internship_poller.SETTINGS


def available() -> bool:
    """Whether whoever runs this bot has given it a Gemini key."""
    return bool(_settings().gemini_key)


def enabled(p: Profile) -> bool:
    """Whether this profile's alerts are checked: a key, its owner has not said no, and has
    been shown what is sent."""
    return p.fit_check and available() and p.fit_notice_at is not None


def notice_due(p: Profile) -> bool:
    """Whether this profile's alerts would be checked but for its owner never having been
    shown the notice, which its next alert must carry (module docstring)."""
    return p.fit_check and available() and p.fit_notice_at is None


def init_db(db: sqlite3.Connection) -> None:
    """Creates fit_verdicts and fit_usage in users.db if absent. Safe on every boot."""
    for ddl in (_VERDICTS_DDL, _VERDICTS_INDEX, _USAGE_DDL):
        db.execute(ddl)
    db.commit()


def quota_day(now: float) -> str:
    """`now`'s date in LLM_DAY_TZ, the day FIT_RPD counts in, as the scraper's --llm does."""
    return datetime.fromtimestamp(now, ZoneInfo(_settings().llm_day_tz)).strftime("%Y-%m-%d")


def usage(db: sqlite3.Connection, day: str) -> Usage:
    row = db.execute("SELECT requests, prompt_tokens, output_tokens FROM fit_usage WHERE day = ?",
                     (day,)).fetchone()
    return Usage(*row) if row else Usage(0, 0, 0)


@dataclass(frozen=True)
class Limits:
    """What bounds the check, as the host set it: for /diayn debug."""
    model: str
    rpd: int
    rpm: int
    batch: int
    zone: str           # the IANA zone the day resets in, LLM_DAY_TZ


def limits() -> Limits:
    s = _settings()
    return Limits(model=s.gemini_model, rpd=s.fit_rpd, rpm=s.fit_rpm, batch=s.fit_batch,
                  zone=s.llm_day_tz)


def cached_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM fit_verdicts").fetchone()[0]


def prune(db: sqlite3.Connection, now: float) -> int:
    """Deletes verdicts older than RETAIN_S; returns how many."""
    with db:
        return db.execute("DELETE FROM fit_verdicts WHERE at < ?", (now - RETAIN_S,)).rowcount


# ------------------------------------------------------------------ what is sent

def _graduation(p: Profile) -> str | None:
    if p.grad_year is None:
        return None
    month = calendar.month_name[p.grad_month] if p.grad_month else ""
    return f"{month} {p.grad_year}".strip()


def labels(p: Profile) -> dict[str, object]:
    """The profile as the labels its owner chose, under PROFILE_KEYS and nothing else."""
    places = describe_locations(p.locations)
    return {
        "majors": [MAJOR_BY_ID[m].label for m in p.majors if m in MAJOR_BY_ID],
        "minors": [MAJOR_BY_ID[m].label for m in p.minors if m in MAJOR_BY_ID],
        "degree": _DEGREE_LABELS.get(p.degree),
        "graduation": _graduation(p),
        "levels": [vocab.LEVEL_LABELS[x] for x in p.levels if x in vocab.LEVEL_LABELS],
        "fields": [vocab.FIELD_LABELS[f] for f in p.fields if f in vocab.FIELD_LABELS],
        "skills": [SKILL_BY_ID[s].label for s in p.skills if s in SKILL_BY_ID],
        "keywords": list(p.keywords),
        "locations": places.split(" · ") if places else [],
        "terms": list(p.terms),
    }


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def profile_fp(p: Profile) -> str:
    """sha256 of the labels sent: the same labels, the same verdicts, whoever holds them."""
    return hashlib.sha256(_canonical(labels(p)).encode("utf-8")).hexdigest()


def posting_fields(i: int, c: Candidate) -> dict[str, object]:
    """One role, under POSTING_KEYS: its number in this request and four fields."""
    return {"i": i, "title": (c.title or "")[:_TITLE_MAX],
            "company": (c.company or "")[:_COMPANY_MAX],
            "location": (c.location or "")[:_LOCATION_MAX] or None, "term": c.term[0]}


def request_payload(p: Profile, matches: Sequence[Match]) -> dict[str, object]:
    return {"profile": labels(p),
            "postings": [posting_fields(i, m.cand) for i, m in enumerate(matches)]}


def prompt(payload: Mapping[str, object]) -> str:
    return FIT_PROMPT + json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------------ the answer

def _reason(text: str) -> str:
    one_line = _SPACES.sub(" ", text).strip()
    return one_line if len(one_line) <= REASON_MAX else one_line[:REASON_MAX - 1].rstrip() + "…"


def _row(row: object, count: int) -> tuple[int, Verdict] | None:
    if not isinstance(row, dict):
        return None
    i, verdict, reason = row.get("i"), row.get("verdict"), row.get("reason")
    if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count:
        return None
    if verdict not in VERDICTS or not isinstance(reason, str):
        return None
    return i, Verdict(verdict, _reason(reason))


def parse_verdicts(data: object, count: int) -> dict[int, Verdict]:
    """{posting number: Verdict} from an answer about `count` postings. Rows that cannot be
    used are dropped, and the first row for a number wins. Unparseable when none is left."""
    if not isinstance(data, list):
        raise Unparseable("not a list")
    found: dict[int, Verdict] = {}
    for row in data:
        parsed = _row(row, count)
        if parsed is not None and parsed[0] not in found:
            found[parsed[0]] = parsed[1]
    if not found:
        raise Unparseable("no usable verdict")
    return found


# ------------------------------------------------------------------ the cache

def cached(db: sqlite3.Connection, fp: str, role_hashes: Iterable[str]) -> dict[str, Verdict]:
    wanted = list(dict.fromkeys(role_hashes))
    if not wanted:
        return {}
    marks = ", ".join("?" * len(wanted))
    rows = db.execute(f"SELECT role_hash, verdict, reason FROM fit_verdicts "
                      f"WHERE profile_fp = ? AND role_hash IN ({marks})", (fp, *wanted))
    return {role_hash: Verdict(verdict, reason) for role_hash, verdict, reason in rows}


def remember(db: sqlite3.Connection, fp: str, verdicts: Mapping[str, Verdict], model: str,
             now: float) -> None:
    with db:
        db.executemany(
            "INSERT INTO fit_verdicts (profile_fp, role_hash, verdict, reason, model, at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(profile_fp, role_hash) DO UPDATE SET "
            "verdict = excluded.verdict, reason = excluded.reason, model = excluded.model, "
            "at = excluded.at",
            [(fp, role_hash, v.verdict, v.reason, model, now) for role_hash, v in verdicts.items()])


# ------------------------------------------------------------------ the budget

class Pace:
    """The requests of the last minute, for FIT_RPM. One lives as long as the process
    (PACE), because the minute does not end when a delivery tick does."""

    def __init__(self) -> None:
        self.stamps: tuple[float, ...] = ()

    def recent(self, now: float) -> tuple[float, ...]:
        return tuple(t for t in self.stamps if now - t < _MINUTE_S)

    def record(self, now: float) -> None:
        self.stamps = self.recent(now) + (now,)


PACE = Pace()


class Budget:
    """FIT_RPD for `day`, counted in fit_usage, and FIT_RPM, paced by `pace`. `acquire` is
    asked before every attempt, a retry included, and counts the request it allows."""

    def __init__(self, db: sqlite3.Connection, day: str, *, rpd: int, rpm: int, pace: Pace,
                 clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]]) -> None:
        self.db, self.day, self.rpd, self.rpm = db, day, rpd, rpm
        self.pace, self.clock, self.sleep = pace, clock, sleep

    async def acquire(self) -> bool:
        if usage(self.db, self.day).requests >= self.rpd:
            return False
        for _ in range(_MAX_WAITS):
            now = self.clock()
            recent = self.pace.recent(now)
            if len(recent) < self.rpm:
                break
            await self.sleep(_MINUTE_S - (now - min(recent)) + 0.5)
        else:
            return False
        self.pace.record(self.clock())
        with self.db:
            self.db.execute("INSERT INTO fit_usage (day, requests) VALUES (?, 1) "
                            "ON CONFLICT(day) DO UPDATE SET requests = requests + 1", (self.day,))
        return True

    def record_usage(self, meta: object) -> None:
        """The tokens a reply reports, added to the day's row. Best effort."""
        import llm
        prompt_tokens, output_tokens = llm.usage_tokens(meta)
        if not (prompt_tokens or output_tokens):
            return
        try:
            with self.db:
                self.db.execute("UPDATE fit_usage SET prompt_tokens = prompt_tokens + ?, "
                                "output_tokens = output_tokens + ? WHERE day = ?",
                                (prompt_tokens, output_tokens, self.day))
        except sqlite3.Error:
            pass


# ------------------------------------------------------------------ checking

def _note_failure(kind: str, now: float) -> None:
    global last_error, quiet_until
    last_error = (kind, now)
    if kind not in _NOT_AN_OUTAGE:
        quiet_until = now + COOL_OFF_S


def _noted(m: Match, verdict: Verdict | None) -> Match:
    return m if verdict is None else dataclasses.replace(m, fit=(verdict.verdict, verdict.reason))


def ordered(matches: Sequence[Match], verdicts: Mapping[str, Verdict]) -> list[Match]:
    """The matches to send: no_fit dropped, fit then unsure then unchecked, each keeping the
    matcher's order, with each verdict and reason attached."""
    noted = [_noted(m, verdicts.get(m.cand.rk_hash)) for m in matches]
    kept = [m for m in noted if m.fit is None or m.fit[0] != "no_fit"]
    return sorted(kept, key=lambda m: _ORDER.get(m.fit[0], _UNCHECKED) if m.fit else _UNCHECKED)


def with_cached(db: sqlite3.Connection, p: Profile, matches: Sequence[Match]) -> list[Match]:
    """For browsing (`/internships matches`): every match, in its order, with the verdict
    already cached for it. Never asks Gemini, so browsing costs no quota."""
    if not matches or not enabled(p):
        return list(matches)
    known = cached(db, profile_fp(p), (m.cand.rk_hash for m in matches))
    return [_noted(m, known.get(m.cand.rk_hash)) for m in matches]


async def _request(text: str, budget: Budget, session) -> object:
    """One request through llm.generate_json, on `session`, or on a session of its own
    whose timeout is GEMINI_HTTP_TIMEOUT. Its retries back off through the budget's
    sleep, the one `check` was given."""
    import aiohttp
    import llm
    s = _settings()
    ask = dict(key=s.gemini_key, model=s.gemini_model, prompt=text, schema=FIT_SCHEMA,
               max_attempts=s.llm_max_attempts, acquire=budget.acquire,
               on_usage=budget.record_usage, label=_LOG, fallback="sending unchecked",
               sleep=budget.sleep)
    if session is not None:
        return await llm.generate_json(session, **ask)
    async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=s.llm_http_timeout)) as own:
        return await llm.generate_json(own, **ask)


async def _ask(db: sqlite3.Connection, p: Profile, fp: str, todo: Sequence[Match], now: float,
               *, session, pace: Pace, sleep, clock, limit: float) -> dict[str, Verdict]:
    """Verdicts for `todo` in one request of at most `limit` seconds, cached; nothing
    when anything goes wrong."""
    import llm
    s = _settings()
    budget = Budget(db, quota_day(now), rpd=s.fit_rpd, rpm=s.fit_rpm, pace=pace, clock=clock,
                    sleep=sleep)
    try:
        data = await asyncio.wait_for(_request(prompt(request_payload(p, todo)), budget,
                                               session), limit)
        by_number = parse_verdicts(data, len(todo))
    except llm.LlmError as error:
        _note_failure(str(error), now)
        return {}
    except asyncio.TimeoutError:
        cut_short = limit < REQUEST_DEADLINE_S       # by the tick's deadline, not its own
        print(f"  {_LOG}: {_OUT_OF_TIME if cut_short else _TIMED_OUT} — falling back to "
              "sending unchecked", file=sys.stderr)
        _note_failure(_OUT_OF_TIME if cut_short else _TIMED_OUT, now)
        return {}
    except Unparseable:
        print(f"  {_LOG}: unparseable response — falling back to sending unchecked",
              file=sys.stderr)
        _note_failure("unparseable response", now)
        return {}
    verdicts = {todo[i].cand.rk_hash: verdict for i, verdict in by_number.items()}
    remember(db, fp, verdicts, s.gemini_model, now)
    return verdicts


async def check(db: sqlite3.Connection, p: Profile, matches: Sequence[Match], now: float, *,
                session=None, pace: Pace | None = None,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                clock: Callable[[], float] = time.time,
                limit: float | None = None) -> list[Match]:
    """
    The matches to send to `p`, as `ordered` puts them. Cached verdicts are used; up to
    FIT_BATCH of the rest go to Gemini in one request of at most `limit` seconds
    (None: REQUEST_DEADLINE_S), unless a failure has it cooling off or `limit` is spent.
    With the check off for `p`, or anything failing, whatever has no verdict comes back
    unchecked (module docstring).
    """
    if not matches or not enabled(p):
        return list(matches)
    limit = REQUEST_DEADLINE_S if limit is None else min(limit, REQUEST_DEADLINE_S)
    fp = profile_fp(p)
    known = cached(db, fp, (m.cand.rk_hash for m in matches))
    todo = list({m.cand.rk_hash: m for m in matches
                 if m.cand.rk_hash not in known}.values())[:_settings().fit_batch]
    asking = todo and now >= quiet_until and limit > 0
    fresh = await _ask(db, p, fp, todo, now, session=session, pace=pace or PACE, sleep=sleep,
                       clock=clock, limit=limit) if asking else {}
    return ordered(matches, {**known, **fresh})


def checker(db: sqlite3.Connection) -> FitCheck:
    """`check` on users.db, for one delivery tick: it never raises. Every request it makes
    shares TICK_DEADLINE_S, counted from now, so make one per tick. A failure of any kind
    is logged by its type and the matches go out unchecked."""
    ends = time.monotonic() + TICK_DEADLINE_S

    async def check_one(p: Profile, matches: Sequence[Match], now: float) -> list[Match]:
        try:
            return await check(db, p, matches, now, limit=ends - time.monotonic())
        except Exception as error:       # an alert must never wait on the check
            print(f"{_LOG} failed: {type(error).__name__}", file=sys.stderr)
            _note_failure(type(error).__name__, now)
            return list(matches)
    return check_one
