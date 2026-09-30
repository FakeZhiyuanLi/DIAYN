"""
resume_parse.py
~~~~~~~~~~~~~~~
From an uploaded resume to a draft profile: closed-vocabulary ids and a date,
never the resume's own words.

Everything past `sniff` runs **only inside the worker process**
(`resume_worker`): reading a PDF hands a stranger's file to a parser, and the
worker is where that may go wrong. The bot calls `sniff` before downloading and
`validate_draft` on what the worker sends back; it never extracts text itself.

There is no pattern here for a name, an email, a phone, an address, a school,
a GPA or an employer. Degrees, majors, skills and a graduation date are all
looked up in `resume_lexicon`, so a draft can only hold ids from a fixed list —
the privacy guarantee, and why `validate_draft` rebuilds a draft rather than
trusting one. Patterns and rule orders are verbatim from the measured reference
(spec 4.4); one that looks loose is usually loose on purpose, and each place
this departs from it says so and why. `pypdf` is
imported inside `extract_text` only, the Word reader uses no XML parser, and
nothing here opens, writes or logs.
"""

import html
import importlib.util
import io
import re
import unicodedata
import zipfile
from datetime import date

from intern_vocab import DEGREE_IDS, FIELD_IDS, MAX_FIELDS, MAX_MAJORS, MAX_MINORS, MAX_SKILLS
from resume_lexicon import MAJOR_BY_ID, SKILL_BY_ID, fields_for, find_skills, majors_alone, majors_in

MAX_BYTES = 2 * 1024 * 1024
MAX_PDF_PAGES = 10
PDF_READ_PAGES = 4
MAX_TEXT_CHARS = 30_000
MIN_DOC_CHARS = 200
MIN_PASTE_CHARS = 20
MAX_DOCX_XML = 5 * 1024 * 1024
KINDS = ("pdf", "docx", "txt")

#: Every code `intern_text.upload_error` has copy for (spec J2d). `rate_limited`
#: is raised by the bot rather than here, but it is the same vocabulary.
REASONS: frozenset[str] = frozenset({
    "too_big", "empty", "bad_type", "type_mismatch", "bad_magic", "no_pdf_support",
    "encrypted", "too_many_pages", "no_text", "corrupt", "zip_bomb", "xml_entity",
    "timeout", "worker_failed", "busy", "already_reading", "rate_limited", "paste_short"})


class ResumeRefusal(Exception):
    """
    Why a resume could not be read: a code from `REASONS`, and nothing else. An
    unknown code becomes `worker_failed`, so it still reaches the user as copy.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in REASONS else "worker_failed"
        super().__init__(self.reason)


def pdf_supported() -> bool:
    """Whether `pypdf` can be imported here, without importing it."""
    try:
        return importlib.util.find_spec("pypdf") is not None
    except (ImportError, ValueError):
        return False


# ------------------------------------------------------------------ checks (2.1)

#: What a content type may start with, parameters removed (a .docx may be a generic zip).
_CONTENT_TYPES = {
    "pdf": ("application/pdf",),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "application/octet-stream", "application/zip"),
    "txt": ("text/plain",),
}
#: How far into a text file a NUL byte means it is not text.
_NUL_WINDOW = 4096
_DOCUMENT = "word/document.xml"


def sniff(filename: "str | None", content_type: "str | None", size: "int | None") -> str:
    """
    Metadata only (2.1). Returns the kind or raises ResumeRefusal(empty|too_big|
    bad_type|type_mismatch|no_pdf_support). no_pdf_support when kind == "pdf"
    and not pdf_supported(). An unknown size is left to the worker's own cap.
    """
    if isinstance(size, int) and not isinstance(size, bool):
        if size < 1:
            raise ResumeRefusal("empty")
        if size > MAX_BYTES:
            raise ResumeRefusal("too_big")
    has_extension = isinstance(filename, str) and "." in filename
    kind = filename.rsplit(".", 1)[1].lower() if has_extension else None
    if kind not in KINDS:
        raise ResumeRefusal("bad_type")
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base and not base.startswith(_CONTENT_TYPES[kind]):
        raise ResumeRefusal("type_mismatch")
    if kind == "pdf" and not pdf_supported():
        raise ResumeRefusal("no_pdf_support")
    return kind


def _is_word_archive(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _DOCUMENT in archive.namelist()
    except Exception:  # an untrusted archive: whatever zipfile raises, it is not a .docx
        return False


def check_magic(data: bytes, kind: str) -> None:
    """The downloaded bytes are what the name said (2.1). Raises bad_magic / too_big."""
    if len(data) > MAX_BYTES:
        raise ResumeRefusal("too_big")
    if kind not in KINDS:
        raise ResumeRefusal("bad_type")
    if kind == "pdf":
        looks_right = data.startswith(b"%PDF-")
    elif kind == "docx":
        looks_right = data.startswith(b"PK\x03\x04") and _is_word_archive(data)
    else:
        looks_right = b"\x00" not in data[:_NUL_WINDOW]
    if not looks_right:
        raise ResumeRefusal("bad_magic")


# ------------------------------------------------------------------ extraction (4.4.1)

# The reference's `[^>]*` let a tag that never closed rescan the rest of the
# document from every later "<w:t": quadratic, so a 7 KB upload held a worker
# until its CPU limit. Well-formed attributes hold no "<", so stopping there
# too abandons such a tag at the next one, and each pattern reads in one pass.
DOCX_T = re.compile(r"<w:t(?:\s[^<>]*)?>([^<]*)</w:t>")
_DOCX_BREAK = re.compile(r"<w:(?:tab|br|cr)\b[^<>]*/>")
#: A tab or break becomes a space inside a run, so it survives (see `docx_text`).
_DOCX_SPACE = "<w:t> </w:t>"


def _document_xml(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo(_DOCUMENT)
            if info.file_size > MAX_DOCX_XML:
                raise ResumeRefusal("zip_bomb")
            return archive.read(info).decode("utf-8", "replace")
    except ResumeRefusal:
        raise
    except Exception:  # an untrusted archive: anything zipfile raises means unreadable
        raise ResumeRefusal("corrupt") from None


def docx_text(data: bytes) -> str:
    """
    The text of a .docx, one line per paragraph (4.4.1). Raises zip_bomb /
    xml_entity / corrupt. No XML parser: a DOCTYPE or entity is refused, never
    expanded. The member is capped at 5 MiB before any pattern runs, and every
    pattern is linear, so the worst file under the cap is read in well under a
    second. One change from the reference: a tab or break becomes a space
    *inside a run*, since only run text is kept — the reference's bare space was
    dropped, so "Python<tab>SQL" came out as one word matching neither skill.
    """
    raw = _document_xml(data)
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise ResumeRefusal("xml_entity")
    raw = raw.replace("</w:p>", "\n")
    raw = _DOCX_BREAK.sub(_DOCX_SPACE, raw)
    lines = ("".join(DOCX_T.findall(line)) for line in raw.split("\n"))
    return html.unescape("\n".join(lines))


def _pdf_text(data: bytes) -> str:
    try:
        import pypdf
    except ImportError:
        raise ResumeRefusal("no_pdf_support") from None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not _unlocked(reader):
            raise ResumeRefusal("encrypted")
        pages = reader.pages
        if len(pages) > MAX_PDF_PAGES:
            raise ResumeRefusal("too_many_pages")
        first = [pages[n] for n in range(min(len(pages), PDF_READ_PAGES))]
        return "\n".join(page.extract_text() or "" for page in first)
    except ResumeRefusal:
        raise
    except Exception:  # pypdf on a stranger's file: any failure means unreadable
        raise ResumeRefusal("corrupt") from None


def _unlocked(reader) -> bool:
    """Whether a PDF opens with the empty password, as many "protected" ones do."""
    try:
        return bool(reader.decrypt(""))
    except Exception:  # an algorithm pypdf cannot handle is still a locked file
        return False


def _txt_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def extract_text(data: bytes, kind: str) -> str:
    """
    The resume's text, cut to 30,000 characters (4.4.1); raises ResumeRefusal.
    A PDF or .docx under 200 visible characters is almost always a scan.
    """
    readers = {"pdf": _pdf_text, "docx": docx_text, "txt": _txt_text}
    if kind not in readers:
        raise ResumeRefusal("bad_type")
    text = readers[kind](data)[:MAX_TEXT_CHARS]
    visible = sum(not char.isspace() for char in text)
    if kind == "txt" and visible < MIN_PASTE_CHARS:
        raise ResumeRefusal("paste_short")
    if kind != "txt" and visible < MIN_DOC_CHARS:
        raise ResumeRefusal("no_text")
    return text


# ------------------------------------------------------------------ patterns (4.4.2-4.4.4), verbatim but where marked

# Zero-width space, non-joiner and joiner, word joiner, BOM, soft hyphen.
_ZW = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
# Bullet glyphs, including U+F0B7: the private-use bullet Word's Symbol font
# writes, which renders as nothing and so is easy to lose when copying this.
BULLETS = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2043\u2219\uf0b7\u27a2]")
SPACED = re.compile(r"^(?:[A-Za-z] ){3,}[A-Za-z]$")
# Added to 4.4.2: "Education & Honors", "Educational Background" and "Academic
# History" are Education too; unread, they left no pool and so no graduation.
# Only these words may follow "Education and": a job title such as "Education and
# Outreach Intern" must not reopen the Education section.
_EDUCATION_WITH = (r"honou?rs|awards|certifications?|certificates?|training|coursework|courses|"
                   r"activities|achievements|scholarships|qualifications|credentials|skills")
HEADINGS = (
    ("education", r"education(al background)?(\s*(and|&|/|\+)\s*(" + _EDUCATION_WITH + r")"
                  r"(\s*(and|&)\s*(" + _EDUCATION_WITH + r"))?)?|academic (background|history)|academics"),
    ("experience", r"(work |professional |relevant |research |industry )?experience|employment( history)?|work history|internships?"),
    ("projects", r"(technical |academic |personal |selected )?projects"),
    ("skills", r"(technical |core |key )?skills( (and|&) (interests|abilities|tools|certifications))?|core competencies|technologies|tools|technical proficiencies"),
    ("coursework", r"(relevant )?coursework|courses"),
    ("leadership", r"leadership( (and|&) (activities|involvement))?|activities|involvement|extracurriculars?|campus involvement"),
    ("certifications", r"certifications?|licenses?( (and|&) certifications?)?"),
    ("awards", r"awards|honors( (and|&) awards)?|achievements"),
    ("other", r"publications|volunteer(ing| experience)?|summary|objective|profile|interests|languages|references"),
)
HEADING_RX = tuple((name, re.compile(r"^(?:" + rx + r")\s*:?$", re.I)) for name, rx in HEADINGS)
INLINE_SKILLS = re.compile(r"^(?:technical skills|skills|languages|programming languages|tools|software|technologies|frameworks|lab skills|laboratory skills)\s*:\s*(.+)$", re.I)
#: A heading is a short line; anything longer is content that happens to start like one.
_MAX_HEADING = 40

# Added to 4.4.3: MSc, MFA, MTech, BFA, BBA, BSBA, BSN, BArch and BTech, whose
# majors were lost with them. An IT resume says "MFA" for multi-factor
# authentication, often in a list ("Okta, MFA, SSO"), so it counts only as
# "MFA in <Major>" or followed by an arts field.
_MFA_FIELDS = (r"Fine Arts?|Studio Arts?|Creative Writing|Writing|Poetry|Fiction|Nonfiction|Design|"
               r"Graphic Design|Illustration|Animation|Film|Filmmaking|Theat(?:re|er)|Drama|Acting|"
               r"Directing|Dance|Music|Photography|Painting|Sculpture|Visual Arts?|Art")
DEGREE_RX = re.compile(
    r"(?P<phd>\bPh\.?\s?D\.?|\bDoctor of Philosophy\b)|"
    r"(?P<pharmd>\bPharm\.?\s?D\.?|\bDoctor of Pharmacy\b)|"
    r"(?P<mba>\bMBA\b|\bM\.B\.A\.|\b[Mm]aster of [Bb]usiness [Aa]dministration\b)|"
    r"(?P<master>\bM\.\s?S\.?(?=[\s,]|$)|\bMS(?=[\s,]|$)|\bM\.?\s?Eng\b|\bM\.\s?A\.(?=[\s,]|$)|\bMPH\b|\bMPP\b|"
    r"\bMSc\b|\bM\.Sc\.|\bMFA(?=\s+in\s+[A-Z]|,?\s*(?:" + _MFA_FIELDS + r")\b)|\bM\.F\.A\.|\bM\.?\s?Tech\b\.?|"
    r"\b[Mm]aster(?:'s|s)?(?:\s+of\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)?(?:\s+[Dd]egree)?)|"
    r"(?P<bachelor>\bB\.\s?S\.?(?=[\s,]|$)|\bBS(?=[\s,]|$)|\bB\.\s?A\.?(?=[\s,]|$)|\bBA(?=[\s,]|$)|"
    r"\bB\.?\s?Eng\b|\bBSE\b|\bBSc\b|\bB\.Sc\.|"
    r"\bBFA\b|\bB\.F\.A\.|\bBBA\b|\bB\.B\.A\.|\bBSBA\b|\bBSN\b|\bB\.?\s?Arch\b\.?|\bB\.?\s?Tech\b\.?|"
    r"\b[Bb]achelor(?:'s|s)?(?:\s+of\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)?(?:\s+[Dd]egree)?|\b[Uu]ndergraduate\b)|"
    r"(?P<associate>\bA\.\s?A\.(?=[\s,]|$)|\bA\.\s?S\.(?=[\s,]|$)|\b[Aa]ssociate(?:'s)?\s+(?:[Dd]egree\s+)?(?:of|in)\s+(?:Arts|Science))")
DEGREE_RANK = {"associate": 1, "bachelor": 2, "master": 3, "mba": 3, "pharmd": 4, "phd": 5}
# Added to 4.4.3: a phrase also stops before ", Viterbi School of ..." and at
# "at"/"from", or the school's "Engineering" is read as a second major.
PHRASE_STOP = re.compile(
    r"\s*(?:[|;(•]|\s[-–]\s|,\s*(?=(?:university|college|institute|school|uc |expected|anticipated|gpa|minor|class|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|spring|summer|fall|winter|\d))|\b(?:expected|anticipated|gpa|minor|"
    r"graduat|class of)\b|\d|"
    r",(?=[^,]*\b(?:school|college|institute)\b)|\b(?:at|from)\b)", re.I)
MAJOR_LABEL = re.compile(r"\b(?:double )?majors?\s*(?:in|:)\s*(?P<p>[^|;(\n]{3,100})", re.I)
MINOR_LABEL = re.compile(r"\bminors?\s*(?:in|:)\s*(?P<p>[^|;(\n]{3,100})", re.I)
_PHRASE_LEAD = re.compile(r"^\s*(?:,|in|of|:|-)?\s*(?:in\s+)?", re.I)
_STUDY_WORDS = re.compile(r"\bmajor|\bstudent\b|\bstudying\b", re.I)
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "nov": 11, "dec": 12}
SEASON_MONTH = {"spring": 6, "summer": 8, "fall": 12, "autumn": 12, "winter": 3}
MON = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
       r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|spring|summer|fall|autumn|winter)")
EXPECTED_RX = re.compile(r"\b(?:expected|anticipated|graduating|graduation(?: date)?|grad(?: date)?|class of|"
                         r"degree expected)\s*:?\s*(?:" + MON + r"\.?,?\s*)?'?(?P<year>20\d{2})\b", re.I)
DATE_RX = re.compile(r"(?:\b" + MON + r"\.?,?\s*)?'?\b(?P<year>20\d{2})\b|\b(?P<num>\d{1,2})/(?P<nyear>20\d{2})\b", re.I)
#: A line under Education that is an activity or a credential, not the degree:
#: a club role or a course still running says nothing about graduating.
_NOT_THE_DEGREE = re.compile(
    r"\b(?:club|society|association|chapter|officer|president|vice|treasurer|secretary|chair|captain|"
    r"member|certificate|certification|certified|coursera|udemy|edx|bootcamp|course\b|research(?:er)?|"
    r"lab\b|assistant|tutor|mentor|volunteer|intern(?:ship)?|scholar(?:ship)?|fellow(?:ship)?)", re.I)
#: The end of a range that is still running: its start is not a graduation.
ONGOING = re.compile(r"\s*(?:-|to|until)\s*(?:present|current(?:ly)?|now|today|ongoing)\b", re.I)
#: A line of courses, by its label or by holding two codes such as "ECON 2030".
COURSE_LINE = re.compile(r"^(?:relevant |related |selected |key )?(?:coursework|courses|classes)\b", re.I)
COURSE_CODE = re.compile(r"\b[A-Z][A-Z&]{1,7} ?\d{2,4}[A-Z]{0,2}\b(?!\.\d)")
_CODES_ON_A_COURSE_LINE = 2
#: A graduation with only a year is June; with no Education section the first 40
#: tagged lines stand in for one; a year is 10 back to 6 ahead of today.
_DEFAULT_MONTH, _POOL_WITHOUT_EDUCATION = 6, 40
_YEARS_BACK, _YEARS_AHEAD = 10, 6


def normalize(text: str) -> list[str]:
    """Lines as the extractor reads them (4.4.2): folded, de-bulleted, spaced letters joined."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _ZW.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("–", "-").replace("—", "-")
    text = BULLETS.sub(" ", text)
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw).strip()
        if SPACED.match(line):
            line = line.replace(" ", "")
        lines.append(line)
    return lines


def sections(lines: list[str]) -> list[tuple[str, str]]:
    """
    [(section, line)], section in {'header', 'education', ...} (4.4.2). A
    heading switches the section and is dropped; lines before any heading are
    'header'; an inline "Skills: ..." line is ('skills', rest) wherever it is.
    """
    current, tagged = "header", []
    for line in lines:
        if not line:
            continue
        if len(line) <= _MAX_HEADING:
            hit = next((name for name, rx in HEADING_RX if rx.match(line)), None)
            if hit:
                current = hit
                continue
        inline = INLINE_SKILLS.match(line)
        tagged.append(("skills", inline.group(1)) if inline else (current, line))
    return tagged


# ------------------------------------------------------------------ the draft (4.4.3-4.4.6)

def _month(token: "str | None") -> "int | None":
    if not token:
        return None
    lowered = token.lower()
    return SEASON_MONTH.get(lowered) or MONTHS.get(lowered[:3])


def _degree_phrase(rest: str) -> str:
    """The words after a degree token, up to where a major phrase stops."""
    rest = _PHRASE_LEAD.sub("", rest)
    cut = PHRASE_STOP.search(rest)
    return rest[:cut.start()] if cut else rest


def _beside(pool: list[str], index: int) -> tuple[str, ...]:
    """
    The major of a degree that names none, from the line after it or else the
    one before ("Bachelor of Science" over "Computer Science", a common PDF
    layout). Added to 4.4.3: such a line counts only if it is a major and
    nothing else, so a school, a date or a club beside the degree adds nothing.
    """
    for near in (index + 1, index - 1):
        if 0 <= near < len(pool):
            named = majors_alone(_degree_phrase(pool[near]))
            if named:
                return named
    return ()


def _study(pool: list[str]) -> tuple["str | None", list[str], list[str]]:
    """(degree, majors, minors) from the pool, in the rule order of 4.4.3."""
    degree, majors, minors = None, [], []
    for index, line in enumerate(pool):
        for match in DEGREE_RX.finditer(line):
            kind = match.lastgroup
            if degree is None or DEGREE_RANK[kind] > DEGREE_RANK[degree]:
                degree = kind
            majors += majors_in(_degree_phrase(line[match.end():])) or _beside(pool, index)
        for match in MAJOR_LABEL.finditer(line):
            majors += majors_in(match.group("p"))
        for match in MINOR_LABEL.finditer(line):
            minors += majors_in(match.group("p"))
    if not majors:
        for line in pool:
            if DEGREE_RX.search(line) or _STUDY_WORDS.search(line):
                majors += majors_in(line, skip_generic=True)
    majors = list(dict.fromkeys(majors))
    return degree, majors, [mid for mid in dict.fromkeys(minors) if mid not in majors]


def _expected_date(pool: list[str], years: range) -> "tuple[int, int] | None":
    """The first "Expected June 2027"-style date whose year is in range (4.4.4 rule 1)."""
    for line in pool:
        match = EXPECTED_RX.search(line)
        if match and int(match.group("year")) in years:
            return int(match.group("year")), _month(match.group("mon")) or _DEFAULT_MONTH
    return None


def _dates_on(line: str, years: range) -> tuple[list[tuple[int, int]], bool]:
    """
    ([(year, month)] in range, whether a range on the line is still running).
    Two departures from 4.4.4: the start of "Sep 2024 - Present" is left out,
    since it is when a degree began, and so is a bare year that is part of a
    course code ("ECON 2030" is a course, not June 2030) or sits on a line
    labelled as coursework. A line naming a degree keeps its years: in "BS MATH
    2023" the year is a graduation, not a course.
    """
    labelled = bool(COURSE_LINE.match(line))
    codes = [] if labelled or DEGREE_RX.search(line) else [m.span() for m in COURSE_CODE.finditer(line)]
    codes = codes if len(codes) >= _CODES_ON_A_COURSE_LINE else []
    dates, ongoing = [], False
    for match in DATE_RX.finditer(line):
        if ONGOING.match(line, match.end()):
            ongoing = True
            continue
        bare = match.group("year") and not match.group("mon")
        in_code = any(start <= match.start("year") < end for start, end in codes) if bare else False
        if bare and (labelled or in_code):
            continue
        year = int(match.group("year") or match.group("nyear"))
        month = _month(match.group("mon")) if match.group("year") else int(match.group("num"))
        if year in years and (month is None or 1 <= month <= 12):
            dates.append((year, month or _DEFAULT_MONTH))
    return dates, ongoing


def _latest_date(pool: list[str], years: range, today: date) -> "tuple[int, int] | None":
    """
    The latest (year, month) of any date in range (4.4.4 rule 2). Beside a
    degree that runs to "Present" a date already past is an earlier degree, not
    this one's end, so then only a date still ahead is a graduation. A club role
    or a course still running (`_NOT_THE_DEGREE`) is not that degree.
    """
    found = [(line, *_dates_on(line, years)) for line in pool]
    best = max((key for _, dates, _ in found for key in dates), default=None)
    still_studying = any(ongoing and not _NOT_THE_DEGREE.search(line) for line, _, ongoing in found)
    if still_studying and best is not None and best <= (today.year, today.month):
        return None
    return best


def derive_draft(text: str, today: date) -> dict:
    """
    The draft a resume suggests (4.4.3-4.4.6); closed vocabulary only. Without
    an Education section only an explicit "Expected ..." date is a graduation:
    the latest year on a resume is as likely to be a job as a degree.
    """
    lines = normalize(text)
    tagged = sections(lines)
    education = [line for section, line in tagged if section == "education"]
    pool = education or [line for _, line in tagged[:_POOL_WITHOUT_EDUCATION]]
    where = "education" if education else "text"
    degree, majors, minors = _study(pool)
    years = range(today.year - _YEARS_BACK, today.year + _YEARS_AHEAD + 1)
    grad = _expected_date(pool, years) or (_latest_date(pool, years, today) if education else None)
    skills = find_skills("\n".join(lines), [line for section, line in tagged if section == "skills"])
    return {
        "majors": majors[:MAX_MAJORS], "minors": minors[:MAX_MINORS], "degree": degree,
        "grad_year": grad[0] if grad else None, "grad_month": grad[1] if grad else None,
        "skills": list(skills), "fields": list(fields_for(majors, skills)),
        "evidence": {"study": where if (majors or degree) else None,
                     "grad": where if grad else None},
    }


def parse_bytes(data: bytes, kind: str, today: date) -> dict:
    """check_magic + extract_text + derive_draft. The worker's only entry point."""
    check_magic(data, kind)
    return derive_draft(extract_text(data, kind), today)


# ------------------------------------------------------------------ the parent's side (4.4.9)

_FIELDS = frozenset(FIELD_IDS)
_EVIDENCE = ("education", "text")
_YEAR_RANGE = (2000, 2100)


def _known(items: object, vocabulary, cap: int) -> list[str]:
    """Ids from a JSON list that are in `vocabulary`, once each, in order, capped."""
    if not isinstance(items, (list, tuple)):
        return []
    kept = (item for item in items if isinstance(item, str) and item in vocabulary)
    return list(dict.fromkeys(kept))[:cap]


def _evidence(value: object) -> "str | None":
    return value if isinstance(value, str) and value in _EVIDENCE else None


def _int_between(value: object, low: int, high: int) -> "int | None":
    is_int = isinstance(value, int) and not isinstance(value, bool)
    return value if is_int and low <= value <= high else None


def validate_draft(obj: object) -> dict:
    """
    A draft rebuilt from scratch out of whatever the worker sent (4.4.9): the
    parent never trusts the child. Unknown ids and keys are dropped, a year is
    an int and not `True`, and a month goes with a missing year.
    """
    if not isinstance(obj, dict):
        raise ResumeRefusal("worker_failed")
    year = _int_between(obj.get("grad_year"), *_YEAR_RANGE)
    evidence = obj.get("evidence") if isinstance(obj.get("evidence"), dict) else {}
    degree = obj.get("degree")
    return {
        "majors": _known(obj.get("majors"), MAJOR_BY_ID, MAX_MAJORS),
        "minors": _known(obj.get("minors"), MAJOR_BY_ID, MAX_MINORS),
        "degree": degree if isinstance(degree, str) and degree in DEGREE_IDS else None,
        "grad_year": year,
        "grad_month": _int_between(obj.get("grad_month"), 1, 12) if year is not None else None,
        "skills": _known(obj.get("skills"), SKILL_BY_ID, MAX_SKILLS),
        "fields": _known(obj.get("fields"), _FIELDS, MAX_FIELDS),
        "evidence": {"study": _evidence(evidence.get("study")),
                     "grad": _evidence(evidence.get("grad"))},
    }
