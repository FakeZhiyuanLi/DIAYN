"""
intern_text.py
~~~~~~~~~~~~~~
Every sentence the internship finder says, and the character budget it says it in.

One home for the copy (spec 1.2), so the Discord modules never write reply text
inline. **Budgets:** Discord rejects a message over 2,000 characters, and a
rejected card or DM is a reply nobody sees and nothing logs, so every renderer
degrades instead — the card shortens lists with "(+N more)", an alert drops
listings and counts them, the delete screen is packed into several messages.
**Outside text** (companies, titles, places, filenames) passes through
`safe_inline` or `safe_url` before it reaches a message.

**Time** is DIAYN_TZ (`intern_clock`): every hour and date is in it, and an
hour always names it.

Component labels live in the view modules and the J4 problem sentences in
`intern_profile`, by design. Pure apart from reading the zone: imports under
bare `python3`.
"""

import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime

import intern_clock
import intern_vocab as vocab
from intern_location import describe_locations
from intern_match import BROWSE_MAX, FIRST_MATCHES, MATCHES_MAX, Coverage, Match, Relaxation
from intern_profile import Profile
from intern_store import (ACCESS_GRACE_S, DM_FAILURE_LIMIT, HIDDEN_RETAIN_S, SENT_RETAIN_S,
                          STORED_COLUMNS)
from message_pack import MAX_CHUNK, pack
from resume_lexicon import MAJOR_BY_ID, SKILL_BY_ID

CARD_MAX = 1900                 # the profile card, one message
ALERT_MAX = MAX_CHUNK           # a DM digest, and every packed chunk
ALERT_LISTINGS_MAX, FILENAME_MAX = 5, 40
COMPANY_MAX, TITLE_MAX, PLACE_MAX, WHY_MAX, REASON_MAX = 60, 150, 80, 250, 120
OPTION_MAX, OPTIONS_MAX, BUTTON_MAX = 100, 25, 80      # Discord's select and button ceilings
FIRST_MATCH_DAYS = 14
_SHOWN = {"skills": 8, "keywords": 10}                   # card lists capped even with room
_DELTA_SHOWN = 6
_LIST_FLOOR = 3                 # items a card list keeps before notices are dropped
_DEBUG_LINE = 400
_DAY_S, _HOUR_S = 86400, 3600

# ------------------------------------------------------------------ outside text

#: C0/C1 controls, soft hyphen, zero-width characters, bidi marks and overrides,
#: word joiners and BOM. Written as code points: never paste these into source.
_INVISIBLE_RANGES = ((0x00, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x200B, 0x200F),
                     (0x202A, 0x202E), (0x2060, 0x2069), (0xFEFF, 0xFEFF))
_INVISIBLE = re.compile("[" + "".join(f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in _INVISIBLE_RANGES) + "]")
_SPACES = re.compile(r"\s+")
_MARKDOWN = re.compile(r"([\\*_~`|>])")
_URL = re.compile(r"https?://[^\s<>\x00-\x1f\x7f]{1,300}")


def _plain(value: object) -> str:
    """One visible line: whitespace runs become a space, then invisible characters go."""
    if not isinstance(value, str):
        return ""
    return _SPACES.sub(" ", _INVISIBLE.sub("", _SPACES.sub(" ", value))).strip()


def _cut(value: str, limit: int, escape: bool) -> str:
    """`value` (markdown-escaped when asked) in at most `limit` characters, cut on a
    whole character, so never through an escape, with an ellipsis when shortened."""
    render = (lambda s: _MARKDOWN.sub(r"\\\1", s)) if escape else (lambda s: s)
    if len(render(value)) <= limit:
        return render(value)
    kept, size = [], 0
    for piece in map(render, value):
        if size + len(piece) > limit - 1:
            break
        kept.append(piece)
        size += len(piece)
    return "".join(kept).rstrip() + "…" if limit >= 1 else ""


def safe_inline(text: str | None, limit: int) -> str:
    """One line, controls and zero-width characters gone, markdown escaped, <= `limit`."""
    return _cut(_plain(text), limit, escape=True)


def safe_url(url: str | None) -> str | None:
    """The URL when it is a plain http(s) link of at most 300 characters, else None."""
    return url if isinstance(url, str) and _URL.fullmatch(url) else None


def safe_filename(name: str | None) -> str:
    """Escaped, controls gone, <= 40 chars; a backtick cannot close the code span it sits in."""
    return _cut(_plain(name).replace("`", "'"), FILENAME_MAX, escape=True) or "your file"


# ------------------------------------------------------------------ small words

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_DEGREE_LABELS = dict(vocab.DEGREES)
_MIN_SCORE_WORDS = {75: "strong matches only", 60: "good and strong matches", 45: "everything relevant"}
#: One role's level, for the second line of a match block (the select labels are plural).
_LEVEL_WORDS = {"intern": "Internship", "coop": "Co-op", "new_grad": "New grad & programs",
                "entry": "Entry-level", "apprentice": "Apprenticeship", "hourly": "Part-time & hourly",
                "unspecified": "Level not stated", "experienced": "Experienced"}


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _clock(hour: int) -> str:
    return f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"


def _hour(hour: int) -> str:
    """An hour as shown to a user: "5pm Los Angeles time", its zone always named."""
    return f"{_clock(hour)} {intern_clock.zone_label()}"


def _month_day(ts: float) -> str:
    local = datetime.fromtimestamp(ts, intern_clock.zone())
    return f"{local:%b} {local.day}"


def _month_year(year: object, month: object) -> str | None:
    if not isinstance(year, int) or isinstance(year, bool):
        return None
    has_month = isinstance(month, int) and not isinstance(month, bool) and 1 <= month <= 12
    return f"{_MONTHS[month - 1]} {year}" if has_month else str(year)


def _labels(ids: Sequence[str], table: Mapping) -> list[str]:
    """Display labels for known ids (lexicon entries carry `.label`; vocab maps are str)."""
    return [getattr(table[i], "label", table[i]) for i in ids if i in table]


def _capped(items: Sequence[str], shown: int, sep: str = ", ") -> str:
    extra = len(items) - shown
    return sep.join(items[:shown]) + (f" (+{extra} more)" if extra > 0 else "")


def _field_phrase(fields: Sequence[str]) -> str:
    labels = _labels(fields, vocab.FIELD_LABELS)
    if not labels or len(labels) > 3:
        return f"your {len(labels)} fields" if labels else "your fields"
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} or {labels[-1]}"


def _loc_phrase(p: Profile) -> str:
    return "" if "us" in p.locations else " in the places you picked"


def _ago(ts: float | None, now: float) -> str:
    """The age format `/diayn debug` has always used."""
    if not ts:
        return "never"
    d = max(0.0, now - ts)
    if d < 5400:
        return f"{d:.0f}s ago" if d < 90 else f"{d / 60:.0f}m ago"
    return f"{d / 3600:.1f}h ago" if d < 172800 else f"{d / 86400:.1f}d ago"


# ------------------------------------------------------------------ J1, J2: consent and upload

_DISCLOSURE = (
    "**Your resume stays private**\n"
    "- It's read once, in a separate process on the computer this bot runs on, to suggest your "
    "major, degree, graduation date and skills. It is **not sent to any AI service** or anyone "
    "else.\n"
    "- The file and its text are never saved, not to disk and not to logs, and are gone as soon as "
    "the suggestion is made.\n"
    "- I keep only what you confirm on the next screen: your major, degree, graduation month, skills "
    "from a fixed list, and the filters you pick. Never your name, contact details, address, school, "
    "GPA or employers.\n"
    "- Whoever runs this bot can read its database, users.db, on that computer.\n"
    "- Discord keeps its own copy of files uploaded to it; I can't delete that one.\n"
    "- `/internships delete` shows everything I hold and erases it, any time. An access "
    "grant by your id stays until whoever runs this bot revokes it.")
_FORMATS = {
    True: "PDF, Word (.docx) or .txt, up to 2 MB. You can also attach it to the command: "
          "`/internships profile resume:`",
    False: "Word (.docx) or .txt, up to 2 MB. This bot can't read PDFs yet (whoever runs this bot "
           "can install `pypdf`), so upload the .docx or paste the text."}


#: The Gemini fit check (intern_fit), shown beside the disclosure wherever the host has a
#: key: exactly what is sent, what never is, and the way to turn it off.
_FIT_NOTE = (
    "**Gemini checks your alerts**\n"
    "- Before an alert is sent, this bot asks Google's Gemini whether each role suits you. It "
    "sends your profile's labels (majors, minors, degree, graduation date, kinds of role, fields, "
    "skills, keywords, places and terms) and each role's title, company, location and term. "
    "Google's terms for the Gemini API cover what it receives.\n"
    "- Never your resume, name, Discord id or contact details. Its answers are kept for 45 days "
    "under a fingerprint of those labels, not under your name or id.\n"
    "- To stop it, pick **Turn the Gemini check off** in the Alerts menu of your profile card.")


def disclosure_text() -> str:
    """What happens to a resume. The fit check's note is its own (`fit_note`)."""
    return _DISCLOSURE


def fit_note() -> str:
    return _FIT_NOTE


def _disclosed(gemini: bool) -> str:
    return f"{_DISCLOSURE}\n\n{_FIT_NOTE}" if gemini else _DISCLOSURE


def consent_screen(*, gemini: bool = False) -> str:
    """The disclosure before an upload, with the fit check's note when the host has a key."""
    return _disclosed(gemini)


def start_card(*, pdf_ok: bool, gemini: bool = False) -> str:
    return ("**Find internships that fit you**\n"
            "I'll DM you internships, co-ops and new-grad roles that fit *your* major, not just CS, "
            "with a line on why each one matched. It takes about a minute.\n\n"
            f"{_disclosed(gemini)}\n\n{_FORMATS[bool(pdf_ok)]}\n"
            "Just browsing? `/internships recent` needs no profile.")


def consent_text(filename: str, *, gemini: bool = False) -> str:
    return f"**Before I read `{safe_filename(filename)}`**\n{_disclosed(gemini)}"


def upload_modal_note() -> str:
    return ("Read once on the computer this bot runs on, never sent to an AI service, and never "
            "saved. Only what you confirm on the next screen is kept.")


_UNSAFE_WORD = "I couldn't read that Word file safely. Save it again as .docx or PDF."
_UPLOAD_ERRORS = {
    "too_big": "That file is {size_mb} MB and I take resumes up to 2 MB. Exporting it again as a PDF "
               "without images usually shrinks it.",
    "empty": "That file is empty.",
    "bad_type": "I can read PDF, Word (.docx) or .txt resumes. For a .doc or Pages file, export it as "
                "PDF first.",
    "type_mismatch": "That file's type doesn't match its name. Export it again and re-upload.",
    "bad_magic": "That doesn't look like a real {kind_label} file. Export it again and re-upload.",
    "no_pdf_support": "This bot can't read PDFs yet (whoever runs this bot can install `pypdf`). "
                      "Upload the .docx, or paste the text.",
    "encrypted": "That PDF is password-protected. Save a copy without a password, or paste the text.",
    "too_many_pages": "That PDF has more than 10 pages. A one- or two-page resume works best.",
    "no_text": "I couldn't find any text in that file. It's probably a scan or an image. Upload the "
               "original .docx, or paste the text.",
    "corrupt": "I couldn't read that file. Try exporting it again as PDF.",
    "zip_bomb": _UNSAFE_WORD,
    "xml_entity": _UNSAFE_WORD,
    "timeout": "That file took too long to read, so I stopped. A PDF exported from Google Docs or "
               "Word works well.",
    "worker_failed": "Something went wrong reading that file. Try again in a minute.",
    "busy": "I'm reading a couple of other resumes right now. Try again in a minute.",
    "already_reading": "I'm still reading the last file you sent. Wait for that one, then try again.",
    "rate_limited": "You've uploaded 5 resumes in the last hour. Try again in {minutes} minutes.",
    "paste_short": "Paste at least your Education and Skills lines, then submit again.",
}
#: Refusals that come after the file was handed over: they say nothing was kept,
#: and carry the "paste instead / pick by hand" buttons (J2d).
READ_FAILURES = frozenset({"bad_magic", "encrypted", "too_many_pages", "no_text", "corrupt",
                           "zip_bomb", "xml_entity", "timeout", "worker_failed", "busy"})
_UPLOAD_DEFAULTS = {"size_mb": "more than 2", "kind_label": "resume", "minutes": "a few"}


def upload_error(reason: str, **ctx: object) -> str:
    """J2d. An unknown reason reads as `worker_failed`: a refusal must never crash the reply."""
    values = {key: _plain(str(ctx[key])) if ctx.get(key) is not None else default
              for key, default in _UPLOAD_DEFAULTS.items()}
    body = _UPLOAD_ERRORS.get(reason, _UPLOAD_ERRORS["worker_failed"]).format(**values)
    return f"{body} Nothing was kept." if reason in READ_FAILURES else body


def upload_empty() -> str:
    return "Attach a file or paste some text, then submit again."


def cancelled_consent(*, had_file: bool = True) -> str:
    return "Cancelled. I never opened the file." if had_file else cancelled_draft()


# ------------------------------------------------------------------ J3, J4: the card

_EVIDENCE_PLACE = {"education": "Education section", "text": "resume"}


def evidence_line(draft: Mapping) -> str | None:
    """"Read from your Education section: Bachelor's · Mechanical Engineering · June 2027"."""
    if not isinstance(draft, Mapping):
        return None
    evidence = draft.get("evidence") if isinstance(draft.get("evidence"), Mapping) else {}
    place = _EVIDENCE_PLACE.get(evidence.get("study") or evidence.get("grad"))
    majors = draft.get("majors") if isinstance(draft.get("majors"), (list, tuple)) else ()
    parts = [_DEGREE_LABELS.get(draft.get("degree")),
             ", ".join(_labels([m for m in majors if isinstance(m, str)], MAJOR_BY_ID)),
             _month_year(draft.get("grad_year"), draft.get("grad_month"))]
    shown = [part for part in parts if part]
    return f"Read from your {place}: {' · '.join(shown)}" if place and shown else None


def reparse_delta(old: Profile, new: Profile) -> str:
    """What a new resume changed: "+ ANSYS, Python · − Excel · major now Aerospace Engineering."."""
    added = _labels([s for s in new.skills if s not in old.skills], SKILL_BY_ID)
    removed = _labels([s for s in old.skills if s not in new.skills], SKILL_BY_ID)
    majors, grad = _labels(new.majors, MAJOR_BY_ID), _month_year(new.grad_year, new.grad_month)
    parts = [f"+ {_capped(added, _DELTA_SHOWN)}" if added else "",
             f"− {_capped(removed, _DELTA_SHOWN)}" if removed else "",
             f"{'major' if len(majors) == 1 else 'majors'} now {', '.join(majors)}"
             if majors and new.majors != old.majors else "",
             f"graduating {grad}" if grad and (new.grad_year, new.grad_month)
             != (old.grad_year, old.grad_month) else ""]
    kept = [part for part in parts if part]
    return f"{' · '.join(kept)}." if kept else "Nothing changed."


def draft_header(p: Profile, *, found_field: bool, replacing: Profile | None) -> str:
    if replacing is not None:
        return (f"**Updated from your new resume.** {reparse_delta(replacing, p)} Your filters were "
                "kept. Press **Save** to keep the changes.")
    if p.source == "manual":
        return "**Let's set up your profile.** Pick at least one field below, then press **Save**."
    if found_field:
        return ("**Here's what I read from your resume.** Fix anything below, then press **Save**. "
                "Nothing is kept until you do.")
    return "**Here's what I read from your resume.** I couldn't find your major; pick your fields below."


def feedback_line(before: int | None, after: int | None) -> str:
    if before is None or after is None or before == after:
        return "Saved."
    return f"Saved. **{_plural(after, 'role')}** {'matches' if after == 1 else 'match'} now (was {before})."


def dm_blocked_banner() -> str:
    return ("**Alerts are paused:** I couldn't DM you. Turn on Direct Messages in this server's "
            "Privacy Settings, then use `/internships ping`.")


def _studying(p: Profile) -> str:
    majors, degree = ", ".join(_labels(p.majors, MAJOR_BY_ID)), _DEGREE_LABELS.get(p.degree)
    minors = _labels(p.minors, MAJOR_BY_ID)
    head = f"{majors} ({degree})" if majors and degree else majors or degree or ""
    tail = f"{'minor' if len(minors) == 1 else 'minors'} {', '.join(minors)}" if minors else ""
    return " · ".join(part for part in (head, tail) if part)


def _alerts(p: Profile, gemini: bool) -> str:
    """The card's Alerts line. With a host key (`gemini`), whether Gemini checks them."""
    try:
        label = vocab.alert_choice_label(p.alerts, p.alert_hour, zone=intern_clock.zone_label())
    except ValueError:
        label = str(p.alerts)
    checked = ("checked by Gemini" if p.fit_check else "not checked by Gemini") \
        if gemini and p.alerts != "off" else ""
    parts = [label, _MIN_SCORE_WORDS.get(p.min_score, "") if p.alerts != "off" else "",
             f"paused until {_month_day(p.paused_until)}" if p.paused_until else "", checked]
    return " · ".join(part for part in parts if part)


def _card_lists(p: Profile, companies: Mapping[str, str] | None) -> dict[str, tuple]:
    """name -> (label, items, separator) for every list the budget may shorten, in card order."""
    names = companies or {}
    display = [safe_inline(names.get(n, n), COMPANY_MAX) for n in (*p.companies_only, *p.companies_hidden)]
    places = describe_locations(p.locations)
    return {"skills": ("Skills", _labels(p.skills, SKILL_BY_ID), ", "),
            "keywords": ("Keywords", [safe_inline(k, 30) for k in p.keywords], ", "),
            "fields": ("Fields", _labels(p.fields, vocab.FIELD_LABELS), " · "),
            "levels": ("Looking for", _labels(p.levels, vocab.LEVEL_LABELS), " · "),
            "where": ("Where", places.split(" · ") if places else [], " · "),
            "terms": ("Terms", list(p.terms), " · "),
            "only": ("only", display[:len(p.companies_only)], ", "),
            "hidden": ("hiding", display[len(p.companies_only):], ", ")}


def _coverage_lines(p: Profile, coverage: Coverage | None) -> tuple[list[str], list[str]]:
    """(the "Last 30 days" line, the notices under the card); both empty without a tracker."""
    if coverage is None:
        return [], []
    n = coverage.total
    head = f"{_plural(n, 'role')} {'fits' if n == 1 else 'fit'} this ({coverage.strong} strong)." \
        if n else "nothing fit this yet."
    if "us" not in p.locations and coverage.us_extra:
        head += f" Anywhere in the US would add {coverage.us_extra}."
    notes = [f"*Heads up: the companies I watch posted only {k} {vocab.FIELD_LABELS.get(f, f)} "
             f"{'role' if k == 1 else 'roles'} for students in the last 30 days. They'll reach you when "
             "they appear.*" for f, k in coverage.thin]
    wants_hourly = vocab.HOURLY_HINT_FIELDS.intersection(p.fields) and "hourly" not in p.levels
    if wants_hourly and coverage.hourly_hint > 0:
        notes.append(f"*Tip: {coverage.hourly_hint} part-time & hourly roles (like Pharmacy Technician) "
                     "fit your fields. Add **Part-time & hourly** under Looking for if you want them.*")
    return [f"**Last 30 days:** {head}"], notes


def _render_card(p: Profile, lists: dict, shown: Mapping[str, int], top: list, bottom: list,
                 gemini: bool) -> str:
    def items(name: str) -> str:
        _, values, sep = lists[name]
        return _capped(values, shown[name], sep)

    companies = " · ".join(f"{lists[n][0]} {items(n)}" for n in ("only", "hidden") if lists[n][1])
    body = [f"**Studying:** {_studying(p)}" if p.majors or p.minors or p.degree else None,
            f"**Graduating:** {_month_year(p.grad_year, p.grad_month)}" if p.grad_year else None,
            *(f"**{lists[n][0]}:** {items(n)}" for n in ("skills", "keywords", "fields", "levels",
                                                           "where", "terms") if lists[n][1]),
            f"**Companies:** {companies}" if companies else None,
            f"**Alerts:** {_alerts(p, gemini)}"]
    return "\n".join(part for part in (*top, *body, *bottom) if part)


def _shrunk(lists: dict, shown: Mapping[str, int], floor: int) -> dict[str, int] | None:
    """`shown` with one item off the longest list still above `floor`; None when none is."""
    open_ = [n for n in lists if shown[n] > floor]
    if not open_:
        return None
    longest = max(open_, key=lambda n: len(_capped(lists[n][1], shown[n], lists[n][2])))
    return {**shown, longest: shown[longest] - 1}


def card_text(p: Profile, *, mode: str, coverage: Coverage | None, feedback: str | None = None,
              evidence: str | None = None, header: str | None = None, notices: Sequence[str] = (),
              companies: Mapping[str, str] | None = None, gemini: bool = False) -> str:
    """J3, one renderer for a draft and a saved profile. <= CARD_MAX: lists shorten with "(+N more)"
    down to three items, then notices go from the end, then lists go down to one. `companies`
    (norm -> display name) names filtered companies; without it the stored normalised names show.
    `gemini`: the host has a key, so the Alerts line says whether Gemini checks them."""
    if mode not in ("draft", "saved"):
        raise ValueError(f"unknown card mode {mode!r}")
    lists = _card_lists(p, companies)
    shown = {n: min(len(values), _SHOWN.get(n, len(values))) for n, (_, values, _) in lists.items()}
    last30, notes = _coverage_lines(p, coverage)
    notes = [*notes, *notices]
    top = [feedback, dm_blocked_banner() if p.dm_failures >= 3 else None, header,
           f"*{evidence}*" if evidence else None]
    while True:
        card = _render_card(p, lists, shown, top, [*last30, *notes], gemini)
        if len(card) <= CARD_MAX:
            return card
        fewer = _shrunk(lists, shown, _LIST_FLOOR)
        if fewer is None and notes:
            notes = notes[:-1]
            continue
        fewer = fewer or _shrunk(lists, shown, 1)
        if fewer is None:
            return card[:CARD_MAX].rsplit("\n", 1)[0]
        shown = fewer


def cancelled_draft() -> str:
    return "Cancelled. Nothing was kept."


# ------------------------------------------------------------------ J5, J6: saving and match blocks

def cadence_phrase(alerts: str, alert_hour: int) -> str:
    phrases = {"hourly": "hourly (at most one DM an hour)",
               "daily": f"every day at {_hour(alert_hour)}",
               "weekly": f"every Monday at {_hour(alert_hour)}",
               "off": "never, because alerts are off"}
    if alerts not in phrases:
        raise ValueError(f"unknown alert cadence {alerts!r}")
    return phrases[alerts]


def _schedule(p: Profile, now: float | None) -> str:
    """The cadence, and when a pause holds it back, the day it ends."""
    phrase = cadence_phrase(p.alerts, p.alert_hour)
    now = time.time() if now is None else now
    if p.alerts != "off" and p.paused_until is not None and p.paused_until > now:
        return f"{phrase}, once your pause ends on {_month_day(p.paused_until)}"
    return phrase


_CANT_DM = "I can't DM you yet: turn on Direct Messages from this server, then run `/internships ping`."


def _dm_promise(p: Profile, now: float | None) -> str:
    """What happens to new matches, true for this person's alert state (J5)."""
    if p.alerts == "off":
        return "Alerts are off, so I won't DM you new ones; `/internships ping` turns them on."
    if p.dm_failures >= DM_FAILURE_LIMIT:
        return _CANT_DM
    return f"New matches will reach you by DM {_schedule(p, now)}."


def _when_one_appears(p: Profile, now: float | None) -> str:
    """The empty state's closing promise (J8), made only where it will be kept."""
    if p.alerts == "off":
        return "Alerts are off, so I won't DM you when one appears; `/internships ping` turns them on."
    if p.dm_failures >= DM_FAILURE_LIMIT:
        return _CANT_DM
    now = time.time() if now is None else now
    if p.paused_until is not None and p.paused_until > now:
        return f"I'll DM you when one appears, once your pause ends on {_month_day(p.paused_until)}."
    return "I'll DM you the moment one appears."


def _posted(m: Match, now: float) -> str:
    verb, ts = ("posted", m.cand.published) if m.cand.published else ("seen", m.cand.first_seen)
    age = max(0.0, now - ts)
    days, hours = int(age // _DAY_S), int(age // _HOUR_S)
    return f"{verb} {f'{days}d ago' if days else f'{hours}h ago' if hours else 'just now'}"


def _why(reasons: Sequence[str], limit: int = WHY_MAX) -> str:
    """Whole reasons, escaped, joined " · ", within `limit` (WHY_MAX)."""
    kept = []
    for reason in (safe_inline(r, REASON_MAX) for r in reasons):
        if reason and len(" · ".join([*kept, reason])) <= limit:
            kept.append(reason)
    return " · ".join(kept)


#: How a role's Gemini verdict reads, before its reason (intern_fit).
_FIT_WORDS = {"fit": "Gemini: fits", "unsure": "Gemini: not sure", "no_fit": "Gemini: doesn't fit"}


def _fit_line(m: Match) -> str | None:
    """The fit check's verdict and reason, for a role that has one. The reason is outside
    text, escaped and cut like any other."""
    if m.fit is None or m.fit[0] not in _FIT_WORDS:
        return None
    verdict, reason = m.fit
    return f"{_FIT_WORDS[verdict]} · {safe_inline(reason, REASON_MAX)}"


def match_block(m: Match, now: float, *, with_why: bool = True) -> str:
    """J6. Under 1,000 characters whatever the posting says. A role the fit check has a
    verdict on shows it, with its reason, above the link, and the matcher's reasons give
    it the room."""
    fit = _fit_line(m)
    c, why = m.cand, _why(m.why, WHY_MAX - len(fit or "")) if with_why else ""
    more = f" (+{m.more} more location{'' if m.more == 1 else 's'})" if m.more > 0 else ""
    meta = (safe_inline(m.place, PLACE_MAX), _LEVEL_WORDS.get(c.level, ""), c.term[0], _posted(m, now))
    company = safe_inline(c.company, COMPANY_MAX) or "Unknown company"
    lines = [f"**{company}** — {safe_inline(c.title, TITLE_MAX)}",
             " · ".join(x for x in meta if x) + more]
    if with_why and (m.band or why):
        lines.append(" · ".join(part for part in (m.band, f"Why: {why}" if why else "") if part))
    if fit:
        lines.append(fit)
    url = safe_url(c.url)
    return "\n".join(lines + ([f"<{url}>"] if url else []))


def saved_followup(matches: Sequence[Match], now: float, p: Profile) -> list[str]:
    """J5 step 1: the best few of the last two weeks, packed into <= ALERT_MAX chunks."""
    blocks = [match_block(m, now) for m in matches[:FIRST_MATCHES]]
    intro = "**Saved.**" + (f" Your best matches from the last {FIRST_MATCH_DAYS} days:" if blocks else "")
    footer = f"{_dm_promise(p, now)} `/internships matches` shows everything any time."
    return pack([intro, *blocks, footer], ALERT_MAX, "\n\n")


def welcome_dm(p: Profile, *, now: float | None = None) -> str:
    """Sent only when it can arrive, so it never mentions refused DMs; a pause it does name."""
    return ("Hi! I'm DIAYN, an internship finder. New roles that fit your profile will arrive here "
            f"{_schedule(p, now)}. Use the menu under each alert to hide a role, "
            "or `/internships profile` to change anything.")


def dm_blocked_text() -> str:
    return ("**I couldn't DM you**, so alerts can't reach you yet. In this server, open the server menu, "
            "then **Privacy Settings**, and turn on **Direct Messages**. Then press **Try again**. "
            "Your profile is saved either way.")


def dm_retry_ok() -> str:
    return "It worked, check your DMs."


# ------------------------------------------------------------------ J7: alerts

_DIGEST = {"daily": " · daily digest", "weekly": " · weekly digest"}


def migrated_intro() -> str:
    """The first DM to a subscriber `diayn.py import-legacy` brought over: it comes from a bot
    they have never used, so it says which bot this is and why it is writing."""
    return ("**Hi, this is DIAYN, an internship finder bot.** You were subscribed to internship "
            "alerts from another bot in a server you share with this bot. Those alerts have moved "
            "here, and I copied your filters, so you still get the tech internships you signed up "
            "for. I also match roles to *your* major now, for any major. `/internships profile` "
            "tailors it to you; `/internships ping` turns these DMs off; `/internships delete` "
            "erases what I hold. Before an alert I may ask Google's Gemini whether a role suits "
            "your filters, never your name or Discord id; **Turn the Gemini check off** in your "
            "profile card's Alerts menu stops that.")


def fit_notice_line() -> str:
    """The Gemini fit check's notice, on the alert that carries it to someone the start card,
    the consent screen and help never told: a profile from before the host had a key. That
    alert goes out unchecked, and the check starts with the next one."""
    return ("**Gemini checks your alerts from the next one on.** Before an alert I'll ask "
            "Google's Gemini whether each role suits your profile's labels, never your resume, "
            "name or Discord id. This alert wasn't checked. **Turn the Gemini check off** in "
            "your profile card's Alerts menu stops it.")


def _alert_text(total: int, blocks: list[str], *, cadence: str, intro: bool, catch_up: bool,
                expiry: str | None, with_controls: bool, fit_notice: bool = False) -> str:
    shown = len(blocks)
    head = (f"**Welcome back: {_plural(total, 'new role')} while you were paused.**"
            + (f" Here are the best {shown}." if 0 < shown < total else "")) if catch_up \
        else f"**{_plural(total, 'new role')} for you**{_DIGEST.get(cadence, '')}"
    footer = ("Not quite right? Hide a role below, or change what you get with `/internships profile`."
              if with_controls else "Not quite right? Change what you get with `/internships profile`.")
    tail = ([f"...and {total - shown} more: `/internships matches`"] if total > shown else []) \
        + [footer] + ([expiry] if expiry else [])
    top = "\n".join(([migrated_intro()] if intro else [])
                    + ([fit_notice_line()] if fit_notice else []) + [head])
    return "\n\n".join([top, *blocks, "\n".join(tail)])


def format_alert(matches: Sequence[Match], now: float, *, cadence: str, intro: bool,
                 catch_up: bool, expiry_note: str | None, with_controls: bool,
                 fit_notice: bool = False) -> tuple[str, tuple[Match, ...]]:
    """J7 and 4.5.6: one DM of at most ALERT_MAX characters and ALERT_LISTINGS_MAX listings, best
    first, the rest counted in "...and N more". With `fit_notice`, it leads with the Gemini
    check's notice (`fit_notice_line`). Returns the text and the matches it shows (only those
    feed `hide_options`)."""
    blocks = [match_block(m, now) for m in matches[:ALERT_LISTINGS_MAX]]
    for shown in range(len(blocks), -1, -1):
        body = _alert_text(len(matches), blocks[:shown], cadence=cadence, intro=intro,
                           catch_up=catch_up, expiry=expiry_note, with_controls=with_controls,
                           fit_notice=fit_notice)
        if len(body) <= ALERT_MAX:
            return body, tuple(matches[:shown])
    return body[:ALERT_MAX], ()


def hide_options(shown: Sequence[Match]) -> tuple[tuple[str, str], ...]:
    """(value, label) for the alert's hide menu: each role, then each company once; <= 25.
    Labels are plain text (select options are not markdown); a value appears once."""
    roles, firms = {}, {}
    for m in shown:
        firm = _plain(m.cand.company) or "this company"
        roles.setdefault(f"r:{m.cand.rk_hash}:{m.cand.ck_hash}", f"Hide this role: {firm} — {m.cand.title}")
        if m.cand.company_norm:
            firms.setdefault(f"c:{m.cand.company_norm[:90]}", f"Hide everything from {firm}")
    return tuple((value, _cut(_plain(label), OPTION_MAX, escape=False))
                 for value, label in {**roles, **firms}.items())[:OPTIONS_MAX]


_ALERT_REPLIES = {
    "hidden_role": "Hidden. You won't be alerted about that role again.",
    "hidden_company": "Hidden. Nothing from that company will reach you. `/internships profile` -> "
                      "More filters lists hidden companies.",
    "stopped": "Alerts are off. Your profile is saved; `/internships ping` turns them back on.",
    "resumed": "Alerts are back on.",
    "no_profile": "I don't have a profile for you any more. `/internships profile` sets one up.",
}


def alert_reply(kind: str, *, until: float | None = None) -> str:
    if kind == "paused" and until is not None:
        return f"Paused until {_month_day(until)}. You'll get a catch-up of the best ones then."
    if kind not in _ALERT_REPLIES:
        raise ValueError(f"unknown alert reply {kind!r} ('paused' needs `until`)")
    return _ALERT_REPLIES[kind]


# ------------------------------------------------------------------ J8-J10: lists and empty states

_SORT_WORDS = {"best": "best first", "newest": "newest first"}


def matches_header(n: int, *, days: int, sort: str) -> str:
    if sort not in _SORT_WORDS:
        raise ValueError(f"unknown sort {sort!r}")
    showing = f" — showing {MATCHES_MAX}" if n > MATCHES_MAX else ""
    return f"**{_plural(n, 'role')} for you** (last {days} days · {_SORT_WORDS[sort]}){showing}"


def matches_messages(matches: Sequence[Match], now: float, *, header: str,
                     with_why: bool = True) -> list[str]:
    """The header and every block given (callers slice), packed into <= ALERT_MAX chunks."""
    return pack([header, *(match_block(m, now, with_why=with_why) for m in matches)], ALERT_MAX, "\n\n")


def matches_no_profile() -> str:
    return "Meanwhile, `/internships recent` lists every field."


def browse_header(n: int, *, field: str | None, level_label: str, where_label: str, days: int) -> str:
    """`n` counts every role that matched; the list itself stops at BROWSE_MAX."""
    named = f" in {vocab.FIELD_LABELS.get(field) or safe_inline(field, 60)}" if field else ""
    showing = f" — showing {BROWSE_MAX}" if n > BROWSE_MAX else ""
    return (f"**{_plural(n, 'recent role')}**{named} ({safe_inline(level_label, 80)} · "
            f"{safe_inline(where_label, 80)} · last {days} days){showing}")


def browse_empty(*, days: int, companies: int) -> str:
    return (f"Nothing on record for that in the last {days} days. The tracker checks {companies} "
            "companies every 15 minutes.")


def empty_state(p: Profile, relax: Sequence[Relaxation], *, pool: int, companies: int, days: int,
                now: float | None = None) -> str:
    fields = _field_phrase(p.fields)
    return "\n".join([
        f"**No matches in the last {days} days for {fields}.**",
        f"I checked {pool:,} postings from {companies} companies; none were {fields} roles at your "
        f"level{_loc_phrase(p)}.",
        "That's about which companies I watch, not about you. Most of them post tech, aerospace, "
        "retail and finance jobs.",
        *(f"- **{safe_inline(r.label, BUTTON_MAX)}** would find {r.gain}." for r in relax[:3]),
        f"{_when_one_appears(p, now)} Know a company that hires for your field? Ask whoever runs "
        "this bot to add it."])


def relax_button_label(r: Relaxation) -> str:
    suffix = f" (+{r.gain})"
    return _cut(_plain(r.label), BUTTON_MAX - len(suffix), escape=False) + suffix


def quiet_note(p: Profile, relax: Sequence[Relaxation], *, companies: int) -> str:
    return "\n".join([
        f"**Still watching, nothing new for you yet.** In the last 14 days the {companies} companies "
        f"I watch posted no new {_field_phrase(p.fields)} roles at your level{_loc_phrase(p)}.",
        *(f"- **{safe_inline(r.label, BUTTON_MAX)}** in `/internships profile` would add {r.gain}."
          for r in relax[:3]),
        "`/internships ping` turns these DMs off."])


def expiry_note(delete_on: float) -> str:
    return (f"I'll delete your internship profile on {_month_day(delete_on)} because it hasn't been "
            "used in a year. Run any `/internships` command before then to keep it.")


# ------------------------------------------------------------------ J11: what is stored

_DELETE_HEADER = "**This is everything the internship finder holds about you:**"
_DELETE_QUESTION = "Delete all of it? Alerts stop and this can't be undone."
_SOURCE_WORDS = {"resume": "your resume", "pasted": "pasted text", "manual": "picked by hand",
                 "migrated": "copied from the old tracker"}
_TIMESTAMPS = frozenset({"paused_until", "cursor", "last_run_at", "last_sent_at", "last_quiet_at",
                         "left_at", "expiry_warned_at", "created_at", "updated_at", "active_at",
                         "access_lapsed_at", "fit_notice_at"})
_ID_LISTS = {"majors": MAJOR_BY_ID, "minors": MAJOR_BY_ID, "skills": SKILL_BY_ID,
             "fields": vocab.FIELD_LABELS, "levels": vocab.LEVEL_LABELS}
_SCALARS = {
    "source": lambda v: _SOURCE_WORDS.get(v),
    "degree": lambda v: _DEGREE_LABELS.get(v),
    "grad_month": lambda v: _MONTHS[v - 1] if isinstance(v, int) and 1 <= v <= 12 else None,
    "alert_hour": lambda v: _hour(v) if isinstance(v, int) else None,
    "min_score": lambda v: f"{v} ({_MIN_SCORE_WORDS[v]})" if v in _MIN_SCORE_WORDS else None,
}


def _stored(column: str, value: object) -> str:
    """One stored value in words, written out in full: showing everything is the point (D19)."""
    if value is None or (isinstance(value, (list, tuple)) and not value):
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if column == "locations":
        return describe_locations(value).replace(" · ", ", ") or "none"
    if column in _ID_LISTS:
        return ", ".join(_labels(list(value), _ID_LISTS[column])) or "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(safe_inline(str(v), 60) for v in value)
    if column in _TIMESTAMPS and isinstance(value, (int, float)):
        local = datetime.fromtimestamp(value, intern_clock.zone())
        return f"{local:%Y-%m-%d %H:%M} {intern_clock.zone_label()}"
    return _SCALARS.get(column, lambda v: None)(value) or safe_inline(str(value), 60)


def privacy_text(rows: Mapping[str, object]) -> tuple[str, ...]:
    """J11: one line per stored column in STORED_COLUMNS order, then the two ledger counts."""
    rows = rows if isinstance(rows, Mapping) else {}
    lines = tuple(f"**{description}:** {_stored(column, rows.get(column))}"
                  for column, description in STORED_COLUMNS.items())
    return lines + (
        f"**Roles I've alerted you about:** {int(rows.get('sent_count') or 0)} "
        f"(kept {SENT_RETAIN_S // _DAY_S} days)",
        f"**Roles you hid:** {int(rows.get('hidden_count') or 0)} (kept {HIDDEN_RETAIN_S // _DAY_S} days)")


def _grant_kept(granted: bool | None) -> str:
    """What deleting leaves: a grant by id is whoever runs this bot's record, kept until they
    revoke it. `granted` None: it could not be read, so the sentence is conditional."""
    if granted is None:
        return ("If whoever runs this bot granted you access by your Discord id, that grant "
                "stays until they revoke it.")
    return ("Whoever runs this bot granted you access by your Discord id, and that grant "
            "stays until they revoke it.") if granted else ""


def delete_confirm(privacy: Sequence[str], *, granted: bool | None = False) -> list[str]:
    """J11: header, the lines, a blank line and the question, packed so no line is split.
    A grant by id, which the delete leaves, is named before the question."""
    kept = _grant_kept(granted)
    return pack([_DELETE_HEADER, *privacy, "", *([kept] if kept else []), _DELETE_QUESTION],
                ALERT_MAX, "\n")


def deleted_text(*, granted: bool | None = False) -> str:
    done = ("**Done. Your profile and its history are deleted.** Messages I already sent stay in "
            "your DMs until you delete them, and Discord keeps its own copy of files you uploaded.")
    kept = _grant_kept(granted)
    return f"{done} {kept}" if kept else done


def nothing_held(*, granted: bool | None = False) -> str:
    kept = _grant_kept(granted)
    return f"I don't hold a profile or any history for you. {kept}" if kept else \
        "I don't hold anything about you."


# ------------------------------------------------------------------ J12 and 1.3: commands

def ping_reply(p: Profile, *, action: str, now: float | None = None) -> str:
    """J12. Setting a cadence leaves a pause in place, and the reply says so."""
    if action == "off":
        return "Alerts are off. Your profile is saved; run `/internships ping` again to turn them back on."
    if action not in ("on", "set"):
        raise ValueError(f"unknown ping action {action!r}")
    schedule = _schedule(p, now)
    resume = (" `/internships ping` with no options resumes them now."
              if schedule != cadence_phrase(p.alerts, p.alert_hour) else "")
    return f"{'Alerts are on:' if action == 'on' else 'Alerts:'} {schedule}.{resume}"


def help_text(*, pdf_ok: bool, companies: int, gemini: bool = False) -> list[str]:
    """Two chunks: what the commands do, then the disclosure (with the fit check's note when
    the host has a key) and whether PDFs can be read."""
    commands = (
        "**Internship finder**\n"
        "It matches internships, co-ops and new-grad roles to *your* major, for any major, and DMs you "
        "new ones. Every reply is private.\n\n"
        "**Commands**\n"
        "- `/internships profile` — set up or edit your profile. Attach a resume to fill it in, or "
        "pick your field by hand.\n"
        "- `/internships matches` — roles that fit you, best first (or newest first), with why each "
        "matched.\n"
        "- `/internships recent` — browse by field, level and place. No profile needed.\n"
        "- `/internships ping` — alerts on or off, hourly, daily or weekly.\n"
        "- `/internships info <role>` — salary, description and fit for one posting.\n"
        "- `/internships delete` — see everything stored about you and erase it (an access "
        "grant made by your id stays until whoever runs this bot revokes it).\n\n"
        f"**Heads up:** the {companies} companies I watch are mostly tech, aerospace, retail and "
        "finance, so some majors see only a few roles a month. Direct Messages from this server must "
        "be on for alerts to reach you.")
    status = "PDF reading: available." if pdf_ok else \
        "PDF reading: not installed on this bot; Word, .txt and pasting work."
    return [commands, f"{_disclosed(gemini)}\n\n{status}"]


def unknown_choice(kind: str, value: str) -> str:
    noun = {"field": "field", "where": "place", "location": "place", "role": "role"}.get(kind)
    if noun is None:
        raise ValueError(f"unknown choice kind {kind!r}")
    return (f"I don't know the {noun} '{safe_inline(value, 60)}'. Start typing and pick one of the "
            "suggestions.")


def fit_line(match: Match | None, reason: str | None) -> str:
    """`info`, for someone with a profile. A match below the list threshold reads "Weak match"."""
    if match is None:
        return (f"**For you:** outside your filters ({safe_inline(reason, REASON_MAX)})." if reason
                else "**For you:** outside your filters.")
    why = _why(match.why)
    return f"**For you:** {match.band or 'Weak match'}" + (f" · Why: {why}" if why else "")


def info_blocks(block: str, salary: str | None, description: str) -> list[str]:
    """1.3 `info`: the listing, its salary line, then the description excerpt or why there is none."""
    return [block, f"**Salary:** {safe_inline(salary or 'not listed', 200)}",
            description or "No description available — the posting may have closed."]


def generic_failure() -> str:
    return "Something went wrong on my side. Try again in a minute."


def disabled_finder(error: str) -> str:
    return (f"The internship finder is switched off on this bot ({safe_inline(error, 200)}). "
            "Whoever runs this bot can check its log.")


def disabled_tracker(error: str) -> str:
    return (f"The internship tracker is disabled: {safe_inline(error, 200)} Fix the postings database "
            "and restart the bot.")


def owner_only() -> str:
    return "That one is only for whoever runs this bot."


def no_access() -> str:
    """The refusal for anyone this bot is not open to. It never names the owner."""
    return "This bot is private. Ask whoever runs it for access."


# ------------------------------------------------------------------ /diayn: the owner's commands
# `who` is a mention: it shows the person's name, and the reply, sent with no mentions
# allowed, pings nobody. A server's name is escaped: its owner chose it.

SERVER_NAME_MAX = 100
_LAPSE_DAYS = ACCESS_GRACE_S // _DAY_S


def granted_user(who: str, *, added: bool) -> str:
    if added:
        return f"{who} may use this bot now."
    return f"{who} already had access of their own; nothing changed."


def granted_server(name: str, *, added: bool) -> str:
    server = safe_inline(name, SERVER_NAME_MAX)
    if added:
        return f"Everyone in **{server}** may use this bot now, here and in DMs."
    return f"**{server}** already had access; nothing changed."


def revoked_user(who: str, *, removed: bool, still: bool) -> str:
    """`still`: they may use the bot anyway, as its owner or through a server that has access."""
    through = "they run this bot, or they are in a server that has access."
    if removed and not still:
        return (f"{who} no longer has access. Their alerts stop at the next delivery, and their "
                f"profile is deleted after {_LAPSE_DAYS} days without access.")
    if removed:
        return f"{who} lost their own grant but still has access: {through}"
    if still:
        return f"{who} had no grant of their own, and still has access: {through}"
    return f"{who} had no access to take away."


def revoked_server(name: str, *, removed: bool) -> str:
    server = safe_inline(name, SERVER_NAME_MAX)
    if removed:
        return (f"**{server}** no longer has access. Its members keep it only through a grant of "
                "their own or another server's; for everyone else, alerts stop at the next "
                f"delivery, and profiles are deleted after {_LAPSE_DAYS} days without access.")
    return f"**{server}** had no grant; nothing changed."


def needs_a_server(command: str) -> str:
    return f"Run `/diayn {command} server` inside the server you mean; there is none here."


def access_summary(*, owners: int, users: int, servers: Sequence[str], gone: int) -> list[str]:
    """`/diayn access`: counts, and the granted servers this bot is in by name. Never a
    person, and never an id. `gone` counts granted servers this bot is no longer in."""
    lines = ["**Who may use this bot**",
             f"Whoever runs it: always ({owners} {'account' if owners == 1 else 'accounts'}).",
             f"People granted by id: {users}",
             f"Servers granted: {len(servers) + gone}"]
    if servers:
        lines.append(", ".join(sorted(safe_inline(s, SERVER_NAME_MAX) for s in servers)))
    if gone:
        lines.append(f"{gone} {'server' if gone == 1 else 'servers'} this bot is no longer in; "
                     "`diayn.py revoke --server <id>` takes a grant away without Discord.")
    return lines


def not_yours() -> str:
    return "That isn't yours."


def _bucket(value: object) -> str:
    """A count as `debug` shows it: 1 and 2 read "<3", so no one person is singled out."""
    n = int(value or 0)
    return "<3" if 1 <= n <= 2 else str(n)


def fit_debug_lines(*, key: bool, model: str, requests: int, prompt_tokens: int,
                    output_tokens: int, rpd: int, rpm: int, batch: int, zone: str, cached: int,
                    opted_out: int, last_error: tuple[str, float] | None, now: float) -> list[str]:
    """The Gemini fit check (intern_fit) in `/diayn debug`: today's requests and tokens
    against its own limits, what is cached, and the class of its last fallback. Counts
    only; `zone` names the zone its day resets in."""
    head = "**Gemini fit check**"
    if not key:
        return [head, "off: this bot has no Gemini key, so alerts go out unchecked"]
    failed = (f"{last_error[0]} ({_ago(last_error[1], now)})" if last_error
              else "none since the bot started")
    return [head,
            f"today: {requests}/{rpd} requests · {prompt_tokens:,} tokens in · "
            f"{output_tokens:,} out · model `{model}`",
            f"limits: {batch} roles a request · {rpm} req/min · {rpd} req/day · resets at "
            f"midnight {zone}",
            f"cached verdicts: {cached:,} · profiles that turned it off: {_bucket(opted_out)}",
            f"last fallback to unchecked: {failed}"]


def debug_lines(summary: Mapping[str, int], report: Mapping[str, float | None],
                supply: Mapping[str, int], *, pdf_ok: bool, migrated: float | None,
                now: float | None = None) -> list[str]:
    """1.3: counts only, bucketed, as short lines for the caller to pack with the rest.
    `migrated` is how many profiles `diayn.py import-legacy` wrote (intern_meta's
    `legacy_imported`), or None when there has been no import."""
    def n(key: str, source: Mapping = summary) -> str:
        return _bucket(source.get(key))

    def tick(key: str) -> str:
        return n(f"delivery_last_{key}", report)

    at = time.time() if now is None else now
    coverage = [f"{label} {n(f, supply)} · {n(f'field:{f}')}" for f, label in vocab.FIELDS]
    return [
        "**Internship finder**",
        f"profiles: {n('profiles')} · alerts on: {n('alerting')} (hourly {n('hourly')} · daily "
        f"{n('daily')} · weekly {n('weekly')}) · DMs closed: {n('dm_blocked')} · left every shared "
        f"server: {n('left')} · without access: {n('no_access')}",
        f"last delivery tick: {_ago(report.get('delivery_last_at'), at)} · due {tick('due')} · sent "
        f"{tick('sent')} · nothing new {tick('empty')} · DMs refused {tick('forbidden')}",
        "resume parsing: PDF available" if pdf_ok
        else "resume parsing: PDF unavailable (install pypdf); DOCX, TXT and paste work",
        f"legacy import: {_bucket(migrated)} subscribers imported" if migrated is not None
        else "legacy import: none",
        "**Coverage by field, last 30 days** (roles for students in the US or unlisted · profiles that "
        "picked it)",
        *pack(coverage, _DEBUG_LINE, " | "),
    ]
