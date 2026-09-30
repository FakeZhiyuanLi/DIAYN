"""
intern_profile.py
~~~~~~~~~~~~~~~~~
What one person asked the internship finder for, and every rule for changing it.

A Profile is a frozen value — vocabulary ids, a graduation month, a few typed
keywords, and the delivery bookkeeping `intern_store` keeps beside them — and
every change is a function from one Profile to a new one. All the ways a user
edits a profile (the card's selects, Edit details, More filters, the relax
buttons, a DM's hide menu) end in `with_changes`, so a value outside the
vocabulary is dropped in one place and no path can store it. The two form
parsers return their problem sentences (spec J4) for the same reason: what
counts as "not a major" is decided next to what counts as a major.

Pure: the standard library plus `intern_vocab`, `intern_location`,
`intern_taxonomy` and `resume_lexicon`, so it imports under bare `python3`.
"""

import dataclasses
import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import intern_vocab as vocab
from intern_location import parse_state_list
from intern_taxonomy import company_norm
from resume_lexicon import (MAJOR_BY_ID, SKILL_BY_ID, fields_for, resolve_major_words,
                            resolve_skill_words)

#: Bump when the disclosure (intern_text.disclosure_text) changes in a way that
#: needs fresh consent: every profile below it sees the consent screen on its
#: next upload (spec 2.4). 2: the consent screen gained the Gemini fit check's
#: note (intern_text.fit_note), which says what is sent to Google.
DISCLOSURE_VERSION = 2

SOURCES = ("resume", "pasted", "manual", "migrated")
CADENCES = ("hourly", "daily", "weekly", "off")
MIN_SCORES = frozenset(score for score, _ in vocab.MIN_SCORE_CHOICES)
DEFAULT_ALERTS, DEFAULT_ALERT_HOUR, DEFAULT_MIN_SCORE = "daily", 9, 60


@dataclass(frozen=True)
class Profile:
    """One `intern_profiles` row, field for field and in column order."""

    user_id: int
    source: str                    # resume | pasted | manual | migrated
    consent_version: int
    majors: tuple[str, ...]
    minors: tuple[str, ...]
    degree: str | None
    grad_year: int | None
    grad_month: int | None
    skills: tuple[str, ...]
    keywords: tuple[str, ...]
    fields: tuple[str, ...]
    fields_locked: bool
    levels: tuple[str, ...]
    levels_locked: bool
    locations: tuple[str, ...]
    terms: tuple[str, ...]
    companies_only: tuple[str, ...]
    companies_hidden: tuple[str, ...]
    alerts: str                    # hourly | daily | weekly | off
    alert_hour: int
    min_score: int                 # 45 | 60 | 75
    paused_until: float | None
    cursor: float
    last_run_at: float | None
    last_sent_at: float | None
    last_quiet_at: float | None
    dm_failures: int
    intro_pending: bool
    left_at: float | None
    expiry_warned_at: float | None
    created_at: float
    updated_at: float
    active_at: float
    access_lapsed_at: float | None     # added in place (intern_store._ADDED_COLUMNS)
    fit_check: bool                    # the Gemini check (intern_fit); added in place, on
    fit_notice_at: float | None        # when its notice was shown; added in place, NULL


#: What a user may change (spec 3.1 "Writes", less the two timestamps every
#: change sets). `alerts` and `alert_hour` are here for drafts only: on a saved
#: profile they change through `intern_store.set_alerts`, which owns the cursor.
EDITABLE: frozenset[str] = frozenset({
    "source", "consent_version", "majors", "minors", "degree", "grad_year", "grad_month",
    "skills", "keywords", "fields", "fields_locked", "levels", "levels_locked", "locations",
    "terms", "companies_only", "companies_hidden", "min_score", "alerts", "alert_hour",
    "fit_check"})

_KEEP = object()   # a normaliser's answer for "leave the current value"
_STATE = "st:"
_YEARS, _MONTHS_OF_YEAR, _LAST_HOUR = range(2000, 2101), range(1, 13), 23
_TERM = re.compile(r"(Winter|Spring|Summer|Fall) (20\d\d)")
_SEASONS = (("Winter", 1), ("Spring", 3), ("Summer", 6), ("Fall", 9))
# Levels by months to graduation: more than a year out, internships; within
# 18 months either side, new-grad and entry roles too; longer ago, only those.
_INTERN_ONLY_AFTER, _GRADUATED_BEFORE = 12, -18
_BOTH_LEVELS = ("intern", "coop", "new_grad", "entry")
_GRADUATE_LEVELS = ("new_grad", "entry")
_LEGACY_US = ("us", "unlisted", "remote_us")


# ------------------------------------------------------------------ building

def new_profile(user_id: int, now: float, *, source: str, cursor: float) -> Profile:
    """A profile with nothing chosen yet and the documented defaults (D1, D13, D23)."""
    _check_source(source)
    return Profile(
        user_id=user_id, source=source, consent_version=0, majors=(), minors=(), degree=None,
        grad_year=None, grad_month=None, skills=(), keywords=(), fields=(), fields_locked=False,
        levels=vocab.DEFAULT_LEVELS, levels_locked=False, locations=vocab.DEFAULT_LOCATIONS,
        terms=(), companies_only=(), companies_hidden=(), alerts=DEFAULT_ALERTS,
        alert_hour=DEFAULT_ALERT_HOUR, min_score=DEFAULT_MIN_SCORE, paused_until=None,
        cursor=cursor, last_run_at=now, last_sent_at=None, last_quiet_at=None, dm_failures=0,
        intro_pending=False, left_at=None, expiry_warned_at=None, created_at=now,
        updated_at=now, active_at=now, access_lapsed_at=None, fit_check=True,
        fit_notice_at=None)


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"unknown profile source {source!r}; expected one of {SOURCES}")


def default_levels(grad_year: int | None, grad_month: int | None, today: date) -> tuple[str, ...]:
    """What to look for, by months until graduation; a missing month counts as June."""
    if grad_year is None:
        return vocab.DEFAULT_LEVELS
    months = (grad_year - today.year) * 12 + (grad_month or 6) - today.month
    if months > _INTERN_ONLY_AFTER:
        return vocab.DEFAULT_LEVELS
    return _BOTH_LEVELS if months >= _GRADUATED_BEFORE else _GRADUATE_LEVELS


def from_draft(user_id: int, draft: Mapping, now: float, *, source: str, cursor: float,
               today: date, existing: Profile | None = None) -> Profile:
    """
    A parsed resume as a profile, consented at DISCLOSURE_VERSION.

    With `existing` (Replace resume), filters, alerts and bookkeeping are kept
    and only what a resume says is replaced: study, graduation and skills, plus
    fields and levels unless the user set those by hand. A resume that names no
    field keeps the fields the profile had, so the replacement stays saveable.
    The draft came out of a subprocess, so its keys are rebuilt rather than trusted.
    """
    _check_source(source)
    study = _draft_study(draft)
    levels = default_levels(study["grad_year"], study["grad_month"], today)
    if existing is None:
        base = new_profile(user_id, now, source=source, cursor=cursor)
        return dataclasses.replace(base, consent_version=DISCLOSURE_VERSION, levels=levels, **study)
    replaced = {key: value for key, value in study.items()
                if not (key == "fields" and (existing.fields_locked or not value))}
    return dataclasses.replace(
        existing, **replaced, user_id=user_id, source=source, consent_version=DISCLOSURE_VERSION,
        levels=existing.levels if existing.levels_locked else levels, updated_at=now, active_at=now)


def _draft_study(draft: Mapping) -> dict[str, object]:
    get = draft.get if isinstance(draft, Mapping) else (lambda _key: None)
    year = get("grad_year") if _is_int(get("grad_year"), _YEARS) else None
    majors = _known(get("majors"), MAJOR_BY_ID, vocab.MAX_MAJORS)
    return {
        "majors": majors,
        "minors": _minors(get("minors"), majors),
        "degree": get("degree") if get("degree") in vocab.DEGREE_IDS else None,
        "grad_year": year,
        "grad_month": get("grad_month") if year and _is_int(get("grad_month"), _MONTHS_OF_YEAR) else None,
        "skills": _known(get("skills"), SKILL_BY_ID, vocab.MAX_SKILLS),
        "fields": vocab.valid_ids("field", get("fields"))[:vocab.MAX_FIELDS],
    }


# ------------------------------------------------------------------ changing

def with_changes(p: Profile, now: float, **changes: object) -> Profile:
    """
    `p` with `changes` applied and normalised; `p` itself is untouched.

    Ids outside the vocabulary are dropped and lists capped; a choice that is
    not one of the listed values (a min_score of 50, an unknown cadence or
    degree) leaves the current value. Setting `fields` or `levels` also locks
    them against re-derivation unless the lock is passed too. A key outside
    EDITABLE is a programming error: ValueError.
    """
    unknown = sorted(set(changes) - EDITABLE)
    if unknown:
        raise ValueError(f"not user-editable: {unknown}")
    normalised = {key: _NORMALISERS[key](raw, p) for key, raw in changes.items()}
    values = {key: value for key, value in normalised.items() if value is not _KEEP}
    locks = {lock: True for key, lock in (("fields", "fields_locked"), ("levels", "levels_locked"))
             if key in changes and lock not in changes}
    changed = dataclasses.replace(p, **values, **locks, updated_at=now, active_at=now)
    return dataclasses.replace(changed, minors=_minors(changed.minors, changed.majors),
                               grad_month=changed.grad_month if changed.grad_year else None)


def can_save(p: Profile) -> bool:
    """A profile with no field matches nothing, so Save waits for one (D5)."""
    return len(p.fields) >= 1


def _members(values: object) -> tuple:
    """The members of a list-like value; nothing for a string or a non-collection."""
    if values is None or isinstance(values, (str, bytes, bytearray)):
        return ()
    try:
        return tuple(values)
    except TypeError:
        return ()


def _known(values: object, table: Mapping, cap: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in _members(values) if isinstance(v, str) and v in table))[:cap]


def _minors(values: object, majors: tuple[str, ...]) -> tuple[str, ...]:
    """Known minors that are not also majors: a subject is shown once."""
    known = _known(values, MAJOR_BY_ID, vocab.MAX_MAJORS + vocab.MAX_MINORS)
    return tuple(m for m in known if m not in majors)[:vocab.MAX_MINORS]


def _is_int(value: object, allowed: range | None = None) -> bool:
    """A real int (a bool is not one), inside `allowed` when given."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and (allowed is None or value in allowed))


def _optional(test):
    """A normaliser for a nullable value: None clears it, a valid value sets it, else keep."""
    return lambda value, _p: value if value is None or test(value) else _KEEP


def _locations(value: object, _p: Profile) -> tuple[str, ...]:
    ids = vocab.valid_ids("location", value)
    presets = tuple(i for i in ids if not i.startswith(_STATE))
    if not presets:
        return vocab.DEFAULT_LOCATIONS
    return presets + tuple(i for i in ids if i.startswith(_STATE))[:vocab.MAX_STATES]


def _companies(cap: int):
    def normalise(value: object, _p: Profile) -> tuple[str, ...]:
        names = (company_norm(v) for v in _members(value) if isinstance(v, str))
        return tuple(dict.fromkeys(n for n in names if n))[:cap]
    return normalise


def _keywords(value: object, _p: Profile) -> tuple[str, ...]:
    cleaned = (vocab.clean_keyword(v) for v in _members(value))
    return tuple(dict.fromkeys(k for k in cleaned if k))[:vocab.MAX_KEYWORDS]


_NORMALISERS = {
    "source": lambda v, _p: v if v in SOURCES else _KEEP,
    "consent_version": lambda v, _p: v if _is_int(v) and v >= 0 else _KEEP,
    "majors": lambda v, _p: _known(v, MAJOR_BY_ID, vocab.MAX_MAJORS),
    # Capped after the majors are taken out, by with_changes.
    "minors": lambda v, _p: _known(v, MAJOR_BY_ID, vocab.MAX_MAJORS + vocab.MAX_MINORS),
    "degree": _optional(lambda v: v in vocab.DEGREE_IDS),
    "grad_year": _optional(lambda v: _is_int(v, _YEARS)),
    "grad_month": _optional(lambda v: _is_int(v, _MONTHS_OF_YEAR)),
    "skills": lambda v, _p: _known(v, SKILL_BY_ID, vocab.MAX_SKILLS),
    "keywords": _keywords,
    # A migrated profile may hold 11 fields (3.3): it keeps them but cannot grow.
    "fields": lambda v, p: vocab.valid_ids("field", v)[:max(vocab.MAX_FIELDS, len(p.fields))],
    "fields_locked": lambda v, _p: bool(v),
    "levels": lambda v, _p: vocab.valid_ids("level", v) or _KEEP,
    "levels_locked": lambda v, _p: bool(v),
    "locations": _locations,
    "terms": lambda v, _p: tuple(dict.fromkeys(
        t for t in _members(v) if isinstance(t, str) and _TERM.fullmatch(t)))[:vocab.MAX_TERMS],
    "companies_only": _companies(vocab.MAX_COMPANIES_ONLY),
    "companies_hidden": _companies(vocab.MAX_COMPANIES_HIDDEN),
    "min_score": lambda v, _p: v if _is_int(v) and v in MIN_SCORES else _KEEP,
    "alerts": lambda v, _p: v if v in CADENCES else _KEEP,
    "alert_hour": lambda v, _p: min(max(v, 0), _LAST_HOUR) if _is_int(v) else _KEEP,
    # Only a real True or False: "no" or 0 from a stale client leaves it as it is.
    "fit_check": lambda v, _p: v if isinstance(v, bool) else _KEEP,
}


# ------------------------------------------------------------------ dates and terms

# The same month and season table the resume extractor reads (spec 4.4.4).
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_SEASON_MONTH = {"spring": 6, "summer": 8, "fall": 12, "autumn": 12, "winter": 3}
_MON = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|spring|summer|fall|autumn|winter)")
_GRAD_TEXT = re.compile(r"(?:" + _MON + r"\.?,?\s*)?'?(?P<year>\d{4})"
                        r"|(?P<num>\d{1,2})\s*/\s*(?P<nyear>\d{4})", re.I)
_YEARS_BACK, _YEARS_AHEAD = 10, 6


def parse_grad(text: str, today: date) -> tuple[int, int] | None:
    """"June 2028", "Spring 2027", "06/2028" or "2028" (June) as (year, month), or None."""
    match = _GRAD_TEXT.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None:
        return None
    if match.group("nyear"):
        year, month = int(match.group("nyear")), int(match.group("num"))
    else:
        word = (match.group("mon") or "").lower()
        year, month = int(match.group("year")), _SEASON_MONTH.get(word) or _MONTHS.get(word[:3], 6)
    in_range = today.year - _YEARS_BACK <= year <= today.year + _YEARS_AHEAD
    return (year, month) if in_range and month in _MONTHS_OF_YEAR else None


def upcoming_terms(today: date, n: int = 4) -> tuple[str, ...]:
    """The next `n` seasons that start after this month: the Terms select's options."""
    years = range(today.year, today.year + n // len(_SEASONS) + 2)
    ahead = (f"{season} {year}" for year in years for season, start in _SEASONS
             if (year, start) > (today.year, today.month))
    return tuple(ahead)[:n]


def term_after_graduation(p: Profile, term_year: int | None, term_month: int | None) -> bool:
    """True when a term starts after the user graduates (spec 4.5.2 rule 4)."""
    if None in (p.grad_year, p.grad_month, term_year, term_month):
        return False
    return (term_year, term_month) > (p.grad_year, p.grad_month)


# ------------------------------------------------------------------ the two modals (J4)

_PARTS = re.compile(r"[,;\n]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MARKDOWN = re.compile(r"([\\*_~`|>])")
_ECHO_MAX = 60
_DEGREE_CHOICE = {"none": None, **{degree: degree for degree in vocab.DEGREE_IDS}}
_MIN_SCORE_TEXT = {str(score): score for score in MIN_SCORES}


def _filled(text: object) -> bool:
    return isinstance(text, str) and bool(text.strip())


def _parts(text: object) -> tuple[str, ...]:
    pieces = (piece.strip() for piece in _PARTS.split(text if isinstance(text, str) else ""))
    return tuple(piece for piece in pieces if piece)


def _echo(text: str) -> str:
    """Typed text quoted back to its author: one visible line that cannot format."""
    return _MARKDOWN.sub(r"\\\1", _CONTROL.sub("", vocab.norm_text(text))[:_ECHO_MAX])


def _kept_as(text: str, keywords: tuple[str, ...]) -> str | None:
    cleaned = vocab.clean_keyword(text)
    return cleaned if cleaned in keywords else None


def _sentence(template: str, items: Sequence[str]) -> tuple[str, ...]:
    """`template` with the items listed, or nothing when there are none."""
    return (template.format(", ".join(items)),) if items else ()


def parse_details_form(majors_text: str, degree: str | None, grad_text: str, skills_text: str,
                       keywords_text: str, *, today: date, current: Profile,
                       ) -> tuple[dict[str, object], tuple[str, ...]]:
    """
    Edit details as (changes for with_changes, problem sentences).

    A blank box leaves its value alone, except Keywords, where blank means
    none. A major or skill the lexicon does not know is kept as a keyword when
    it can be one, and the sentence says which happened: it never claims a word
    was kept when it was not.
    """
    majors = resolve_major_words(majors_text) if _filled(majors_text) else None
    skills = resolve_skill_words(skills_text) if _filled(skills_text) else None
    unknown_majors, unknown_skills = majors[2] if majors else (), skills[1] if skills else ()
    grad, grad_problems = _grad_changes(grad_text, today, current)
    typed = _parts(keywords_text)
    keywords = _keywords(typed + unknown_majors + unknown_skills, current)
    changes = {**({"majors": majors[0], "minors": majors[1]} if majors else {}),
               **({"skills": skills[0]} if skills else {}),
               **({"degree": _DEGREE_CHOICE[degree]} if degree in _DEGREE_CHOICE else {}),
               **grad, "keywords": keywords}
    problems = (_major_problems(unknown_majors, keywords) + grad_problems
                + _word_problems(unknown_skills, typed, keywords))
    return {**changes, **_rederived_fields(changes, current)}, problems


def _grad_changes(grad_text: str, today: date, current: Profile):
    if not _filled(grad_text):
        return {}, ()
    parsed = parse_grad(grad_text, today)
    if parsed is None:
        return {}, (f"I couldn't read '{_echo(grad_text)}' as a date. Try 'June 2028'.",)
    changes = {"grad_year": parsed[0], "grad_month": parsed[1]}
    if parsed == (current.grad_year, current.grad_month) or current.levels_locked:
        return changes, ()
    return {**changes, "levels": default_levels(*parsed, today), "levels_locked": False}, ()


def _rederived_fields(changes: Mapping[str, object], current: Profile) -> dict[str, object]:
    """New majors suggest new fields, unless the user picked fields by hand."""
    majors = changes.get("majors", current.majors)
    if current.fields_locked or majors == current.majors:
        return {}
    fields = fields_for(majors, changes.get("skills", current.skills))
    # Never re-derive to nothing: a saved profile with no field matches nothing.
    return {"fields": fields, "fields_locked": False} if fields else {}


def _major_problems(unknown: tuple[str, ...], keywords: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"I didn't recognise '{_echo(part)}' as a major; kept it as a keyword."
        if _kept_as(part, keywords) else f"I didn't recognise '{_echo(part)}' as a major."
        for part in unknown)


def _word_problems(unknown_skills: tuple[str, ...], typed: tuple[str, ...],
                   keywords: tuple[str, ...]) -> tuple[str, ...]:
    kept = tuple(dict.fromkeys(k for k in (_kept_as(s, keywords) for s in unknown_skills) if k))
    return (_sentence("Not in my skills list, so kept as keywords: {}.", kept)
            + _sentence("Not in my skills list, so left out: {}.",
                        [_echo(s) for s in unknown_skills if not _kept_as(s, keywords)])
            + _sentence("I couldn't keep these keywords: {}. A keyword is 2 to 30 letters, digits "
                        f"or + # . / & -, and I keep at most {vocab.MAX_KEYWORDS}.",
                        [_echo(t) for t in typed if not _kept_as(t, keywords)]))


def parse_filters_form(states_text: str, terms: Sequence[str], hide_text: str, only_text: str,
                       min_score: str, *, today: date, current: Profile,
                       known_companies: Mapping[str, str],
                       ) -> tuple[dict[str, object], tuple[str, ...]]:
    """
    More filters as (changes for with_changes, problem sentences).

    Typed states replace every stored state and keep the presets. Companies are
    stored as normalised names, and only when tracked (`known_companies` maps
    norm -> display name) or already on the user's list, so a board that went
    away does not silently take a user's filter with it.
    """
    good, bad = parse_state_list(states_text)
    presets = tuple(t for t in current.locations if not t.startswith(_STATE))
    hidden, hide_problems = _company_changes(hide_text, current.companies_hidden, known_companies,
                                             vocab.MAX_COMPANIES_HIDDEN, "hidden companies")
    only, only_problems = _company_changes(only_text, current.companies_only, known_companies,
                                           vocab.MAX_COMPANIES_ONLY, "only-these companies")
    allowed = set(upcoming_terms(today)) | set(current.terms)
    chosen = tuple(dict.fromkeys(t for t in _members(terms) if isinstance(t, str) and t in allowed))
    score = _MIN_SCORE_TEXT.get(str(min_score).strip())
    changes = {"locations": presets + good[:vocab.MAX_STATES], "terms": chosen[:vocab.MAX_TERMS],
               "companies_hidden": hidden, "companies_only": only,
               **({"min_score": score} if score else {})}
    state_problems = tuple(f"Not a US state: '{_echo(token)}'." for token in bad) + _sentence(
        f"I keep at most {vocab.MAX_STATES} states, so I left out: {{}}.",
        [token[len(_STATE):] for token in good[vocab.MAX_STATES:]])
    return changes, state_problems + hide_problems + only_problems


def _company_changes(text: str, stored: tuple[str, ...], known: Mapping[str, str], cap: int,
                     what: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    named = tuple((name, n) for name, n in ((name, company_norm(name)) for name in _parts(text)) if n)
    usable = tuple(dict.fromkeys(n for _, n in named if n in known or n in stored))
    unknown = dict.fromkeys(name for name, n in named if n not in known and n not in stored)
    problems = tuple(_untracked(name, known) for name in unknown) + _sentence(
        f"I keep at most {cap} {what}, so I left out: {{}}.",
        [_echo(known.get(n, n)) for n in usable[cap:]])
    return usable[:cap], problems


def _untracked(name: str, known: Mapping[str, str]) -> str:
    by_lower = {display.lower(): display for display in known.values()}
    close = difflib.get_close_matches(name.lower(), list(by_lower), n=1, cutoff=0.75)
    if close:
        return f"I don't track '{_echo(name)}'. Did you mean {_echo(by_lower[close[0]])}?"
    return f"I don't track '{_echo(name)}' yet. Ask whoever runs this bot to add it."


# ------------------------------------------------------------------ legacy (3.3)

def legacy_fields(categories: str | None) -> tuple[str, ...]:
    """The old tracker's category CSV as fields; empty, `other` or nothing known = all tech."""
    parts = categories.split(",") if isinstance(categories, str) else ()
    named = tuple(p.strip().lower() for p in parts if p.strip())
    if not named or "other" in named:
        return vocab.LEGACY_ALL_TECH
    fields = (f for name in named for f in vocab.LEGACY_CATEGORY_FIELDS.get(name, ()))
    return tuple(dict.fromkeys(fields)) or vocab.LEGACY_ALL_TECH


def legacy_locations(us_only: int | None) -> tuple[str, ...]:
    """The old US-only switch dropped only non-US roles, so only `abroad` depends on it."""
    return _LEGACY_US if us_only == 1 else _LEGACY_US + ("abroad",)
