"""
intern_vocab.py
~~~~~~~~~~~~~~~
Every word the internship finder is allowed to store, and the two cleaners all
text passes through first.

A profile holds ids, never prose: a field is `mechanical`, a place is `oc` or
`st:WA`, a level is `coop`. This module is the closed list those ids come from.
The card's selects are built from these tuples, the resume worker's output is
checked against them, and `valid_ids` is how anything read back from a JSON
column, a select or a subprocess gets in. An id that is not in here is dropped,
not stored and fixed later.

Split out for the reason `report_text.py` is: it is data and string handling
with nothing else in it, so every other finder module can import it under bare
`python3`, and so can the resume worker, which starts Python with `-I` and needs
nothing beyond the standard library and these pure modules.

The tables marked verbatim were measured against a month of real postings
(spec section 4). Their order is select order and their weights are measured;
edit them as data, not as style.
"""

import re
import unicodedata
from collections.abc import Iterable

from intern_places import US_STATES


# ------------------------------------------------------------------ text

#: U+200B-U+200D, U+2060 and U+FEFF. Invisible, survive NFKC, and turn up in
#: titles pasted from rich text — where they split a word no regex then finds.
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
#: En, em and figure dash. Boards use all three where a regex expects `-`.
_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2012": "-"})
_WHITESPACE = re.compile(r"\s+")
#: C0, DEL and C1. For a keyword, which is matched against titles and shown
#: back on the card, none of them carries meaning.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def norm_text(s: "str | None") -> str:
    """
    A title or location as every classifier expects to read it.

    NFKC (so a ligature or a full-width letter is the plain one), zero-width
    characters gone, the three dashes turned into `-`, whitespace collapsed and
    trimmed. The classifiers are cached on the raw string and all of them call
    this first, so two spellings of one title cannot classify differently.

    **Never raises.** A title column can be NULL, and a classifier that threw
    on one would take the whole list down with it; anything that is not text
    reads as the empty string, which every classifier already handles.
    """
    if not isinstance(s, str):
        return ""
    folded = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", s))
    return _WHITESPACE.sub(" ", folded.translate(_DASHES)).strip()


# ------------------------------------------------------------------ fields (4.2)

#: Verbatim, in select order. The card lists them in this order and the Why
#: line names them by these labels.
FIELDS: tuple[tuple[str, str], ...] = (
    ("software", "Software engineering"),
    ("data_ml", "Data science & ML"),
    ("analytics", "Data & business analytics"),
    ("security", "Cybersecurity"),
    ("it", "IT & systems"),
    ("electrical", "Electrical & computer hardware"),
    ("mechanical", "Mechanical engineering"),
    ("aerospace", "Aerospace engineering"),
    ("manufacturing", "Manufacturing, industrial & quality"),
    ("civil", "Civil & environmental engineering"),
    ("chem_materials", "Chemical engineering & materials"),
    ("biology_lab", "Biology & lab research"),
    ("healthcare", "Healthcare & clinical"),
    ("pharmacy", "Pharmacy"),
    ("finance", "Finance & accounting"),
    ("quant", "Quant research & trading"),
    ("business_ops", "Business operations & strategy"),
    ("supply_chain", "Supply chain & logistics"),
    ("marketing", "Marketing & communications"),
    ("sales", "Sales & customer success"),
    ("product", "Product & program management"),
    ("design", "Design & UX"),
    ("hr", "People & HR"),
    ("legal_policy", "Legal, policy & compliance"),
)
FIELD_IDS: tuple[str, ...] = tuple(f for f, _ in FIELDS)
#: Plus `engineering_general`, which the title classifier produces for
#: "Engineering Intern" with no discipline named. It is never selectable.
FIELD_LABELS: dict[str, str] = dict(FIELDS, engineering_general="Engineering (general)")
ENGINEERING_FIELDS: frozenset[str] = frozenset({"software", "electrical", "mechanical", "aerospace",
                                                "manufacturing", "civil", "chem_materials"})
# One hop. Nothing is adjacent INTO pharmacy (a Pharmacy Intern needs pharmacy-school
# enrolment). Weight = how much a profile field counts toward the posting field.
FIELD_ADJACENT: dict[str, dict[str, float]] = {
    "software": {"data_ml": .6, "security": .6, "it": .5, "electrical": .3, "product": .3, "quant": .3},
    "data_ml": {"software": .6, "analytics": .7, "quant": .4},
    "analytics": {"data_ml": .6, "business_ops": .5, "finance": .4, "supply_chain": .4, "marketing": .3},
    "security": {"software": .6, "it": .6},
    "it": {"security": .5, "software": .4},
    "electrical": {"software": .3, "aerospace": .4, "manufacturing": .4, "mechanical": .3},
    "mechanical": {"aerospace": .7, "manufacturing": .6, "civil": .3, "electrical": .3},
    "aerospace": {"mechanical": .7, "electrical": .4, "manufacturing": .4},
    "manufacturing": {"mechanical": .6, "supply_chain": .5, "chem_materials": .4},
    "civil": {"mechanical": .3, "manufacturing": .3},
    "chem_materials": {"manufacturing": .5, "biology_lab": .4},
    "biology_lab": {"healthcare": .5, "chem_materials": .4},
    "healthcare": {"biology_lab": .4},
    "pharmacy": {"healthcare": .5},
    "finance": {"quant": .5, "analytics": .5, "business_ops": .5},
    "quant": {"finance": .5, "data_ml": .5, "software": .4},
    "business_ops": {"supply_chain": .6, "finance": .4, "analytics": .5, "product": .4,
                     "marketing": .4, "sales": .4, "hr": .3},
    "supply_chain": {"business_ops": .6, "manufacturing": .4, "analytics": .4},
    "marketing": {"sales": .5, "design": .4, "business_ops": .4},
    "sales": {"marketing": .5, "business_ops": .4},
    "product": {"software": .4, "business_ops": .4, "design": .4},
    "design": {"product": .4, "marketing": .4},
    "hr": {"business_ops": .4},
    "legal_policy": {"finance": .3, "hr": .3},
}
#: The fields whose card suggests adding "Part-time & hourly" (D11): for
#: pre-pharmacy students it is the only sizeable pool.
HOURLY_HINT_FIELDS: frozenset[str] = frozenset({"pharmacy", "healthcare", "biology_lab"})


# ------------------------------------------------------------------ levels (4.1.1)

#: Verbatim, in select order. `experienced` and `excluded` are rule levels the
#: classifier produces, labelled below, and never offered.
LEVELS: tuple[tuple[str, str], ...] = (
    ("intern", "Internships"),
    ("coop", "Co-ops & placements"),
    ("new_grad", "New grad & rotational programs"),
    ("entry", "Entry-level jobs (Associate, I, Junior)"),
    ("apprentice", "Apprenticeships & trainee roles"),
    ("hourly", "Part-time & hourly (store, pharmacy tech)"),
    ("unspecified", "Full-time, level not stated (often mid-career)"),
)
LEVEL_IDS: tuple[str, ...] = tuple(i for i, _ in LEVELS)
LEVEL_LABELS: dict[str, str] = dict(LEVELS, experienced="Experienced", excluded="Not a job posting")
EARLY_CAREER: frozenset[str] = frozenset({"intern", "coop", "new_grad", "entry", "apprentice"})
DEFAULT_LEVELS: tuple[str, ...] = ("intern", "coop")


# ------------------------------------------------------------------ degrees

#: The six `intern_profiles.degree` allows. "Prefer not to say" is the absence
#: of one, not a seventh id.
DEGREES: tuple[tuple[str, str], ...] = (
    ("bachelor", "Bachelor's"), ("master", "Master's"), ("phd", "PhD"),
    ("mba", "MBA"), ("pharmd", "PharmD"), ("associate", "Associate"))
DEGREE_IDS: tuple[str, ...] = tuple(d for d, _ in DEGREES)
#: A role whose title reserves it for graduate students costs 25 points for
#: anyone without one of these (4.5.3 `G`); an unknown degree counts as none.
GRAD_DEGREES: frozenset[str] = frozenset({"master", "mba", "pharmd", "phd"})


# ------------------------------------------------------------------ locations (4.3.5)

#: In select order: 23, so the Where select fits Discord's 25 with room to
#: spare. The metro ids are `intern_places.METRO_CITIES` keys by design. Any
#: US state is also available as `st:XX`, added in More filters.
LOCATION_PRESETS: tuple[tuple[str, str], ...] = (
    ("us", "Anywhere in the US"),
    ("remote_us", "Remote (US)"),
    ("oc", "Orange County / Irvine"),
    ("la", "Los Angeles area"),
    ("sd", "San Diego area"),
    ("ie", "Inland Empire"),
    ("socal", "All of Southern California"),
    ("bay", "Bay Area"),
    ("sac", "Sacramento area"),
    ("ca", "Anywhere in California"),
    ("sea", "Seattle area"),
    ("pdx", "Portland area"),
    ("phx", "Phoenix area"),
    ("den", "Denver / Boulder"),
    ("tx", "Texas"),
    ("chi", "Chicago area"),
    ("dc", "Washington DC area"),
    ("nyc", "New York City area"),
    ("bos", "Boston area"),
    ("atl", "Atlanta area"),
    ("fl", "Florida"),
    ("abroad", "Outside the US"),
    ("unlisted", "Include roles that don't list a location"),
)
LOCATION_PRESET_IDS: tuple[str, ...] = tuple(p for p, _ in LOCATION_PRESETS)
#: D1: anywhere in the US, plus roles that list no location. Never derived
#: from the resume — a hometown is not a preference.
DEFAULT_LOCATIONS: tuple[str, ...] = ("us", "unlisted")
_STATE_TOKEN = "st:"


# ------------------------------------------------------------------ alerts

#: What `intern_profiles.alerts` allows.
_CADENCES = ("hourly", "daily", "weekly", "off")
_LAST_HOUR = 23
#: The hour the fixed labels of `hourly` and `off` are listed under. Their
#: stored hour is kept for when alerts go back to daily and means nothing now.
_LISTED_HOUR = 9
ALERT_CHOICES: tuple[tuple[str, str], ...] = (   # (value "alerts:hour", label)
    ("hourly:9", "Hourly (at most one DM an hour)"),
    ("daily:9", "Daily at 9am Pacific"),
    ("daily:17", "Daily at 5pm Pacific"),
    ("weekly:9", "Weekly, Mondays at 9am Pacific"),
    ("off:9", "Off"))
_ALERT_LABELS = dict(ALERT_CHOICES)
#: Exactly what `alert_choice_value` writes: no padding, no leading zero, ASCII
#: digits only (`\d` would also take Arabic-Indic ones, which `int` accepts).
_ALERT_VALUE = re.compile(r"(hourly|daily|weekly|off):(0|[1-9][0-9]?)")

#: Values match the CHECK on `intern_profiles.min_score`.
MIN_SCORE_CHOICES: tuple[tuple[int, str], ...] = ((75, "Strong matches only"),
    (60, "Good and strong matches (recommended)"), (45, "Everything relevant"))


def alert_choice_value(alerts: str, alert_hour: int) -> str:
    """The Alerts select's value for a stored cadence and hour: `daily:17`."""
    return f"{alerts}:{alert_hour}"


def parse_alert_choice(value: str) -> "tuple[str, int] | None":
    """
    `daily:17` -> `("daily", 17)`. None for anything `alert_choice_value`
    could not have produced — the value comes back from a client, and the
    store has a CHECK behind both halves.
    """
    if not isinstance(value, str):
        return None
    match = _ALERT_VALUE.fullmatch(value)
    if match is None:
        return None
    hour = int(match.group(2))
    return (match.group(1), hour) if hour <= _LAST_HOUR else None


def _clock(hour: int) -> str:
    """0 -> "12am", 9 -> "9am", 12 -> "12pm", 19 -> "7pm"."""
    return f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"


def alert_choice_label(alerts: str, alert_hour: int) -> str:
    """
    The ALERT_CHOICES label, or for an hour not listed (set with
    `/internships ping hour:`) "Daily at 7am Pacific" / "Weekly, Mondays at
    7pm Pacific". The card's Alerts select adds this as an extra preselected
    option when the current value is not in ALERT_CHOICES.

    Raises ValueError for a cadence or hour the profile table would refuse:
    a label for a setting that cannot be stored would describe nothing real.
    """
    if alerts not in _CADENCES:
        raise ValueError(f"unknown alert cadence {alerts!r}; expected one of {_CADENCES}")
    if isinstance(alert_hour, bool) or not isinstance(alert_hour, int) \
            or not 0 <= alert_hour <= _LAST_HOUR:
        raise ValueError(f"alert hour must be an int from 0 to {_LAST_HOUR}, not {alert_hour!r}")
    listed = _ALERT_LABELS.get(alert_choice_value(alerts, alert_hour))
    if listed is not None:
        return listed
    if alerts in ("hourly", "off"):
        return _ALERT_LABELS[alert_choice_value(alerts, _LISTED_HOUR)]
    if alerts == "daily":
        return f"Daily at {_clock(alert_hour)} Pacific"
    return f"Weekly, Mondays at {_clock(alert_hour)} Pacific"


# ------------------------------------------------------------------ legacy (3.3)

#: "All tech" as the old tracker meant it, in FIELDS order: every field any
#: legacy category maps to except `business_ops`, which only `pm` brought.
#: D20: quant, product, aerospace and manufacturing are all kept.
LEGACY_ALL_TECH: tuple[str, ...] = (
    "software", "data_ml", "analytics", "security", "it", "electrical", "mechanical",
    "aerospace", "manufacturing", "quant", "product")
#: The old `/internships ping` categories (`poller.CATEGORIES` plus `other`).
LEGACY_CATEGORY_FIELDS: dict[str, tuple[str, ...]] = {
    "swe": ("software", "security", "it"),
    "data-ml": ("data_ml", "analytics"),
    "hardware": ("electrical", "mechanical", "aerospace", "manufacturing"),
    "quant": ("quant",),
    "pm": ("product", "business_ops"),
    "other": LEGACY_ALL_TECH,
}


# ------------------------------------------------------------------ caps

#: Enforced by `intern_profile.with_changes` on user edits. A migrated profile
#: may hold more fields than MAX_FIELDS (3.3); it can shrink but not grow.
MAX_FIELDS = 6
MAX_MAJORS = 3
MAX_MINORS = 2
MAX_SKILLS = 40
MAX_KEYWORDS = 10
MAX_TERMS = 4
MAX_STATES = 10
MAX_COMPANIES_ONLY = 20
MAX_COMPANIES_HIDDEN = 30
#: What a typed keyword may look like once cleaned: the characters skill names
#: use (`c++`, `c#`, `node.js`, `r&d`, `tcp/ip`) and nothing that could format,
#: mention or break a line when the card shows it back.
KEYWORD_RE: re.Pattern = re.compile(r"^[a-z0-9+#./& -]{2,30}$")


# ------------------------------------------------------------------ validation

_VOCABULARIES: dict[str, frozenset[str]] = {
    "field": frozenset(FIELD_IDS),
    "level": frozenset(LEVEL_IDS),
    "location": frozenset(LOCATION_PRESET_IDS),
    "degree": frozenset(DEGREE_IDS),
}


def _is_state_token(item: str) -> bool:
    """`st:CA` yes; `st:ca`, `st:ZZ` and `st:` no. Codes are upper case."""
    return item.startswith(_STATE_TOKEN) and item[len(_STATE_TOKEN):] in US_STATES


def _members(ids: object) -> Iterable[object]:
    """
    What to look through. Nothing, for something that is not a collection.

    The ids are often read back out of a JSON column, so they can be anything
    JSON can hold. A string is iterable one character at a time, and some of
    those characters could one day be ids; it is refused whole.
    """
    if ids is None or isinstance(ids, (str, bytes, bytearray)):
        return ()
    try:
        return iter(ids)
    except TypeError:
        return ()


def valid_ids(kind: str, ids: Iterable[object]) -> tuple[str, ...]:
    """
    kind in {"field", "level", "location", "degree"}. Keeps only str members of
    that vocabulary ("location" also accepts "st:XX" for XX in
    intern_places.US_STATES), deduplicated, order kept. Unknown kind ->
    ValueError. Never mutates the input.

    An unknown kind is a programming error and raises; an unknown id is data
    and is dropped quietly, since the list it came from may simply predate a
    vocabulary change.
    """
    if not isinstance(kind, str) or kind not in _VOCABULARIES:
        raise ValueError(f"unknown vocabulary {kind!r}; expected one of {sorted(_VOCABULARIES)}")
    allowed = _VOCABULARIES[kind]
    accepts_states = kind == "location"
    kept = (
        item for item in _members(ids)
        if isinstance(item, str)
        and (item in allowed or (accepts_states and _is_state_token(item)))
    )
    return tuple(dict.fromkeys(kept))


def clean_keyword(text: str) -> "str | None":
    """
    Lower-case, strip, collapse spaces, remove control chars; the result if it
    matches KEYWORD_RE, else None.

    Tabs and newlines become spaces before the control characters go, so
    "lab<TAB>safety" is two words rather than one. Zero-width characters are
    removed with them. Not NFKC-folded: anything outside the keyword alphabet
    is refused rather than guessed at.
    """
    if not isinstance(text, str):
        return None
    spaced = _WHITESPACE.sub(" ", text)
    visible = _ZERO_WIDTH.sub("", _CONTROL.sub("", spaced))
    cleaned = _WHITESPACE.sub(" ", visible).strip().lower()
    return cleaned if KEYWORD_RE.match(cleaned) else None
