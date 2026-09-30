"""
intern_location.py
~~~~~~~~~~~~~~~~~~
Where a posting is, and whether that is somewhere the student asked for.

A board's location column is free text in a dozen dialects: `CA - Dublin`,
`USA - El Segundo, CA`, `02551 - CVS Albany, L.L.C.`, `27 Locations`.
`parse_location` reads it into places and a work mode. `location_for` falls
back to the title when the column says nothing, because a Target store
programme lists `10 Locations` and puts the city in the title. `location_fit`
decides whether a profile's presets take the result, in the words the Why line
shows.

The segment parser is ported from the measured reference (spec 4.3) with its
regexes verbatim, and its rule order is its accuracy: a US state suffix is
tried before the foreign-city table, so `Dublin, CA` is California while a bare
`Dublin` is Ireland. Every misread is a student shown roles in the wrong
country, so change a rule as data and rerun it against the postings it was
measured on.

Pure: stdlib, `intern_places` and `intern_vocab` only, so it loads under bare
`python3`. The two parsers are cached; the whole 30-day window goes through
them on every load.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import lru_cache

from intern_places import (CA_PROV_CODES, CA_PROVINCES, COUNTRIES, ISO3, METRO_CITIES,
                           METRO_EXTRA_STATES, METRO_PHRASES, METRO_STATE, NON_US_CITIES,
                           NON_US_REGIONS, SOCAL_EXTRA_CITIES, SOCAL_METROS, STATE_BY_NAME,
                           US_CITIES, US_STATES)
from intern_vocab import LOCATION_PRESETS, norm_text, valid_ids

_CACHE_SIZE = 65536


def _ci(pattern: str) -> re.Pattern:
    """The spec's `R`: case-insensitive unless a pattern says otherwise."""
    return re.compile(pattern, re.I)


@dataclass(frozen=True)
class Place:
    """One place a posting names. An empty string means the posting did not say."""

    city: str = ""      # as written, e.g. "Irvine"
    state: str = ""     # US state code when country == "US"
    country: str = ""   # ISO-3166 alpha-2; "XX" = foreign, country unknown; "" = unknown


@dataclass(frozen=True)
class LocInfo:
    """Everything the finder knows about where one posting is."""

    places: tuple[Place, ...] = ()
    mode: str = "unknown"            # "remote" | "hybrid" | "onsite" | "unknown"
    unlisted: int = 0                # n from "<n> Locations"
    from_title: bool = False         # places recovered from the title
    foreign_hint: bool = False       # title is in another language / a foreign programme
    metros: frozenset[str] = frozenset()

    @property
    def countries(self) -> frozenset[str]:
        return frozenset(p.country for p in self.places if p.country)

    @property
    def us(self) -> str:
        """"yes", "no", "mixed" (the US and abroad) or "unknown" (no country known)."""
        countries = self.countries
        if not countries:
            return "unknown"
        if countries == {"US"}:
            return "yes"
        return "mixed" if "US" in countries else "no"

    @property
    def states(self) -> frozenset[str]:
        return frozenset(p.state for p in self.places if p.state)


# ------------------------------------------------------------------ patterns (4.3.2)

REMOTE_RX = _ci(r"\b(remote|work from home|wfh|telecommute|virtual|distributed|anywhere|"
                r"nationwide)\b")
HYBRID_RX = _ci(r"\bhybrid\b")
ONSITE_RX = _ci(r"\b(on-?site|in[- ]office)\b")
N_LOC_RX = _ci(r"^\s*(\d+)\s+locations?\s*$")
ZIP_RX = re.compile(r"\b([A-Z]{2})\s*(\d{5})(-\d{4})?\b")
ISO3_PREFIX = re.compile(r"^([A-Z]{3})\s*-\s*(.*)$")
STATE_PREFIX = re.compile(r"^([A-Z]{2})\s*-\s*(.*)$")
STORE_ENTITY = re.compile(r"^\d{4,5}\s*-\s*(.*)$")
REGION_NON_US = _ci(r"\b(emea|europe|apac|asia|latam|latin america|middle east|africa)\b")
NORTH_AMERICA = _ci(r"\bnorth america\b|\bamericas\b")
SEG_SPLIT = re.compile(r"\s*(?:;|•|\||\n| / (?=[A-Z])| or (?=[A-Z]))\s*")
_PLACEHOLDERS = frozenset({"n/a", "na", "tbd", "various", "multiple locations", "location"})


def _word_scan(table: dict[str, str], longest_first: bool = False,
               min_len: int = 0) -> tuple[tuple[re.Pattern, str, str], ...]:
    """
    (`\\bname\\b`, name, code) for each entry, in the order the reference scans.

    Compiled once here rather than per call: the four scans hold some 400
    patterns between them, which would churn the `re` module's cache.
    """
    items = sorted(table.items(), key=lambda kv: -len(kv[0])) if longest_first else table.items()
    return tuple((re.compile(r"\b" + re.escape(name) + r"\b"), name, code)
                 for name, code in items if len(name) > min_len)


# Longest name first, so "west virginia" is found before "virginia".
_STATES_LONGEST_FIRST = tuple(sorted(STATE_BY_NAME.items(), key=lambda kv: -len(kv[0])))
_STATE_NAME_SCAN = _word_scan(STATE_BY_NAME, longest_first=True)
_COUNTRY_SCAN = _word_scan(COUNTRIES, longest_first=True, min_len=3)
_NON_US_CITY_SCAN = _word_scan(NON_US_CITIES)
_US_CITY_SCAN = _word_scan(US_CITIES)
_METRO_PHRASE_RX = tuple((_ci(p), metro, st) for p, metro, st in METRO_PHRASES)
_METRO_OK_STATES = {metro: frozenset({METRO_STATE[metro]} | METRO_EXTRA_STATES.get(metro, set()))
                    for metro in METRO_CITIES}

_Found = tuple[list[Place], str | None]      # one segment's places, and its mode word


# ------------------------------------------------------------------ one segment

def _clean_seg(s: str) -> str:
    s = re.sub(r"\((on-?site|remote|hybrid|preferred)\)", " ", s, flags=re.I)
    s = re.sub(r"\b(preferred|or remote|remote or)\b", " ", s, flags=re.I)
    return s.strip(" ,-/")


def _mode_of(s: str) -> str | None:
    if HYBRID_RX.search(s):
        return "hybrid"
    if REMOTE_RX.search(s):
        return "remote"
    if ONSITE_RX.search(s):
        return "onsite"
    return None


def _strip_mode_words(s: str) -> str:
    s = re.sub(r"\b(remote|hybrid|work from home|wfh|on-?site|in[- ]office|distributed|"
               r"nationwide|anywhere|virtual|telecommute)\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,-/:")


def _state_code(tok: str) -> str | None:
    t = tok.strip().strip(".")
    if t.upper() in US_STATES and (t.isupper() or len(t) == 2):
        return t.upper()
    return STATE_BY_NAME.get(t.lower())


def _parse_prefixed(s: str, mode: str | None) -> _Found | None:
    """
    The forms a leading code gives away: a CVS legal entity ('02551 - CVS
    Albany, L.L.C.'), an ISO3 prefix ('GBR - Bristol, UK', 'USA - Everett,
    WA'), 'US-San Francisco', and a state prefix ('MA - Sturbridge', 'FL-Miami')
    — unless the city says the code is a country ('CA-Toronto', 'IN-Bengaluru').
    """
    m = STORE_ENTITY.match(s)
    if m:
        rest = m.group(1).lower()
        for name, code in _STATES_LONGEST_FIRST:
            if name in rest:
                return [Place("", code, "US")], mode
        if "albany" in rest:
            return [Place("Albany", "NY", "US")], mode
        return [Place("", "", "US")], mode
    m = ISO3_PREFIX.match(s)
    if m and m.group(1) in ISO3:
        country, rest = ISO3[m.group(1)], m.group(2)
        sub = _parse_segment(rest)[0] if country == "US" else []
        if sub:
            return [Place(p.city, p.state, "US") for p in sub], mode
        return [Place(rest.split(",")[0].strip(), "", country)], mode
    m = re.match(r"^U\.?S\.?A?\s*-\s*(.*)$", s)
    if m:
        sub, sub_mode = _parse_segment(m.group(1))
        sub = [p for p in sub if p.country in ("US", "")] or [Place("", "", "US")]
        return [Place(p.city, p.state, "US") for p in sub], mode or sub_mode
    m = STATE_PREFIX.match(s) or re.match(r"^([A-Z]{2})-(.+)$", s)
    if m and m.group(1) in US_STATES:
        city = m.group(2).split(" - ")[0].split("-")[0].strip()
        foreign = NON_US_CITIES.get(city.lower().split(",")[0].strip())
        if foreign and foreign == m.group(1):
            return [Place(city, "", foreign)], mode
        city = city if _strip_mode_words(city) else ""
        return [Place(city, m.group(1), "US")], mode or _mode_of(m.group(2))
    return None


def _suffixed(parts: list[str], country: str | None) -> Place | None:
    """
    `City, Province`, `City, ST`, `City, State Name`: the last part settles
    the country. A foreign city before a state name ('London, New York') or a
    city of another state ('Boston, New York') is a list, not one place, and
    is left to the caller.
    """
    first, last = parts[0], parts[-1]
    if last.upper() == "CA" and len(parts) >= 3 and parts[-2].lower() in CA_PROVINCES:
        return Place(first, "", "CA")
    if last.lower() in NON_US_REGIONS and country is None:
        return Place(first, "", NON_US_REGIONS[last.lower()])
    if last.upper() in CA_PROV_CODES and country in (None, "CA") and last.upper() not in US_STATES:
        return Place(first, "", "CA")
    state = _state_code(last)
    foreign = NON_US_CITIES.get(first.lower())
    if state and foreign and foreign == last.upper():
        return Place(first, "", foreign)                   # 'Bangalore, IN'
    is_list = len(last) > 2 and (first.lower() in NON_US_CITIES
                                 or US_CITIES.get(first.lower(), state) != state)
    if state and country in (None, "US") and not is_list:
        return Place(parts[-2] if len(parts) >= 3 and first[:1].isdigit() else first, state, "US")
    return None


def _is_foreign_province(first: str, second: str) -> bool:
    """Workday's 'City, State-or-Province' with nothing American in either half:
    'Mannheim, Baden-Wurttemberg', 'Torreon, Coahuila', 'King Abdullah Economic City, 02'."""
    a, b = first.lower(), second.lower()
    return (not _state_code(second) and a not in US_CITIES and b not in US_CITIES
            and a not in NON_US_CITIES and b not in NON_US_CITIES
            and not _state_code(first) and b not in COUNTRIES)


def _region(core: str, whole: str) -> list[Place]:
    """Region words: the US, California, the world, abroad, North America."""
    if re.search(r"\bnationwide\b|\bus\b|\busa\b", whole) or re.search(r"\bUS\b", core):
        return [Place("", "", "US")]
    if re.search(r"\b(bay area|silicon valley|socal|norcal)\b", whole):
        return [Place("", "CA", "US")]
    if re.search(r"\b(global|worldwide|amer)\b", whole):
        return [Place("", "", "US"), Place("", "", "XX")]
    if REGION_NON_US.search(whole):
        return [Place("", "", "XX")]
    if NORTH_AMERICA.search(whole):
        return [Place("", "", "US"), Place("", "", "CA")]
    return []


def _bare(core: str) -> list[Place]:
    """No suffix to go on: one known name, then a name found inside, then a region word."""
    whole = core.lower().strip()
    if whole in NON_US_CITIES:
        return [Place(core, "", NON_US_CITIES[whole])]
    if whole in US_CITIES:
        return [Place(core, US_CITIES[whole], "US")]
    if whole in COUNTRIES:
        return [Place("", "", COUNTRIES[whole])]
    state = _state_code(core)
    if state:
        return [Place("", state, "US")]
    for rx, _, code in _STATE_NAME_SCAN:                # 'Virginia - Remote', 'Field-Florida'
        if rx.search(whole):
            return [Place("", code, "US")]
    for rx, _, code in _COUNTRY_SCAN:
        if rx.search(whole):
            return [Place("", "", code)]
    for rx, name, code in _NON_US_CITY_SCAN:
        if rx.search(whole):
            return [Place(name.title(), "", code)]
    for rx, name, code in _US_CITY_SCAN:
        if rx.search(whole):
            return [Place(name.title(), code, "US")]
    return _region(core, whole)


def _parse_core(core: str, mode: str | None) -> _Found:
    """A segment with its prefixes and mode words gone: suffixes, city lists, bare words."""
    parts = [p.strip() for p in core.split(",") if p.strip()]
    country = None
    if parts and parts[-1].lower().strip(".") in COUNTRIES:
        country, parts = COUNTRIES[parts[-1].lower().strip(".")], parts[:-1]
    elif parts and parts[-1].upper() in ISO3:
        country, parts = ISO3[parts[-1].upper()], parts[:-1]
    single = _suffixed(parts, country) if len(parts) >= 2 else None
    if single:
        return [single], mode
    if country and country != "US":
        return [Place(parts[0] if parts else "", "", country)], mode
    if country == "US":
        if not parts:
            return [Place("", "", "US")], mode
        state = _state_code(parts[-1])
        if state:
            return [Place(parts[0] if len(parts) > 1 else "", state, "US")], mode
        return [Place(parts[0], US_CITIES.get(parts[0].lower(), ""), "US")], mode
    if len(parts) == 2 and _is_foreign_province(*parts):
        return [Place(parts[0], "", "XX")], mode
    if len(parts) >= 2:
        listed = [place for part in parts for place in _parse_segment(part)[0]]
        if listed:
            return listed, mode
    return _bare(core), mode


def _parse_segment(seg: str) -> _Found:
    """(places, mode or None) for one segment, trying the forms in the reference's order."""
    mode = _mode_of(seg)
    s = _clean_seg(seg)
    if not s:
        return [], mode
    hit = _parse_prefixed(s, mode)
    if hit is not None:
        return hit
    m = ZIP_RX.search(s)                    # '1000 Nicollet Mall, Minneapolis,MN 55403-2542'
    if m and m.group(1) in US_STATES:
        before = s[:m.start()].rstrip(", ")
        return [Place(before.split(",")[-1].strip(), m.group(1), "US")], mode
    s = re.sub(r"(?<=[A-Za-z])\s+(United States|USA)$", r", \1", s)
    m = re.search(r"^(.*?)[\s-]+([A-Z]{2})$", s)     # 'Needham- MA', '... - Bridgeville PA'
    if m and m.group(2) in US_STATES and "," not in s and not ISO3_PREFIX.match(s):
        city = m.group(1).split(" - ")[-1].strip(" -")
        if NON_US_CITIES.get(city.lower()) != m.group(2):
            return [Place(city, m.group(2), "US")], mode
    if re.search(r"\bnationwide\b", s, re.I) \
            and not re.search(r"[A-Za-z]{4,}", _strip_mode_words(s)):
        return [Place("", "", "US")], mode
    core = _strip_mode_words(s)
    return _parse_core(core, mode) if core else ([], mode)


# ------------------------------------------------------------------ the whole string

def _metros(places: Iterable[Place], text: str) -> frozenset[str]:
    """Metro ids from the US cities in `places` and the region phrases in `text` (4.3.4)."""
    by_city = {metro for p in places if p.country == "US" and p.city
               for metro, cities in METRO_CITIES.items()
               if p.city.lower().strip() in cities
               and (p.state in _METRO_OK_STATES[metro] or not p.state)}
    by_phrase = {metro for rx, metro, _ in _METRO_PHRASE_RX if metro and rx.search(text or "")}
    return frozenset(by_city | by_phrase)


@lru_cache(maxsize=_CACHE_SIZE)
def parse_location(raw: str | None) -> LocInfo:
    """
    The places and work mode a board's location string names (spec 4.3.2).

    Hybrid if any segment says so, else remote, else on-site; places are
    deduplicated in order. Never raises: anything that is not text is empty.
    """
    text = norm_text(raw)
    if not text or text.lower() in _PLACEHOLDERS:
        return LocInfo()
    m = N_LOC_RX.match(text)
    if m:
        return LocInfo(unlisted=int(m.group(1)))
    found = [_parse_segment(seg) for seg in SEG_SPLIT.split(text) if seg.strip()]
    places = tuple(dict.fromkeys(place for ps, _ in found for place in ps))
    modes = {mode for _, mode in found if mode}
    mode = next((m for m in ("hybrid", "remote", "onsite") if m in modes), "unknown")
    return LocInfo(places=places, mode=mode, metros=_metros(places, text))


# ------------------------------------------------------------------ title fallbacks (4.3.3)

TITLE_LOC_TAIL = re.compile(r"(?:\s-\s|\s-(?=\S)|,\s)([^-()]*?,\s*[A-Z]{2})\b(?:[^A-Za-z].*)?$")
CITY_ST = re.compile(r"\b([A-Z][A-Za-z.'\- ]{2,30}?),\s*([A-Z]{2})\b")
FOREIGN_TITLE = _ci(r"\b(practicante|pr[a\u00e1]cticas|becario|est[a\u00e1]gio|estagi[a\u00e1]rio|"
                    r"aprendiz|werkstudent|dhbw|m/w/d|f/m/d|w/m/d|stagiaire|alternance|jovem|"
                    r"analista|ingenier[i\u00ed]a|engenharia|praktikum)\b|[\u4e00-\u9fff]")


def _title_places(t: str) -> tuple[tuple[Place, ...], frozenset[str]] | None:
    """(places, metros) read from a title by the first of the four fallbacks that hits."""
    m = TITLE_LOC_TAIL.search(re.sub(r"\([^)]*\)", " ", t))
    if m:
        sub = parse_location(m.group(1))
        if sub.places:
            return sub.places, sub.metros | _metros((), t)
    for m in CITY_ST.finditer(t):
        if m.group(2) in US_STATES:
            places = (Place(m.group(1).strip(), m.group(2), "US"),)
            return places, _metros(places, t)
    for rx, metro, state in _METRO_PHRASE_RX:
        if rx.search(t):
            return (Place("", state, "US"),), frozenset({metro}) if metro else frozenset()
    parts = re.split(r"\s-\s|\)\s", t, maxsplit=1)
    if len(parts) == 2:
        low = parts[1].lower()
        for rx, _, code in _STATE_NAME_SCAN:
            if rx.search(low):
                return (Place("", code, "US"),), frozenset()
    return None


@lru_cache(maxsize=_CACHE_SIZE)
def location_for(title: str | None, raw: str | None) -> LocInfo:
    """
    Where a posting is: the location column first, then its title (spec 4.3.3).

    Used for every posting, never `parse_location` alone. A column that names
    a place or says remote wins, with the title's metro phrases added; else the
    title is tried for a ' - City, ST' tail, a 'City, ST' anywhere, a metro
    phrase, and a state name after the first separator, in that order.
    """
    info = parse_location(raw)
    t = norm_text(title)
    foreign = bool(FOREIGN_TITLE.search(t))
    if info.places or info.mode == "remote":
        return replace(info, foreign_hint=foreign, metros=info.metros | _metros((), t))
    hit = _title_places(t)
    if hit is None:
        return LocInfo(mode=info.mode, unlisted=info.unlisted, foreign_hint=foreign)
    return LocInfo(places=hit[0], mode=info.mode, unlisted=info.unlisted, from_title=True,
                   foreign_hint=foreign, metros=hit[1])


# ------------------------------------------------------------------ preferences (4.3.5)

#: The metro presets share their ids with `intern_places.METRO_CITIES`.
PRESET_METRO = {metro: metro for metro in METRO_CITIES}
PRESET_STATE = {"ca": "CA", "tx": "TX", "fl": "FL"}
_STATE_TOKEN = "st:"
_UNLISTED_TEXT = "roles that don't list a location"
_MAX_CHOICES = 25            # Discord's autocomplete ceiling
_MAX_BAD_TOKEN = 30          # how much of an unrecognised state token is echoed back


def _state_name(code: str) -> str:
    # str.title() alone writes "District Of Columbia".
    return US_STATES.get(code, code).title().replace(" Of ", " of ")


def _place_label(p: Place) -> str:
    if p.city and p.state:
        return f"{p.city.title() if p.city.islower() else p.city}, {p.state}"
    return _state_name(p.state) if p.state else "in the US"


def _preset_takes(pid: str, p: Place, info: LocInfo) -> bool:
    """Whether a specific preset (a metro, SoCal, a state preset, st:XX) takes this place."""
    metro = PRESET_METRO.get(pid)
    if metro and metro in info.metros:
        return True
    if pid == "socal" and p.state == "CA" and (
            info.metros & SOCAL_METROS or p.city.lower() in SOCAL_EXTRA_CITIES):
        return True
    is_token = pid.startswith(_STATE_TOKEN)
    state = PRESET_STATE.get(pid) or (pid[len(_STATE_TOKEN):] if is_token else None)
    return bool(state) and p.state == state


def _state_only(chosen: tuple[str, ...], us_places: list[Place]) -> str | None:
    """With `unlisted`: the state of a metro or SoCal preset that a city-less place is in."""
    if "unlisted" not in chosen:
        return None
    cityless = {p.state for p in us_places if not p.city}
    for pid in chosen:
        metro = PRESET_METRO.get(pid)
        want = METRO_STATE.get(metro) if metro else ("CA" if pid == "socal" else None)
        if want and want in cityless:
            return want
    return None


def location_fit(info: LocInfo, prefs: Iterable[str]) -> tuple[float | None, str]:
    """
    (P, why) when the presets take this posting, (None, reason) when they do not.

    The first rule of spec 4.3.5 that applies wins. Presets are read in the
    order given, so when two could word the answer the first one does, and one
    profile always gets the same sentence. The argument is not modified.
    """
    chosen = tuple(dict.fromkeys(p for p in prefs if isinstance(p, str)))
    us_places = [p for p in info.places if p.country == "US"]
    for p in us_places:                                           # 1. a specific preset
        if any(_preset_takes(pid, p, info) for pid in chosen):
            return 1.0, _place_label(p)
    if info.mode == "remote" and info.us in ("yes", "unknown", "mixed"):
        if "remote_us" in chosen:                                 # 2. remote
            return 1.0, "Remote (US)"
        if "us" in chosen:
            return 0.8, "Remote (US)"
    if "us" in chosen and us_places:                              # 3. anywhere in the US
        if any(p.state for p in us_places):
            return 0.8, _place_label(us_places[0])
        return 0.6, "in the US (city not listed)"
    state = _state_only(chosen, us_places)                        # 4. state, no city
    if state:
        return 0.3, f"somewhere in {_state_name(state)}, city not listed"
    if "abroad" in chosen and info.us in ("no", "mixed"):         # 5. abroad
        foreign = [p for p in info.places if p.country not in ("US", "")]
        return 0.8, (foreign[0].city or "outside the US") if foreign else "outside the US"
    if info.places or info.mode == "remote":                      # 7. anything else
        return None, "outside your locations"
    if "unlisted" in chosen and not info.foreign_hint:            # 6. nothing listed
        count = f" ({info.unlisted} locations)" if info.unlisted else ""
        return 0.3, f"location not listed{count}, check the posting"
    return None, "location not listed"


STATE_TOKEN_SPLIT = re.compile(r"[,;/]|\band\b")


def parse_state_list(text: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """'WA, Oregon, ny and SoCal' -> (('st:WA', 'st:OR', 'st:NY'), ('SoCal',))."""
    typed = text if isinstance(text, str) else ""
    tokens = [tok.strip().strip(".") for tok in STATE_TOKEN_SPLIT.split(typed)]
    read = [(tok, tok.upper() if tok.upper() in US_STATES else STATE_BY_NAME.get(tok.lower()))
            for tok in tokens if tok]
    good = tuple(dict.fromkeys(f"{_STATE_TOKEN}{code}" for _, code in read if code))
    return good, tuple(tok[:_MAX_BAD_TOKEN] for tok, code in read if not code)


def describe_locations(prefs: Iterable[str]) -> str:
    """
    Card text: preset labels in LOCATION_PRESETS order, then "State: {Name}"
    for st:XX, with "unlisted" rendered last as "roles that don't list a
    location", joined " · ". Anything outside the vocabulary is left out.
    """
    chosen = valid_ids("location", prefs)
    presets = [label for pid, label in LOCATION_PRESETS if pid in chosen and pid != "unlisted"]
    states = [f"State: {_state_name(tok[len(_STATE_TOKEN):])}" for tok in chosen
              if tok.startswith(_STATE_TOKEN)]
    return " · ".join(presets + states + ([_UNLISTED_TEXT] if "unlisted" in chosen else []))


_CHOICES: tuple[tuple[str, str], ...] = tuple(
    [(label, pid) for pid, label in LOCATION_PRESETS if pid != "unlisted"]
    + sorted((f"State: {_state_name(code)}", f"{_STATE_TOKEN}{code}") for code in US_STATES))


def state_choices(query: str) -> list[tuple[str, str]]:
    """(label, value) for presets (except "unlisted") then states ("State:
    California", "st:CA") whose label contains every token of `query`; at most 25."""
    tokens = norm_text(query).lower().split()
    return [c for c in _CHOICES if all(tok in c[0].lower() for tok in tokens)][:_MAX_CHOICES]
