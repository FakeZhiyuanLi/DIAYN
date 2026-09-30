#!/usr/bin/env python3
"""
Internship poller. Covers Greenhouse, Lever, Ashby, Workday.

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python internship_poller.py verify           # check every board is live
    .venv/bin/python internship_poller.py list --us        # all open US internships
    .venv/bin/python internship_poller.py list --sector finance
    .venv/bin/python internship_poller.py list --category swe
    .venv/bin/python internship_poller.py sweep --init     # first sweep of a new postings.db
    .venv/bin/python internship_poller.py sweep            # store + print what's new
    .venv/bin/python internship_poller.py watch            # every 15 min until Ctrl-C
    .venv/bin/python internship_poller.py stats
    .venv/bin/python internship_poller.py llm-diff         # compare regex vs Gemini on stored rows
    .venv/bin/python internship_poller.py sweep --llm      # classify new postings with Gemini
    .venv/bin/python internship_poller.py config           # the settings a run would use
    .venv/bin/python internship_poller.py upgrade-db       # a v2 postings.db, up to CONTRACT.md

    export GEMINI_API_KEY=...               # required for --llm
    export GEMINI_MODEL=gemini-3.5-flash-lite   # or gemini-3.6-flash
    export GEMINI_RPM=15 GEMINI_RPD=500     # match your AI Studio dashboard
    .venv/bin/python internship_poller.py discover         # mine + check ~2k boards -> boards.json
    .venv/bin/python internship_poller.py discover --yc    # + probe YC's 6k company dataset (slow)
    .venv/bin/python internship_poller.py prune --dry-run  # see what the retention rule removes

Settings come from the environment, filled in from the scraper's own .env:
the file POLLER_ENV_FILE names, else the one in this checkout — never the
working directory's. Every variable, and its default, is in Settings below.

Rows older than 30 days are deleted from `postings` on every sweep. A separate
`seen` table keeps every id forever, so pruned roles are never re-announced.

postings.db is read by DIAYN's bot, in bot/, under the promises in
CONTRACT.md; contract/ holds their machine-readable half. Nothing creates the
file unasked: `sweep` and `watch` refuse a missing file, or an empty ledger,
without --init.

Exactly one process writes it. `watch` holds <POSTINGS_DB>.lock for as long as
it runs; sweep, prune, upgrade-db, llm-diff, discover and list --llm hold it
while they run; and any of them exits 3, having done nothing, if another
process has it. stats, verify, config and plain `list` only read, and never
wait on it. Under pm2, `watch` outlives a failed sweep and, when restarted,
waits until the next sweep is due rather than sweeping at once.

Salary and descriptions are not fetched here: the bot fetches them itself, for
the one role a user asks about.

Sectors: tech, finance, healthcare, defense, industrial, retail, energy.

To stop hearing from a company entirely, add its name to BLOCKED_COMPANIES
below: its board is dropped at load and its stored rows are filtered out of
anything that reads `postings`.

WORKDAY NOTE
  Workday needs a (tenant, wd-instance, site) triple, not a single slug, and the
  site path is NOT guessable — brute-forcing common names ("External", "Careers")
  resolves ~0%. Harvest it from the company's careers URL:
      https://capitalone.wd1.myworkdayjobs.com/Capital_One_Careers
                ^tenant   ^wd            ^site
  Its postedOn field is human text ("Posted 4 Days Ago"), so lag resolution is
  day-level. Fine for detection, useless for measuring minutes.
"""

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import math
import os
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hints

# Run on its own, the scraper checks the Python version before it imports aiohttp
# and llm: on 3.9, macOS's own python3, llm's annotations raise a TypeError as it is
# imported. Everything above is the standard library, or hints, which 3.9 runs.
# Imported, it checks nothing: diayn.py has checked already.
if __name__ == "__main__":
    hints.exit_if_old_python()

import aiohttp  # noqa: E402

import llm  # noqa: E402

# --------------------------------------------------------------------------
# Configuration. Read once, by main(), after the scraper's own .env has loaded
# — never while this module is imported. Importing it reads no file and
# changes nothing in os.environ: until main() runs, SETTINGS holds the code
# defaults below and BOARDS is empty. Every default, and the variable that can
# change it, is in this block and nowhere else; `config` prints what a run
# would actually use.
# --------------------------------------------------------------------------

# The checkout this file lives in. The data files and the .env default to
# paths under it, never under the working directory: pm2 starts the scraper
# from wherever it was told to, and the bot's checkout has a .env and a
# postings.db of its own that must never be mistaken for these.
CHECKOUT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CHECKOUT, "data")


class ConfigError(RuntimeError):
    """A setting the scraper cannot start with. main() prints it and exits 1."""


@dataclass(frozen=True)
class Settings:
    """Everything the environment can change, bound once by main() as SETTINGS.

    Frozen because a `watch` process reads it on every sweep for as long as it
    runs: a value that could change underneath would let two sweeps in one
    process disagree about their own limits. SETTINGS_FROM_ENV maps each field
    to its variable.
    """

    # The data directory, and the data files, which default to it (DATA_FILES).
    # Each file can still be pointed somewhere else on its own.
    data_dir: str = DATA_DIR
    postings_db: str = os.path.join(DATA_DIR, "postings.db")
    boards_file: str = os.path.join(DATA_DIR, "boards.json")
    yc_cache: str = os.path.join(DATA_DIR, "yc_cache.json")
    # Who the owners of the boards can write to about this traffic. Every
    # request carries it in its User-Agent (user_agent, below), and `config`
    # shows it.
    contact: str = ""
    # The per-host politeness gate; see _HostGate.
    host_concurrency: int = 4
    host_min_interval: float = 0.12
    # repr=False, so a traceback or a stray print of the settings never shows
    # the key.
    gemini_key: Optional[str] = field(default=None, repr=False)
    # One model, one budget. A fallback chain across several models used to
    # live here, sized for a workload that classified full job descriptions
    # every few hours. That consumer is gone; what remains classifies
    # newly-seen postings by title — a handful of calls on a busy day — which
    # fits one model's daily cap.
    gemini_model: str = "gemini-3.5-flash-lite"
    llm_batch: int = 25
    # Free AI Studio tier: flash-lite is 15 RPM / 500 RPD, flash is 5 RPM / 250
    # RPD; both are 250k TPM. Defaults here are the conservative flash numbers
    # so a model switch can't silently exceed the limit — .env raises them for
    # flash-lite. Check your own dashboard; the published tables go stale.
    llm_rpm: int = 5
    llm_rpd: int = 250
    llm_tpm: int = 250000
    # How many times one Gemini request may be attempted before the batch
    # gives up and leaves its postings to the regex classifier. 3 = the call
    # plus 2 retries.
    llm_max_attempts: int = 3
    # Per-request client deadline for Gemini calls, in seconds.
    llm_http_timeout: float = 150.0
    # Circuit breaker: consecutive BATCHES that fail every attempt before the
    # run gives up and leaves the rest to the regex classifier. Each unit here
    # is a fully exhausted retry chain, so 3 is a much stronger signal of a
    # real outage than it was when one timeout counted as a failure.
    llm_max_batch_failures: int = 3
    # The quota day. The free tier's daily cap resets at midnight Pacific, and
    # the bot's quota panel says so; scraper_meta publishes this for it, and
    # llm_usage.day is the date in this zone (quota_day; CONTRACT.md, P7).
    llm_day_tz: str = "America/Los_Angeles"
    # The Discord bot (bot/), which reads this same .env. The token is its own
    # bot's, from the developer portal; repr=False as for the Gemini key.
    discord_token: Optional[str] = field(default=None, repr=False)
    # Who owns this bot, and may grant others access. Empty means the Discord
    # application's owner. Discord ids, so kept out of the repr too.
    owner_ids: tuple = field(default=(), repr=False)
    # The zone for alert hours and the bot's daily housekeeping. Separate from
    # llm_day_tz, which is the zone of Gemini's quota day.
    tz: str = "UTC"
    # The Gemini fit check: postings checked per request, and its own budget,
    # which with the --llm one above must fit the key's quota.
    fit_batch: int = 15
    fit_rpd: int = 200
    fit_rpm: int = 10

    @property
    def users_db(self) -> str:
        """The bot's own database, users.db, in the data directory: its users'
        profiles and ledgers, never the scraper's postings. It moves with
        DIAYN_DATA and has no variable of its own."""
        return os.path.join(self.data_dir, "users.db")


def absolute_path(raw) -> str:
    """`raw`, which must be an absolute path; ValueError otherwise.

    A relative one would resolve against the working directory, which under
    pm2 is wherever the process was started, and a .env does not expand `~`.
    """
    if not os.path.isabs(raw):
        raise ValueError(f"{raw!r} is not an absolute path")
    return raw


# The largest a Discord id can be: it is a 64-bit snowflake, and sqlite
# stores integers as signed 64-bit. 19 digits at most, which is checked first
# so a runaway value is never handed to int().
_MAX_DISCORD_ID = 2 ** 63 - 1
_MAX_DISCORD_ID_DIGITS = len(str(_MAX_DISCORD_ID))


def discord_ids(raw) -> tuple:
    """Comma-separated Discord user ids as a tuple of ints, each once.

    A refused entry is named by its position, never quoted: it is most likely
    somebody's id with a typo, and the message goes to a log.
    """
    ids = []
    for n, entry in enumerate(raw.split(","), 1):
        entry = entry.strip()
        if not entry:
            continue
        if not (entry.isascii() and entry.isdigit()
                and len(entry) <= _MAX_DISCORD_ID_DIGITS
                and 0 < int(entry) <= _MAX_DISCORD_ID):
            raise ValueError(f"entry {n} is not a Discord user id "
                             "(digits only, separated by commas)")
        ids.append(int(entry))
    return tuple(dict.fromkeys(ids))


# The data files that live in DIAYN_DATA unless named on their own, each by
# its file name there.
DATA_FILES = (("postings_db", "postings.db"), ("boards_file", "boards.json"),
              ("yc_cache", "yc_cache.json"))

# (field, variable, type), in the order `config` prints them.
SETTINGS_FROM_ENV = (
    ("data_dir", "DIAYN_DATA", absolute_path),
    ("postings_db", "POSTINGS_DB", str),
    ("boards_file", "BOARDS_FILE", str),
    ("yc_cache", "YC_CACHE", str),
    ("contact", "POLL_CONTACT", str),
    ("host_concurrency", "POLL_HOST_CONCURRENCY", int),
    ("host_min_interval", "POLL_HOST_MIN_INTERVAL", float),
    ("gemini_key", "GEMINI_API_KEY", str),
    ("gemini_model", "GEMINI_MODEL", str),
    ("llm_batch", "GEMINI_BATCH", int),
    ("llm_rpm", "GEMINI_RPM", int),
    ("llm_rpd", "GEMINI_RPD", int),
    ("llm_tpm", "GEMINI_TPM", int),
    ("llm_max_attempts", "GEMINI_MAX_ATTEMPTS", int),
    ("llm_http_timeout", "GEMINI_HTTP_TIMEOUT", float),
    ("llm_max_batch_failures", "GEMINI_MAX_BATCH_FAILURES", int),
    ("llm_day_tz", "LLM_DAY_TZ", ZoneInfo),
    ("discord_token", "DISCORD_TOKEN", str),
    ("owner_ids", "DIAYN_OWNER_IDS", discord_ids),
    ("tz", "DIAYN_TZ", ZoneInfo),
    ("fit_batch", "FIT_BATCH", int),
    ("fit_rpd", "FIT_RPD", int),
    ("fit_rpm", "FIT_RPM", int),
)
# `config` says whether these are set, and never what they are.
SECRET_VARIABLES = frozenset({"GEMINI_API_KEY", "DISCORD_TOKEN"})
# `config` says how many of these there are, and never which: Discord ids.
COUNTED_VARIABLES = frozenset({"DIAYN_OWNER_IDS"})
_LABEL_WIDTH = max(len(var) for _, var, _ in SETTINGS_FROM_ENV) + 2

# The code defaults, until main() binds what the environment asks for.
SETTINGS = Settings()


def _parse(var, raw, kind):
    """`raw` as a `kind`, or a ConfigError naming `var`.

    A count below 1 is refused rather than obeyed: POLL_HOST_CONCURRENCY=0 is
    a semaphore nobody can acquire, and every request would wait forever with
    no error. A seconds value may be 0 (no gap, no deadline) but not negative.
    A time zone is checked here and kept as its name, so a misspelt one stops
    the start instead of failing every later ask for today's quota day. Any
    other kind is a parser that raises ValueError with its own reason.
    """
    if kind is str:
        return raw
    if kind is ZoneInfo:
        try:
            ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"{var}={raw!r} is not a time zone "
                              "(for example America/Los_Angeles)") from None
        return raw
    if kind not in (int, float):
        try:
            return kind(raw)
        except ValueError as e:
            raise ConfigError(f"{var}: {e}") from None
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError(f"{var}={raw!r} is not a number") from None
    floor = 1 if kind is int else 0
    if value < floor:
        raise ConfigError(f"{var}={raw!r} must be at least {floor}")
    return value


def configure(environ) -> Settings:
    """The settings `environ` asks for, over the code defaults.

    Reads only the mapping it is given — main() passes os.environ once the
    .env has loaded, tests pass a dict — and an unset or empty variable keeps
    its default, so `POSTINGS_DB=` in a .env switches the line off rather than
    naming a database "" in the working directory. A value that cannot be used
    raises ConfigError, naming its variable, before anything is swept.
    DIAYN_DATA moves every data file that is not named on its own.
    """
    changes = {}
    for name, var, kind in SETTINGS_FROM_ENV:
        raw = (environ.get(var) or "").strip()
        if raw:
            changes[name] = _parse(var, raw, kind)
    data_dir = changes.get("data_dir")
    if data_dir:
        for name, file_name in DATA_FILES:
            changes.setdefault(name, os.path.join(data_dir, file_name))
    return Settings(**changes)


def env_file_path(environ=os.environ, checkout=CHECKOUT) -> str:
    """Where the scraper's .env is: POLLER_ENV_FILE, else the checkout's own.

    Never the working directory and never the data directory, so a scraper
    started from the bot's checkout, or sharing the bot's data directory, can
    never pick up the bot's .env.
    """
    return environ.get("POLLER_ENV_FILE") or os.path.join(checkout, ".env")


def load_env_file() -> Optional[str]:
    """Load the scraper's .env into os.environ; return its path, or None.

    override=False: a variable already in the environment — pm2's env block,
    an export in the shell — is the operator's last word, and the file only
    fills in what is unset. No file at the default path is normal; plain
    environment variables work alone. A POLLER_ENV_FILE naming no file, or a
    file with no python-dotenv to read it, is a ConfigError instead of the old
    silent skip: either way the run would go ahead on settings nobody chose.
    """
    path = env_file_path()
    if not os.path.isfile(path):
        if os.environ.get("POLLER_ENV_FILE"):
            raise ConfigError(f"POLLER_ENV_FILE={path}: no such file")
        return None
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise ConfigError(
            f"{path} exists, but python-dotenv is not installed to read it. "
            f"{hints.install_hint()} Or export the variables instead."
        ) from None
    load_dotenv(path, override=False)
    return path


def _shown(var, value) -> str:
    """One setting's value as `config` prints it: a secret only as set or not,
    and a list of ids only as how many."""
    if var in SECRET_VARIABLES:
        return "set" if value else "not set"
    if var in COUNTED_VARIABLES and value:
        return f"set ({len(value)} id{'' if len(value) == 1 else 's'})"
    return "(not set)" if value in ("", None, ()) else str(value)


def config_lines(settings, env_file) -> list:
    """What `config` prints: the .env used, then every setting by its variable.

    The keys are shown as set or not set, never as a value, and the owners
    only as a count, so the output can be pasted into an issue or a chat as
    it stands.
    """
    used = env_file or f"none (no {os.path.join(CHECKOUT, '.env')})"
    return ([f"{'env file':<{_LABEL_WIDTH}}{used}"]
            + [f"{var:<{_LABEL_WIDTH}}{_shown(var, getattr(settings, name))}"
               for name, var, _ in SETTINGS_FROM_ENV])


def _parent_made(path) -> str:
    """`path`, once the directory it goes in exists.

    The data files default to <checkout>/data/, which a fresh checkout does
    not have (it is gitignored), and neither open() nor sqlite creates it.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


CONCURRENCY = 20

# --------------------------------------------------------------------------
# Politeness. These are public endpoints belonging to other people, and a
# burst of ~80 req/s to one host is exactly what gets an IP blocked — the
# global semaphores above bound total in-flight work, not per-HOST load. This
# gate caps concurrent requests AND enforces a minimum gap per host, so a
# sweep looks like steady background traffic instead of a scrape.
# --------------------------------------------------------------------------

# Its limits are SETTINGS.host_concurrency and host_min_interval, set by
# POLL_HOST_CONCURRENCY and POLL_HOST_MIN_INTERVAL.
_host_sems: dict = {}
_host_last: dict = {}
_host_locks: dict = {}


def _host_of(url) -> str:
    m = re.match(r"https?://([^/]+)", str(url))
    return (m.group(1) if m else str(url)).lower()


class _HostGate:
    """Async context manager: bounded concurrency + min spacing per host."""

    def __init__(self, url):
        self.host = _host_of(url)

    async def __aenter__(self):
        sem = _host_sems.setdefault(
            self.host, asyncio.Semaphore(SETTINGS.host_concurrency))
        await sem.acquire()
        lock = _host_locks.setdefault(self.host, asyncio.Lock())
        async with lock:
            wait = SETTINGS.host_min_interval - (time.time() - _host_last.get(self.host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            _host_last[self.host] = time.time()
        return self

    async def __aexit__(self, *exc):
        _host_sems[self.host].release()
        return False


def polite_session(**kw):
    """aiohttp session whose every request passes through the host gate.

    Wrapping _request covers all adapters at once — no adapter has to
    remember to be polite, and new ones inherit it for free. The Gemini API
    is exempt: every caller of llm.py has a budget of its own (LlmBudget here,
    the fit check's in the bot).
    """
    sess = aiohttp.ClientSession(**kw)
    inner = sess._request

    async def gated(method, url, **rkw):
        if "generativelanguage" in str(url):
            return await inner(method, url, **rkw)
        async with _HostGate(url):
            return await inner(method, url, **rkw)

    sess._request = gated
    return sess
TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_AGE_DAYS = 30   # postings older than this are ignored; override with --max-age
PRUNE_DAYS = 30     # rows older than this are deleted from `postings` on each sweep
SCHEMA_VERSION = 2
# The release, and the read contract it keeps (CONTRACT.md). MAJOR moves with
# CONTRACT_VERSION, and both are published in scraper_meta for the bot to check.
__version__ = "1.0.0"
CONTRACT_VERSION = "1"
# How long a connection waits out another's lock before failing, in ms.
BUSY_TIMEOUT_MS = 5000
# `watch`'s gap between sweeps, in seconds, unless --interval says otherwise.
DEFAULT_INTERVAL_S = 900
# The shortest --interval accepted. Less would sweep every board all but back
# to back, and publish that to the bot as the gap to expect.
MIN_INTERVAL_S = 60
# Where the traffic comes from, named in every request.
PROJECT_URL = "https://github.com/FakeZhiyuanLi/DIAYN"


def user_agent(contact) -> str:
    """The User-Agent of every request: the project, and `contact` if set.

    Honest rather than a browser's, so a board's owner can tell this traffic
    apart and knows where to write about it. Takes the contact rather than
    reading SETTINGS, so resolve_boards.py can send the very same agent. The
    version is MAJOR.MINOR of __version__.
    """
    version = ".".join(__version__.split(".")[:2])
    reach = f"; contact: {contact}" if contact else ""
    return f"DIAYN/{version} (+{PROJECT_URL}{reach})"

# --------------------------------------------------------------------------
# Board registry. Every entry below was probed live on 2026-07-31.
# Format: (platform, slug, display name, sector)
# Workday slug format: "tenant/wdN/SitePath"
# --------------------------------------------------------------------------

SEED_BOARDS = [
    # ---- quant / trading ----
    ("greenhouse", "jumptrading",        "Jump Trading",         "finance"),
    ("greenhouse", "point72",            "Point72",              "finance"),
    ("greenhouse", "imc",                "IMC Trading",          "finance"),
    ("greenhouse", "virtu",              "Virtu Financial",      "finance"),
    ("greenhouse", "akunacapital",       "Akuna Capital",        "finance"),
    ("greenhouse", "schonfeld",          "Schonfeld",            "finance"),
    ("greenhouse", "squarepointcapital", "Squarepoint Capital",  "finance"),
    ("greenhouse", "flowtraders",        "Flow Traders",         "finance"),
    ("lever",      "voleon",             "Voleon",               "finance"),
    # ---- banks / insurance / fintech (Workday) ----
    ("workday",    "statestreet/wd1/Global",                "State Street",  "finance"),
    ("greenhouse", "robinhood",          "Robinhood",            "finance"),
    ("greenhouse", "coinbase",           "Coinbase",             "finance"),
    ("ashby",      "ramp",               "Ramp",                 "finance"),
    # ---- healthcare / bio ----
    ("workday",    "cvshealth/wd1/CVS_Health_Careers",      "CVS Health",    "healthcare"),
    ("workday",    "humana/wd5/Humana_External_Career_Site", "Humana",       "healthcare"),
    ("greenhouse", "truveta",            "Truveta",              "healthcare"),
    ("greenhouse", "pathai",             "PathAI",               "healthcare"),
    ("greenhouse", "ginkgobioworks",     "Ginkgo Bioworks",      "healthcare"),
    ("ashby",      "nabla",              "Nabla",                "healthcare"),
    # ---- defense / aerospace ----
    ("workday",    "boeing/wd1/EXTERNAL_CAREERS",           "Boeing",        "defense"),
    ("greenhouse", "spacex",             "SpaceX",               "defense"),
    ("greenhouse", "rocketlab",          "Rocket Lab",           "defense"),
    ("greenhouse", "astranis",           "Astranis",             "defense"),
    ("greenhouse", "vast",               "Vast",                 "defense"),
    ("lever",      "shieldai",           "Shield AI",            "defense"),
    ("lever",      "palantir",           "Palantir",             "defense"),
    # ---- industrial / auto / energy ----
    ("workday",    "cat/wd5/CaterpillarCareers",            "Caterpillar",   "industrial"),
    ("greenhouse", "lucidmotors",        "Lucid Motors",         "industrial"),
    ("greenhouse", "nuro",               "Nuro",                 "industrial"),
    ("greenhouse", "waymo",              "Waymo",                "industrial"),
    ("lever",      "weride",             "WeRide",               "industrial"),
    ("greenhouse", "solidpower",         "Solid Power",          "energy"),
    ("greenhouse", "redwoodmaterials",   "Redwood Materials",    "energy"),
    # ---- retail / consumer ----
    ("workday",    "target/wd5/targetcareers",              "Target",        "retail"),
    ("greenhouse", "tripadvisor",        "Tripadvisor",          "retail"),
    ("lever",      "matchgroup",         "Match Group",          "retail"),
    ("greenhouse", "flexport",           "Flexport",             "retail"),
    # ---- tech ----
    ("greenhouse", "cloudflare",         "Cloudflare",           "tech"),
    ("greenhouse", "zscaler",            "Zscaler",              "tech"),
    ("greenhouse", "databricks",         "Databricks",           "tech"),
    ("greenhouse", "stripe",             "Stripe",               "tech"),
    ("greenhouse", "verkada",            "Verkada",              "tech"),
    ("greenhouse", "scaleai",            "Scale AI",             "tech"),
    ("greenhouse", "samsara",            "Samsara",              "tech"),
    ("greenhouse", "figma",              "Figma",                "tech"),
    ("greenhouse", "airtable",           "Airtable",             "tech"),
    ("greenhouse", "gitlab",             "GitLab",               "tech"),
    ("greenhouse", "cribl",              "Cribl",                "tech"),
    ("greenhouse", "braze",              "Braze",                "tech"),
    ("greenhouse", "chainguard",         "Chainguard",           "tech"),
    ("ashby",      "perplexity",         "Perplexity",           "tech"),
    ("ashby",      "notion",             "Notion",               "tech"),
    ("ashby",      "replit",             "Replit",               "tech"),
    ("ashby",      "modal",              "Modal",                "tech"),
    ("ashby",      "cursor",             "Cursor",               "tech"),
    ("ashby",      "linear",             "Linear",               "tech"),
    ("ashby",      "supabase",           "Supabase",             "tech"),
    ("ashby",      "vanta",              "Vanta",                "tech"),
    ("ashby",      "posthog",            "PostHog",              "tech"),
    ("ashby",      "sierra",             "Sierra",               "tech"),
    ("ashby",      "cognition",          "Cognition",            "tech"),
    ("ashby",      "harvey",             "Harvey",               "tech"),
    ("ashby",      "weaviate",           "Weaviate",             "tech"),
]

# Discovered boards live in boards.json (SETTINGS.boards_file, written by
# `discover`). The seed list above is the hand-curated fallback so the tool
# works with no setup.


def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


# --------------------------------------------------------------------------
# Companies nobody wants to hear from. A blocked company's board is dropped at
# load, so it is never polled, never stored and never announced — one edit
# here is the whole block for everything the tracker fetches from now on.
#
# Two consequences worth knowing before editing this set:
#   * `postings` holds up to PRUNE_DAYS of history, so rows stored before the
#     block stay readable until the pruner reaches them. `drop_blocked` is how
#     a reader of that table honours the block in the meantime; every query
#     that shows somebody a stored posting goes through it.
#   * unblocking a company hands its whole open board to the next sweep as
#     "new" — its ids never entered `seen` while it was blocked.
# --------------------------------------------------------------------------

BLOCKED_COMPANIES = {"Rocket Lab"}


def _blocked_norm(names) -> frozenset:
    """The normalised prefixes `names` block.

    Empties dropped deliberately: "" is a prefix of everything, so one blank
    entry would block the entire registry. The bot applies the same rule to
    the blocked_companies table (CONTRACT.md, B7), and
    contract/company_norm_cases.json pins the two together.
    """
    return frozenset(n for n in (_norm(c) for c in names) if n)


_BLOCKED_NORM = _blocked_norm(BLOCKED_COMPANIES)


def is_blocked_company(name) -> bool:
    """True for a company the tracker must not show.

    Matched on letters and digits only, and as a *prefix* rather than an exact
    string, because the name a board is identified by is not the name anybody
    types. `discover` writes the ATS slug into the company column, a company
    files under a longer legal name, and a Workday board is identified by a
    `tenant/wd-instance/site` path. All three are the blocked name with
    something stuck on the end:

        Rocket Lab -> rocketlab -> rocketlabusa, rocketlabinc,
                                   rocketlabwd1rocketlabcareers

    An exact match blocks the hand-written seed row and none of those, which is
    the shape of a block that looks fine and quietly stops working.

    The boundary, and the trap when editing BLOCKED_COMPANIES: this matches the
    START of a name, so "Astro Rocket Labs" is not blocked but "Rocket Lab
    Adjacent Inc" is. Over-blocking is the safe direction for a blocklist, but
    it means a short or common entry would take unrelated companies with it —
    keep the entries long and distinctive, or block the exact slug instead.

    Anything that is not a string is not a company name: never blocked, and
    never an error. `load_boards` already drops rows that are not all strings,
    so this is the second line of defence rather than the first — but it runs
    at start-up, where a raise is a scraper that will not start.
    """
    if not isinstance(name, str):
        return False
    candidate = _norm(name)
    return any(candidate.startswith(b) for b in _BLOCKED_NORM)


def drop_blocked(rows, company_at=0):
    """`rows` minus the blocked companies, order kept, input untouched.

    `company_at` is the index of the company in each row, because the callers
    are SQL queries with different column lists.
    """
    return [r for r in rows if not is_blocked_company(r[company_at])]


def _as_board(entry):
    """`entry` as a board tuple, or None if it is not one.

    A board is exactly [platform, slug, company, sector], all strings — what
    SEED_BOARDS holds and all `discover` ever writes (`r[:4]`). boards.json is
    edited by hand, and every other shape breaks something: a row that is not
    four long makes `fetch_all` raise before a single board is polled, because
    it spreads each row into a five-argument call.
    """
    if (isinstance(entry, list) and len(entry) == 4
            and all(isinstance(v, str) for v in entry)):
        return tuple(entry)
    return None


def load_boards():
    """boards.json, plus every seed board it does not mention, minus the blocked.

    Runs at start-up, before any command (main), and reads SETTINGS.boards_file
    — so nothing boards.json can hold may raise here: a raise is a scraper that
    will not start, and under pm2 a restart loop. A file that will not parse
    falls back to the seed boards, and a row that is not a board is dropped.
    Both are named on stderr; neither stops the start, and neither stops a
    sweep.
    """
    try:
        with open(SETTINGS.boards_file) as f:
            listed = json.load(f)
    except FileNotFoundError:
        listed = []
    except (OSError, ValueError) as e:  # ValueError includes JSONDecodeError
        print(f"boards.json: cannot read it ({e}); using the seed boards only",
              file=sys.stderr)
        listed = []
    if not isinstance(listed, list):
        print("boards.json: expected a list of boards; using the seed boards only",
              file=sys.stderr)
        listed = []
    rows = []
    for entry in listed:
        board = _as_board(entry)
        if board is None:
            print(f"boards.json: ignoring {entry!r}, which is not "
                  "[platform, slug, company, sector]", file=sys.stderr)
        else:
            rows.append(board)
    seen = {(r[0], r[1]) for r in rows}
    rows += [b for b in SEED_BOARDS if (b[0], b[1]) not in seen]
    # Both identifying columns, and after the merge rather than inside the seed
    # list. `cmd_discover` appends [plat, slug, slug, "unknown", n] and dumps
    # r[:4], so a discovered board has no display name at all — its company
    # column IS the slug. Checking the label alone would drop the hand-written
    # seed row and let the next `discover` run put the same company straight
    # back under whatever slug it was found at.
    return [r for r in rows
            if not (is_blocked_company(r[1]) or is_blocked_company(r[2]))]


# Bound by main() to `load_boards()`, after the settings. Functions read it
# when they run, never at import, so it is empty until then.
BOARDS = ()
# When this process booted, for scraper_meta.started_at. Bound by main(), like
# BOARDS; a process that never booted (a test) reports when it published.
STARTED_AT: Optional[float] = None

# --------------------------------------------------------------------------
# Discovery — the registry is the bottleneck, not the poller. This mines ATS
# slugs out of the community internship repos' apply links, then validates each
# against the live API. ~29k links yield ~2.3k slugs at a ~90% live rate.
# --------------------------------------------------------------------------

# Repo listing files, mined for apply links. The new-grad repos are included on
# purpose: those companies hire interns too, and their apply links expose slugs
# the internship repos miss. ~11MB each, so this is a once-a-week job.
REPO_LISTINGS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/vanshb03/New-Grad-2027/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/main/data/jobs.json",
]

SLUG_PATTERNS = {
    "greenhouse": r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_-]+)",
    "lever": r"jobs\.lever\.co/([a-z0-9_-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([a-z0-9_-]+)",
}
WORKDAY_PATTERN = (r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
                   r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")

# Slugs that are real boards but never worth polling.
SLUG_BLOCKLIST = {"embed", "job_app", "jobs", "job", "www", "api", "static",
                  "assets", "search", "board", "boards", "error", "404"}


def extract_slugs(text):
    found = set()
    for plat, rx in SLUG_PATTERNS.items():
        for m in re.finditer(rx, text, re.I):
            slug = m.group(1).lower()
            if slug not in SLUG_BLOCKLIST and len(slug) > 1:
                found.add((plat, slug))
    for m in re.finditer(WORKDAY_PATTERN, text):
        found.add(("workday", f"{m.group(1)}/{m.group(2)}/{m.group(3)}"))
    return found


async def mine_repos(sess):
    found, nbytes = set(), 0
    for u in REPO_LISTINGS:
        label = u.split("/")[4]
        try:
            async with sess.get(u) as r:
                if r.status != 200:
                    print(f"  -- {label:<28} HTTP {r.status}")
                    continue
                raw = await r.text()
        except Exception as e:
            print(f"  -- {label:<28} {type(e).__name__}")
            continue
        nbytes += len(raw)
        f = extract_slugs(raw)
        found |= f
        print(f"  ok {label:<28} {len(raw)//1024:>6}KB  {len(f)} slugs")
    return found, nbytes


async def mine_commoncrawl(sess, max_pages=3):
    """Best-effort. CC's index service is frequently 503 — treat as a bonus,
    never a dependency. When it works it yields far more slugs than the repos."""
    try:
        async with sess.get("https://index.commoncrawl.org/collinfo.json") as r:
            if r.status != 200:
                print(f"  -- common crawl              HTTP {r.status} (index service "
                      f"is often down; skipping)")
                return set()
            idx = (await r.json(content_type=None))[0]["id"]
    except Exception as e:
        print(f"  -- common crawl              {type(e).__name__} (skipping)")
        return set()

    found = set()
    for host in ("boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com"):
        for page in range(max_pages):
            u = (f"https://index.commoncrawl.org/{idx}-index?"
                 f"url={host}/*&output=json&page={page}")
            try:
                async with sess.get(u) as r:
                    if r.status != 200:
                        break
                    text = await r.text()
            except Exception:
                break
            found |= extract_slugs(text)
        print(f"  ok common crawl {host:<26} running total {len(found)}")
    return found


@dataclass
class Posting:
    platform: str
    external_id: str
    company: str
    sector: str
    title: str
    location: str
    url: str
    published: Optional[float]
    approx_date: bool = False   # True when we only know the day, not the minute
    unbounded: bool = False     # True for Workday "30+ Days Ago" — a floor, not a date


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------


def _ts(v) -> Optional[float]:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Offset-less strings from the ATS APIs mean UTC; .timestamp() on a
            # naive datetime would read them as machine-local time instead.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


POSTED_RE = re.compile(r"(\d+)\+?\s*(day|hour|month)", re.I)


def _workday_posted(text) -> tuple:
    """'Posted 4 Days Ago' -> (epoch, approx, unbounded). Day-level resolution.

    "Posted 30+ Days Ago" is Workday's terminal bucket — at least 30 days, but
    possibly 400. Recorded as 30d with unbounded=True so the age filter treats
    it as unknown rather than pretending it is fresh.
    """
    if not text:
        return None, True, False
    t = str(text).lower()
    if "today" in t:
        return time.time(), True, False
    if "yesterday" in t:
        return time.time() - 86400, True, False
    m = POSTED_RE.search(t)
    if not m:
        return None, True, False
    n, unit = int(m.group(1)), m.group(2)
    secs = {"hour": 3600, "day": 86400, "month": 2592000}[unit]
    return time.time() - n * secs, True, "+" in t


async def fetch_greenhouse(sess, slug, company, sector, etag):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    h = {"If-None-Match": etag} if etag else {}
    async with sess.get(url, headers=h) as r:
        if r.status != 200:
            return r.status, [], r.headers.get("ETag")
        d = await r.json(content_type=None)
        return 200, [
            Posting("greenhouse", str(j.get("id")), company, sector,
                    j.get("title", ""), (j.get("location") or {}).get("name", ""),
                    j.get("absolute_url", ""),
                    # first_published is the true post date; updated_at moves on
                    # any edit and will lie about freshness.
                    _ts(j.get("first_published")) or _ts(j.get("updated_at")))
            for j in d.get("jobs", [])
        ], r.headers.get("ETag")


async def fetch_lever(sess, slug, company, sector, etag):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    h = {"If-None-Match": etag} if etag else {}
    async with sess.get(url, headers=h) as r:
        if r.status != 200:
            return r.status, [], r.headers.get("ETag")
        out = []
        for j in await r.json(content_type=None):
            loc = (j.get("categories") or {}).get("location") or ""
            country = j.get("country") or ""
            if country and country.lower() not in loc.lower():
                loc = f"{loc}, {country}".strip(", ")
            out.append(Posting("lever", str(j.get("id")), company, sector,
                               j.get("text", ""), loc, j.get("hostedUrl", ""),
                               _ts(j.get("createdAt"))))
        return 200, out, r.headers.get("ETag")


async def fetch_ashby(sess, slug, company, sector, etag):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    h = {"If-None-Match": etag} if etag else {}
    async with sess.get(url, headers=h) as r:
        if r.status != 200:
            return r.status, [], r.headers.get("ETag")
        d = await r.json(content_type=None)
        # employmentType is unreliable — Perplexity tags its internships "FullTime".
        return 200, [
            Posting("ashby", str(j.get("id")), company, sector, j.get("title", ""),
                    j.get("location", "") or "",
                    j.get("jobUrl") or j.get("applyUrl") or "",
                    _ts(j.get("publishedAt")))
            for j in d.get("jobs", []) if j.get("isListed", True)
        ], r.headers.get("ETag")


async def fetch_workday(sess, slug, company, sector, etag):
    """slug = 'tenant/wdN/SitePath'  (host <tenant>.<wdN>.myworkdayjobs.com)
          or 'wdN/tenant/SitePath@site' (host <wdN>.myworkdaysite.com)

    Workday serves career sites from two different host shapes. The second
    form ("myworkdaysite.com") is common for mid-size firms and is what makes
    tenants like IMEG reachable — guessing the myworkdayjobs.com form for them
    just returns 422.
    """
    parts = slug.split("/")
    if len(parts) != 3:
        return 400, [], None
    if slug.endswith("@site"):
        wd, tenant, site = parts[0], parts[1], parts[2][:-5]
        base = f"https://{wd}.myworkdaysite.com/recruiting/{tenant}/{site}"
        api = f"https://{wd}.myworkdaysite.com/wday/cxs/{tenant}/{site}/jobs"
    else:
        tenant, wd, site = parts
        base = f"https://{tenant}.{wd}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out, offset, total = [], 0, None
    # No searchText: an "intern" query hides new-grad/EIT/"Engineer I"
    # titles, so pull everything. The classifier filters client-side.
    while offset < 1000:
        body = {"appliedFacets": {}, "limit": 20, "offset": offset,
                "searchText": ""}
        async with sess.post(api, json=body,
                             headers={"Accept": "application/json"}) as r:
            if r.status != 200:
                return (200, out, None) if out else (r.status, [], None)
            d = await r.json(content_type=None)
        posts = d.get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath", "")
            pub, approx, unb = _workday_posted(j.get("postedOn"))
            out.append(Posting(
                "workday", f"{tenant}:{path}", company, sector,
                j.get("title", ""), j.get("locationsText", "") or "",
                f"{base}/{site}{path}", pub, approx, unb))
        # `total` is only populated on the first page; later pages report 0,
        # so capture it once and page against that.
        if total is None:
            total = d.get("total") or 0
        offset += 20
        if len(posts) < 20 or (total and offset >= total):
            break
    return 200, out, None


ICIMS_JOB_URL_RE = re.compile(
    r"<loc>(https://[^<]+?/jobs/(\d+)/([^/]+)/job[^<]*)</loc>", re.I)


async def fetch_icims(sess, slug, company, sector, etag):
    """slug = an iCIMS careers host, e.g. 'careers-kimley-horn.icims.com',
    or a company careers origin that proxies one.

    iCIMS renders everything client-side, so there is no listings API and no
    JSON-LD to scrape. What IS public is the sitemap: it enumerates every job
    URL, and each URL carries the requisition id and a slugified title. That
    is enough for the title/level/field prefilter; the bot, when a user asks
    about a role, pulls its description from the job page's iframe view.

    Locations are not in the sitemap, so postings come back with an empty
    location, the same as Parsons' blank-location Workday postings.
    """
    host = slug.strip().rstrip("/")
    host = re.sub(r"^https?://", "", host)

    # Some employers front iCIMS with their own careers site that exposes a
    # real JSON API (Rivian). Prefer it — it carries locations and dates.
    try:
        async with sess.get(f"https://{host}/api/jobs?page=1&limit=100",
                            headers={"Accept": "application/json"}) as r:
            if r.status == 200 and "json" in r.headers.get("content-type", ""):
                d = await r.json(content_type=None)
                rows = d.get("jobs") or []
                if rows:
                    out = []
                    for page in range(1, 11):
                        if page > 1:
                            async with sess.get(
                                    f"https://{host}/api/jobs?page={page}&limit=100",
                                    headers={"Accept": "application/json"}) as r2:
                                if r2.status != 200:
                                    break
                                rows = (await r2.json(content_type=None)).get("jobs") or []
                        if not rows:
                            break
                        for row in rows:
                            j = row.get("data", row)
                            out.append(Posting(
                                "icims",
                                f"https://{host}|{j.get('req_id') or j.get('slug')}",
                                company, sector, j.get("title", ""),
                                (j.get("location_name") or j.get("full_location")
                                 or ""), j.get("apply_url") or "",
                                _ts(j.get("posted_date")) or _ts(j.get("create_date"))))
                        if len(rows) < 100:
                            break
                    return 200, out, None
    except Exception:
        pass

    async with sess.get(f"https://{host}/sitemap.xml") as r:
        if r.status != 200:
            return r.status, [], None
        sm = await r.text()
    out, seen = [], set()
    for url, jid, title_slug in ICIMS_JOB_URL_RE.findall(sm):
        if jid in seen:
            continue
        seen.add(jid)
        title = re.sub(r"[-_]+", " ", title_slug).strip().title()
        out.append(Posting("icims", f"{host}|{jid}", company, sector,
                           title, "", url, None))
    return 200, out, None


async def fetch_eightfold(sess, slug, company, sector, etag):
    """slug = 'subdomain|domain', e.g. 'arcadis|arcadis.com'.

    Eightfold's public career sites call /api/pcsx/search — plain GET, no auth.
    Locations arrive as a list; postedTs is epoch milliseconds.
    """
    try:
        sub, domain = slug.split("|")
    except ValueError:
        return 400, [], None
    base = f"https://{sub}.eightfold.ai/api/pcsx/search"
    # The API caps each response at 10 regardless of `num`, so a big board is
    # 100+ requests. Fetch page 1 to learn the total, then pull the rest
    # concurrently — sequentially this took ~90s and stalled the sweep.
    PAGE, MAX_JOBS, CONC = 10, 1200, 8

    async def page(start):
        url = f"{base}?domain={domain}&query=&location=&start={start}&num={PAGE}"
        try:
            async with sess.get(url,
                                headers={"Accept": "application/json"}) as r:
                if r.status != 200:
                    return None
                d = await r.json(content_type=None)
        except Exception:
            return None
        return (d.get("data") or {})

    first = await page(0)
    if first is None:
        return 502, [], None
    total = min(first.get("count") or 0, MAX_JOBS)
    batches = list(range(PAGE, total, PAGE))
    results = [first]
    sem = asyncio.Semaphore(CONC)

    async def guarded(s):
        async with sem:
            return await page(s)

    if batches:
        results += [d for d in await asyncio.gather(
            *(guarded(s) for s in batches)) if d]

    out = []
    for data in results:
        for j in data.get("positions") or []:
            locs = j.get("locations") or j.get("standardizedLocations") or []
            loc = "; ".join(str(x) for x in locs if x)[:120]
            pid = j.get("id") or j.get("displayJobId")
            out.append(Posting(
                "eightfold", f"{sub}|{domain}|{pid}", company, sector,
                j.get("name", ""), loc,
                j.get("positionUrl") or
                f"https://{sub}.eightfold.ai/careers/job/{pid}",
                _ts(j.get("postedTs") or j.get("creationTs"))))
    return 200, out, None


async def fetch_taleo(sess, slug, company, sector, etag):
    """slug = 'tenant|portalId', e.g. 'hdr|101430233'.

    Taleo's career sections expose a JSON search under
    /careersection/rest/jobboard/searchjobs. The payload shape is fussy and
    varies by tenant, so a 500 here simply yields no postings rather than
    failing the sweep.
    """
    try:
        tenant, portal = slug.split("|")
    except ValueError:
        return 400, [], None
    url = (f"https://{tenant}.taleo.net/careersection/rest/jobboard/searchjobs"
           f"?lang=en&portal={portal}")
    # Taleo 500s unless the payload matches what its own UI sends, filter
    # arrays included — captured from a real career-section session.
    await sess.get(f"https://{tenant}.taleo.net/careersection/ex/jobsearch.ftl"
                   f"?lang=en&portal={portal}")     # sets the session cookie
    out = []
    for page in range(1, 21):
        body = {"multilineEnabled": False,
                "sortingSelection": {"sortBySelectionParam": "1",
                                     "ascendingSortingOrder": "false"},
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": "",
                                         "CATEGORY": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": [
                    {"id": "LOCATION", "selectedValues": []},
                    {"id": "JOB_FIELD", "selectedValues": []},
                    {"id": "JOB_SCHEDULE", "selectedValues": []},
                    {"id": "POSTING_DATE", "selectedValues": []}]},
                "advancedSearchFiltersSelectionParam": {
                    "searchFilterSelections": [
                        {"id": "LOCATION", "selectedValues": []},
                        {"id": "JOB_FIELD", "selectedValues": []},
                        {"id": "EMPLOYEE_STATUS", "selectedValues": []},
                        {"id": "JOB_SCHEDULE", "selectedValues": []}]},
                "pageNo": page}
        # Taleo rejects the call without these — its UI always sends them.
        async with sess.post(url, json=body, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "tz": "GMT-08:00", "tzname": "America/Los_Angeles",
                "Referer": (f"https://{tenant}.taleo.net/careersection/ex/"
                            f"jobsearch.ftl?lang=en&portal={portal}")}) as r:
            if r.status != 200:
                return (200, out, None) if out else (r.status, [], None)
            d = await r.json(content_type=None)
        reqs = d.get("requisitionList") or []
        if not reqs:
            break
        for j in reqs:
            # column = [title, '["United States-Arizona-Phoenix"]', posted]
            cols = j.get("column") or []
            title = cols[0] if cols else (j.get("title") or "")
            loc = ""
            if len(cols) > 1 and cols[1]:
                try:
                    places = json.loads(cols[1])
                    # "United States-California-Irvine" -> "Irvine, California"
                    loc = "; ".join(
                        ", ".join(reversed(str(x).split("-")[1:])) or str(x)
                        for x in places)[:120]
                except Exception:
                    loc = str(cols[1])[:120]
            jid = j.get("jobId") or j.get("contestNo") or title
            out.append(Posting(
                "taleo", f"{tenant}|{portal}|{jid}", company, sector,
                str(title), str(loc),
                f"https://{tenant}.taleo.net/careersection/ex/jobdetail.ftl"
                f"?job={jid}", None))
        if len(reqs) < 25:
            break
    return 200, out, None


ADAPTERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "ashby": fetch_ashby, "workday": fetch_workday,
            "icims": fetch_icims, "eightfold": fetch_eightfold,
            "taleo": fetch_taleo}

# --------------------------------------------------------------------------
# Classifier — pure function
# --------------------------------------------------------------------------

INTERN_RE = re.compile(r"\b(intern|interns|internship|interning|co-?op)\b", re.I)
EXCLUDE_RE = re.compile(
    r"\binternal\b|\binternational\b|\binternist\b|"
    r"\bintern(ship)?\s+(manager|coordinator|recruiter|program manager)\b|"
    r"\b(medical|nursing|clinical|pharmacy|physician|resident)\s+intern", re.I)
TERM_RE = re.compile(r"\b(summer|fall|autumn|winter|spring)\s*'?\s*(20\d{2})\b", re.I)
YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Only surface tech-flavoured roles — matters because the registry includes
# CVS, Target and Caterpillar, which post hundreds of non-tech interns.
TECHY_RE = re.compile(
    r"\b(software|engineer|engineering|developer|\bswe\b|backend|back-end|frontend|"
    r"front-end|full.?stack|infra|infrastructure|platform|security|cyber|systems|"
    r"compiler|distributed|\bapi\b|mobile|ios|android|\bweb\b|cloud|devops|\bsre\b|"
    r"data|analytics|machine learning|\bml\b|\bai\b|\bnlp\b|computer vision|"
    r"quant|quantitative|research|technology|technical|\bit\b|information systems|"
    r"hardware|asic|fpga|rtl|silicon|embedded|electrical|mechanical|robotics|"
    r"avionics|propulsion|aerospace|product manage|program manage)\b", re.I)

US_HINT = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|"
    r"WV|WI|WY|DC)\b|\b(united states|\bUSA\b|u\.s\.|remote|san francisco|new york|"
    r"seattle|austin|boston|chicago|los angeles|palo alto|mountain view|"
    r"sunnyvale|bellevue|denver|atlanta|washington|minneapolis|peoria)\b", re.I)
NON_US = re.compile(
    r"\b(london|dublin|berlin|paris|amsterdam|zurich|geneva|bangalore|bengaluru|"
    r"hyderabad|pune|chennai|gurgaon|noida|tokyo|osaka|singapore|sydney|melbourne|"
    r"tel aviv|haifa|toronto|vancouver|montreal|ottawa|munich|hamburg|stockholm|"
    r"oslo|copenhagen|helsinki|warsaw|krakow|gdansk|prague|lisbon|porto|madrid|"
    r"barcelona|milan|rome|belgrade|bucharest|sofia|budapest|vienna|brussels|"
    r"bristol|manchester|edinburgh|s[a\u00e3]o paulo|mexico city|bogot[a\u00e1]|"
    r"buenos aires|santiago|lagos|nairobi|cairo|dubai|abu dhabi|riyadh|seoul|"
    r"taipei|hong kong|shanghai|beijing|shenzhen|kuala lumpur|jakarta|manila|"
    r"bangkok|ho chi minh|hanoi|auckland|wellington|united kingdom|england|"
    r"scotland|ireland|india|germany|france|netherlands|switzerland|japan|"
    r"australia|israel|canada|china|poland|spain|italy|sweden|norway|denmark|"
    r"serbia|romania|brazil|mexico|korea|taiwan)\b|\b(POL|DEU|GBR|IND|CAN|AUS)\s*-",
    re.I)

CATEGORIES = [(name, re.compile(pat, re.I)) for name, pat in [
    ("quant",    r"\b(quant|quantitative|trading|trader|systematic|market mak)"),
    ("hardware", r"\b(hardware|asic|fpga|rtl|silicon|analog|rf\b|antenna|avionics|"
                 r"electrical|mechanical|embedded|thermal|propulsion|structures|"
                 r"manufactur|integration and test|dsp|robotic)"),
    ("data-ml",  r"\b(machine learning|\bml\b|deep learning|data scien|data engineer|"
                 r"analytics|research scien|research engineer|\bai\b|\bnlp\b|"
                 r"computer vision|perception)"),
    ("pm",       r"\b(product manage|product management|\bpm\b|program manage|"
                 r"business analyst|strategy)"),
    ("swe",      r"\b(software|engineer|developer|\bswe\b|backend|back-end|frontend|"
                 r"front-end|full.?stack|infra|platform|security|cyber|systems|"
                 r"compiler|distributed|api|mobile|ios|android|web|cloud|devops)"),
]]


def classify(p: "Posting") -> dict:
    t = p.title
    is_intern = bool(INTERN_RE.search(t)) and not EXCLUDE_RE.search(t)
    is_tech = bool(TECHY_RE.search(t))

    term = None
    m = TERM_RE.search(t)
    if m:
        term = f"{m.group(1).title()} {m.group(2)}"
    else:
        y = YEAR_RE.search(t)
        if y:
            term = y.group(1)

    category = "other"
    for name, rx in CATEGORIES:
        if rx.search(t):
            category = name
            break

    loc = p.location or ""
    if NON_US.search(loc):
        region = "non-us"
    elif US_HINT.search(loc) or not loc:
        region = "us"
    else:
        region = "unknown"

    return {"is_intern": is_intern, "is_tech": is_tech, "term": term,
            "category": category, "region": region}


# --------------------------------------------------------------------------
# LLM classifier. Replaces the regex heuristics where it can, falls back to
# them where it can't. Three rules make this safe on a rate-limited free tier:
#
#   1. Fail open. Any error, timeout, missing key, or exhausted quota falls
#      back to the regex classifier. The digest ships regardless.
#   2. Cache by content hash. Same title+location is never classified twice,
#      so re-runs and crashes cost nothing and results are deterministic.
#   3. Batch + budget. 25 postings per call, with a token-bucket RPM limiter
#      and a persisted daily counter. A busy day is ~20 calls, not 500.
#
# Model ids (verified 2026-07-31): gemini-3.5-flash-lite is the cheap
# high-throughput tier and the right default for classification;
# gemini-3.6-flash is the stronger workhorse. Note the mixed versioning —
# Flash-Lite stayed on the 3.5 line in the same release. Free-tier RPM/RPD
# vary by project; check your own AI Studio rate-limit view and set GEMINI_RPM,
# GEMINI_RPD and GEMINI_TPM to match, since the published tables go stale. The
# key, the model and every limit are in Settings, at the top of this file.
#
# The request itself — the endpoint, JSON output, retries and what is and is
# not retried, the token counts — is llm.py's, shared with the bot's fit
# check. What stays here is what only --llm needs: its prompt and schema, its
# budget and its cache.
# --------------------------------------------------------------------------

LLM_PROMPT = """You classify job postings for a tech-internship alert bot.

For each numbered posting, return one object with these fields:
  i          the posting number
  is_intern  true only for internships/co-ops for current students. False for
             full-time, new-grad, contractor, or fellowship roles.
  is_tech    true if the work is software, data/ML, hardware, IT, quant,
             security, robotics, or technical product/program management.
             False for retail, nursing, pharmacy, marketing, finance-ops,
             clinical, or administrative roles.
  category   one of: swe, data-ml, hardware, quant, pm, other
  term       the season and year if stated, e.g. "Summer 2027", "Fall 2026".
             Use null if the posting does not say.
  region     "us" if the location is in the United States or fully remote-US,
             "non-us" for anywhere else, "unknown" if you cannot tell.

Judge from the title and location only. Be inclusive on is_intern when a
posting is plausibly a student internship under an unusual title (e.g.
"Campus Engineer", "Technology Analyst Program", "Founding Engineer, Student").

Return ONLY a JSON array. No markdown fences, no commentary.

Postings:
"""

LLM_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "is_intern": {"type": "BOOLEAN"},
            "is_tech": {"type": "BOOLEAN"},
            "category": {"type": "STRING",
                         "enum": ["swe", "data-ml", "hardware", "quant", "pm", "other"]},
            "term": {"type": "STRING", "nullable": True},
            "region": {"type": "STRING", "enum": ["us", "non-us", "unknown"]},
        },
        "required": ["i", "is_intern", "is_tech", "category", "region"],
    },
}


# High-recall pre-filter. The LLM is a PRECISION layer, not a replacement for
# the whole pass — sending every posting on every board would be ~16k rows per
# sweep. This pattern is deliberately over-inclusive: it must catch the odd
# titles the strict regex misses ("Campus Engineer", "Technology Analyst
# Program", "Founding Engineer, Student") while discarding the obvious
# full-time roles. False positives here are cheap; false negatives are not.
LLM_CANDIDATE_RE = re.compile(
    r"\b(intern|interns|internship|interning|co-?op|campus|student|students|"
    r"university|undergrad|undergraduate|graduate|new\s?grad|early\s?career|"
    r"apprentice|apprenticeship|trainee|analyst\s+program|rotational|"
    r"summer|fall|spring|winter|20\d{2})\b", re.I)


def llm_candidates(posts):
    return [p for p in posts if LLM_CANDIDATE_RE.search(p.title or "")]


def posting_hash(p):
    return hashlib.sha256(
        f"{p.title}|{p.location}".encode("utf-8")).hexdigest()[:32]


def quota_day(at=None) -> str:
    """The quota day, YYYY-MM-DD in SETTINGS.llm_day_tz: now, or at epoch `at`.

    The key of llm_usage (CONTRACT.md, P7). The free tier's daily cap resets
    at midnight Pacific, and the bot's quota panel reads today's row by the
    date in that zone; counting by the box's own zone would start a new row
    in the afternoon, Pacific time, on a UTC server, hours before Gemini
    resets anything.
    """
    zone = ZoneInfo(SETTINGS.llm_day_tz)
    moment = datetime.now(zone) if at is None else datetime.fromtimestamp(at, zone)
    return moment.strftime("%Y-%m-%d")


class LlmBudget:
    """Rate limiter for the free AI Studio tier: requests/min, tokens/min, and
    a requests/day counter persisted in sqlite.

    TPM matters once we send descriptions rather than titles — a description
    batch is thousands of tokens, so RPM alone would blow the 250k/min cap.
    Token counts are estimated at ~4 chars/token (Gemini's rule of thumb) with
    headroom; being approximate is fine because we only need to stay under a
    ceiling, not bill against it.
    """

    def __init__(self, conn, rpm=None, rpd=None, tpm=None):
        # Unset limits come from SETTINGS when the budget is made, not when
        # this class was defined, which was before main() had read them.
        self.conn = conn
        self.rpm = SETTINGS.llm_rpm if rpm is None else rpm
        self.tpm = SETTINGS.llm_tpm if tpm is None else tpm
        # GEMINI_RPD is the daily cap of the one model we call, and llm_usage
        # counts calls to that same model, so the running total compares
        # against this ceiling directly.
        self.rpd = SETTINGS.llm_rpd if rpd is None else rpd
        self.calls = []          # timestamps of recent requests
        self.tokens = []         # (timestamp, est_tokens) of recent requests
        self.day = quota_day()
        row = conn.execute("SELECT n FROM llm_usage WHERE day=?",
                           (self.day,)).fetchone()
        self.used = row[0] if row else 0

    def remaining(self):
        return max(0, self.rpd - self.used)

    def record_usage(self, meta):
        """Persist Gemini's own token counts from a response's usageMetadata.

        estimate_tokens() is a 4-chars/token guess used to stay under the TPM
        ceiling; these are the real numbers, kept for reporting. Best-effort —
        a missing or malformed usageMetadata must never fail a classification
        that already succeeded.
        """
        pt, ot = llm.usage_tokens(meta)
        if not (pt or ot):
            return
        try:
            self.conn.execute(
                "UPDATE llm_usage SET prompt_tokens=COALESCE(prompt_tokens,0)+?,"
                " output_tokens=COALESCE(output_tokens,0)+? WHERE day=?",
                (pt, ot, self.day))
            self.conn.commit()
        except sqlite3.Error:
            pass

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return llm.estimate_tokens(text)

    def _prune(self, now):
        self.calls = [t for t in self.calls if now - t < 60]
        self.tokens = [(t, n) for t, n in self.tokens if now - t < 60]

    async def acquire(self, est_tokens=0):
        """Wait until this request fits inside RPM and TPM. False if the daily
        request budget is exhausted."""
        if self.used >= self.rpd:
            return False
        for _ in range(12):      # bounded: never wait more than ~12 windows
            now = time.time()
            self._prune(now)
            over_rpm = len(self.calls) >= self.rpm
            over_tpm = (sum(n for _, n in self.tokens) + est_tokens) > self.tpm
            if not over_rpm and not over_tpm:
                break
            # Sleep until the oldest relevant entry ages out of the window.
            oldest = min([t for t in self.calls] +
                         [t for t, _ in self.tokens] or [now])
            await asyncio.sleep(max(0.5, 60 - (now - oldest) + 0.5))
        else:
            return False
        now = time.time()
        self.calls.append(now)
        self.tokens.append((now, est_tokens))
        self.used += 1
        # Name the columns: llm_usage gained token counters, so positional
        # VALUES(?,?) no longer matches the table width.
        self.conn.execute(
            "INSERT INTO llm_usage(day, n) VALUES(?,?) ON CONFLICT(day) "
            "DO UPDATE SET n=excluded.n", (self.day, self.used))
        self.conn.commit()
        return True


async def _llm_call(sess, budget, batch):
    """One batched request. Returns {index: fields} or {} on any failure.

    llm.generate_json sends it, asking the budget before every attempt, and
    says on stderr why a failed one falls back to the regular expressions.
    """
    lines = "\n".join(f'{i}. {p.title} — {p.location or "no location given"}'
                       for i, p in batch)
    prompt = LLM_PROMPT + lines
    est_tokens = budget.estimate_tokens(prompt)
    try:
        rows = await llm.generate_json(
            sess, key=SETTINGS.gemini_key, model=SETTINGS.gemini_model, prompt=prompt,
            schema=LLM_SCHEMA, max_attempts=SETTINGS.llm_max_attempts,
            acquire=lambda: budget.acquire(est_tokens), on_usage=budget.record_usage)
    except llm.LlmError:
        return {}
    try:
        return {r["i"]: r for r in rows if isinstance(r, dict) and "i" in r}
    except Exception:
        print("  llm: unparseable response — falling back to regex",
              file=sys.stderr)
        return {}


async def llm_classify(conn, postings, verbose=True):
    """Return {posting_hash: classification}. Cached rows never re-requested."""
    out, todo, seen = {}, [], set()
    for p in postings:
        h = posting_hash(p)
        if h in out or h in seen:
            continue
        row = conn.execute("SELECT payload FROM llm_cache WHERE hash=?",
                           (h,)).fetchone()
        if row:
            out[h] = json.loads(row[0])
        else:
            seen.add(h)
            todo.append(p)

    if not todo:
        return out
    if not SETTINGS.gemini_key:
        if verbose:
            print("  llm: GEMINI_API_KEY not set — using regex classifier",
                  file=sys.stderr)
        return out

    budget = LlmBudget(conn)
    size = SETTINGS.llm_batch
    batches = [todo[i:i + size] for i in range(0, len(todo), size)]
    need = len(batches)
    if need > budget.remaining():
        if verbose:
            print(f"  llm: {need} calls needed, {budget.remaining()} left in "
                  f"today's budget — classifying what fits, regex for the rest",
                  file=sys.stderr)
        batches = batches[:budget.remaining()]
    if verbose and batches:
        print(f"  llm: {len(todo)} uncached postings -> {len(batches)} calls "
              f"({SETTINGS.gemini_model})")

    fails = 0
    async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=SETTINGS.llm_http_timeout)) as sess:
        for batch in batches:
            # Circuit breaker: a bad key or a dead endpoint would otherwise
            # burn one daily-quota slot per batch before giving up. Each
            # "failure" is now an exhausted retry chain, not a single blip.
            if fails >= SETTINGS.llm_max_batch_failures:
                if verbose:
                    print(f"  llm: {SETTINGS.llm_max_batch_failures} consecutive failed "
                          "batches — aborting, regex for the rest",
                          file=sys.stderr)
                break
            indexed = list(enumerate(batch))
            res = await _llm_call(sess, budget, indexed)
            fails = 0 if res else fails + 1
            for i, p in indexed:
                if i not in res:
                    continue
                f = res[i]
                rec = {"is_intern": bool(f.get("is_intern")),
                       "is_tech": bool(f.get("is_tech")),
                       "category": f.get("category") or "other",
                       "term": f.get("term") or None,
                       "region": f.get("region") or "unknown"}
                h = posting_hash(p)
                out[h] = rec
                # An upsert, because another run may have cached the same
                # posting since the lookup above.
                conn.execute(
                    "INSERT INTO llm_cache(hash, payload, created) VALUES(?,?,?) "
                    "ON CONFLICT(hash) DO UPDATE SET payload=excluded.payload, "
                    "created=excluded.created", (h, json.dumps(rec), time.time()))
            # Committed batch by batch: LlmBudget can sleep up to a minute
            # before the next request, and rows left uncommitted across that
            # sleep hold the write lock, so every other writer would wait.
            conn.commit()
    return out


def dedup_key(p: "Posting") -> str:
    t = re.sub(r"\s*[-\u2013\u2014]\s*(us|usa|uk|emea|apac|commercial|defense tech|"
               r"us government|uk government|aus government|intel|infrastructure|"
               r"production infrastructure|france|poland).*$", "", p.title, flags=re.I)
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"[^a-z0-9]+", "", t.lower())
    return f"{p.company}|{t}"


def group_roles(items, ts=None):
    """Collapse posting tuples into distinct roles, newest first.

    `items` are tuples whose first element is a Posting (extra elements ride
    along untouched). Groups by dedup_key; each returned group is sorted
    newest-first, as is the group list itself. `ts` maps an item to its sort
    timestamp (default: published, undated last). Shared by cmd_list and the
    Discord bot so the two frontends can't drift on what a "distinct role" is.
    """
    ts = ts or (lambda item: item[0].published or 0)
    groups = defaultdict(list)
    for item in items:
        groups[dedup_key(item[0])].append(item)
    out = list(groups.values())
    for g in out:
        g.sort(key=ts, reverse=True)
    out.sort(key=lambda g: ts(g[0]), reverse=True)
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


async def fetch_all(etags=None, on_status=None, sector=None):
    etags = etags or {}
    boards = [b for b in BOARDS if not sector or b[3] == sector]
    sem = asyncio.Semaphore(CONCURRENCY)
    out, stats = [], {"ok": 0, "not_modified": 0, "error": 0, "new_etags": {}}

    async def one(sess, plat, slug, company, sect):
        async with sem:
            try:
                st, posts, et = await ADAPTERS[plat](
                    sess, slug, company, sect, etags.get((plat, slug)))
            except Exception as e:
                stats["error"] += 1
                if on_status:
                    on_status(plat, slug, company, f"ERR {type(e).__name__}", 0)
                return
        if st == 304:
            stats["not_modified"] += 1
            if on_status:
                on_status(plat, slug, company, "304", 0)
            return
        if st != 200:
            stats["error"] += 1
            if on_status:
                on_status(plat, slug, company, f"HTTP {st}", 0)
            return
        stats["ok"] += 1
        if et:
            stats["new_etags"][(plat, slug)] = et
        out.extend(posts)
        if on_status:
            on_status(plat, slug, company, "ok", len(posts))

    async with polite_session(timeout=TIMEOUT,
                              headers={"User-Agent": user_agent(SETTINGS.contact)}) as s:
        await asyncio.gather(*(one(s, *b) for b in boards))
    return out, stats


def age_str(p):
    if not p.published:
        return "unknown"
    d = (time.time() - p.published) / 86400
    s = f"{d*24:.0f}h" if d < 1 else f"{d:.0f}d"
    if p.unbounded:
        return f"{s}+"
    return f"~{s}" if p.approx_date else s


def age_days(p) -> Optional[float]:
    return None if not p.published else (time.time() - p.published) / 86400


def select(posts, us_only, category, tech_only=True,
           max_age=MAX_AGE_DAYS, keep_undated=True, llm=None):
    """Filter to the roles worth showing.

    Age rule: drop a posting only if we KNOW it is older than max_age. Undated
    postings, and Workday's unbounded "30d+" bucket, are kept by default —
    dropping a real opening is worse than showing a stale one. keep_undated=False
    (--strict) drops them instead.
    """
    out, dropped_stale, dropped_undated = [], 0, 0
    for p in posts:
        # LLM result when we have one, regex otherwise. Per-posting, so a
        # partial batch or exhausted quota degrades gracefully instead of
        # taking the whole run down.
        c = (llm or {}).get(posting_hash(p)) or classify(p)
        if not c["is_intern"]:
            continue
        if tech_only and not c["is_tech"]:
            continue
        if us_only and c["region"] not in ("us", "unknown"):
            continue
        if category and c["category"] != category:
            continue
        if max_age:
            age = age_days(p)
            if age is None or p.unbounded:
                if not keep_undated:
                    dropped_undated += 1
                    continue
            elif age > max_age:
                dropped_stale += 1
                continue
        out.append((p, c))
    return out, {"stale": dropped_stale, "undated": dropped_undated}


# --------------------------------------------------------------------------
# YC discovery — the repos only surface companies a human already found, so
# startups posting their first internship are invisible. YC publishes an open
# dataset of every funded company; we derive slug candidates from it and probe
# directly. Measured hit rate ~13%, i.e. ~800 boards across the full 6k list.
#
# Two things this must get right:
#   1. Verification. Name-derived slugs collide with unrelated boards —
#      "agency", "juno" and "prosper" all resolve to some other company. Every
#      hit is checked against the YC record before being accepted.
#   2. Politeness. ~24k requests across the full dataset. This runs at low
#      concurrency with a permanent cache so it is a one-time overnight cost.
# --------------------------------------------------------------------------

YC_DATASET = "https://yc-oss.github.io/api/companies/all.json"

# Slugs that are real boards but belong to somebody else. Anything short or
# dictionary-ish collides; require positive proof for these rather than
# trusting the name match.
AMBIGUOUS_SLUGS = {
    "agency", "juno", "prosper", "apex", "atlas", "orbit", "nova", "vertex",
    "summit", "pilot", "scout", "beacon", "anchor", "bridge", "compass",
    "spark", "pulse", "flow", "wave", "shift", "lattice", "prism", "cobalt",
    "onyx", "slate", "north", "found", "level", "range", "arc", "mach", "unit",
    "column", "vantage", "signal", "sonar", "radar", "helix", "quanta", "kite",
}


def yc_variants(c):
    out = set()
    for field in (c.get("slug") or "", c.get("name") or ""):
        b = re.sub(r"[^a-z0-9 -]", "", field.lower()).strip()
        if not b:
            continue
        out.add(b.replace(" ", ""))
        out.add(b.replace(" ", "-"))
    return {v for v in out if 2 < len(v) < 40}


YC_ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
}


def yc_verify(company, plat, slug, jobs):
    """Return the evidence type if this board really belongs to `company`."""
    yc_name = _norm(company.get("name"))
    site = (company.get("website") or "").lower()
    site = re.sub(r"^https?://(www\.)?", "", site).split("/")[0]

    # Strongest: Greenhouse echoes the employer's own company_name per job.
    for j in jobs[:8]:
        cn = j.get("company_name") if isinstance(j, dict) else None
        if cn and _norm(cn) == yc_name:
            return "name"

    # Next: the apply/hosted URL points at the company's own domain.
    if site and "." in site:
        for j in jobs[:25]:
            u = (j.get("absolute_url") or j.get("hostedUrl")
                 or j.get("jobUrl") or j.get("applyUrl") or "")
            if site in u.lower():
                return "domain"

    # Lever and Ashby expose nothing identifying beyond the slug, but both
    # return description text, which almost always names the company.
    if len(yc_name) >= 4:
        for j in jobs[:4]:
            body = ((j.get("descriptionPlain") or "") + " " +
                    (j.get("description") or ""))[:6000]
            if body and yc_name in _norm(body):
                return "text"
            if site and "." in site and site in body.lower():
                return "text"

    # Weakest: exact match to the YC slug, and not a word that collides.
    if _norm(slug) == _norm(company.get("slug")) and len(slug) >= 6 \
            and slug.lower() not in AMBIGUOUS_SLUGS:
        return "slug"
    return None


async def mine_yc(sess, limit=None, concurrency=6, recheck=False):
    try:
        async with sess.get(YC_DATASET) as r:
            companies = await r.json(content_type=None)
    except Exception as e:
        print(f"  -- yc dataset               {type(e).__name__} (skipping)")
        return set()

    try:
        cache = {} if recheck else json.load(open(SETTINGS.yc_cache))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    todo = [c for c in companies if str(c.get("id")) not in cache]
    if limit:
        todo = todo[:limit]
    print(f"  {len(companies)} YC companies · {len(cache)} cached · "
          f"{len(todo)} to probe")
    if not todo:
        print("  (all cached — pass --yc-recheck to re-probe)")

    sem = asyncio.Semaphore(concurrency)
    done, rejected = [0], [0]

    async def probe(company):
        cid = str(company.get("id"))
        result = None
        for slug in sorted(yc_variants(company)):
            for plat, tmpl in YC_ENDPOINTS.items():
                async with sem:
                    try:
                        async with sess.get(tmpl.format(slug)) as r:
                            if r.status != 200:
                                continue
                            d = await r.json(content_type=None)
                    except Exception:
                        continue
                jobs = d if isinstance(d, list) else d.get("jobs", [])
                if not jobs:
                    continue
                ev = yc_verify(company, plat, slug, jobs)
                if ev:
                    result = [plat, slug, company.get("name") or slug, ev]
                    break
                rejected[0] += 1
            if result:
                break
        cache[cid] = result
        done[0] += 1
        if done[0] % 250 == 0:
            found = sum(1 for v in cache.values() if v)
            print(f"    {done[0]}/{len(todo)} probed · {found} verified boards · "
                  f"{rejected[0]} rejected by verification", flush=True)

    try:
        await asyncio.gather(*(probe(c) for c in todo))
    finally:
        with open(_parent_made(SETTINGS.yc_cache), "w") as f:
            json.dump(cache, f)

    hits = [v for v in cache.values() if v]
    by_ev = defaultdict(int)
    for h in hits:
        by_ev[h[3]] += 1
    print(f"  {len(hits)} verified boards from YC "
          f"({', '.join(f'{k}={v}' for k, v in sorted(by_ev.items()))}) · "
          f"{rejected[0]} candidates rejected as collisions")
    return {(h[0], h[1]) for h in hits}


async def cmd_discover(min_interns, include_workday, use_cc, use_yc,
                       yc_limit=None, yc_recheck=False):
    async with polite_session(
            timeout=aiohttp.ClientTimeout(total=90),
            connector=aiohttp.TCPConnector(limit=25),
            headers={"User-Agent": user_agent(SETTINGS.contact)}) as sess:
        print("mining slugs from repo listings...")
        cands, nbytes = await mine_repos(sess)
        if use_cc:
            print("\nquerying common crawl...")
            cands |= await mine_commoncrawl(sess)
        if use_yc:
            print("\nprobing YC companies (slow and polite; results are cached)...")
            cands |= await mine_yc(sess, yc_limit, recheck=yc_recheck)

        if not include_workday:
            cands = {c for c in cands if c[0] != "workday"}
        by_plat = defaultdict(int)
        for plat, _ in cands:
            by_plat[plat] += 1
        print(f"\n{len(cands)} unique slugs from {nbytes//1024//1024}MB: " +
              ", ".join(f"{k}={v}" for k, v in sorted(by_plat.items())))

        print("\nvalidating against live APIs (several minutes)...")
        sem = asyncio.Semaphore(25)
        keep, live, checked = [], 0, [0]

        async def check(plat, slug):
            nonlocal live
            async with sem:
                try:
                    st, posts, _ = await ADAPTERS[plat](sess, slug, slug, "unknown", None)
                except Exception:
                    return
                finally:
                    checked[0] += 1
                    if checked[0] % 500 == 0:
                        print(f"    {checked[0]}/{len(cands)} checked, "
                              f"{len(keep)} kept", flush=True)
            if st != 200 or not posts:
                return
            live += 1
            fresh = len(select(posts, False, None)[0])
            if fresh >= min_interns:
                keep.append([plat, slug, slug, "unknown", fresh])

        await asyncio.gather(*(check(p, s) for p, s in sorted(cands)))

    keep.sort(key=lambda r: -r[4])
    with open(_parent_made(SETTINGS.boards_file), "w") as f:
        json.dump([r[:4] for r in keep], f, indent=1)
    print(f"\n{live}/{len(cands)} boards live · {len(keep)} with >={min_interns} "
          f"fresh tech internship(s)")
    print(f"wrote {len(keep)} to {SETTINGS.boards_file} (+{len(SEED_BOARDS)} seed merged at load)")
    print("\ntop boards:")
    for plat, slug, _, _, n in keep[:20]:
        print(f"  {n:>3} fresh  {plat:<11}{slug}")
    print("\nSectors default to 'unknown' — edit boards.json to tag them.")

async def cmd_verify(sector):
    boards = [b for b in BOARDS if not sector or b[3] == sector]
    print(f"probing {len(boards)} boards...\n")
    rows = []
    posts, stats = await fetch_all(
        on_status=lambda p, s, c, st, n: rows.append((p, s, c, st, n)), sector=sector)

    interns = defaultdict(int)
    for p, _ in select(posts, False, None)[0]:
        interns[(p.platform, p.company)] += 1

    for plat, slug, company, status, n in sorted(rows, key=lambda r: (r[0], r[1])):
        i = interns.get((plat, company), 0)
        mark = "ok " if status == "ok" and i else ("-- " if status == "ok" else "!! ")
        print(f" {mark}{plat:<11}{slug[:30]:<32}{status:<9}{n:>5} jobs {i:>4} tech-intern")
    dead = [r for r in rows if r[3] not in ("ok", "304")]
    print(f"\n{stats['ok']} live · {len(dead)} failed · "
          f"{sum(interns.values())} tech internships posted in the last "
          f"{MAX_AGE_DAYS} days")


async def cmd_list(us_only, show_dupes, category, sector, all_roles,
                   max_age, strict, use_llm=False):
    posts, stats = await fetch_all(sector=sector)
    llm = (await llm_classify(db_init(), llm_candidates(posts))
           if use_llm else None)
    sel, drops = select(posts, us_only, category, tech_only=not all_roles,
                        max_age=max_age, keep_undated=not strict, llm=llm)

    by_sector = defaultdict(lambda: defaultdict(list))
    for items in group_roles(sel):
        by_sector[items[0][0].sector][items[0][0].company].append(items)

    total = 0
    for sect in sorted(by_sector):
        print(f"\n{'='*60}\n{sect.upper()}\n{'='*60}")
        for company in sorted(by_sector[sect]):
            print(f"\n{company}")
            for items in sorted(by_sector[sect][company],
                                key=lambda g: -(g[0][0].published or 0)):
                p, c = items[0]
                total += 1
                extra = f"  (+{len(items)-1} more)" if len(items) > 1 else ""
                term = f" · {c['term']}" if c["term"] else ""
                print(f"  {p.title}")
                print(f"    {p.location} · {c['category']}{term} · {c['region']} · "
                      f"posted {age_str(p)} ago{extra}")
                print(f"    {p.url}")
                if show_dupes:
                    for q, _ in items[1:]:
                        print(f"      + {q.location}  {q.url}")

    note = f" · ignored {drops['stale']} older than {max_age}d" if max_age else ""
    if drops["undated"]:
        note += f" · ignored {drops['undated']} undated"
    print(f"\n{total} distinct roles ({len(sel)} postings pre-dedup) "
          f"from {stats['ok']} boards · {stats['error']} errors{note}")


class SchemaMismatch(RuntimeError):
    """postings.db was created by a different SCHEMA_VERSION."""


class DatabaseRefused(RuntimeError):
    """postings.db is not a file this command will use as it stands.

    Missing, an empty ledger without --init, a failed integrity check, or an
    upgrade that did not keep every row. main() prints it and exits 1.
    """


class EmptyLedger(DatabaseRefused):
    """postings.db's seen ledger is empty, and this command was not asked to bootstrap
    it. Its own class so that `diayn.py run`, which takes no --init, can say something
    else than the --init this message names."""


class LockHeld(RuntimeError):
    """Another process holds the sweeper lock. main() prints it and exits 3."""


# The exit status of a command refused by the sweeper lock. Not 1, so a pm2
# log or a cron wrapper can tell "another sweeper is running" from "failed".
LOCK_HELD_EXIT = 3


# The whole schema. contract/postings_v1.sql is its published copy: the bot
# builds its fixtures from that file, and tests/test_contract.py requires the
# two to match. Every table is IF NOT EXISTS, so opening a file never changes
# one that is there — the last three, the contract's own, were added that way,
# and a scraper from before them opens the file unharmed.
SCHEMA = f"""
    -- Permanent dedup ledger. Never pruned. ~40 bytes/row, so a decade of
    -- postings costs a few MB. This is what makes pruning safe: `postings`
    -- can be emptied without a single role being re-announced.
    CREATE TABLE IF NOT EXISTS seen(
      platform TEXT, external_id TEXT, first_seen REAL,
      PRIMARY KEY(platform, external_id));

    -- Prunable detail table. Only holds rows inside the retention window.
    -- No INTEGER PRIMARY KEY, so its rowids are SQLite's own, and the bot
    -- keys on them: never VACUUM, rebuild, or replace a row (CONTRACT.md, P3).
    CREATE TABLE IF NOT EXISTS postings(
      platform TEXT, external_id TEXT, company TEXT, sector TEXT, title TEXT,
      location TEXT, url TEXT, category TEXT, term TEXT, region TEXT,
      is_intern INT, is_tech INT, published REAL, unbounded INT,
      first_seen REAL, PRIMARY KEY(platform, external_id));
    CREATE INDEX IF NOT EXISTS idx_pub ON postings(published);

    CREATE TABLE IF NOT EXISTS llm_cache(
      hash TEXT PRIMARY KEY, payload TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS llm_usage(
      day TEXT PRIMARY KEY, n INT, prompt_tokens INT DEFAULT 0,
      output_tokens INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS etags(
      platform TEXT, slug TEXT, etag TEXT, PRIMARY KEY(platform, slug));
    CREATE TABLE IF NOT EXISTS sweeps(
      started REAL, duration REAL, not_modified INT, errors INT,
      new_rows INT, pruned INT);

    -- The contract tables, rewritten by publish_registry.
    CREATE TABLE IF NOT EXISTS scraper_meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS boards(
      platform TEXT, slug TEXT, company TEXT, sector TEXT,
      PRIMARY KEY(platform, slug));
    CREATE TABLE IF NOT EXISTS blocked_companies(name TEXT PRIMARY KEY);
    PRAGMA user_version = {SCHEMA_VERSION};
"""

# The tables that hold data, as opposed to the contract tables, which are
# rewritten whole. upgrade-db must leave each one's count and MAX(rowid) as
# it found them.
LEDGER_TABLES = ("seen", "postings", "sweeps", "etags", "llm_cache", "llm_usage")


def _journal_mode(conn) -> str:
    return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _open(path, mode="rw"):
    """A connection to `path` in sqlite's `mode`: "ro", "rw" or "rwc".

    rw rather than a plain sqlite3.connect, which creates a file that is not
    there. An empty file at the wrong path — a typo in POSTINGS_DB, a volume
    not mounted yet — is an empty ledger, and its first sweep records every
    open posting as new (CONTRACT.md, P6). Only rwc makes the file, and its
    directory with it.
    """
    if mode == "rwc":
        _parent_made(path)
    try:
        return sqlite3.connect(f"file:{quote(os.path.abspath(path))}?mode={mode}",
                               uri=True)
    except sqlite3.OperationalError:
        if os.path.exists(path):
            raise
        raise _no_such_database(path) from None


def _no_such_database(path) -> DatabaseRefused:
    return DatabaseRefused(f"{path}: no such database. Only `sweep --init` or "
                           "`watch --init` creates one.")


def lock_path(db_path) -> str:
    return f"{db_path}.lock"


@contextlib.contextmanager
def sweeper_lock(db_path, create=False):
    """Hold <db_path>.lock for the block, or raise LockHeld at once (P5).

    Exactly one process writes postings.db: two would double the traffic to
    every board and race on the ledger the bot reads. flock rather than a pid
    file, because the kernel drops it when its holder dies, however it dies,
    so a crashed `watch` never leaves a stale lock for pm2's restart to trip
    over. LOCK_NB, because a second sweeper that queued for its turn would
    still double the traffic, only later. The file stays after release:
    deleting it would let a process holding the old file and one creating a
    new one both believe they hold the lock.

    Without `create` a missing database is refused here, before the lock file
    is made, so a wrong POSTINGS_DB leaves nothing behind (P6). `create` is for
    --init, and for `discover`, which never opens the database at all.
    Yields the lock file's path.
    """
    if not create and not os.path.exists(db_path):
        raise _no_such_database(db_path)
    path = lock_path(db_path)
    fd = os.open(_parent_made(path) if create else path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise LockHeld(
            f"{path} is held by another process: a `watch`, or a one-shot command "
            f"that writes. Only one process may write {db_path} at a time "
            "(CONTRACT.md, P5), so this one did nothing.") from None
    except OSError:
        os.close(fd)
        raise
    try:
        yield path
    finally:
        os.close(fd)


def _refuse_unless_ours(conn, path, create=False):
    """Raise, having written nothing, unless `conn` is a postings.db to use.

    That is a v2 file, or with `create` a file that holds nothing yet. A typo
    in POSTINGS_DB can name a file that does exist — the bot's stats.db,
    another app's database — and adopting it would switch it to WAL and write
    the postings schema into it before anything else could refuse (P6). So
    user_version 0 is refused too, and the refusal does not suggest --init,
    which would bootstrap into the wrong file. Raises instead of sys.exit: the
    CLI turns both errors into exit(1).
    """
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver == SCHEMA_VERSION:
        return
    if ver:
        raise SchemaMismatch(
            f"{path}: db schema v{ver} != v{SCHEMA_VERSION}. The bot reads this "
            "file too: see CONTRACT.md before changing either side.")
    if create and conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None:
        return
    raise DatabaseRefused(
        f"{path} is not a postings.db: its user_version is 0, not "
        f"{SCHEMA_VERSION}. Nothing was written to it. Check POSTINGS_DB; "
        "`internship_poller.py config` prints the path in use.")


def _prepare(conn, path, create=False):
    """`conn`, with the schema in place, in WAL mode and waiting on locks.

    WAL so the bot's reads and the scraper's writes never block each other,
    and a reader only ever sees committed sweeps. The switch needs the file to
    itself, so it is made only when the file is not WAL already; the busy
    timeout is set first, so the switch and every write after it wait out
    another connection's lock rather than failing on it. Closes `conn` and
    raises, before any of that, unless the file is one to use
    (_refuse_unless_ours).
    """
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _refuse_unless_ours(conn, path, create)
        if _journal_mode(conn) != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Token accounting, added after llm_usage shipped. Additive ALTERs for
        # files from before it, rather than a SCHEMA_VERSION bump: a bump
        # would force users to delete postings.db and throw away every cached
        # Gemini verdict, which costs real quota to rebuild. Old rows keep
        # NULL and read as 0.
        for _col in ("prompt_tokens", "output_tokens"):
            try:
                conn.execute(f"ALTER TABLE llm_usage ADD COLUMN {_col} INT DEFAULT 0")
            except sqlite3.OperationalError:
                pass          # already migrated
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def db_init(create=False):
    """postings.db, ready to use. Raises DatabaseRefused if it does not exist.

    Every command that writes opens the database through this, and only
    `create` — `sweep --init` and `watch --init` — may make a new one.
    """
    path = SETTINGS.postings_db
    return _prepare(_open(path, "rwc" if create else "rw"), path, create)


def db_read_only():
    """postings.db for `stats`, the one command that opens it only to read.

    mode=ro, and none of _prepare's work: no WAL switch, no DDL, no
    user_version stamp. So it takes no lock and changes nothing under a writer
    that holds none — before stage 2's upgrade-db, the bot's own in-process
    sweep (P5). The same files are refused as db_init refuses.
    """
    path = SETTINGS.postings_db
    conn = _open(path, "ro")
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _refuse_unless_ours(conn, path)
    except Exception:
        conn.close()
        raise
    return conn


def open_for_sweeping(init, interval=DEFAULT_INTERVAL_S):
    """db_init for `sweep` and `watch`: refuse what P6 refuses, then publish.

    An empty `seen` is refused as firmly as a missing file. The first sweep
    into it records every open posting as new, which makes it a bootstrap,
    and a bootstrap is something the operator asks for with --init — never
    what a wrong path or a file restored empty does by accident. The registry
    is published here, at start-up (P8), so the bot sees this process's
    boards, blocklist and settings before its first sweep commits.
    """
    conn = db_init(create=init)
    if not init and conn.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is None:
        conn.close()
        raise EmptyLedger(
            f"{SETTINGS.postings_db}: the seen ledger is empty, so this sweep would "
            "record every open posting as new. Pass --init if this is the first "
            "sweep of a new database.")
    publish_registry(conn, interval)
    conn.commit()
    return conn


def scraper_meta(db_path, interval) -> dict:
    """The scraper_meta rows: exactly the keys CONTRACT.md lists, all as text.

    A new key is additive and needs no contract bump; removing or renaming
    one does.
    """
    started = STARTED_AT if STARTED_AT is not None else time.time()
    return {
        "contract_version": CONTRACT_VERSION,
        "scraper_version": __version__,
        "db_path": db_path,
        "prune_days": str(PRUNE_DAYS),
        "sweep_interval_s": str(interval),
        "gemini_model": SETTINGS.gemini_model,
        "llm_rpd": str(SETTINGS.llm_rpd),
        "llm_rpm": str(SETTINGS.llm_rpm),
        "llm_tpm": str(SETTINGS.llm_tpm),
        "llm_day_tz": SETTINGS.llm_day_tz,
        "started_at": str(started),
    }


def publish_registry(conn, interval=DEFAULT_INTERVAL_S):
    """Rewrite the three contract tables from what this process runs with (P8).

    `boards` is the registry after the blocklist, `blocked_companies` the
    blocklist as written, `scraper_meta` what the bot checks and shows. Each
    is rewritten whole, so a board dropped from boards.json leaves the table
    too. db_path is read off the connection itself, so it names the file
    actually opened, symlinks resolved.

    Uncommitted: the caller's transaction. cmd_sweep calls this just before
    its commit, so the tables change with a sweep or not at all, and in WAL
    mode the bot never reads them half-written.
    """
    opened = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.execute("DELETE FROM boards")
    # OR IGNORE: boards.json is edited by hand. A board listed twice is
    # polled twice, harmlessly; a publish that raised on it would fail every
    # sweep.
    conn.executemany(
        "INSERT OR IGNORE INTO boards(platform, slug, company, sector) "
        "VALUES(?,?,?,?)", BOARDS)
    conn.execute("DELETE FROM blocked_companies")
    # A name that normalises to nothing blocks nothing here, and in the bot's
    # prefix rule it would block everything.
    conn.executemany("INSERT INTO blocked_companies(name) VALUES(?)",
                     [(n,) for n in sorted(BLOCKED_COMPANIES) if _norm(n)])
    conn.execute("DELETE FROM scraper_meta")
    conn.executemany(
        "INSERT INTO scraper_meta(key, value) VALUES(?,?)",
        sorted(scraper_meta(os.path.realpath(opened), interval).items()))


def census(conn) -> dict:
    """{table: (rows, MAX(rowid))} for each of LEDGER_TABLES the file has."""
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {t}").fetchone()
            for t in LEDGER_TABLES if t in present}


def _census_line(table, before, after) -> str:
    def shown(c):
        return "absent" if c is None else f"{c[0]} rows, max rowid {c[1]}"
    return f"{table}: {shown(before)} -> {shown(after)}"


def _refuse_to_upgrade(conn, path):
    """Raise, having changed nothing, unless `conn` is a sound v2 postings.db."""
    problems = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    if problems != ["ok"]:
        raise DatabaseRefused(f"{path}: integrity_check failed, nothing was "
                              "changed: " + "; ".join(problems[:5]))
    print("integrity_check: ok")
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver != SCHEMA_VERSION:
        raise DatabaseRefused(
            f"{path}: user_version {ver}, not {SCHEMA_VERSION}. upgrade-db only "
            f"upgrades a v{SCHEMA_VERSION} postings.db; nothing was changed.")


def cmd_upgrade_db(interval=DEFAULT_INTERVAL_S):
    """Bring the live v2 postings.db up to the contract without moving a row.

    For the file the bot has been sweeping until now: it switches the file to
    WAL, adds the contract tables and writes scraper_meta, so the bot's
    contract check (B2) passes before this scraper's first sweep. A file that
    fails PRAGMA integrity_check or is not user_version 2 is refused before
    anything is written. Each ledger table's count and MAX(rowid) is printed
    before and after, and must not change: the rowids are the bot's
    autocomplete values (P3). Safe to run again.
    """
    path = SETTINGS.postings_db
    conn = _open(path)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _refuse_to_upgrade(conn, path)
        mode, before = _journal_mode(conn), census(conn)
        _prepare(conn, path)
        publish_registry(conn, interval)
        conn.commit()
        after = census(conn)
        print(f"journal_mode: {mode} -> {_journal_mode(conn)}")
        for table in LEDGER_TABLES:
            if table in before or table in after:
                print(_census_line(table, before.get(table), after.get(table)))
        moved = [t for t in before if after.get(t) != before[t]]
        if moved:
            raise DatabaseRefused(
                f"{path}: {', '.join(moved)} changed during the upgrade. Restore "
                "the backup before anything sweeps.")
        print(f"scraper_meta: contract_version {CONTRACT_VERSION}, "
              f"db_path {os.path.realpath(path)}")
    finally:
        conn.close()


def prune(conn, days=PRUNE_DAYS, dry_run=False):
    """Delete rows older than `days` from `postings`; return how many.

    Never touches `seen`, so pruned roles stay deduped. Rows with no date, and
    Workday's unbounded "30d+" bucket, are left alone — we can't prove they're
    old, and deleting them would only lose data we already have.

    Uncommitted: the caller commits. A sweep's prune lands with the rest of
    that sweep or not at all (P4). `days` below PRUNE_DAYS is refused, from
    any caller: the bot shows postings that young (P2).
    """
    if days < PRUNE_DAYS:
        raise ValueError(f"prune: {days} days is inside the bot's "
                         f"{PRUNE_DAYS}-day window")
    cutoff = time.time() - days * 86400
    q = ("published IS NOT NULL AND unbounded=0 AND published < ?", (cutoff,))
    n = conn.execute(f"SELECT COUNT(*) FROM postings WHERE {q[0]}", q[1]).fetchone()[0]
    if not dry_run and n:
        conn.execute(f"DELETE FROM postings WHERE {q[0]}", q[1])
    return n


def log(line, file=None):
    """Print one timestamped line, flushed at once.

    pm2 reads stdout through a pipe, and Python holds piped output back until
    its buffer fills: unflushed, a quiet day's log would show nothing at all.
    """
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}",
          file=file or sys.stdout, flush=True)


@dataclass(frozen=True)
class SweepResult:
    """What one sweep did: the postings worth announcing, and its log line."""
    fresh: tuple
    summary: str


def sweep_summary(stats, new, pruned, seconds) -> str:
    """A sweep in one line: what it fetched, what was new, how long it took."""
    pr = f" · pruned {pruned}" if pruned else ""
    return (f"{stats['ok']} fetched · {stats['not_modified']} unchanged · "
            f"{stats['error']} errors · {new} new{pr} · {seconds:.1f}s")


async def cmd_sweep(conn, quiet=False, use_llm=False, interval=DEFAULT_INTERVAL_S):
    """One sweep: fetch every board, store what is new, prune, publish.

    The sweep's own writes — etags, seen, postings, the prune, the sweeps row
    and the contract tables — are one transaction, committed at the end, and
    nothing awaits between taking `now` and that commit (CONTRACT.md, P1).
    With `use_llm`, llm_classify commits its cache before that transaction
    begins. `interval` is what scraper_meta tells the bot to expect between
    sweeps. Unless `quiet`, prints its summary and each new posting; either
    way, returns them as a SweepResult.
    """
    t0 = time.time()
    etags = {(r[0], r[1]): r[2]
             for r in conn.execute("SELECT platform, slug, etag FROM etags")}
    posts, stats = await fetch_all(etags)

    # Dedup against `seen`, never against `postings` — postings gets pruned.
    # One ledger read instead of a SELECT per posting: this loop runs on the
    # Discord bot's event loop, so per-row round-trips add up fast.
    seen_ids = set(conn.execute("SELECT platform, external_id FROM seen"))

    # Classify the delta only — postings we have never seen. That is what keeps
    # this inside a free-tier budget: a busy day is tens of new rows, not
    # thousands of re-classified ones.
    llm = {}
    if use_llm:
        cand = llm_candidates(
            [p for p in posts if (p.platform, p.external_id) not in seen_ids])
        if cand:
            llm = await llm_classify(conn, cand, verbose=not quiet)

    for (plat, slug), et in stats["new_etags"].items():
        conn.execute("INSERT INTO etags(platform, slug, etag) VALUES(?,?,?) "
                     "ON CONFLICT(platform, slug) DO UPDATE SET etag=excluded.etag",
                     (plat, slug, et))

    now, fresh, seen_rows, posting_rows = time.time(), [], [], []
    for p in posts:
        key = (p.platform, p.external_id)
        if key in seen_ids:
            continue
        seen_ids.add(key)   # also dedups repeats within this batch
        c = llm.get(posting_hash(p)) or classify(p)
        seen_rows.append((p.platform, p.external_id, now))
        posting_rows.append(
            (p.platform, p.external_id, p.company, p.sector, p.title,
             p.location, p.url, c["category"], c["term"], c["region"],
             int(c["is_intern"]), int(c["is_tech"]), p.published,
             int(p.unbounded), now))
        # Always store — the seen-set must cover stale rows too, or they'd
        # re-trigger as "new" on every sweep. Age only gates what we announce.
        age = age_days(p)
        if (c["is_intern"] and c["is_tech"]
                and (age is None or p.unbounded or age <= MAX_AGE_DAYS)):
            fresh.append((p, c))
    # OR IGNORE: a concurrent CLI/bot sweep racing on the same DB loses the
    # duplicate row instead of aborting the whole sweep with IntegrityError.
    # Named columns throughout: a positional INSERT breaks, or writes the
    # wrong column, the day its table gains one.
    conn.executemany("INSERT OR IGNORE INTO seen(platform, external_id, first_seen) "
                     "VALUES(?,?,?)", seen_rows)
    conn.executemany(
        "INSERT OR IGNORE INTO postings(platform, external_id, company, sector, "
        "title, location, url, category, term, region, is_intern, is_tech, "
        "published, unbounded, first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        posting_rows)
    pruned = prune(conn)
    conn.execute("INSERT INTO sweeps(started, duration, not_modified, errors, "
                 "new_rows, pruned) VALUES(?,?,?,?,?,?)",
                 (t0, now - t0, stats["not_modified"], stats["error"],
                  len(fresh), pruned))
    publish_registry(conn, interval)
    conn.commit()

    summary = sweep_summary(stats, len(fresh), pruned, now - t0)
    if not quiet:
        log(summary)
        for p, c in fresh:
            print(f"  [{p.sector}] {p.company} — {p.title}")
            print(f"    {p.location} · {c['category']} · {c['region']} · "
                  f"posted {age_str(p)} ago")
            print(f"    {p.url}")
    return SweepResult(tuple(fresh), summary)


def cmd_stats(conn):
    tot, ints = conn.execute(
        "SELECT COUNT(*), SUM(is_intern AND is_tech) FROM postings").fetchone()
    ledger = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    print(f"retained: {tot} postings · {ints or 0} tech internships")
    print(f"dedup ledger: {ledger} ids (never pruned)\n")
    print("by sector:")
    for row in conn.execute("SELECT sector, COUNT(*) FROM postings "
                            "WHERE is_intern=1 AND is_tech=1 GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {row[0]:<12} {row[1]}")
    print("\nby category / region:")
    for row in conn.execute("SELECT category, region, COUNT(*) FROM postings "
                            "WHERE is_intern=1 AND is_tech=1 GROUP BY 1,2 ORDER BY 3 DESC"):
        print(f"  {row[0]:<10} {row[1]:<9} {row[2]}")
    lags = sorted((r[0] - r[1]) / 60 for r in conn.execute(
        "SELECT first_seen, published FROM postings WHERE is_intern=1 AND is_tech=1 "
        "AND published IS NOT NULL AND platform != 'workday'") if r[0] > r[1])
    print("\ndetection lag, minutes (Workday excluded — day-level only):")
    if lags:
        print(f"  n={len(lags)} median={lags[len(lags)//2]:.0f} "
              f"min={lags[0]:.0f} max={lags[-1]:.0f}")
    else:
        print("  no data yet")
    print(f"\nage of retained internships (pruned at {PRUNE_DAYS}d):")
    for label, lo, hi in [("<3d", 0, 3), ("3-7d", 3, 7), ("7-14d", 7, 14),
                          ("14-30d", 14, 30)]:
        n = conn.execute(
            "SELECT COUNT(*) FROM postings WHERE is_intern=1 AND is_tech=1 "
            "AND published IS NOT NULL AND (?-published)/86400 >= ? "
            "AND (?-published)/86400 < ?",
            (time.time(), lo, time.time(), hi)).fetchone()[0]
        print(f"  {label:<16} {n}")

    cached = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
    if cached:
        today = conn.execute("SELECT n FROM llm_usage WHERE day=?",
                             (quota_day(),)).fetchone()
        print(f"\nllm cache: {cached} classified · {today[0] if today else 0} "
              f"api calls today in {SETTINGS.llm_day_tz} "
              f"(budget {SETTINGS.llm_rpd}/day on {SETTINGS.gemini_model})")

    print("\nrecent sweeps:")
    for s in conn.execute("SELECT started, duration, not_modified, errors, "
                          "new_rows, pruned FROM sweeps "
                          "ORDER BY started DESC LIMIT 10"):
        print(f"  {datetime.fromtimestamp(s[0]):%m-%d %H:%M}  {s[1]:5.1f}s  "
              f"304s={s[2]:<3} err={s[3]:<3} new={s[4]:<4} pruned={s[5] or 0}")


async def cmd_llm_diff(conn, limit):
    """Run both classifiers over stored postings and show disagreements.

    This is how you decide whether the LLM is worth the dependency. Run it
    before switching `sweep` over to --llm."""
    rows = conn.execute(
        "SELECT platform, external_id, company, sector, title, location, url, "
        "published, unbounded FROM postings ORDER BY first_seen DESC LIMIT ?",
        (limit,)).fetchall()
    posts = [Posting(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                     False, bool(r[8])) for r in rows]
    if not posts:
        print("no stored postings — run `sweep` first")
        return
    posts = llm_candidates(posts)
    print(f"comparing {len(posts)} candidate postings...\n")
    llm = await llm_classify(conn, posts)
    if not llm:
        print("no llm results (missing key or quota) — nothing to compare")
        return

    diffs = defaultdict(list)
    n = 0
    for p in posts:
        l = llm.get(posting_hash(p))
        if not l:
            continue
        n += 1
        r = classify(p)
        for field in ("is_intern", "is_tech", "category", "region", "term"):
            if r.get(field) != l.get(field):
                diffs[field].append((p, r.get(field), l.get(field)))

    print(f"{n} classified by both · "
          f"{sum(len(v) for v in diffs.values())} field disagreements\n")
    for field, items in sorted(diffs.items(), key=lambda kv: -len(kv[1])):
        print(f"{field}: {len(items)} disagreements")
        for p, rv, lv in items[:6]:
            print(f"  {p.title[:62]}")
            print(f"    {p.location[:50]}")
            print(f"    regex={rv!r}  llm={lv!r}")
        if len(items) > 6:
            print(f"  ... and {len(items)-6} more")
        print()
    print("Spot-check these by hand. Where the LLM is right, switch sweep to")
    print("--llm. Where it is wrong, tighten the prompt, not the regex.")


def note_attempt(lock, when):
    """Record in the held lock file that a sweep begins at `when`."""
    with open(lock, "w", encoding="ascii") as f:
        f.write(f"{when!r}\n")


def last_attempt(lock) -> Optional[float]:
    """When the last sweep began, as the lock file records it, or None.

    A sweep killed before it commits — the OOM killer, a SIGKILL — leaves no
    sweeps row, so MAX(started) alone would let each restart of that crash
    loop sweep every board at once. The record lives in the lock file, which
    only the process holding it writes, and which is not part of the contract.
    A file that records nothing readable — a new one, or one cut short — is no
    attempt.
    """
    try:
        with open(lock, encoding="ascii") as f:
            when = float(f.read())
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        return None
    return when if math.isfinite(when) else None


def seconds_until_due(conn, interval, now, attempted=None) -> float:
    """How long until the next sweep is due: an interval after the last began.

    The last is the later of MAX(sweeps.started) and `attempted`, the last
    attempt, finished or not. 0 when neither is known, or a sweep is overdue.
    Never more than one interval, so a sweep stamped in the future — the clock
    set back since — cannot stall the loop for longer than one gap.
    """
    last = conn.execute("SELECT MAX(started) FROM sweeps").fetchone()[0]
    began = max((t for t in (last, attempted) if t is not None), default=None)
    if began is None:
        return 0.0
    return min(float(interval), max(0.0, began + interval - now))


def _roll_back(conn) -> str:
    """Roll back what a failed sweep left open, and say how that went."""
    try:
        conn.rollback()
    except sqlite3.Error as e:
        return f"rollback failed too: {e}"
    return "rolled back"


async def cmd_watch(conn, interval, use_llm=False, sleep=asyncio.sleep,
                    clock=time.time):
    """Sweep every `interval` seconds until stopped; never exit over a sweep.

    pm2 restarts a process that exits, at once. A loop that swept as soon as
    it started would sweep every ATS host on every restart of a crash loop,
    so the first sweep waits until one is due — counting from the last sweep
    that began, which each sweep records in the lock file before its first
    request, so a sweep killed midway counts too. A sweep that raises is rolled
    back — otherwise its rows would stay pending, holding the write lock, and
    ride in on the next sweep's commit — logged, and followed a full interval
    later by the next. Anything that is not an Exception, Ctrl-C among them,
    still stops the loop.

    One log line per sweep, to stdout, or to stderr when it failed. The new
    postings are the bot's to announce, not the log's. `sleep` and `clock`
    are for the tests.
    """
    log(f"watching {len(BOARDS)} boards every {interval}s. Ctrl-C to stop.")
    lock = lock_path(SETTINGS.postings_db)
    wait = seconds_until_due(conn, interval, clock(), last_attempt(lock))
    if wait:
        log(f"next sweep due in {wait:.0f}s")
        await sleep(wait)
    while True:
        try:
            note_attempt(lock, clock())
            result = await cmd_sweep(conn, quiet=True, use_llm=use_llm,
                                     interval=interval)
            log(f"sweep: {result.summary}")
        except Exception as e:
            log(f"sweep failed, {_roll_back(conn)}: {type(e).__name__}: {e}",
                file=sys.stderr)
        await sleep(interval)


def cmd_config(settings, env_file):
    print("\n".join(config_lines(settings, env_file)))


def boot() -> Optional[str]:
    """What runs before any command, in this order; returns the .env it used.

    The .env first, so the settings see it; the settings next, so load_boards
    reads the BOARDS_FILE they name; the boards last. Each is bound once, here,
    and every function reads SETTINGS and BOARDS when it runs.
    """
    global SETTINGS, BOARDS, STARTED_AT
    STARTED_AT = time.time()
    env_file = load_env_file()
    SETTINGS = configure(os.environ)
    BOARDS = load_boards()
    return env_file


# Every command, in the order --help lists them. diayn.py hands each of these
# to main() unchanged.
COMMANDS = ("verify", "list", "sweep", "watch", "stats", "prune", "discover",
            "llm-diff", "upgrade-db", "config")


def arguments() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=COMMANDS)
    ap.add_argument("--us", action="store_true", help="US/remote only")
    ap.add_argument("--dupes", action="store_true", help="show collapsed duplicates")
    ap.add_argument("--category", help="swe|quant|hardware|data-ml|pm|other")
    ap.add_argument("--sector", help="tech|finance|healthcare|defense|industrial|retail|energy")
    ap.add_argument("--all-roles", action="store_true",
                    help="include non-technical internships")
    ap.add_argument("--max-age", type=int, default=MAX_AGE_DAYS,
                    help=f"ignore postings older than N days (default {MAX_AGE_DAYS}; "
                         f"0 = no limit). prune: delete rows older than N days, "
                         f"N at least {PRUNE_DAYS}")
    ap.add_argument("--strict", action="store_true",
                    help="also drop postings with unknown or unbounded dates")
    ap.add_argument("--dry-run", action="store_true", help="prune: count only")
    ap.add_argument("--min-interns", type=int, default=1,
                    help="discover: keep boards with at least N fresh internships")
    ap.add_argument("--workday", action="store_true",
                    help="discover: also validate mined Workday triples (slow)")
    ap.add_argument("--common-crawl", action="store_true",
                    help="discover: also query Common Crawl (often 503; best-effort)")
    ap.add_argument("--yc", action="store_true",
                    help="discover: probe YC's 6k open company dataset (slow, cached)")
    ap.add_argument("--yc-limit", type=int,
                    help="discover: only probe N uncached YC companies this run")
    ap.add_argument("--yc-recheck", action="store_true",
                    help="discover: ignore yc_cache.json and re-probe everything")
    ap.add_argument("--llm", action="store_true",
                    help="classify with Gemini instead of regex (needs GEMINI_API_KEY)")
    ap.add_argument("--limit", type=int, default=200,
                    help="llm-diff: how many stored postings to compare")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S,
                    help="watch: seconds between sweeps, which scraper_meta tells "
                         f"the bot to expect (default {DEFAULT_INTERVAL_S}, "
                         f"at least {MIN_INTERVAL_S})")
    ap.add_argument("--init", action="store_true",
                    help="sweep/watch: create postings.db if it is missing, and "
                         "allow a first sweep into an empty ledger")
    return ap


# The commands that write, and so take the sweeper lock (P5): postings.db, or
# for `discover` boards.json and yc_cache.json. `list` joins them with --llm,
# which writes llm_cache and llm_usage. The rest — stats, verify, config and
# plain `list` — only read, and never wait on a sweeper.
WRITING_COMMANDS = frozenset({"sweep", "watch", "prune", "upgrade-db",
                              "llm-diff", "discover"})


def lock_for(a):
    """The sweeper lock command `a` must hold while it runs, or a no-op."""
    if a.cmd not in WRITING_COMMANDS and not (a.cmd == "list" and a.llm):
        return contextlib.nullcontext()
    create = a.cmd == "discover" or (a.init and a.cmd in ("sweep", "watch"))
    return sweeper_lock(SETTINGS.postings_db, create=create)


def run(a, env_file):
    """Run command `a`, with SETTINGS and BOARDS bound and any lock held."""
    if a.cmd == "config":
        cmd_config(SETTINGS, env_file)
    elif a.cmd == "discover":
        asyncio.run(cmd_discover(a.min_interns, a.workday, a.common_crawl,
                                 a.yc, a.yc_limit, a.yc_recheck))
    elif a.cmd == "verify":
        asyncio.run(cmd_verify(a.sector))
    elif a.cmd == "list":
        asyncio.run(cmd_list(a.us, a.dupes, a.category, a.sector, a.all_roles,
                             a.max_age, a.strict, a.llm))
    elif a.cmd == "stats":
        cmd_stats(db_read_only())
    elif a.cmd == "prune":
        conn = db_init()
        n = prune(conn, a.max_age, a.dry_run)
        conn.commit()
        print(f"{'would prune' if a.dry_run else 'pruned'} {n} rows older than "
              f"{a.max_age}d · dedup ledger untouched")
    elif a.cmd == "sweep":
        conn = open_for_sweeping(a.init, a.interval)
        asyncio.run(cmd_sweep(conn, use_llm=a.llm, interval=a.interval))
    elif a.cmd == "llm-diff":
        asyncio.run(cmd_llm_diff(db_init(), a.limit))
    elif a.cmd == "upgrade-db":
        cmd_upgrade_db(a.interval)
    else:
        conn = open_for_sweeping(a.init, a.interval)
        try:
            asyncio.run(cmd_watch(conn, a.interval, use_llm=a.llm))
        except KeyboardInterrupt:
            print("\nstopped.")


def main(argv=None):
    """Run the command in `argv`, or on the command line when it is None.

    Exits 1 on a refusal or a bad setting, 2 on a usage error (argparse), 3
    when another sweeper holds the lock; returns when the command is done.
    """
    ap = arguments()
    a = ap.parse_args(argv)
    if a.cmd == "prune" and a.max_age < PRUNE_DAYS:
        ap.error(f"prune --max-age {a.max_age}: the bot shows postings up to "
                 f"{PRUNE_DAYS} days old, so prune never deletes a younger row. "
                 f"Pass {PRUNE_DAYS} or more.")
    if a.interval < MIN_INTERVAL_S:
        ap.error(f"--interval {a.interval}: sweeps are at least {MIN_INTERVAL_S} "
                 "seconds apart, and scraper_meta tells the bot to expect the gap. "
                 f"Pass {MIN_INTERVAL_S} or more.")
    try:
        env_file = boot()
        with lock_for(a):
            run(a, env_file)
    except LockHeld as e:
        print(e, file=sys.stderr)
        sys.exit(LOCK_HELD_EXIT)
    except (ConfigError, SchemaMismatch, DatabaseRefused) as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
