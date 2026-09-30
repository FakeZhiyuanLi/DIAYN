"""
intern_match.py
~~~~~~~~~~~~~~~
Which recent postings fit one person's profile, how well, and why.

Every list the finder shows and every DM it sends comes out of `rank`, so the
same rules decide what a student sees in `/internships matches`, in a digest,
on the card's "Last 30 days" line and in the empty state's suggestions. The
window is read once (`load_window`), tagged by the classifiers in
`intern_taxonomy` and `intern_location`, and then scored per profile in memory:
nothing about a person is ever written back into postings.db.

The weights, thresholds and grouping are ported from the measured reference
(spec 4.5, 4.6), with the spec's three deliberate changes: a title that names
none of the user's fields but one of their keywords is floored at 45 so it can
be listed at all; a migrated profile with no degree is never charged the
graduate-only penalty (its owner was never asked); and a clone key excludes a
group only when that group is a regional clone, so hiding "Corporate Intern -
Accounting" never silences "Corporate Intern - Finance".

Pure: stdlib plus the finder's pure modules, so it imports under bare
`python3`. `load_window` is the one coroutine; its tagging runs in a thread
because a cold window costs seconds of CPU the event loop cannot spare.
"""

import asyncio
import dataclasses
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType

import intern_vocab as vocab
from intern_location import LocInfo, location_fit, location_for
from intern_profile import Profile, term_after_graduation, with_changes
from intern_taxonomy import (bucket, clearance_note, clone_key, company_norm, group_hash,
                             requires_grad, role_key, term_of, title_fields)
from resume_lexicon import SKILL_BY_ID

SHOW_MIN = 45; GOOD_MIN = 60; STRONG_MIN = 75
W_FIELD, W_KEYWORD, W_PLACE, W_FRESH, W_TERM, GRAD_PENALTY = 55, 20, 10, 10, 5, 25
CLONE_MIN_MEMBERS = 3; CLONE_MIN_PLACES = 2
THIN_FIELD_MAX = 4
MATCHES_MAX = 15; FIRST_MATCHES = 5; BROWSE_MAX = 20

#: The 30-day window (4.5.1). `unbounded = 0` drops Workday's "30d+" bucket,
#: whose real age is unknown. Blocked companies are dropped after the read.
WINDOW_SQL: str = (
    "SELECT rowid, platform, external_id, company, title, location, url, published, first_seen "
    "FROM postings "
    "WHERE unbounded = 0 AND first_seen <= ? AND COALESCE(published, first_seen) >= ?")

DAY_S = 86400
_COMPANY_AT = 3                          # index of `company` in a WINDOW_SQL row
_FRESH_DAYS, _RECENT_DAYS = 3, 7
_KEYWORD_ONE, _KEYWORD_MANY = 0.6, 1.0
_TERM_PICKED, _TERM_OTHER = 1.0, 0.5
_MIN_SKILL_PART = 3                      # "HTML/CSS" -> HTML, CSS; "C#" is too short to trust
_MAX_WHY, _MAX_HIT_NAMES = 4, 3
_BROWSE_FIELD_CONF = 0.5
_MAX_RELAXATIONS = 3
_BEFORE_GRADUATION = frozenset({"intern", "coop"})
_ENGINEERING_WHY = "an engineering role (title names no discipline)"
_NO_EVIDENCE = "no field or keyword of yours in the title"
_US_AND_UNLISTED = ("us", "unlisted")
_GRAD_LEVELS = ("new_grad", "entry")


@dataclass(frozen=True)
class Candidate:
    """One posting of the window, with everything the scorer reads already tagged."""

    rowid: int; platform: str; external_id: str; company: str; title: str; location: str
    url: str; published: float | None; first_seen: float
    level: str; fields: tuple[tuple[str, float], ...]; loc: LocInfo
    term: tuple[str | None, int | None, int | None]; grad_only: str | None; note: str | None
    rk: str; ck: str; rk_hash: str; ck_hash: str; company_norm: str

    @property
    def ts(self) -> float:
        """When it was posted, or when the sweep first saw it if the board gives no date."""
        return self.published or self.first_seen


@dataclass(frozen=True)
class Match:
    """One group (a role, or a regional clone of it) as a profile sees it."""

    cand: Candidate            # representative
    score: int
    band: str | None           # "Strong match" | "Good match" | "Worth a look" | None (browse)
    why: tuple[str, ...]
    place: str                 # location_fit text of the representative
    group_key: str
    ledger: tuple[str, ...]    # (rep.rk_hash, rep.ck_hash)
    more: int
    #: The fit check's (verdict, reason) for this role, where it has one (intern_fit);
    #: nothing here sets it.
    fit: tuple[str, str] | None = None


@dataclass(frozen=True)
class Coverage:
    total: int; strong: int; us_extra: int | None
    thin: tuple[tuple[str, int], ...]; hourly_hint: int


@dataclass(frozen=True)
class Relaxation:
    id: str                    # us | adjacent | levels | unlisted | hourly
    label: str                 # J8 labels, e.g. "Add Chemical engineering & materials"
    gain: int
    changes: Mapping[str, object]   # kwargs for intern_profile.with_changes


# ------------------------------------------------------------------ the window

def read_window(pconn: sqlite3.Connection, *, upto: float, oldest: float,
                is_blocked: Callable[[str], bool]) -> list[tuple]:
    """The window's rows, blocked companies dropped. Event-loop thread only (sqlite3)."""
    rows = pconn.execute(WINDOW_SQL, (upto, oldest)).fetchall()
    return [r for r in rows if not is_blocked(r[_COMPANY_AT])]


def tag_rows(rows: Sequence[tuple]) -> list[Candidate]:
    """Rows as Candidates. Pure CPU and seconds of it when cold: run it in a thread."""
    return [_tag(r) for r in rows]


def _tag(row: tuple) -> Candidate:
    rowid, platform, external_id, company, title, location, url, published, first_seen = row
    company, title, location = company or "", title or "", location or ""
    rk, ck = role_key(company, title), clone_key(company, title)
    return Candidate(
        rowid=rowid, platform=platform or "", external_id=external_id or "", company=company,
        title=title, location=location, url=url or "", published=published,
        first_seen=first_seen or 0.0, level=bucket(title), fields=title_fields(title),
        loc=location_for(title, location), term=term_of(title), grad_only=requires_grad(title),
        note=clearance_note(title), rk=rk, ck=ck, rk_hash=group_hash(rk), ck_hash=group_hash(ck),
        company_norm=company_norm(company))


async def load_window(pconn, *, now: float, max_age_days: int,
                      is_blocked: Callable[[str], bool]) -> list[Candidate]:
    """read_window(upto=now, oldest=now - max_age_days*86400) then asyncio.to_thread(tag_rows)."""
    rows = read_window(pconn, upto=now, oldest=now - max_age_days * DAY_S, is_blocked=is_blocked)
    return await asyncio.to_thread(tag_rows, rows)


def group_map(cands: Sequence[Candidate]) -> dict[int, str]:
    """rowid -> group key: the clone key for a regional clone (4.6), else the role key."""
    members, places = defaultdict(int), defaultdict(set)
    for c in cands:
        members[c.ck] += 1
        places[c.ck].update((p.city.lower(), p.state, p.country) for p in c.loc.places)
    regional = {ck for ck, n in members.items()
                if n >= CLONE_MIN_MEMBERS and len(places[ck]) >= CLONE_MIN_PLACES}
    return {c.rowid: c.ck if c.ck in regional else c.rk for c in cands}


# ------------------------------------------------------------------ one posting

def field_fit(profile_fields: Sequence[str],
              posting_fields: Sequence[tuple[str, float]]) -> tuple[float, str | None]:
    """F (4.5.3) and the Why line's field reason. Profile fields are read in their order,
    so when two lend the same weight the first one names the relation, every time."""
    chosen = tuple(profile_fields)
    best, why = 0.0, None
    for field, conf in posting_fields:
        weight, label = _field_weight(field, chosen)
        if conf * weight > best:
            best, why = conf * weight, label
    return best, why


def _field_weight(field: str, chosen: tuple[str, ...]) -> tuple[float, str | None]:
    labels = vocab.FIELD_LABELS
    if field in chosen:
        return 1.0, f"{labels.get(field, field)} (your field)"
    if field == "engineering_general":
        return (1.0 if vocab.ENGINEERING_FIELDS.intersection(chosen) else 0.0), _ENGINEERING_WHY
    weight, source = 0.0, None
    for pf in chosen:
        lent = vocab.FIELD_ADJACENT.get(pf, {}).get(field, 0.0)
        if lent > weight:
            weight, source = lent, pf
    if source is None:
        return 0.0, None
    return weight, f"{labels.get(field, field)} (related to your {labels.get(source, source)})"


def title_terms(p: Profile) -> tuple[str, ...]:
    """The user's keywords, then each part of a non-strict skill's label ("HTML/CSS" ->
    HTML, CSS) of 3+ characters; once each, ignoring case. Strict skills (C, Go, R) are
    ordinary words in a title and are never looked for."""
    parts = (part.strip() for sid in p.skills if sid in SKILL_BY_ID and not SKILL_BY_ID[sid].strict
             for part in SKILL_BY_ID[sid].label.split("/"))
    skills = tuple(part for part in parts if len(part) >= _MIN_SKILL_PART)
    unique = {}
    for term in (*p.keywords, *skills):
        unique.setdefault(term.casefold(), term)
    return tuple(unique.values())


@lru_cache(maxsize=4096)
def _term_rx(term: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.I)


def _hits(p: Profile, title: str) -> list[str]:
    text = vocab.norm_text(title)
    return [term for term in title_terms(p) if _term_rx(term).search(text)]


def _hard_filter(p: Profile, c: Candidate) -> tuple[float | None, str]:
    """(P, place text) when rules 1-5 of 4.5.2 pass, else (None, the first failure)."""
    if c.level not in p.levels:
        return None, f"level not in Looking for ({vocab.LEVEL_LABELS.get(c.level, c.level)})"
    P, place = location_fit(c.loc, p.locations)
    if P is None:
        return None, place
    reason = _term_reason(p, c) or _company_reason(p, c)
    return (None, reason) if reason else (P, place)


def _term_reason(p: Profile, c: Candidate) -> str | None:
    label, year, month = c.term
    if p.terms and label:
        seasonal = " " in label
        if (seasonal and label not in p.terms) or \
                (not seasonal and not any(t.endswith(label) for t in p.terms)):
            return f"term {label} isn't one you picked"
    if c.level in _BEFORE_GRADUATION and term_after_graduation(p, year, month):
        return "starts after you graduate"
    return None


def _company_reason(p: Profile, c: Candidate) -> str | None:
    """The poller's prefix rule: "boeing" hides "Boeing Defense"."""
    if any(h and c.company_norm.startswith(h) for h in p.companies_hidden):
        return "company hidden"
    if p.companies_only and not any(o and c.company_norm.startswith(o) for o in p.companies_only):
        return "not one of your only-these companies"
    return None


def _freshness(age_s: float) -> float:
    days = age_s / DAY_S
    return 1.0 if days <= _FRESH_DAYS else 0.5 if days <= _RECENT_DAYS else 0.0


def _evaluate(p: Profile, c: Candidate, now: float):
    """((score, why, P, place), None) when the posting passes, else (None, reason)."""
    P, place = _hard_filter(p, c)
    if P is None:
        return None, place
    F, field_why = field_fit(p.fields, c.fields)
    hits = _hits(p, c.title)
    if F == 0 and not hits:
        return None, _NO_EVIDENCE
    K = _KEYWORD_MANY if len(hits) >= 2 else _KEYWORD_ONE if hits else 0.0
    T = _TERM_PICKED if c.term[0] and c.term[0] in p.terms else _TERM_OTHER
    raw = W_FIELD * F + W_KEYWORD * K + W_PLACE * P + W_FRESH * _freshness(now - c.ts) + W_TERM * T
    if F == 0 and K > 0:
        raw = max(raw, SHOW_MIN)                        # keyword-only floor (4.5.3)
    caveat = bool(c.grad_only) and p.degree not in vocab.GRAD_DEGREES
    penalised = caveat and not (p.source == "migrated" and p.degree is None)
    value = round(raw - GRAD_PENALTY * penalised)
    # 4.5.5's fourth reason is "Caveats": the graduate one then the clearance one share it,
    # so the four-reason cap can never cut the caveat D16 promises everyone.
    caveats = ((f"asks for {c.grad_only}",) if caveat else ()) + ((c.note,) if c.note else ())
    why = ((field_why,) if field_why else ()) \
        + ((f"title mentions {', '.join(hits[:_MAX_HIT_NAMES])}",) if hits else ()) \
        + (place,) + ((" · ".join(caveats),) if caveats else ())
    return (value, why[:_MAX_WHY], P, place), None


def score(p: Profile, c: Candidate, now: float) -> tuple[int, tuple[str, ...], float, str] | None:
    """(score, why, P, place) or None when a per-posting hard filter (4.5.2 rules 1-5, 7) fails."""
    return _evaluate(p, c, now)[0]


def band_for(value: int) -> str | None:
    """The band a list shows (4.5.4), None below the list threshold."""
    if value >= STRONG_MIN:
        return "Strong match"
    if value >= GOOD_MIN:
        return "Good match"
    return "Worth a look" if value >= SHOW_MIN else None


def explain(p: Profile, c: Candidate, now: float) -> tuple[Match | None, str | None]:
    """For `info`: (Match, None) when it passes (band None below SHOW_MIN), else (None, reason)."""
    result, reason = _evaluate(p, c, now)
    if result is None:
        return None, reason
    value, why, _, place = result
    return Match(cand=c, score=value, band=band_for(value), why=why, place=place, group_key=c.rk,
                 ledger=(c.rk_hash, c.ck_hash), more=0), None


# ------------------------------------------------------------------ groups

def _groups(cands: Sequence[Candidate], gmap: Mapping[int, str] | None) -> dict[str, list[Candidate]]:
    keys = gmap if gmap is not None else group_map(cands)
    groups = defaultdict(list)
    for c in cands:
        groups[keys.get(c.rowid, c.rk)].append(c)
    return groups


def _excluded(key: str, members: list[Candidate], exclude: frozenset[str]) -> bool:
    """4.5.2 rule 6: any member's role hash; the clone hash only for a regional group."""
    if not exclude:
        return False
    regional = key == members[0].ck
    return any(m.rk_hash in exclude for m in members) or (regional and members[0].ck_hash in exclude)


def _group_match(p: Profile, key: str, members: list[Candidate], now: float):
    """(Match, representative's P) for the group, or None when no member passes."""
    scored = [(result, c) for c in members if (result := score(p, c, now)) is not None]
    if not scored:
        return None
    (_, why, P, place), rep = max(scored, key=lambda sc: (sc[0][2], sc[0][0], sc[1].ts))
    top = max(result[0] for result, _ in scored)
    return Match(cand=rep, score=top, band=band_for(top), why=why, place=place, group_key=key,
                 ledger=(rep.rk_hash, rep.ck_hash), more=len(members) - 1), P


_SORTS = {
    "best": lambda mp: (-mp[0].score, -mp[1], -mp[0].cand.ts),
    "newest": lambda mp: (-mp[0].cand.ts, -mp[0].score),
}


def rank(p: Profile, cands: Sequence[Candidate], now: float, *, min_score: int = SHOW_MIN,
         exclude: frozenset[str] = frozenset(), gmap: Mapping[int, str] | None = None,
         sort: str = "best") -> list[Match]:
    """Groups by gmap (computed from `cands` when None); drops excluded groups (4.5.2 rule 6);
    keeps groups whose best score >= min_score. sort "best": (-score, -P, -ts);
    "newest": (-ts, -score)."""
    if sort not in _SORTS:
        raise ValueError(f"unknown sort {sort!r}; expected one of {sorted(_SORTS)}")
    kept = []
    for key, members in _groups(cands, gmap).items():
        if _excluded(key, members, exclude):
            continue
        found = _group_match(p, key, members, now)
        if found is not None and found[0].score >= min_score:
            kept.append(found)
    return [m for m, _ in sorted(kept, key=_SORTS[sort])]


# ------------------------------------------------------------------ coverage (4.7)

def field_supply(cands: Sequence[Candidate], gmap: Mapping[int, str] | None = None) -> dict[str, int]:
    """Per field id: distinct early-career groups naming it outright (conf 1.0), in the US
    or with no place listed. Every field is present, 0 when nobody posts for it."""
    keys = gmap if gmap is not None else group_map(cands)
    groups = defaultdict(set)
    for c in cands:
        if c.level in vocab.EARLY_CAREER and _us_or_unlisted(c.loc):
            for field, conf in c.fields:
                if conf >= 1.0:
                    groups[field].add(keys.get(c.rowid, c.rk))
    return {field: len(groups[field]) for field in vocab.FIELD_IDS}


def _us_or_unlisted(loc: LocInfo) -> bool:
    return not loc.places or any(p.country == "US" for p in loc.places)


def _added(current: tuple[str, ...], *extra: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*current, *extra)))


def coverage(p: Profile, cands: Sequence[Candidate], now: float, *,
             supply: Mapping[str, int], exclude: frozenset[str] = frozenset(),
             gmap: Mapping[int, str] | None = None) -> Coverage:
    """The card's "Last 30 days" line and its notices (4.7), computed by `rank` itself."""
    keys = gmap if gmap is not None else group_map(cands)

    def count(q: Profile) -> int:
        return len(rank(q, cands, now, exclude=exclude, gmap=keys))

    listed = rank(p, cands, now, exclude=exclude, gmap=keys)
    total = len(listed)
    us_extra = None if "us" in p.locations else max(
        0, count(with_changes(p, now, locations=_added(p.locations, *_US_AND_UNLISTED))) - total)
    hints = vocab.HOURLY_HINT_FIELDS.intersection(p.fields) and "hourly" not in p.levels
    hourly = max(0, count(with_changes(p, now, levels=_added(p.levels, "hourly"))) - total) if hints else 0
    thin = tuple((f, supply.get(f, 0)) for f in p.fields if supply.get(f, 0) <= THIN_FIELD_MAX)
    return Coverage(total=total, strong=sum(1 for m in listed if m.score >= STRONG_MIN),
                    us_extra=us_extra, thin=thin, hourly_hint=hourly)


def _options(p: Profile) -> list[tuple[str, str, dict[str, object]]]:
    """Every relaxation that would change something, before its gain is known."""
    options = []
    if "us" not in p.locations:
        options.append(("us", "Anywhere in the US", {"locations": _added(p.locations, *_US_AND_UNLISTED)}))
    for field in _adjacent_targets(p):
        options.append(("adjacent", f"Add {vocab.FIELD_LABELS[field]}",
                        {"fields": _added(p.fields, field)}))
    if not set(_GRAD_LEVELS) <= set(p.levels):
        options.append(("levels", "Include new grad & entry-level",
                        {"levels": _added(p.levels, *_GRAD_LEVELS)}))
    if "unlisted" not in p.locations:
        options.append(("unlisted", "Include roles without a listed location",
                        {"locations": _added(p.locations, "unlisted")}))
    if vocab.HOURLY_HINT_FIELDS.intersection(p.fields) and "hourly" not in p.levels:
        options.append(("hourly", "Include part-time & hourly", {"levels": _added(p.levels, "hourly")}))
    return options


def _adjacent_targets(p: Profile) -> tuple[str, ...]:
    """Fields one hop from the user's, not already chosen. Never pharmacy (D10)."""
    targets = (f for pf in p.fields for f in vocab.FIELD_ADJACENT.get(pf, {}))
    return tuple(dict.fromkeys(f for f in targets if f not in p.fields and f != "pharmacy"))


def _relaxable_pool(p: Profile, cands: Sequence[Candidate], keys: Mapping[int, str],
                    exclude: frozenset[str]) -> list[Candidate]:
    """
    The candidates any relaxation could ever show, with excluded groups removed whole.

    Every relaxation only widens levels, places or fields, so a posting that fails the
    widest levels and places fails them all. Dropping those up front makes the five or
    so re-ranks cheap enough for the event loop; exclusion is decided here on the full
    groups first, so trimming members cannot let a hidden group back in.
    """
    widest = dataclasses.replace(p, locations=_added(p.locations, *_US_AND_UNLISTED),
                                 levels=_added(p.levels, *_GRAD_LEVELS, "hourly"))
    return [c for key, members in _groups(cands, keys).items()
            if not _excluded(key, members, exclude)
            for c in members if _hard_filter(widest, c)[0] is not None]


def relaxations(p: Profile, cands: Sequence[Candidate], now: float, *,
                min_score: int = SHOW_MIN, exclude: frozenset[str] = frozenset(),
                gmap: Mapping[int, str] | None = None) -> list[Relaxation]:
    """At most three changes that would each add roles, largest gain first (4.7, J8)."""
    keys = gmap if gmap is not None else group_map(cands)
    pool = _relaxable_pool(p, cands, keys, exclude)

    def count(q: Profile) -> int:
        return len(rank(q, pool, now, min_score=min_score, gmap=keys))

    base = count(p)
    best: dict[str, Relaxation] = {}
    for rid, label, changes in _options(p):
        gain = count(with_changes(p, now, **changes)) - base
        if gain > 0 and (rid not in best or gain > best[rid].gain):
            best[rid] = Relaxation(id=rid, label=label, gain=gain, changes=MappingProxyType(changes))
    return sorted(best.values(), key=lambda r: -r.gain)[:_MAX_RELAXATIONS]


# ------------------------------------------------------------------ browse (4.7)

def _browse_fit(c: Candidate, field: str | None, levels: frozenset[str], prefs: tuple[str, ...],
                oldest: float) -> tuple[float, str] | None:
    if c.level not in levels or c.ts < oldest:
        return None
    if field is not None and not any(f == field and conf >= _BROWSE_FIELD_CONF for f, conf in c.fields):
        return None
    P, place = location_fit(c.loc, prefs)
    return None if P is None else (P, place)


def browse(cands: Sequence[Candidate], *, field: str | None, levels: Sequence[str],
           locations: Sequence[str], days: int, now: float,
           gmap: Mapping[int, str] | None = None, limit: int | None = BROWSE_MAX) -> list[Match]:
    """`/internships recent`: no profile, no score. Grouped, newest first, at most `limit`
    (None: all of them, so a caller can count what it does not show)."""
    wanted, prefs, oldest = frozenset(levels), tuple(locations), now - days * DAY_S
    found = []
    for key, members in _groups(cands, gmap).items():
        fits = [(fit, c) for c in members if (fit := _browse_fit(c, field, wanted, prefs, oldest))]
        if not fits:
            continue
        (_, place), rep = max(fits, key=lambda fc: (fc[0][0], fc[1].ts))
        found.append(Match(cand=rep, score=0, band=None, why=(), place=place, group_key=key,
                           ledger=(rep.rk_hash, rep.ck_hash), more=len(members) - 1))
    return sorted(found, key=lambda m: -m.cand.ts)[:limit]
