"""
intern_taxonomy.py
~~~~~~~~~~~~~~~~~~
What a posting is, read from its title alone: its level, its fields, the term
it names, who it is reserved for, and which postings are one role listed many
times.

The finder classifies at read time. Nothing here is stored in postings.db and
the poller is not touched (D21), so a better rule reaches every posting in the
window on the next load, and a worse one reaches all of them too.

Every regex, table and rule order is ported verbatim from the measured
reference (spec 4.1, 4.2, 4.6), which was run against 17,548 real postings.
There are two additions: the spec's professional-title guard in `is_hourly`,
and the abbreviated clinical licences in `LICENSED` (D10). The
order is load-bearing — `intern` is tried before any seniority word, so an
"Operations Manager Intern" is an intern — and a regex that looks redundant is
usually there because one board wrote a title that way. Edit these as data,
and rerun the fixture tables in `test_intern_taxonomy.py` when you do.

Pure: stdlib plus `intern_vocab` and `intern_location`, so it imports under
bare `python3`. Every classifier is cached on the raw string.
"""

import hashlib
import re
from functools import lru_cache
from typing import NamedTuple

from intern_location import parse_location
from intern_vocab import norm_text

_CACHE_SIZE = 65536


def _ci(pattern: str) -> re.Pattern:
    """The spec's `R`: case-insensitive unless a pattern says otherwise."""
    return re.compile(pattern, re.I)


class LevelTag(NamedTuple):
    """A title's rule level, and the words that decided it (None when nothing did)."""

    level: str
    evidence: str | None


# ------------------------------------------------------------------ level (4.1.2)

NOT_A_JOB = _ci(r"current interns only|talent (community|network|pool)|general application|"
                r"expression of interest|future opportunit|hackathon|networking event|"
                r"info(rmation)? session|webinar|coffee chat|case competition|\bskillbridge\b|"
                r"foreign pharmacy grad\w*|international pharmacy intern")
INTERN_STAFF = _ci(r"\bintern(ship)?s?\s+(manager|coordinator|recruiter|program manager|"
                   r"program lead|mentor|supervisor|housing)\b|\binternist\b")
COOP = _ci(r"\b(co-?op|coop|\d+\s*-?\s*months?\s+placement|placement (student|year)|"
           r"industrial placement|work term)\b")
INTERN = _ci(r"实习|\b(intern|interns|internship|internships|practicante|pr[aá]cticas|"
             r"stagiaire|werkstudent|working student|summer analyst|summer associate|"
             r"summer scholar|dhbw student|off-cycle|est[aá]gio|estagi[aá]rio|becario|"
             r"student (worker|assistant|researcher|trainee))\b")
RANGE_SPAN = _ci(r"\(?\s*\b(entry[- ]level|associate)\s*(or|/|,|&)\s*(associate|experienced|"
                 r"mid[- ]level|senior)[^)]*\)?")
SENIOR_GUARD = _ci(r"\b(senior|sr|principal|staff|lead|director|head of|vp|svp|evp|avp|"
                   r"vice president|chief|(technical|distinguished|senior|principal|corporate) "
                   r"fellow)\b")
NEW_GRAD = _ci(r"\b(new grad|new grads|new graduate|new college grad(uate)?|recent grad(uate)?|"
               r"university grad(uate)?|college grad(uate)?|grad pharmacist|early career|"
               r"early-career|emerging talent|graduate (program|scheme|engineer|analyst|"
               r"trainee|researcher program)|rotational|rotation program|"
               r"(leadership |professional |technical |management )?development program|ldp|fldp|"
               r"fadp|erdp|campus hire|20\d\d start|fellowship|fellow|residency)\b|"
               r"校园招聘|校招")
RECRUITER = _ci(r"\b(recruit(er|ing)|talent acquisition (partner|lead|manager)|"
                r"program (manager|coordinator|lead)|university relations|coordinator)\b")
APPRENTICE = _ci(r"\b(apprentice|apprenticeship|pre-apprentice|trainee|aprendiz|jovem aprendiz)\b")
# The last two lines are not in the reference, and D10 is why they were added:
# CVS writes "NP or PA", "NP/PA" or plain "Triage Nurse", and those part-time
# and per-diem postings were read as hourly. A bare "PA" is left out on purpose
# because it is usually Pennsylvania ("NP/PA" is caught by the NP). "Nurse" is
# not matched when it is followed by aide, assistant, tech, extern or recruiter,
# or preceded by "student", because those roles need no licence.
LICENSED = _ci(r"\b(pharmacist|pharmacists|pharmacy manager|pharmacy supervisor|physician|"
               r"physician assistant|nurse practitioner|registered nurse|rn|lpn|lvn|"
               r"licensed (practical|vocational) nurse|attorney|dentist|optometrist|psychologist|"
               r"therapist|"
               r"np|fnp|pa-c|aprn|crna|cnm|midwife|lcsw|lmsw|lisw|lpc|lpcc|lcpc|lmft|lmhc|"
               r"(?<!student )nurse(?!\s+(aides?|assistants?|techs?|technicians?|externs?|"
               r"recruit\w*)\b))\b")
ENTRY_STRONG = _ci(r"\b(entry[- ]level|junior|jr)\b|"
                   r"\b(engineer|analyst|scientist|developer|designer|specialist|technician|"
                   r"representative|accountant|consultant|associate|coordinator|administrator)"
                   r"\s+(i|1)\b|"
                   r"\bassociate\s+(product manager|engineer|analyst|scientist|designer|"
                   r"consultant|developer|researcher|buyer|planner|accountant|actuary|attorney)\b|"
                   r"\b(mechanical|cad|design|test|software)\s+associate\s+engineer\b|\bapm\b")
EXPERIENCED = _ci(r"\b(senior|sr|staff|principal|lead|leader|manager|director|head of|"
                  r"vp|svp|evp|avp|vice president|chief|president|experienced|expert|"
                  r"mid\s*-?\s*level|supervisor|counsel|architect|fellow engineer|"
                  r"officer|partner|superintendent|foreman)\b|"
                  r"\b(ii|iii|iv|v)\b|,\s*(associate|analyst|engineer)?\s*[2-5]\b|"
                  r"\b(engineer|analyst|scientist|developer|associate|specialist|"
                  r"technician|designer|accountant|consultant|coordinator|representative)"
                  r"\s+[2-5]\b")
ENTRY_WEAK = _ci(r",\s*associate(\s*1)?\s*$|-\s*associate(\s*1)?\s*$|"
                 r"^associate\s+(?!director|manager|general counsel|vice)")


@lru_cache(maxsize=_CACHE_SIZE)
def classify_level(title: str | None) -> LevelTag:
    """The rule level of a title; the first matching rule of spec 4.1.2 wins."""
    t = norm_text(title)
    if not t:
        return LevelTag("unspecified", None)
    m = NOT_A_JOB.search(t)
    if m:
        return LevelTag("excluded", m.group(0))
    if not INTERN_STAFF.search(t):          # an intern mentor is not an intern
        m = COOP.search(t)
        if m:
            return LevelTag("coop", m.group(0))
        m = INTERN.search(t)
        if m:
            return LevelTag("intern", m.group(0))
    rng = RANGE_SPAN.search(t)
    m = SENIOR_GUARD.search(RANGE_SPAN.sub(" ", t) if rng else t)
    if m:
        return LevelTag("experienced", m.group(0))
    m = NEW_GRAD.search(t)
    if m and not RECRUITER.search(t):
        return LevelTag("new_grad", m.group(0))
    m = APPRENTICE.search(t)
    if m:
        return LevelTag("apprentice", m.group(0))
    m = LICENSED.search(t)
    if m:
        return LevelTag("experienced", m.group(0))
    if rng:
        return LevelTag("entry", rng.group(0).strip())
    for rx, level in ((ENTRY_STRONG, "entry"), (EXPERIENCED, "experienced"), (ENTRY_WEAK, "entry")):
        m = rx.search(t)
        if m:
            return LevelTag(level, m.group(0))
    return LevelTag("unspecified", None)


# ------------------------------------------------------------------ hourly (4.1.3, 4.1.4)

HOURLY = _ci(r"\b(pharmacy technician|pharmacy tech|store associate|sales associate|"
             r"shift supervisor|cashier|barista|guest advocate|team member|crew member|stocking|"
             r"(overnight|\d+\s*am) inbound|inbound (operations )?team|fulfillment (expert|"
             r"operations|associate)|warehouse|seasonal|part[- ]time|per diem|prn|"
             r"beauty (sales )?consultant|beauty (studio )?advisor|beauty team|merchandiser|"
             r"food (service|& beverage|and beverage)|cook|sous chef|chef(?! de projet)|"
             r"dishwasher|crew|driver|delivery (driver|associate|helper)|security specialist|"
             r"security officer|security guard|assets protection|asset protection|loss prevention|"
             r"store manager|store manager in training|operations supervisor|welcome coordinator|"
             r"medical scribe|medical assistant|patient care technician|phlebotom\w*|caregiver|"
             r"nursing assistant|cna|home health aide|assembler|welder|machinist|mechanic|"
             r"machining technician|inspector|material handler|forklift|custodian|janitor|"
             r"housekeep\w*|production (associate|worker|operator)|production (apprentice|"
             r"technician)|equipment operator|machine operator|fitter|fabricator|monteur|"
             r"packag\w* (assistant|associate)|picker|packer|sorter|laborer|installer|electrician|"
             r"plumber|hvac|carpenter|painter|fabrication specialist|(?<!executive )team leader|"
             r"(human resources|food service|fulfillment|style|beauty|guest service|"
             r"service & engagement|general merchandise) expert|attendant|shift lead|"
             r"customer service representative|call center|data entry|receptionist|"
             r"lab (technician|assistant)|stock|tooling mechanic|flight operations mechanic|"
             r"temporary|(1st|2nd|3rd|first|second|third|night|overnight|weekend|all) shifts?)\b|"
             r"\b(call center|customer (service|services|care|support)|"
             r"client support)\b.*\b(representative|rep|advocate|specialist)\b|"
             r"\brepresentative\s+(i|1)\b|\(t\)\s*$|\(t\d{3,5}\)|\bstore\s*#?\d+|"
             r"^operations manager(-[a-z]{2})?$")
# The professional-title guard (spec 4.1.3; not in the reference). These three
# soft alternatives also appear in engineering and cyber titles: "Crew Starship",
# "MP&P Engineer 2nd shift", "Cloud Security Specialist".
HOURLY_SOFT = _ci(r"^(crew|security specialist|"
                  r"(1st|2nd|3rd|first|second|third|night|overnight|weekend|all) shifts?)$")
PRO_TITLE = _ci(r"\b(engineer|engineering|developer|scientist|cyber\w*|cloud|soc|"
                r"information security|network security|application security)\b")
TECHNICIAN = _ci(r"\btechnician\b")
ENGINEER_WORD = _ci(r"engineer")
HOURLY_EXEMPT_LEVELS = frozenset({"intern", "coop", "new_grad", "excluded"})


@lru_cache(maxsize=_CACHE_SIZE)
def is_hourly(title: str | None, level: str) -> bool:
    """
    Whether a title is a frontline or hourly role (store, pharmacy tech, shift work).

    Never for a student level or a licensed role. A title whose only hourly
    words are soft ones is hourly only when it is not a professional title.
    """
    if level in HOURLY_EXEMPT_LEVELS:
        return False
    t = norm_text(title)
    if LICENSED.search(t):
        return False
    hits = [m.group(0) for m in HOURLY.finditer(t)]
    if hits and not (all(HOURLY_SOFT.match(h) for h in hits) and PRO_TITLE.search(t)):
        return True
    return bool(TECHNICIAN.search(t) and not ENGINEER_WORD.search(t))


@lru_cache(maxsize=_CACHE_SIZE)
def bucket(title: str | None) -> str:
    """The user-facing level id: "hourly" overrides the rule level (spec 4.1.4)."""
    level = classify_level(title).level
    return "hourly" if is_hourly(title, level) else level


# ------------------------------------------------------------------ fields (4.2.2)

FIELD_PATTERNS = [
    ("software",
     r"software|developer|\bswe\b|back-?end|front-?end|full.?stack|mobile|\bios\b|android|"
     r"\bweb\b|devops|\bsre\b|site reliability|platform engineer|infrastructure engineer|"
     r"forward deployed|c\+\+|java\b|python|agent development|compiler|distributed systems|"
     r"embedded software|flight software|firmware|desarrollo (digital|de software)",
     r"technology|\btech\b|\bcloud\b|\bapi\b|solutions engineer"),
    ("data_ml",
     r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|data scien|"
     r"data engineer|\bnlp\b|computer vision|perception|research engineer|research scientist|"
     r"applied scientist|autonomy|intelligent systems|prediction|llm\b",
     r"\bdata\b|algorithm"),
    ("analytics",
     r"analytics|data analyst|business intelligence|\bbi\b|insights|reporting analyst|"
     r"business analyst|inventory analyst|pricing|data intern|cad data|decision science",
     r"analyst|\bdata\b"),
    ("security",
     r"cyber|information security|infosec|product security|application security|"
     r"security engineer|security analyst|security research|threat|vulnerab|"
     r"penetration|detection engineer|\bsoc\b|identity and access|software security", None),
    ("it",
     r"\bit\b|information technology|help ?desk|service desk|desktop support|"
     r"systems administrator|sysadmin|network (engineer|administrator|technician)|"
     r"technical support|it analyst", None),
    ("electrical",
     r"electrical|electronic|hardware|asic|fpga|\brtl\b|silicon|analog|\brf\b|antenna|"
     r"microelectronic|semiconductor|circuit|pcb|power electronic|embedded|signal "
     r"processing|\bdsp\b|verification engineer|avionics|payload|elektrotechnik|photonic", None),
    ("mechanical",
     r"mechanical|mechanism|thermal|structural analysis|structures|stress|fluid|"
     r"cad\b|solidworks|design engineer|design, analysis and test|machine design|"
     r"maschinenbau|hvac engineer|mechatronic|component|turbomachinery|combustion|"
     r"fluid dynamics|\bcfd\b|mechanical development|product development", None),
    ("aerospace",
     r"aerospace|aeronautic|astronautic|propulsion|\bgnc\b|guidance|navigation and control|"
     r"flight (dynamics|test|sciences|systems)|spacecraft|launch|rocket|orbital|"
     r"aerodynamic|starship|satellite|space systems|vehicle integration", None),
    ("manufacturing",
     r"manufactur|calidad|manufactura|industrial engineer|quality|process engineer|"
     r"production engineer|lean|six sigma|operational excellence|opex|additive|npi|"
     r"integration (&|and) test|test engineer|reliability engineer|supplier (quality|development)|"
     r"ehs|environmental,? health|safety engineer|plant|operations engineer|machining|"
     r"mantenimiento|manuten[cç][aã]o", None),
    ("civil",
     r"\bcivil\b|structural engineer|construction|geotechnical|transportation engineer|"
     r"environmental engineer|water resources|surveying|real estate (&|and) builds|"
     r"facilities engineer|site development|\bbim\b", None),
    ("chem_materials",
     r"chemical|chemist|chemistry|materials|polymer|battery|cell (engineer|slurry)|"
     r"slurry|synthesis|electrochem|metallurg|ceramic|coatings|formulation|"
     r"process chemistry|corrosion|electrolyte|cathode|anode", None),
    ("biology_lab",
     r"biolog|biotech|life science|molecular|genom|microbio|cell culture|bioinformatic|"
     r"protein|assay|immunolog|neuroscien|biochem|pathology|clinical research|wet lab",
     r"lab\b|laboratory|research associate|research assistant|scientist"),
    ("healthcare",
     r"nurse|nursing|\brn\b|clinical|patient|care (coordinat|manage|coach)|medical|"
     r"physician|therap|health (educat|coach)|behavioral health|public health|"
     r"population health|epidemiolog|health science|home health|dental|optometr|"
     r"care management|case manager|utilization management", None),
    ("pharmacy", r"pharmac", None),
    ("finance",
     r"financ|finanzas|contabilidad|accounting|accountant|audit|\btax\b|treasury|fp&a|"
     r"controller|actuar|investment|fund accounting|valuation|credit\b|risk\b|"
     r"asset management|wealth|banking|capital markets|fixed income|equity research|"
     r"middle office|transfer agency|collateral|settlements|reconcil|payroll|billing|revenue",
     None),
    ("quant",
     r"quant|quantitative|trading|trader|systematic|market mak|derivatives|portfolio|alpha",
     None),
    ("business_ops",
     r"strategy|strategic|operaciones|business operations|operations (management|planning|"
     r"analyst|manager intern|management)|\bcoo\b|consult|general management|corporate "
     r"development|transformation|business development program|operations associate|"
     r"chief of staff|management program|leadership (development )?program|"
     r"store executive|store leadership|store management|operation manager intern|"
     r"operations manager intern|real estate",
     r"operations|business|corporate intern|commercial"),
    ("supply_chain",
     r"supply chain|logistic|procurement|purchasing|sourcing|buyer|material planning|"
     r"scm\b|supply network|log[ií]stica|compras|inventory|demand planning|distribution|"
     r"warehouse operations|transportation (analyst|planner)|supply base|import|export|"
     r"trade compliance|fulfillment (analyst|operations manager)|cadena de suministro|"
     r"cadeia de suprimentos", None),
    ("marketing",
     r"marketing|comunicaci[oó]n|brand|advertising|communications|public relations|\bpr\b|"
     r"social media|content (market|strateg|writer)|copywrit|seo|growth market|campaign|"
     r"roundel|media|events", None),
    ("sales",
     r"\bsales\b|account (executive|manager|development)|business development "
     r"representative|\bbdr\b|\bsdr\b|customer success|client (service|relations)|"
     r"partnerships|go-to-market|solutions consultant|account services", None),
    ("product",
     r"product manage|\bpm\b|program manage|project manage|\bpmo\b|technical program|"
     r"product owner|scrum|product operations|product manager", None),
    ("design",
     r"\bux\b|\bui\b|user experience|user research|product design|visual design|"
     r"graphic design|interaction design|brand design|motion design|creative|"
     r"illustrat|industrial design|content design|designer(?! engineer)", None),
    ("hr",
     r"human resources|recursos humanos|\bhr\b|talent acquisition|recruit|people (operations|"
     r"partner|team)|compensation|benefits|learning and development|workforce|"
     r"employee (and|&) workplace|people analytics", None),
    ("legal_policy",
     r"legal|counsel|paralegal|compliance|regulatory|government affairs|public policy|"
     r"policy|privacy|contracts|contract administrator|ethics", None),
]
_FIELD_RX = tuple((fid, _ci(r"\b(?:" + s + r")"), _ci(r"\b(?:" + w + r")") if w else None)
                  for fid, s, w in FIELD_PATTERNS)
GENERIC_ENGINEERING = _ci(r"\bengineering\b|\bengineer\b|\bingenier[ií]a\b|\bengenharia\b")
NOISE_PAREN = _ci(r"\([^)]*\b(20\d\d|summer|fall|winter|spring|starting|associate,|"
                  r"experienced|mid-level|senior|lead|m/w/d|f/m/d|hybrid|remote|on-?site|"
                  r"r\d{3,})\b[^)]*\)")
SUBSIDIARY = _ci(r"\s-\s*(millennium space systems|starlink|starship|starshield|"
                 r"aurora flight sciences|wisk|jeppesen|insitu|mtv|bixby)\b.*$")
PLACE_TAIL = re.compile(r"(?:\s-\s*|\s-(?=\S)|,\s)[^-(),]*?,\s*[A-Z]{2}\b.*$")
PHYSICAL_SECURITY = _ci(r"target security specialist|security (specialist|officer|guard|operator)")
ENG_CONTEXT = _ci(r"\b(engineer|engineering|systems|satellite|payload|space|network|rf|"
                  r"signal|radio)\b")
MKT_NON_COMMS = _ci(r"\b(marketing|brand|advertising|public relations|social media|"
                    r"copywrit|seo|campaign)")
ELEC_SW_UI = _ci(r"\b(electrical|electronic|asic|fpga|rtl|software|firmware|user interface|"
                 r"\bui\b|\bux\b|web|circuit)\b")
MECH_CORE = _ci(r"mechanical|thermal|structur|stress|fluid|solidworks|turbomach|combust|cfd|"
                r"mechatron")
CLINICAL = _ci(r"\b(clinical|health|care|patient|medical|pharmac)\w*")
LOGIC_SYNTH = _ci(r"\b(asic|fpga|rtl|timing|physical design|logic)\b")
BIO_WEAK_OK = _ci(r"\b(biolog|biotech|life science|molecular|genom|microbio|cell culture|"
                  r"bioinformatic|protein|assay|immunolog|neuroscien|biochem|pathology|wet lab|"
                  r"clinical research)")


def clean_title(title: str | None) -> str:
    """The title without year/level parentheticals, a subsidiary suffix or a place tail."""
    t = norm_text(title)
    t = NOISE_PAREN.sub(" ", t)
    t = SUBSIDIARY.sub("", t)
    t = PLACE_TAIL.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _post_rules(strong: dict[str, float], t: str) -> dict[str, float]:
    """Section 4.2.2 step 3, in order, applied to a copy of the strong matches."""
    out = dict(strong)
    if "marketing" in out and ENG_CONTEXT.search(t) and not MKT_NON_COMMS.search(t):
        out.pop("marketing")                     # satcom "communications"
    if "mechanical" in out and ELEC_SW_UI.search(t) and not MECH_CORE.search(t):
        out.pop("mechanical")
    if ("mechanical" in out and re.search(r"product development", t, re.I)
            and not re.search(r"engineer|mechanical", t, re.I)):
        out.pop("mechanical")
    if ("design" in out and re.search(r"design (engineer|and analysis|, analysis)", t, re.I)
            and not re.search(r"\b(ui|ux|user interface|user experience)\b", t, re.I)):
        out.pop("design")
    if ("manufacturing" in out and CLINICAL.search(t)
            and not re.search(r"manufactur|process engineer|production", t, re.I)):
        out.pop("manufacturing")
    if "chem_materials" in out and LOGIC_SYNTH.search(t):
        out.pop("chem_materials")
        out["electrical"] = 1.0
    if re.search(r"\b(electrolyte|cathode|anode)\b", t, re.I):
        out.pop("biology_lab", None)
    return out


@lru_cache(maxsize=_CACHE_SIZE)
def title_fields(title: str | None) -> tuple[tuple[str, float], ...]:
    """
    ((field_id, confidence), ...) sorted by (-confidence, id): 1.0 strong,
    0.5 weak, 0.6 for the internal `engineering_general` (spec 4.2.2).
    Target's store security is matched against a title without it: not cyber.
    """
    t = clean_title(title)
    t_for = PHYSICAL_SECURITY.sub(" ", t)
    strong = {fid: 1.0 for fid, srx, _ in _FIELD_RX if srx.search(t_for)}
    weak = {fid: 0.5 for fid, _, wrx in _FIELD_RX
            if fid not in strong and wrx is not None and wrx.search(t_for)}
    strong = _post_rules(strong, t)
    if strong:
        out = strong
    elif GENERIC_ENGINEERING.search(t):
        out = {"engineering_general": 0.6}      # an engineering word beats weak evidence
    else:
        out = weak
    if out.get("biology_lab", 1.0) < 1.0 and not BIO_WEAK_OK.search(t):
        out = {fid: conf for fid, conf in out.items() if fid != "biology_lab"}
    return tuple(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


# ------------------------------------------------------------------ term, grad, notes (4.1.5)

TERM_RE = _ci(r"\b(summer|fall|autumn|winter|spring)\s*'?\s*(20\d{2})\b")
YEAR_RE = re.compile(r"\b(20\d{2})\b")
TERM_START_MONTH = {"winter": 1, "spring": 3, "summer": 6, "fall": 9, "autumn": 9}
GRAD_ONLY = _ci(r"\b(graduate researcher|graduate student|grad student|ms/phd|m\.s\./ph\.d\.?|"
                r"phd|ph\.d\.?|doctoral|mba|master'?s|m\.s\.|ms|graduate (\w+ )?intern(ship)?)\b")
UNDERGRAD_OK = _ci(r"\b(bs|b\.s\.|ba|b\.a\.|bachelor'?s?|undergrad\w*)\b")
CLEARANCE = _ci(r"\b(ts/sci|top secret|secret clearance|security clearance|clearance required|"
                r"active clearance|polygraph)\b")
CITIZEN = _ci(r"\b(us citizen(ship)?|u\.s\. citizen(ship)?|citizenship required)\b")


@lru_cache(maxsize=_CACHE_SIZE)
def term_of(title: str | None) -> tuple[str | None, int | None, int | None]:
    """(label, year, start month): ("Summer 2027", 2027, 6), ("2027", 2027, None) or Nones."""
    t = norm_text(title)
    m = TERM_RE.search(t)
    if m:
        season = m.group(1).lower()
        label = "Fall" if season == "autumn" else season.title()
        return f"{label} {m.group(2)}", int(m.group(2)), TERM_START_MONTH[season]
    m = YEAR_RE.search(t)
    return (m.group(1), int(m.group(1)), None) if m else (None, None, None)


@lru_cache(maxsize=_CACHE_SIZE)
def requires_grad(title: str | None) -> str | None:
    """Who a graduate-only title is for ("a PhD student"); None when undergraduates may apply."""
    t = norm_text(title)
    m = GRAD_ONLY.search(t)
    if not m or UNDERGRAD_OK.search(t):
        return None
    word = m.group(0).lower().replace(".", "")
    if word == "ms/phd":
        return "an MS/PhD student"
    if word in ("phd", "doctoral"):
        return "a PhD student"
    if word == "mba":
        return "an MBA student"
    if word.startswith("master") or word == "ms":
        return "a master's student"
    return "a graduate student"


@lru_cache(maxsize=_CACHE_SIZE)
def clearance_note(title: str | None) -> str | None:
    """The caveat a title naming a clearance or citizenship earns in the Why line (D16)."""
    t = norm_text(title)
    if CLEARANCE.search(t):
        return "title mentions a security clearance"
    if CITIZEN.search(t):
        return "title mentions US citizenship"
    return None


# ------------------------------------------------------------------ grouping (4.6)

TERM_NOISE = _ci(r"\(?\s*(starting\s+)?(summer|fall|autumn|winter|spring)?\s*,?\s*'?(20\d\d)\s*\)?|"
                 r"\bstarting (june|summer)\b")
STORE_NO = _ci(r"\b(store\s*)?#?\d{3,6}\b")
METRO_WORDS = _ci(r"\b(greater|metro|county|north|south|east|west|northern|southern|eastern|"
                  r"western|central|bay area|inland empire|suburbs?|valley|panhandle|region)\b")
CLONE_HEAD = _ci(r"^(.*?\b(intern|internship|co-?op|program|new grad|trainee|apprentice|"
                 r"fellowship)\b[^-:(]*)")
_TRAILING_SEGMENT = re.compile(r"^(.*\S)\s*(?:-|,|:)\s*([^-,:]+(?:,\s*[a-z]{2})?)$")
_DC_KINDS = re.compile(r"\b(regional|flow|food|fulfillment|last mile|upstream|ir3|import)\s+"
                       r"distribution center\b.*$")


def company_norm(name: str | None) -> str:
    """Letters and digits only, lower case: the same key as `internship_poller._norm`."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _base(title: str | None) -> str:
    """The title lower-cased, without parentheticals, terms, store numbers or plurals."""
    t = norm_text(title).lower()
    t = re.sub(r"\([^)]*\)", " ", t)
    t = TERM_NOISE.sub(" ", t)
    t = STORE_NO.sub(" ", t)
    t = re.sub(r"\binternships?\b", "intern", t)
    t = re.sub(r"\boperations\b", "operation", t)
    t = re.sub(r"\bstores\b", "store", t)
    return re.sub(r"\s+", " ", t).strip(" -,")


def _is_placey(seg: str) -> bool:
    """A trailing title segment that names a place ('pueblo, co', 'north texas region')."""
    seg = seg.strip(" -,:")
    if not seg or parse_location(seg).places:
        return True
    return bool(METRO_WORDS.search(seg) and not title_fields(seg))


@lru_cache(maxsize=_CACHE_SIZE)
def role_key(company: str | None, title: str | None) -> str:
    """The company and the role with its place segments peeled off the end."""
    t = _base(title)
    while True:
        m = _TRAILING_SEGMENT.match(t)
        if not m or len(m.group(1)) < 6 or not _is_placey(m.group(2)):
            break
        t = m.group(1).strip(" -,")
    t = _DC_KINDS.sub("distribution center", t)
    return company_norm(company) + "|" + re.sub(r"[^a-z0-9]+", "", t)


@lru_cache(maxsize=_CACHE_SIZE)
def clone_key(company: str | None, title: str | None) -> str:
    """Company, the title up to its first level word, its fields and its bucket (4.6)."""
    t = NOISE_PAREN.sub(" ", norm_text(title))
    m = CLONE_HEAD.match(t)
    head = m.group(1) if m else t
    fields = ",".join(sorted(fid for fid, _ in title_fields(title)))
    return "|".join((company_norm(company), re.sub(r"[^a-z0-9]+", "", head.lower()), fields,
                     bucket(title)))


def group_hash(key: str) -> str:
    """The first 16 hex digits of the key's SHA-1: what the sent/hidden ledger stores."""
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
