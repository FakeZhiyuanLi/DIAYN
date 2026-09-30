"""
posting_details.py
~~~~~~~~~~~~~~~~~~
Salary and full description for ONE posting, on demand: what `/internships
info` shows under a role. The sweep never stores these (they would bloat the
database and the list endpoints mostly omit them), so the bot asks the board
itself when a user asks about a role.

Moved from the root `internship_poller.py`, whose only caller this ever was,
because the scraper is moving to its own repository and process and this is
the bot's request, not the scraper's. The regexes, `strip_html`, the salary
helpers and each platform's branch are unchanged. Four things are new:

  * **Its own session**, behind CONCURRENT_FETCHES slots, with a User-Agent
    that says which bot is asking. It no longer borrows the poller's host gate.
  * **The stored URL no longer picks the host.** Once the scraper is another
    process, `postings.url` is data another repository writes. `fetchable`
    allows only https, on the default port, to a host that belongs to the
    posting's platform: `*.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`,
    `*.myworkdayjobs.com`, and for iCIMS `*.icims.com` or the board's own host
    as the registry publishes it (the adapter supports a company careers origin
    fronting iCIMS). Anything else is refused before a session is opened.
  * The fixed API hosts (`boards-api.greenhouse.io`, `api.lever.co`,
    `api.ashbyhq.com`) are still built from slugs parsed out of the URL.
  * It serves both sweep modes: `INTERN_SWEEP` changes where postings are
    read from, not how their details are fetched.

The URL shapes each adapter stores are pinned by the contract fixture
`contract/sample_urls.json`, and test_posting_details runs these regexes
against it.
"""

import asyncio
import html
import re
from collections.abc import Collection
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp

#: At most this many detail fetches in flight: it runs on a person's command, not a sweep.
CONCURRENT_FETCHES = 2
TIMEOUT_S = 20
#: Redirects an iCIMS board may take, each judged by `fetchable`; the fixed API hosts
#: follow none (aiohttp would otherwise follow ten, to any host).
MAX_REDIRECTS = 3
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
UA = ("DIAYN/1.0 (Discord bot; fetches one posting when a user asks; "
      "+https://github.com/FakeZhiyuanLi/DIAYN)")

#: platform -> the hosts its stored URLs may name; "*." means any subdomain.
PLATFORM_HOSTS = {
    "greenhouse": ("*.greenhouse.io",),
    "lever": ("jobs.lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
    "workday": ("*.myworkdayjobs.com",),
    "icims": ("*.icims.com",),
}

GH_JOB_URL_RE = re.compile(r"greenhouse\.io/([a-z0-9_-]+)/jobs/(\d+)", re.I)
LEVER_JOB_URL_RE = re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)/([0-9a-f-]{16,})", re.I)
ASHBY_JOB_URL_RE = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/", re.I)
WD_JOB_URL_RE = re.compile(
    r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/([^/]+)(/job/.+)$", re.I)

SALARY_TEXT_RE = re.compile(
    r"(?:\$|USD\s?|€|£)\s?\d{1,3}(?:[,.]\d{3})*(?:\.\d{2})?\s?(?:k\b)?"
    r"(?:\s*(?:[-–—]|to\s)\s*(?:\$|USD\s?|€|£)?\s?\d{1,3}(?:[,.]\d{3})*"
    r"(?:\.\d{2})?\s?(?:k\b)?)?"
    r"(?:\s*(?:per|/)\s*(?:hour|hr|year|yr|annum|month|week))?", re.I)

_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _fetch_slots() -> asyncio.Semaphore:
    # One semaphore per event loop: one made on one loop cannot be awaited on
    # another, and the tests run each case in its own asyncio.run.
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(CONCURRENT_FETCHES))
    return _slots[1]


# ------------------------------------------------------------------ which URLs may be fetched

def _host_matches(host: str, pattern: str) -> bool:
    return host.endswith(pattern[1:]) if pattern.startswith("*.") else host == pattern


def fetchable(platform: str, url: str | None, icims_hosts: Collection[str] = ()) -> bool:
    """True when `url` is https, on the default port, to a host of `platform`'s own
    (module docstring). A platform with no detail fetch is never fetchable."""
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:                   # an unparseable netloc or port
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or port not in (None, 443):
        return False
    if platform == "icims" and host in icims_hosts:
        return True
    return any(_host_matches(host, p) for p in PLATFORM_HOSTS.get(platform, ()))


# ------------------------------------------------------------------ text helpers (moved unchanged)

def strip_html(s: str) -> str:
    """Best-effort HTML -> readable plain text, no external deps."""
    s = html.unescape(s or "")  # Greenhouse escapes its whole content field
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr|table)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


def find_salary_in_text(text) -> Optional[str]:
    """Fallback for boards without structured pay data: first believable
    money figure in the description. Demands a range, a 'k', a per-period,
    or a 5+ digit amount so a bare '$5' can't match."""
    for m in SALARY_TEXT_RE.finditer(text or ""):
        s = m.group(0).strip()
        if (re.search(r"[-–—]|\bto\s", s) or re.search(r"\dk\b", s, re.I)
                or re.search(r"per|/", s) or re.search(r"\d[\d,]{4,}", s)):
            return s
    return None


# ------------------------------------------------------------------ one platform each (moved unchanged)

async def _greenhouse(sess, url, external_id, out) -> None:
    m = GH_JOB_URL_RE.search(url or "")
    if not m:
        return
    slug, jid = m.groups()
    async with sess.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}",
                        allow_redirects=False) as r:
        if r.status != 200:
            return
        d = await r.json(content_type=None)
    out["description"] = strip_html(d.get("content") or "")
    parts = []
    for pr in d.get("pay_input_ranges") or []:
        lo, hi = pr.get("min_cents"), pr.get("max_cents")
        if lo is None or hi is None:
            continue
        sym = "$" if (pr.get("currency_type") or "USD") == "USD" else f"{pr['currency_type']} "
        rng = f"{sym}{lo / 100:,.0f}–{sym}{hi / 100:,.0f}"
        if pr.get("title"):
            rng += f" ({pr['title']})"
        parts.append(rng)
    out["salary"] = "; ".join(parts) or None


async def _lever(sess, url, external_id, out) -> None:
    m = LEVER_JOB_URL_RE.search(url or "")
    if not m:
        return
    slug, pid = m.groups()
    async with sess.get(f"https://api.lever.co/v0/postings/{slug}/{pid}", allow_redirects=False) as r:
        if r.status != 200:
            return
        d = await r.json(content_type=None)
    pieces = [d.get("descriptionPlain") or strip_html(d.get("description") or "")]
    for sec in d.get("lists") or []:
        pieces.append(f"{sec.get('text', '')}\n{strip_html(sec.get('content') or '')}")
    out["description"] = "\n".join(x for x in pieces if x).strip()
    sr = d.get("salaryRange") or {}
    if sr.get("min") is not None and sr.get("max") is not None:
        sym = "$" if (sr.get("currency") or "USD") == "USD" else f"{sr['currency']} "
        iv = (sr.get("interval") or "").replace("-", " ")
        out["salary"] = f"{sym}{sr['min']:,}–{sym}{sr['max']:,}" + (f" {iv}" if iv else "")


async def _ashby(sess, url, external_id, out) -> None:
    m = ASHBY_JOB_URL_RE.search(url or "")
    if not m:
        return
    async with sess.get("https://api.ashbyhq.com/posting-api/job-board/"
                        f"{m.group(1)}?includeCompensation=true", allow_redirects=False) as r:
        if r.status != 200:
            return
        d = await r.json(content_type=None)
    job = next((j for j in d.get("jobs", []) if str(j.get("id")) == str(external_id)), None)
    if not job:
        return
    out["description"] = strip_html(job.get("descriptionHtml") or job.get("descriptionPlain") or "")
    comp = job.get("compensation") or {}
    out["salary"] = (job.get("compensationTierSummary") or comp.get("compensationTierSummary")
                     or comp.get("scrapeableCompensationSalarySummary"))


async def _icims(sess, url, external_id, out, hosts: Collection[str] = ()) -> None:
    # The plain job page is a 4KB JS shell; the ?in_iframe=1 view is
    # server-rendered and carries the full posting text. A board may redirect, so
    # each hop is judged by `fetchable` exactly as the first request was: without
    # that, one redirect could hand users the body of any host, private ones too.
    sep = "&" if "?" in (url or "") else "?"
    target = f"{url}{sep}in_iframe=1"
    for _ in range(MAX_REDIRECTS + 1):
        async with sess.get(target, allow_redirects=False) as r:
            if r.status in _REDIRECTS:
                location = r.headers.get("Location")
                target = urljoin(target, location) if location else ""
                if not fetchable("icims", target, hosts):
                    return
                continue
            if r.status != 200:
                return
            out["description"] = strip_html(await r.text())
            return


async def _workday(sess, url, external_id, out) -> None:
    m = WD_JOB_URL_RE.match(url or "")
    if not m:
        return
    tenant, wd, site, path = m.groups()
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
    async with sess.get(api, headers={"Accept": "application/json"}, allow_redirects=False) as r:
        if r.status != 200:
            return
        d = await r.json(content_type=None)
    info = d.get("jobPostingInfo") or {}
    out["description"] = strip_html(info.get("jobDescription") or "")


_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "icims": _icims,
             "workday": _workday}


async def fetch_details(platform: str, url: str | None, external_id,
                        *, icims_hosts: Collection[str] = ()) -> dict:
    """{'salary': str|None, 'description': str|None} for one posting, plus
    'salary_certain' once a request was made.

    Best-effort by design: any HTTP failure, unrecognized URL, or missing
    field just leaves the value None — callers treat both as optional. A URL
    `fetchable` refuses is answered the same way, without a request.
    """
    out = {"salary": None, "description": None}
    fetcher = _FETCHERS.get(platform)
    if fetcher is None or not fetchable(platform, url, icims_hosts):
        return out
    async with _fetch_slots():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                                         headers={"User-Agent": UA}) as sess:
            if platform == "icims":
                await _icims(sess, url, external_id, out, icims_hosts)
            else:
                await fetcher(sess, url, external_id, out)
    # Everything above came from a structured pay field — trustworthy. Mark it
    # so callers know whether the value needs confirming.
    out["salary_certain"] = out["salary"] is not None
    if not out["salary"] and out["description"]:
        # Regex guess, used only when nothing better is available (the
        # /internships path makes no LLM call).
        out["salary"] = find_salary_in_text(out["description"])
    return out
