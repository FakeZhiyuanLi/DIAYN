"""
resume_lexicon.py
~~~~~~~~~~~~~~~~~
Every major and skill the internship finder can read off a resume, and the
lookups over them.

This is the gate between a resume and a profile. The extractor in
`resume_parse` has no pattern for a name, an email, a school or an employer;
the only things it can emit are ids from the two tables below, so a word that
is not in here cannot be stored, whatever the resume says. The Edit details
modal goes through the same lookups, which is why they live here rather than
in the worker: a major typed by hand and a major read off a PDF must resolve
the same way.

Pure, importing only `intern_vocab`, so it loads in the worker process and
under bare `python3`. `MAJORS` and `SKILLS` are verbatim from the measured
reference (spec 4.4.7, 4.4.8), one record per line as there, so the two can be
compared with `diff`. Aliases are lower case with "and" for "&" (text is folded
the same way before matching); one that looks redundant is usually UCI's own
name for a programme. Edit them as data, keeping each entry's fields in order:
the primary field comes first on the card. Each label is also an alias of its
own major (added in `_major`, so the rows stay verbatim): Edit details prefills
the Major(s) box with labels, and one that did not read back was dropped.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from intern_vocab import FIELD_IDS, MAX_FIELDS, MAX_MAJORS, MAX_MINORS, MAX_SKILLS


@dataclass(frozen=True)
class Major:
    """One programme of study, and the fields it suggests, primary first."""

    id: str
    label: str
    aliases: tuple[str, ...]
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Skill:
    """
    One skill. `pattern` is a case-insensitive regular expression body matched
    between word boundaries; a `strict` skill (C, Go, R — ordinary words in
    prose) counts only as a whole item of a Skills line, spelled as `label`.
    """

    id: str
    label: str
    pattern: str
    fields: tuple[str, ...]
    strict: bool


# ------------------------------------------------------------------ majors (4.4.7)

#: (id, label, aliases, fields), verbatim.
_MAJOR_ROWS = (
    # computing
    ("computer_science", "Computer Science", ("computer science", "comp sci", "computer sciences"), ("software", "data_ml")),
    ("computer_science_engineering", "Computer Science & Engineering", ("computer science and engineering", "cse"), ("software", "electrical")),
    ("software_engineering", "Software Engineering", ("software engineering",), ("software", "product")),
    ("computer_engineering", "Computer Engineering", ("computer engineering", "electrical and computer engineering", "ece"), ("electrical", "software")),
    ("informatics", "Informatics", ("informatics", "human-computer interaction", "human computer interaction", "hci"), ("software", "design", "product")),
    ("computer_game_science", "Computer Game Science", ("computer game science", "game design and interactive media", "game design", "game development"), ("software", "design")),
    ("data_science", "Data Science", ("data science", "data analytics"), ("data_ml", "analytics")),
    ("statistics", "Statistics", ("statistics", "statistical science", "biostatistics"), ("data_ml", "analytics", "quant")),
    ("business_information_management", "Business Information Management",
     ("business information management", "bim", "information systems", "management information systems", "mis", "information and computer science", "ics"),
     ("analytics", "it", "business_ops")),
    ("cybersecurity", "Cybersecurity", ("cybersecurity", "cyber security", "information security"), ("security", "it")),
    # math & physical science
    ("mathematics", "Mathematics", ("mathematics", "applied mathematics", "math", "applied math"), ("analytics", "quant", "data_ml")),
    ("physics", "Physics", ("physics", "applied physics", "astrophysics", "physics and astronomy"), ("electrical", "quant", "data_ml")),
    ("chemistry", "Chemistry", ("chemistry", "chemical sciences"), ("chem_materials", "biology_lab")),
    ("earth_system_science", "Earth System Science", ("earth system science", "earth and environmental sciences", "geology", "geoscience", "environmental science"), ("civil", "legal_policy")),
    ("environmental_science_policy", "Environmental Science & Policy", ("environmental science and policy", "environmental studies", "environmental policy"), ("legal_policy", "civil")),
    # engineering
    ("mechanical_engineering", "Mechanical Engineering", ("mechanical engineering", "mechanical and aerospace engineering"), ("mechanical", "manufacturing")),
    ("aerospace_engineering", "Aerospace Engineering", ("aerospace engineering", "aeronautical engineering", "astronautical engineering", "aeronautics and astronautics"), ("aerospace", "mechanical")),
    ("electrical_engineering", "Electrical Engineering", ("electrical engineering", "electrical and electronics engineering"), ("electrical",)),
    ("civil_engineering", "Civil Engineering", ("civil engineering", "structural engineering"), ("civil",)),
    ("environmental_engineering", "Environmental Engineering", ("environmental engineering",), ("civil", "chem_materials")),
    ("chemical_engineering", "Chemical Engineering", ("chemical engineering", "chemical and biomolecular engineering"), ("chem_materials", "manufacturing")),
    ("materials_science", "Materials Science & Engineering", ("materials science and engineering", "materials science", "materials engineering"), ("chem_materials", "manufacturing")),
    ("biomedical_engineering", "Biomedical Engineering", ("biomedical engineering", "bioengineering", "biomedical engineering: premedical"), ("biology_lab", "mechanical", "healthcare")),
    ("industrial_engineering", "Industrial Engineering",
     ("industrial engineering", "industrial and systems engineering", "operations research", "systems engineering"),
     ("manufacturing", "supply_chain", "analytics")),
    ("general_engineering", "Engineering", ("engineering", "general engineering", "engineering science"), ("mechanical", "electrical", "manufacturing")),
    # life & health sciences
    ("biological_sciences", "Biological Sciences", ("biological sciences", "biology", "biological science", "human biology", "biology/education"), ("biology_lab", "healthcare")),
    ("neurobiology", "Neurobiology", ("neurobiology", "neuroscience", "neurosciences"), ("biology_lab", "healthcare")),
    ("molecular_biology", "Molecular & Cell Biology",
     ("biochemistry and molecular biology", "molecular biology", "developmental and cell biology", "cell biology", "molecular and cell biology", "genetics"),
     ("biology_lab", "chem_materials")),
    ("biochemistry", "Biochemistry", ("biochemistry",), ("biology_lab", "chem_materials")),
    ("microbiology", "Microbiology & Immunology", ("microbiology and immunology", "microbiology", "immunology"), ("biology_lab", "healthcare")),
    ("ecology", "Ecology & Evolutionary Biology", ("ecology and evolutionary biology", "ecology", "evolutionary biology", "marine biology"), ("biology_lab", "legal_policy")),
    ("pharmaceutical_sciences", "Pharmaceutical Sciences",
     ("pharmaceutical sciences", "pharmaceutical science", "pharmacy", "pharmd", "pharm.d", "doctor of pharmacy", "pharmacology"),
     ("pharmacy", "biology_lab")),
    ("public_health", "Public Health", ("public health sciences", "public health policy", "public health", "global health", "epidemiology"), ("healthcare", "analytics")),
    ("nursing", "Nursing Science", ("nursing science", "nursing"), ("healthcare",)),
    ("kinesiology", "Kinesiology", ("kinesiology", "exercise science", "sports medicine"), ("healthcare",)),
    ("nutrition", "Nutrition", ("nutrition", "nutritional science", "dietetics"), ("healthcare",)),
    # business & economics
    ("business_administration", "Business Administration",
     ("business administration", "business", "business management", "management", "mba", "master of business administration", "commerce"),
     ("business_ops", "finance", "marketing")),
    ("business_economics", "Business Economics", ("business economics",), ("finance", "business_ops", "analytics")),
    ("economics", "Economics", ("economics", "quantitative economics", "econ"), ("finance", "analytics", "business_ops")),
    ("accounting", "Accounting", ("accounting", "accountancy"), ("finance",)),
    ("finance", "Finance", ("finance", "financial engineering", "financial mathematics"), ("finance", "quant")),
    ("marketing", "Marketing", ("marketing", "advertising"), ("marketing", "sales")),
    ("supply_chain_management", "Supply Chain Management", ("supply chain management", "supply chain", "logistics", "operations management"), ("supply_chain", "business_ops")),
    # social sciences
    ("psychology", "Psychology", ("psychological science", "psychology", "psychology and social behavior"), ("hr", "design", "healthcare")),
    ("cognitive_science", "Cognitive Sciences", ("cognitive sciences", "cognitive science"), ("design", "data_ml", "hr")),
    ("sociology", "Sociology", ("sociology",), ("hr", "analytics")),
    ("anthropology", "Anthropology", ("anthropology",), ("design", "hr")),
    ("political_science", "Political Science", ("political science", "politics", "government"), ("legal_policy",)),
    ("international_studies", "International Studies", ("international studies", "international relations", "global studies", "global middle east studies"), ("legal_policy", "business_ops")),
    ("criminology", "Criminology, Law & Society",
     ("criminology, law and society", "criminology law and society", "criminology", "criminal justice", "legal studies", "law and society"),
     ("legal_policy",)),
    ("public_policy", "Public Policy", ("public policy", "public administration", "urban and regional planning"), ("legal_policy", "analytics")),
    ("urban_studies", "Urban Studies", ("urban studies", "urban planning", "city planning"), ("civil", "legal_policy")),
    ("social_ecology", "Social Ecology", ("social ecology",), ("legal_policy", "healthcare")),
    ("education_sciences", "Education Sciences", ("education sciences", "education"), ("hr",)),
    # humanities & communication
    ("english", "English", ("english", "literary journalism", "comparative literature", "creative writing", "literature"), ("marketing",)),
    ("communications", "Communications", ("communications", "communication", "journalism", "media studies", "public relations"), ("marketing", "sales")),
    ("history", "History", ("history", "art history"), ("legal_policy",)),
    ("philosophy", "Philosophy", ("philosophy",), ("legal_policy",)),
    ("languages", "Languages & Cultures",
     ("east asian studies", "east asian cultures", "chinese studies", "japanese language and literature", "korean literature", "spanish", "french",
      "german studies", "european studies", "global cultures", "asian american studies", "chicano/latino studies", "gender and sexuality studies",
      "african american studies", "classics", "religious studies", "linguistics", "language science"),
     ("marketing", "legal_policy")),
    # arts & design
    ("art", "Art & Design", ("studio art", "art", "fine arts", "graphic design", "visual arts", "industrial design", "design"), ("design", "marketing")),
    ("film_media", "Film & Media Studies", ("film and media studies", "film studies", "film", "digital media", "media arts"), ("design", "marketing")),
    ("drama_music", "Drama, Dance & Music", ("drama", "music theatre", "dance", "music", "theatre", "theater"), ("marketing", "design")),
    ("architecture", "Architecture", ("architecture",), ("civil", "design")),
)


# ------------------------------------------------------------------ skills (4.4.8)

#: (id, label, pattern, fields, strict), verbatim.
_SKILL_ROWS = (
    # programming & data
    ("python", "Python", r"python", ("software", "data_ml"), False),
    ("java", "Java", r"java(?!script)", ("software",), False),
    ("javascript", "JavaScript", r"javascript|\bjs\b|ecmascript", ("software",), False),
    ("typescript", "TypeScript", r"typescript", ("software",), False),
    ("cpp", "C++", r"c\+\+|cpp", ("software", "electrical"), False),
    ("c", "C", r"C", ("software", "electrical"), True),
    ("csharp", "C#", r"c#|c sharp|\.net", ("software",), False),
    ("go", "Go", r"Go", ("software",), True),
    ("rust", "Rust", r"rust", ("software",), False),
    ("r", "R", r"R", ("data_ml", "analytics"), True),
    ("rstudio", "RStudio", r"rstudio|r programming", ("data_ml", "analytics"), False),
    ("sql", "SQL", r"sql|postgres(ql)?|mysql|sqlite", ("software", "analytics", "data_ml"), False),
    ("html_css", "HTML/CSS", r"html5?|css3?", ("software", "design"), False),
    ("react", "React", r"react(\.js|js)?", ("software",), False),
    ("node", "Node.js", r"node(\.js|js)", ("software",), False),
    ("django_flask", "Django/Flask", r"django|flask|fastapi", ("software",), False),
    ("swift", "Swift", r"swift(ui)?", ("software",), False),
    ("kotlin", "Kotlin", r"kotlin", ("software",), False),
    ("aws", "AWS", r"aws|amazon web services", ("software", "it"), False),
    ("gcp_azure", "GCP/Azure", r"gcp|google cloud|azure", ("software", "it"), False),
    ("docker", "Docker", r"docker|kubernetes|k8s", ("software", "it"), False),
    ("git", "Git", r"git(hub|lab)?", ("software",), False),
    ("linux", "Linux", r"linux|unix|bash", ("software", "it"), False),
    ("pandas", "pandas/NumPy", r"pandas|numpy|scipy", ("data_ml", "analytics"), False),
    ("ml_frameworks", "PyTorch/TensorFlow", r"pytorch|tensorflow|keras|scikit-learn|sklearn", ("data_ml",), False),
    ("machine_learning", "Machine learning", r"machine learning|deep learning", ("data_ml",), False),
    ("nlp", "NLP", r"nlp|natural language processing", ("data_ml",), False),
    ("computer_vision", "Computer vision", r"computer vision|opencv", ("data_ml",), False),
    ("spark", "Spark", r"spark|hadoop|databricks", ("data_ml", "software"), False),
    ("tableau", "Tableau", r"tableau", ("analytics",), False),
    ("power_bi", "Power BI", r"power bi|powerbi", ("analytics",), False),
    ("excel", "Excel", r"excel|vlookup|pivot tables?", ("analytics", "finance", "business_ops"), False),
    ("sas_spss", "SAS/SPSS", r"sas|spss|stata", ("analytics", "healthcare"), False),
    ("matlab", "MATLAB", r"matlab|simulink", ("mechanical", "electrical", "aerospace"), False),
    ("labview", "LabVIEW", r"labview", ("electrical", "manufacturing"), False),
    ("wireshark", "Wireshark", r"wireshark|nmap|metasploit|burp suite", ("security",), False),
    ("security_certs", "Security+", r"security\+|cissp|ceh|oscp", ("security", "it"), False),
    ("networking", "Networking", r"tcp/ip|ccna|routing and switching|network administration", ("it", "security"), False),
    # mechanical / aerospace / manufacturing
    ("solidworks", "SolidWorks", r"solidworks|solid works", ("mechanical",), False),
    ("autocad", "AutoCAD", r"autocad|auto cad", ("mechanical", "civil"), False),
    ("catia_nx", "CATIA/NX", r"catia|siemens nx|unigraphics|creo|pro/?engineer", ("mechanical", "aerospace"), False),
    ("fusion360", "Fusion 360", r"fusion 360|inventor|onshape", ("mechanical",), False),
    ("cad", "CAD", r"cad", ("mechanical",), False),
    ("ansys", "ANSYS", r"ansys|abaqus|comsol|nastran", ("mechanical", "aerospace"), False),
    ("fea", "FEA", r"fea|finite element( analysis)?", ("mechanical", "aerospace"), False),
    ("cfd", "CFD", r"cfd|computational fluid dynamics|star-?ccm", ("mechanical", "aerospace"), False),
    ("gdt", "GD&T", r"gd&t|gd and t|geometric dimensioning", ("mechanical", "manufacturing"), False),
    ("machining", "Machining", r"machining|cnc|lathe|mill(ing)?", ("manufacturing", "mechanical"), False),
    ("3d_printing", "3D printing", r"3d printing|additive manufacturing", ("manufacturing", "mechanical"), False),
    ("lean_six_sigma", "Lean/Six Sigma", r"lean manufacturing|six sigma|kaizen|5s", ("manufacturing", "supply_chain"), False),
    ("thermodynamics", "Thermodynamics", r"thermodynamics|heat transfer", ("mechanical", "aerospace"), False),
    ("propulsion", "Propulsion", r"propulsion|rocketry", ("aerospace",), False),
    # electrical
    ("circuits", "Circuit design", r"circuit (design|analysis)|analog design", ("electrical",), False),
    ("pcb", "PCB design", r"pcb|altium|kicad|eagle cad", ("electrical",), False),
    ("verilog", "Verilog/VHDL", r"verilog|systemverilog|vhdl", ("electrical",), False),
    ("fpga", "FPGA", r"fpga|vivado|quartus", ("electrical",), False),
    ("embedded", "Embedded systems", r"embedded systems?|microcontrollers?|arduino|raspberry pi|stm32", ("electrical", "software"), False),
    ("oscilloscope", "Lab instruments", r"oscilloscope|multimeter|soldering", ("electrical",), False),
    ("signal_processing", "Signal processing", r"signal processing|dsp", ("electrical",), False),
    # civil
    ("revit", "Revit", r"revit|civil 3d|bim", ("civil",), False),
    ("gis", "GIS", r"gis|arcgis|qgis", ("civil", "analytics"), False),
    ("surveying", "Surveying", r"surveying|total station", ("civil",), False),
    ("structural_analysis", "Structural analysis", r"structural analysis|sap2000|etabs|risa", ("civil", "mechanical"), False),
    # chemistry / lab
    ("hplc", "HPLC", r"hplc|gc-?ms|lc-?ms|mass spectrometry", ("chem_materials", "biology_lab"), False),
    ("spectroscopy", "Spectroscopy", r"spectroscopy|nmr|ftir|uv-?vis", ("chem_materials",), False),
    ("titration", "Titration", r"titration|wet chemistry", ("chem_materials",), False),
    ("pcr", "PCR", r"q?pcr|rt-?pcr", ("biology_lab",), False),
    ("cell_culture", "Cell culture", r"cell culture|tissue culture|aseptic technique", ("biology_lab",), False),
    ("western_blot", "Western blot", r"western blot(ting)?|sds-?page|elisa", ("biology_lab",), False),
    ("gel_electrophoresis", "Gel electrophoresis", r"gel electrophoresis|electrophoresis", ("biology_lab",), False),
    ("microscopy", "Microscopy", r"microscopy|confocal|flow cytometry|facs", ("biology_lab",), False),
    ("crispr", "CRISPR", r"crispr|cloning|transfection", ("biology_lab",), False),
    ("bioinformatics", "Bioinformatics", r"bioinformatics|blast|genomics|rna-?seq", ("biology_lab", "data_ml"), False),
    ("glp", "GLP/GMP", r"glp|gmp|good (laboratory|manufacturing) practice", ("biology_lab", "manufacturing"), False),
    # healthcare / pharmacy
    ("cpr_bls", "CPR/BLS", r"cpr|bls|basic life support|first aid", ("healthcare",), False),
    ("emt", "EMT", r"emt|emergency medical technician", ("healthcare",), False),
    ("hipaa", "HIPAA", r"hipaa", ("healthcare",), False),
    ("ehr", "EHR (Epic)", r"epic systems|epic ehr|electronic health records?|ehr|emr", ("healthcare",), False),
    ("medical_terminology", "Medical terminology", r"medical terminology|phlebotomy|patient care", ("healthcare",), False),
    ("pharmacy_tech", "Pharmacy technician", r"pharmacy technician|ptcb|compounding|pharmacology", ("pharmacy",), False),
    ("clinical_research", "Clinical research", r"clinical research|irb|clinical trials?", ("healthcare", "biology_lab"), False),
    # business / finance
    ("financial_modeling", "Financial modeling", r"financial model(l)?ing|dcf|lbo|valuation", ("finance",), False),
    ("accounting_skill", "Accounting", r"gaap|accounts (payable|receivable)|bookkeeping|quickbooks|reconciliation", ("finance",), False),
    ("bloomberg", "Bloomberg", r"bloomberg( terminal)?|factset|capital iq", ("finance", "quant"), False),
    ("cfa", "CFA/CPA", r"cfa|cpa|series 7|sie exam", ("finance",), False),
    ("sap_erp", "SAP/ERP", r"sap|oracle erp|erp systems?|netsuite", ("supply_chain", "finance"), False),
    ("salesforce", "Salesforce", r"salesforce|hubspot|crm", ("sales", "marketing"), False),
    ("market_research", "Market research", r"market research|competitive analysis|consumer insights", ("marketing", "business_ops"), False),
    ("seo", "SEO/SEM", r"seo|sem|google analytics|google ads", ("marketing",), False),
    ("social_media", "Social media", r"social media|content creation|instagram|tiktok", ("marketing",), False),
    ("copywriting", "Copywriting", r"copywriting|copy editing|ap style", ("marketing",), False),
    ("project_management", "Project management", r"project management|agile|scrum|jira|asana|pmp", ("product", "business_ops"), False),
    ("product_management", "Product management", r"product management|product roadmap|user stories", ("product",), False),
    ("supply_chain_skill", "Supply chain", r"supply chain|procurement|inventory management|logistics", ("supply_chain",), False),
    ("public_speaking", "Public speaking", r"public speaking|presentations?", ("sales", "marketing"), False),
    ("negotiation", "Negotiation", r"negotiation|sales|cold calling", ("sales",), False),
    # design
    ("figma", "Figma", r"figma|sketch app|framer", ("design",), False),
    ("adobe", "Adobe Creative Suite", r"adobe( creative (suite|cloud))?|photoshop|illustrator|indesign|premiere|after effects|lightroom", ("design", "marketing"), False),
    ("ux_research", "UX research", r"user research|usability testing|user interviews|wireframing|prototyping", ("design", "product"), False),
    ("blender", "Blender/Maya", r"blender|maya|cinema 4d|unity|unreal( engine)?", ("design", "software"), False),
    # people / legal / policy
    ("recruiting", "Recruiting", r"recruiting|talent acquisition|onboarding|workday hcm", ("hr",), False),
    ("legal_research", "Legal research", r"legal research|westlaw|lexisnexis|legal writing", ("legal_policy",), False),
    ("policy_analysis", "Policy analysis", r"policy analysis|policy research|legislative", ("legal_policy",), False),
    ("grant_writing", "Grant writing", r"grant writing|nonprofit", ("legal_policy", "marketing"), False),
    ("spanish_lang", "Spanish", r"spanish \((fluent|native|proficient|conversational)\)|fluent in spanish|bilingual", ("healthcare", "sales"), False),
)

def _folded(text: str) -> str:
    """Text as aliases are written: lower case, "&" spelled "and"."""
    return text.lower().replace("&", "and")


def _major(mid: str, label: str, aliases: tuple[str, ...], fields: tuple[str, ...]) -> Major:
    """A row as a Major, its own label an alias, so the label the card shows reads back."""
    own = _folded(label)
    return Major(mid, label, aliases if own in aliases else (*aliases, own), fields)


MAJORS: tuple[Major, ...] = tuple(_major(*row) for row in _MAJOR_ROWS)
SKILLS: tuple[Skill, ...] = tuple(Skill(*row) for row in _SKILL_ROWS)
MAJOR_BY_ID: dict[str, Major] = {major.id: major for major in MAJORS}
SKILL_BY_ID: dict[str, Skill] = {skill.id: skill for skill in SKILLS}

#: Aliases that are also everyday words. They still name a major after a degree
#: ("B.S. Biology") or a label ("Major: Business"), but not when a line is merely
#: searched for any major at all, where "Biology" might be a course or a club.
GENERIC_ALIASES: frozenset[str] = frozenset({
    "engineering", "business", "management", "art", "design", "math", "english", "history",
    "music", "film", "finance", "commerce", "education", "government", "politics",
    "literature", "theatre", "theater", "dance", "communication", "marketing", "accounting",
    "logistics", "supply chain", "advertising", "econ", "cse", "ece", "bim", "mis", "ics",
    "hci", "biology", "physics", "chemistry", "pharmacy", "nursing", "statistics",
    "genetics", "ecology", "drama"})

# Longest alias first, so "business economics" claims its words before
# "business" or "economics" can; `sorted` is stable, so ties keep table order.
_MAJOR_ALIASES = sorted(((alias, major.id) for major in MAJORS for alias in major.aliases),
                        key=lambda pair: -len(pair[0]))
_MAJOR_RX = tuple((re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"), alias, mid)
                  for alias, mid in _MAJOR_ALIASES)
_SKILL_RX = tuple((skill.id, re.compile(r"(?<![A-Za-z0-9])(?:" + skill.pattern
                                        + r")(?![A-Za-z0-9+#])", re.I))
                  for skill in SKILLS if not skill.strict)
_STRICT = {skill.label: skill.id for skill in SKILLS if skill.strict}
_SKILL_ORDER = {skill.id: index for index, skill in enumerate(SKILLS)}
#: A field needs this many distinct skills behind it before skills alone suggest it.
_MIN_SKILL_VOTES = 3
_MAX_SKILL_FIELDS = 2
#: How much of an unrecognised part the Edit details modal echoes back.
_UNKNOWN_CHARS = 30
_SKILL_ITEM_SPLIT = re.compile(r"[,;|/]|\band\b|\s-\s")
_MAJOR_PARTS = re.compile(r"[;\n]")
_SKILL_PARTS = re.compile(r"[,;\n]")
_MINOR_PREFIX = re.compile(r"^minors?\b\s*(?:in\b|:)?\s*", re.I)
#: What may sit between the majors of a line that names nothing else.
_MAJOR_JOINERS = re.compile(r"(?:[\s,/+]|\band\b)*")
_SPACES = re.compile(r"\s+")


# ------------------------------------------------------------------ lookups

def majors_in(text: "str | None", *, skip_generic: bool = False) -> tuple[str, ...]:
    """
    Every major named in `text`: longest alias first, non-overlapping, order of
    appearance. Lower-cases and maps "&" -> "and" first.

    "Biology and Chemistry" is two majors; "Business Economics" is one, because
    the longer alias takes those words before "economics" is tried. A major
    named twice keeps the position of its longest alias.
    """
    return _ids(_alias_hits(_folded(text or ""), skip_generic))


def majors_alone(text: "str | None") -> tuple[str, ...]:
    """
    The majors `text` names when it names nothing else, joined only by commas,
    slashes and "and"; otherwise (). For the line beside a bare "Bachelor of
    Science", which may be the major or may be a school, a date or a club.
    """
    low = _folded(text or "")
    hits = _alias_hits(low, skip_generic=False)
    spans = sorted(hits)
    ends = [0, *(end for _, end, _ in spans)]
    starts = [*(start for start, _, _ in spans), len(low)]
    gaps_are_joiners = all(_MAJOR_JOINERS.fullmatch(low, end, start)
                           for end, start in zip(ends, starts))
    return _ids(hits) if hits and gaps_are_joiners else ()


def _alias_hits(low: str, skip_generic: bool) -> list[tuple[int, int, str]]:
    """(start, end, id) of each alias in folded `low`, longest alias first, non-overlapping."""
    taken = []
    for rx, alias, mid in _MAJOR_RX:
        if skip_generic and alias in GENERIC_ALIASES:
            continue
        for match in rx.finditer(low):
            if any(match.start() < end and start < match.end() for start, end, _ in taken):
                continue
            taken.append((match.start(), match.end(), mid))
    return taken


def _ids(hits: list[tuple[int, int, str]]) -> tuple[str, ...]:
    """Each id once, at the position of its longest alias, in order of position."""
    first: dict[str, int] = {}
    for start, _, mid in hits:
        first.setdefault(mid, start)
    return tuple(sorted(first, key=first.__getitem__))


def match_major(phrase: "str | None") -> "str | None":
    """The first major `phrase` names, or None."""
    named = majors_in(phrase)
    return named[0] if named else None


def find_skills(text: str, skills_lines: Sequence[str]) -> tuple[str, ...]:
    """
    Skill ids by mentions, most first, ties in lexicon order; at most 40 (4.4.5).
    Ordinary skills are counted over the whole of `text`; the strict ones (C, Go
    and R: a letter, a verb, an initial) only as a whole item of one of
    `skills_lines`, spelled exactly as the label.
    """
    whole, counts = text or "", {}
    for sid, rx in _SKILL_RX:
        hits = len(rx.findall(whole))
        if hits:
            counts[sid] = hits
    for line in skills_lines:
        for token in _SKILL_ITEM_SPLIT.split(line):
            sid = _STRICT.get(token.strip().strip(".:()"))
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
    ordered = sorted(counts, key=lambda sid: (-counts[sid], _SKILL_ORDER[sid]))
    return tuple(ordered[:MAX_SKILLS])


def fields_for(majors: Sequence[str], skills: Sequence[str]) -> tuple[str, ...]:
    """
    The fields a draft starts with (4.4.6): each major's fields, primary major
    first, cut to six. With no major, the top two fields that at least three
    distinct skills vote for (ties by field order), so a Skills line alone gets
    a starting point and one stray skill does not decide it. Minors never add
    fields, which is why they are not taken; unknown ids count for nothing.
    """
    fields = []
    for mid in majors:
        major = MAJOR_BY_ID.get(mid)
        for field in major.fields if major else ():
            if field not in fields:
                fields.append(field)
    if fields:
        return tuple(fields[:MAX_FIELDS])
    votes = {}
    for sid in dict.fromkeys(skills):
        skill = SKILL_BY_ID.get(sid)
        for field in skill.fields if skill else ():
            votes[field] = votes.get(field, 0) + 1
    ranked = sorted((field for field, count in votes.items() if count >= _MIN_SKILL_VOTES),
                    key=lambda field: (-votes[field], FIELD_IDS.index(field)))
    return tuple(ranked[:_MAX_SKILL_FIELDS])


# ------------------------------------------------------------------ typed by hand

def _parts(text: "str | None", separators: re.Pattern) -> list[str]:
    """Non-empty parts of `text`, each stripped, inner whitespace collapsed."""
    pieces = (_SPACES.sub(" ", piece).strip() for piece in separators.split(text or ""))
    return [piece for piece in pieces if piece]


def resolve_major_words(text: "str | None") -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """
    For the Edit details modal. Splits `text` on ";" and newlines. A part
    starting with "minor"/"minors" (optionally "in"/":") feeds minors. Returns
    (majors <= 3, minors <= 2, unrecognised parts, each stripped and cut to 30
    chars). An unrecognised minor comes back without "minor", since it becomes
    a keyword; a minor that is also a major is dropped rather than shown twice.
    """
    majors, minors, unknown = [], [], []
    for part in _parts(text, _MAJOR_PARTS):
        prefix = _MINOR_PREFIX.match(part)
        words = part[prefix.end():] if prefix else part
        named = majors_in(words)
        if prefix:
            minors += named
        else:
            majors += named
        if words and not named:
            unknown.append(words[:_UNKNOWN_CHARS])
    kept_majors = tuple(dict.fromkeys(majors))[:MAX_MAJORS]
    kept_minors = tuple(m for m in dict.fromkeys(minors) if m not in kept_majors)[:MAX_MINORS]
    return kept_majors, kept_minors, tuple(dict.fromkeys(unknown))


def _skill_for(part: str) -> "str | None":
    """The skill a typed part names outright, by label first and then by pattern."""
    folded = part.casefold()
    for skill in SKILLS:
        same = part == skill.label if skill.strict else folded == skill.label.casefold()
        if same:
            return skill.id
    for sid, rx in _SKILL_RX:
        if rx.fullmatch(part):
            return sid
    return None


def resolve_skill_words(text: "str | None") -> tuple[tuple[str, ...], tuple[str, ...]]:
    """
    Splits on "," ";" and newlines. A part equal to a label (case-insensitive,
    strict skills case-sensitive) or fully matching a non-strict pattern maps to
    that id. Returns (skill ids <= 40, unrecognised parts).

    "Fully" is the point: "python scripting" contains a skill but is not one,
    and is kept whole as a keyword rather than silently shortened to Python.
    """
    skills, unknown = [], []
    for part in _parts(text, _SKILL_PARTS):
        sid = _skill_for(part)
        if sid:
            skills.append(sid)
        else:
            unknown.append(part)
    return tuple(dict.fromkeys(skills))[:MAX_SKILLS], tuple(dict.fromkeys(unknown))
