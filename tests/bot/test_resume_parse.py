"""
Reading a resume: the checks before and after download, and the draft it yields.

    python3 -m unittest discover -s tests      # PDF cases skip without pypdf
    .venv/bin/python -m unittest discover -s tests

`resume_parse` runs inside the sandboxed worker, so everything here is the
worker's side of the pipe: metadata and magic checks, text extraction, and the
extractor that turns text into closed-vocabulary ids. The rule every fixture
checks is the one the whole design rests on — nothing outside the lexicons
reaches the draft — which is why each resume opens with a canary name, email,
phone and street that must never come back out.

Every file is built in memory. No resume and no binary is committed, and no
test writes to disk. The PDF cases need `pypdf` and skip without it, which is
the only dependency this file may skip for.
"""

import ast
import contextlib
import html
import io
import json
import logging
import pathlib
import struct
import sys
import time
import unittest
import zipfile
from datetime import date
from unittest import mock

import resume_lexicon
import resume_parse
from resume_parse import ResumeRefusal

BOT = pathlib.Path(__file__).resolve().parents[2] / "bot"
TODAY = date(2026, 9, 28)
MIB = 1024 * 1024

#: Far above what a linear read of a capped document takes, far below a quadratic one.
LINEAR_SECONDS = 5

CANARY = ("Jane Canaryperson", "jane.canary@example.com", "(949) 555-0142", "1234 Canary Lane")

needs_pypdf = unittest.skipUnless(resume_parse.pdf_supported(), "pypdf is not installed")


def resume(*body: str) -> str:
    """A resume whose first two lines are exactly what must never leave the worker."""
    header = f"{CANARY[1]} | {CANARY[2]} | {CANARY[3]}, Irvine, CA 92617"
    return "\n".join([CANARY[0], header, *body])


def make_docx(lines, *, prolog: str = "") -> bytes:
    """The smallest .docx the parser accepts: one zip member, one run per paragraph."""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{html.escape(line)}</w:t></w:r></w:p>'
        for line in lines
    )
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>{prolog}'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    return zip_of({"[Content_Types].xml": "<Types/>", "word/document.xml": xml})


def zip_of(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def make_pdf(lines) -> bytes:
    """A one-page text PDF written by hand, so building it needs no library."""
    ops = ["BT", "/F1 11 Tf", "14 TL", "72 740 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def blank_pdf(pages: int, *, password: "str | None" = None) -> bytes:
    """`pages` empty pages, written by pypdf, optionally behind a user password."""
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(612, 792)
    if password is not None:
        writer.encrypt(user_password=password, algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@contextlib.contextmanager
def pypdf_quiet():
    """
    pypdf logs a warning about a broken file. In the worker that goes to a
    discarded stderr; here it would only be noise in the test output.
    """
    logger = logging.getLogger("pypdf")
    with mock.patch.object(logger, "handlers", [logging.NullHandler()]), \
            mock.patch.object(logger, "propagate", False):
        yield


class RefusalCase(unittest.TestCase):
    @contextlib.contextmanager
    def refused(self, reason: str):
        """`with self.refused("bad_magic"):` — the block raises exactly that refusal."""
        with self.assertRaises(ResumeRefusal) as caught:
            yield
        self.assertEqual(caught.exception.reason, reason)


#: Mechanical row of 4.4.6, long enough for the 200-character document floor.
MECH_LINES = resume(
    "EDUCATION", "University of California, Irvine",
    "B.S. Mechanical Engineering, Expected June 2027", "GPA: 3.7",
    "SKILLS", "SolidWorks, MATLAB, Python, GD&T, FEA, ANSYS, C",
    "EXPERIENCE", "Intern, Acme Corp - designed CAD fixtures for the assembly line",
).split("\n")

#: The eight rows of the 4.4.6 table: (name, lines, majors, minors, degree, grad, fields).
EXTRACTOR_ROWS = (
    ("mechanical", ("EDUCATION", "B.S. Mechanical Engineering, Expected June 2027", "SKILLS",
                    "SolidWorks, MATLAB, Python, GD&T, FEA, ANSYS, C"),
     ["mechanical_engineering"], [], "bachelor", (2027, 6), ["mechanical", "manufacturing"]),
    ("biology", ("E D U C A T I O N",
                 "Bachelor of Science in Biological Sciences, minor in Chemistry",
                 "UC Irvine | Sep 2023 - Jun 2027",
                 "Lab Skills: PCR, cell culture, Western blot, gel electrophoresis, R"),
     ["biological_sciences"], ["chemistry"], "bachelor", (2027, 6),
     ["biology_lab", "healthcare"]),
    ("business", ("Education", "UCI — Business Economics, B.A. (Class of 2028)",
                  "Technical Skills: Excel (pivot tables), Tableau, SQL, Bloomberg Terminal"),
     ["business_economics"], [], "bachelor", (2028, 6),
     ["finance", "business_ops", "analytics"]),
    ("games", ("EDUCATION",
               "University of California, Irvine — Computer Game Science, B.S.",
               "Anticipated Graduation: Spring 2029", "Skills: C++, Unity, Blender, Git, Go"),
     ["computer_game_science"], [], "bachelor", (2029, 6), ["software", "design"]),
    ("pharmacy", ("EDUCATION", "Pharm.D. Candidate, 2028", "B.S. Pharmaceutical Sciences, 2024"),
     ["pharmaceutical_sciences"], [], "pharmd", (2028, 6), ["pharmacy", "biology_lab"]),
    ("double major", ("EDUCATION",
                      "Double major in Political Science and Criminology, Law & Society",
                      "Expected 2027"),
     ["political_science", "criminology"], [], None, (2027, 6), ["legal_policy"]),
    ("no education", ("Skills: Python, Java, SQL, React, AWS, Docker, Git", "Experience",
                      "Software intern 2025"),
     [], [], None, (None, None), ["software"]),
    ("masters", ("EDUCATION",
                 "M.S. Computer Science, University of Southern California, May 2027",
                 "B.S. Mathematics, UCLA, 2025"),
     ["computer_science", "mathematics"], [], "master", (2027, 5),
     ["software", "data_ml", "analytics", "quant"]),
)


class Sniff(RefusalCase):
    """Metadata checks before anything is downloaded (2.1)."""

    def test_a_pdf_is_a_pdf_when_pypdf_is_there(self):
        if resume_parse.pdf_supported():
            self.assertEqual(resume_parse.sniff("resume.pdf", "application/pdf", 1000), "pdf")
        else:
            with self.refused("no_pdf_support"):
                resume_parse.sniff("resume.pdf", "application/pdf", 1000)

    def test_size_must_be_one_byte_to_two_mebibytes(self):
        with self.refused("empty"):
            resume_parse.sniff("resume.txt", "text/plain", 0)
        with self.refused("too_big"):
            resume_parse.sniff("resume.txt", "text/plain", 2 * MIB + 1)
        self.assertEqual(resume_parse.sniff("resume.txt", "text/plain", 2 * MIB), "txt")

    def test_other_formats_are_refused_by_extension(self):
        for name in ("resume.doc", "resume.pages", "resume.rtf", "resume.png", "resume", None):
            with self.subTest(name=name), self.refused("bad_type"):
                resume_parse.sniff(name, None, 1000)

    def test_a_content_type_that_contradicts_the_name_is_refused(self):
        with self.refused("type_mismatch"):
            resume_parse.sniff("resume.pdf", "image/png", 1000)
        with self.refused("type_mismatch"):
            resume_parse.sniff("resume.docx", "application/pdf", 1000)

    def test_a_missing_content_type_passes(self):
        self.assertEqual(resume_parse.sniff("resume.docx", None, 1000), "docx")
        self.assertEqual(resume_parse.sniff("resume.docx", "", 1000), "docx")

    def test_the_extension_is_case_insensitive(self):
        self.assertEqual(resume_parse.sniff("RESUME.DOCX", None, 1000), "docx")

    def test_content_type_parameters_and_generic_zip_types_pass(self):
        self.assertEqual(resume_parse.sniff("a.txt", "text/plain; charset=utf-8", 10), "txt")
        for generic in ("application/octet-stream", "application/zip"):
            with self.subTest(generic=generic):
                self.assertEqual(resume_parse.sniff("a.docx", generic, 10), "docx")

    def test_a_pdf_is_refused_up_front_when_pypdf_is_missing(self):
        with mock.patch.object(resume_parse, "pdf_supported", return_value=False):
            with self.refused("no_pdf_support"):
                resume_parse.sniff("a.pdf", "application/pdf", 1000)


class CheckMagic(RefusalCase):
    """What the bytes must look like once downloaded (2.1)."""

    def test_the_wrong_magic_is_refused(self):
        with self.refused("bad_magic"):
            resume_parse.check_magic(b"hello, I am not a PDF", "pdf")
        with self.refused("bad_magic"):
            resume_parse.check_magic(b"PK\x03\x04 but not a zip at all", "docx")

    def test_a_zip_without_a_word_document_is_refused(self):
        with self.refused("bad_magic"):
            resume_parse.check_magic(zip_of({"notes.txt": "hello"}), "docx")

    def test_real_magic_passes(self):
        resume_parse.check_magic(make_docx(["EDUCATION"]), "docx")
        resume_parse.check_magic(make_pdf(["EDUCATION"]), "pdf")
        resume_parse.check_magic(b"EDUCATION", "txt")

    def test_a_nul_early_in_a_text_file_is_refused(self):
        with self.refused("bad_magic"):
            resume_parse.check_magic(b"EDUCATION\x00", "txt")
        resume_parse.check_magic(b"x" * 4096 + b"\x00", "txt")

    def test_more_than_the_cap_is_refused(self):
        with self.refused("too_big"):
            resume_parse.check_magic(b"x" * (resume_parse.MAX_BYTES + 1), "txt")


class Docx(RefusalCase):
    """Word files, read with regular expressions and no XML parser (4.4.1)."""

    def test_each_paragraph_becomes_a_line(self):
        text = resume_parse.docx_text(make_docx(["EDUCATION", "B.S. Physics & Math"]))
        self.assertEqual(text.split("\n")[:2], ["EDUCATION", "B.S. Physics & Math"])

    def test_tabs_and_breaks_separate_words(self):
        # Word resumes put a tab between a degree and its date; lost, the two
        # run together and neither is found.
        xml = ('<w:document><w:body><w:p><w:r><w:t>Python</w:t><w:tab/><w:t>SQL</w:t>'
               '<w:br/><w:t>Excel</w:t></w:r></w:p></w:body></w:document>')
        text = resume_parse.docx_text(zip_of({"word/document.xml": xml}))
        self.assertEqual(text.split("\n")[0], "Python SQL Excel")

    def test_a_doctype_or_an_entity_is_refused(self):
        for prolog in ('<!DOCTYPE w [<!ELEMENT w ANY>]>', '<!entity x "y">'):
            with self.subTest(prolog=prolog), self.refused("xml_entity"):
                resume_parse.docx_text(make_docx(["EDUCATION"], prolog=prolog))

    def test_an_oversized_document_member_is_refused(self):
        bomb = zip_of({"word/document.xml": " " * (5 * MIB + 1)})
        with self.refused("zip_bomb"):
            resume_parse.docx_text(bomb)

    def test_a_docx_with_almost_no_text_has_no_text(self):
        with self.refused("no_text"):
            resume_parse.extract_text(make_docx(["EDUCATION", "B.S."]), "docx")

    def test_a_tag_that_never_closes_is_read_in_linear_time(self):
        # A run of "<w:tab " with no ">" once cost the square of its length: a
        # 7 KB upload held a worker until the CPU limit killed it. Up to the
        # cap, each of these now takes a fraction of a second, not hours.
        for tag in ("<w:tab ", "<w:br ", "<w:cr ", "<w:t ", '<w:t x="'):
            xml = tag * ((resume_parse.MAX_DOCX_XML - 1024) // len(tag))
            data = zip_of({"word/document.xml": xml})
            with self.subTest(tag=tag):
                started = time.monotonic()
                resume_parse.docx_text(data)
                self.assertLess(time.monotonic() - started, LINEAR_SECONDS)

    def test_a_member_that_understates_its_size_is_never_read_past_the_cap(self):
        # The cap is checked on the declared size before any pattern runs; a
        # member larger than it says must be refused, not read in full.
        data = bytearray(zip_of({"word/document.xml": "<w:t>x</w:t>" * MIB}))
        central, local = data.rfind(b"PK\x01\x02"), data.find(b"PK\x03\x04")
        struct.pack_into("<I", data, central + 24, 1000)
        struct.pack_into("<I", data, local + 22, 1000)
        with self.refused("corrupt"):
            resume_parse.docx_text(bytes(data))

    def test_attributes_on_runs_tabs_and_breaks_still_read(self):
        xml = ('<w:p><w:r><w:t xml:space="preserve">B.S. </w:t><w:tab w:val="left"/>'
               '<w:t>Physics</w:t><w:br w:type="page"/><w:t>2027</w:t></w:r></w:p>')
        text = resume_parse.docx_text(zip_of({"word/document.xml": xml}))
        self.assertEqual(text.split("\n")[0], "B.S.  Physics 2027")


class Txt(RefusalCase):
    def test_utf8_and_cp1252_both_decode(self):
        words = "Résumé — Bachelor of Science in Biology"
        self.assertEqual(resume_parse.extract_text(words.encode("utf-8"), "txt"), words)
        self.assertEqual(resume_parse.extract_text(words.encode("cp1252"), "txt"), words)

    def test_under_20_characters_is_a_short_paste(self):
        with self.refused("paste_short"):
            resume_parse.extract_text(b"B.S. Physics", "txt")

    def test_text_is_cut_to_30000_characters(self):
        text = resume_parse.extract_text(b"x" * 40_000, "txt")
        self.assertEqual(len(text), resume_parse.MAX_TEXT_CHARS)


class Pdf(RefusalCase):
    @needs_pypdf
    def test_a_text_pdf_is_read(self):
        text = resume_parse.extract_text(make_pdf(MECH_LINES), "pdf")
        self.assertIn("B.S. Mechanical Engineering", text)

    @needs_pypdf
    def test_more_than_ten_pages_is_refused(self):
        with self.refused("too_many_pages"):
            resume_parse.extract_text(blank_pdf(11), "pdf")

    @needs_pypdf
    def test_a_page_with_no_text_has_no_text(self):
        with self.refused("no_text"):
            resume_parse.extract_text(blank_pdf(1), "pdf")

    @needs_pypdf
    def test_a_password_protected_pdf_is_refused(self):
        with self.refused("encrypted"):
            resume_parse.extract_text(blank_pdf(1, password="hunter2"), "pdf")

    @needs_pypdf
    def test_a_broken_pdf_is_corrupt(self):
        with pypdf_quiet(), self.refused("corrupt"):
            resume_parse.extract_text(b"%PDF-1.4\nnothing else", "pdf")

    def test_without_pypdf_a_pdf_is_unsupported(self):
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            with self.refused("no_pdf_support"):
                resume_parse.extract_text(make_pdf(MECH_LINES), "pdf")


class Normalize(unittest.TestCase):
    """Text as the extractor reads it (4.4.2)."""

    def test_glyphs_invisibles_and_spacing_are_normalised(self):
        raw = ("E D U C A T I O N\r\n• B.S.\u00a0\u00a0Bio\u200bchem\u00adistry "
               "— 2027\rEnd")
        self.assertEqual(resume_parse.normalize(raw),
                         ["EDUCATION", "B.S. Biochemistry - 2027", "End"])

    def test_nothing_normalises_to_one_empty_line(self):
        self.assertEqual(resume_parse.normalize(""), [""])

    def test_headings_switch_sections_and_are_dropped(self):
        tagged = resume_parse.sections(
            ["Jane", "EDUCATION", "B.S. Physics", "", "Experience:", "Intern",
             "Skills: Python, C"])
        self.assertEqual(tagged, [("header", "Jane"), ("education", "B.S. Physics"),
                                  ("experience", "Intern"), ("skills", "Python, C")])


class DeriveDraft(RefusalCase):
    """The extractor, on the synthetic resumes of 4.4.6 (today = 2026-09-28)."""

    def test_every_row_of_the_fixture_table(self):
        for name, lines, majors, minors, degree, grad, fields in EXTRACTOR_ROWS:
            with self.subTest(row=name):
                draft = resume_parse.derive_draft(resume(*lines), TODAY)
                self.assertEqual(draft["majors"], majors)
                self.assertEqual(draft["minors"], minors)
                self.assertEqual(draft["degree"], degree)
                self.assertEqual((draft["grad_year"], draft["grad_month"]), grad)
                self.assertEqual(draft["fields"], fields)

    def test_no_resume_detail_outside_the_vocabulary_comes_back(self):
        # The canary: each fixture opens with a name, email, phone and street.
        for name, lines, *_ in EXTRACTOR_ROWS:
            draft = resume_parse.derive_draft(resume(*lines), TODAY)
            for rendering in (json.dumps(draft), repr(draft), str(draft)):
                for secret in CANARY:
                    with self.subTest(row=name, secret=secret):
                        self.assertNotIn(secret.lower(), rendering.lower())

    def test_a_date_outside_education_is_never_a_graduation(self):
        draft = resume_parse.derive_draft(resume("Experience", "Software intern 2025"), TODAY)
        self.assertIsNone(draft["grad_year"])
        self.assertIsNone(draft["evidence"]["grad"])

    def test_a_pharmd_outranks_a_bachelors(self):
        draft = resume_parse.derive_draft(
            resume("EDUCATION", "B.S. Pharmaceutical Sciences, 2024", "Pharm.D. Candidate"),
            TODAY)
        self.assertEqual(draft["degree"], "pharmd")

    def test_a_spaced_out_skills_heading_is_recognised(self):
        # C only counts on a Skills line, so it is found only if the heading was.
        draft = resume_parse.derive_draft(
            resume("EDUCATION", "B.S. Physics", "S K I L L S", "Python, C"), TODAY)
        self.assertIn("c", draft["skills"])

    def test_evidence_says_where_the_study_and_the_date_were_read(self):
        from_education = resume_parse.derive_draft(resume(*EXTRACTOR_ROWS[0][1]), TODAY)
        self.assertEqual(from_education["evidence"], {"study": "education", "grad": "education"})
        from_text = resume_parse.derive_draft(resume("B.S. Physics, Expected May 2028"), TODAY)
        self.assertEqual(from_text["evidence"], {"study": "text", "grad": "text"})

    def test_every_id_in_a_draft_is_in_the_vocabulary(self):
        for name, lines, *_ in EXTRACTOR_ROWS:
            draft = resume_parse.derive_draft(resume(*lines), TODAY)
            with self.subTest(row=name):
                self.assertLessEqual(set(draft["majors"] + draft["minors"]),
                                     set(resume_lexicon.MAJOR_BY_ID))
                self.assertLessEqual(set(draft["skills"]), set(resume_lexicon.SKILL_BY_ID))

    def test_parse_bytes_reads_a_text_upload_end_to_end(self):
        draft = resume_parse.parse_bytes(resume(*EXTRACTOR_ROWS[0][1]).encode(), "txt", TODAY)
        self.assertEqual(draft["majors"], ["mechanical_engineering"])

    def test_parse_bytes_reads_a_docx_end_to_end(self):
        draft = resume_parse.parse_bytes(make_docx(MECH_LINES), "docx", TODAY)
        self.assertEqual(draft["majors"], ["mechanical_engineering"])

    def test_parse_bytes_refuses_a_kind_it_does_not_know(self):
        with self.refused("bad_type"):
            resume_parse.parse_bytes(b"MZ", "exe", TODAY)


def draft_of(*lines: str) -> dict:
    return resume_parse.derive_draft(resume(*lines), TODAY)


def grad_of(*lines: str) -> tuple:
    draft = draft_of(*lines)
    return draft["grad_year"], draft["grad_month"]


class GraduationDate(unittest.TestCase):
    """Dates in Education that are not a graduation (4.4.4 rule 2)."""

    def test_a_range_ending_in_present_is_not_a_graduation(self):
        # The start of an ongoing degree read as its end made a current
        # student a graduate, and every internship dropped out of their list.
        for when in ("Sep 2024 - Present", "2023 - Present", "Fall 2023 - Current",
                     "09/2023 - present", "Sept 2023 to Now", "Sep 2023 – Present"):
            with self.subTest(when=when):
                self.assertEqual(grad_of("EDUCATION", f"University of California, Irvine {when}",
                                         "B.S. Computer Science"), (None, None))

    def test_an_earlier_degree_beside_an_ongoing_one_is_not_a_graduation(self):
        self.assertEqual(grad_of("EDUCATION", "M.S. Computer Science, Sep 2025 - Present",
                                 "B.S. Mathematics, UCLA, 2025"), (None, None))

    def test_a_future_date_beside_an_ongoing_range_still_counts(self):
        self.assertEqual(grad_of("EDUCATION", "B.S. Physics, Sep 2023 - Jun 2027",
                                 "Undergraduate researcher, Jan 2025 - Present"), (2027, 6))

    def test_an_expected_date_wins_over_an_ongoing_range(self):
        self.assertEqual(grad_of("EDUCATION", "UCI Sep 2024 - Present",
                                 "B.S. Computer Science, Expected June 2028"), (2028, 6))

    def test_course_numbers_are_not_years(self):
        # Many schools number courses 2010, 2020, 2030: ECON 2030 was June 2030.
        for courses in ("Coursework: ACCT 2020, ECON 2030, FIN 3000",
                        "ACCT 2020, ECON 2030, FIN 3000",
                        "Relevant courses: Econ 2030, Intermediate Accounting"):
            with self.subTest(courses=courses):
                self.assertEqual(grad_of("EDUCATION", "Aug 2023 - May 2027", "B.S. Accounting",
                                         courses), (2027, 5))

    def test_an_ongoing_club_or_certificate_leaves_a_past_degree_standing(self):
        # A recent graduate who lists a club role or a course still running
        # under Education is not a current student.
        self.assertEqual(grad_of("EDUCATION", "University of California, Irvine Sep 2021 - Jun 2026",
                                 "B.S. Computer Science", "Chess Club officer, Sep 2022 - Present"),
                         (2026, 6))
        self.assertEqual(grad_of("EDUCATION", "B.S. Economics, UCLA, Jun 2025",
                                 "Google Data Analytics Certificate, Coursera, Jan 2026 - Present"),
                         (2025, 6))

    def test_an_ongoing_school_line_without_the_word_university_still_means_studying(self):
        self.assertEqual(grad_of("EDUCATION", "UCI Sep 2024 - Present", "B.S. Computer Science"),
                         (None, None))

    def test_a_graduation_year_beside_course_codes_on_a_degree_line_is_kept(self):
        for line in ("B.S. Computer Science 2027 | Coursework: CS 161, CS 171",
                     "UC Irvine, B.S. CS, 2027. Courses: ICS 31, ICS 32",
                     "BS MATH 2023, MS CS 2027"):
            with self.subTest(line=line):
                self.assertEqual(grad_of("EDUCATION", line), (2027, 6))

    def test_a_job_title_that_starts_with_education_is_not_a_heading(self):
        self.assertEqual(grad_of("EDUCATION", "UC Irvine", "Sep 2022 - Jun 2026", "B.S. Biology",
                                 "EXPERIENCE", "Irvine Nature Center", "Education and Outreach Intern",
                                 "Jul 2026 - Present"), (2026, 6))

    def test_a_school_and_a_year_are_still_a_graduation(self):
        self.assertEqual(grad_of("EDUCATION", "B.S. Physics, UCI 2027"), (2027, 6))
        self.assertEqual(grad_of("EDUCATION", "MBA 2027"), (2027, 6))

    def test_common_education_headings_are_recognised(self):
        # Without the heading there is no Education pool, and a graduation is
        # then read only from an explicit "Expected ...".
        for heading in ("EDUCATION & HONORS", "Education and Certifications",
                        "Educational Background", "ACADEMIC HISTORY", "Education & Awards",
                        "EDUCATION / CERTIFICATIONS", "Education:", "Academic Background"):
            with self.subTest(heading=heading):
                draft = draft_of(heading, "UCI Sep 2022 - Jun 2026",
                                 "B.S. Mechanical Engineering")
                self.assertEqual((draft["grad_year"], draft["grad_month"]), (2026, 6))
                self.assertEqual(draft["evidence"], {"study": "education", "grad": "education"})


class MajorPhrase(unittest.TestCase):
    """Which words after a degree are its major (4.4.3)."""

    def test_a_school_name_on_the_degree_line_is_not_a_major(self):
        # "..., Viterbi School of Engineering" added General Engineering and
        # three fields a CS student never chose.
        for line, majors in (
                ("Bachelor of Science in Computer Science, Viterbi School of Engineering",
                 ["computer_science"]),
                ("B.S. Computer Science, Henry Samueli School of Engineering, UC Irvine",
                 ["computer_science"]),
                ("B.S. Chemical Engineering, Grainger College of Engineering",
                 ["chemical_engineering"]),
                ("B.A. Business Economics, Paul Merage School of Business",
                 ["business_economics"]),
                ("B.S. Computer Science at the Pratt School of Engineering",
                 ["computer_science"]),
                ("B.S. Computer Science, Mathematics, Samueli School",
                 ["computer_science", "mathematics"])):
            with self.subTest(line=line):
                self.assertEqual(draft_of("EDUCATION", line)["majors"], majors)
        viterbi = draft_of("EDUCATION", "B.S. Computer Science, Viterbi School of Engineering")
        self.assertEqual(viterbi["fields"], ["software", "data_ml"])

    def test_short_degree_forms_are_degrees_with_majors(self):
        for line, degree, majors in (
                ("BFA, Graphic Design", "bachelor", ["art"]),
                ("B.F.A. Graphic Design", "bachelor", ["art"]),
                ("BBA, Finance", "bachelor", ["finance"]),
                ("B.B.A. in Finance", "bachelor", ["finance"]),
                ("BSBA, Marketing", "bachelor", ["marketing"]),
                ("BSN, Nursing", "bachelor", ["nursing"]),
                ("B.Arch, Architecture", "bachelor", ["architecture"]),
                ("B.Tech in Computer Science", "bachelor", ["computer_science"]),
                ("BTech Computer Science", "bachelor", ["computer_science"]),
                ("MSc Computer Science", "master", ["computer_science"]),
                ("M.Sc. Computer Science", "master", ["computer_science"]),
                ("MFA, Drama", "master", ["drama_music"]),
                ("M.Tech in Computer Science", "master", ["computer_science"])):
            with self.subTest(line=line):
                draft = draft_of("EDUCATION", line)
                self.assertEqual((draft["degree"], draft["majors"]), (degree, majors))

    def test_mfa_the_login_step_is_not_a_degree(self):
        # With no Education heading the first 40 lines are searched, and an
        # IT resume says "MFA" for multi-factor authentication.
        self.assertIsNone(draft_of("Implemented MFA for VPN access", "Enabled MFA.")["degree"])

    def test_mfa_in_a_list_of_security_tools_is_not_a_masters(self):
        for lines in (("EDUCATION", "B.S. Information Technology, Expected May 2027", "Okta, MFA, SSO, Active Directory"),
                      ("EDUCATION", "B.S. Cybersecurity, Expected May 2027", "MFA, Duo, CrowdStrike"),
                      ("SKILLS", "MFA, SSO, Okta")):
            with self.subTest(lines=lines):
                self.assertIn(draft_of(*lines)["degree"], ("bachelor", None))

    def test_an_mfa_in_an_arts_field_is_a_masters(self):
        for line in ("MFA in Creative Writing", "MFA, Fine Arts", "M.F.A., Studio Art"):
            with self.subTest(line=line):
                self.assertEqual(draft_of("EDUCATION", line)["degree"], "master")

    def test_a_major_on_the_line_beside_a_bare_degree_is_read(self):
        for lines in (("Bachelor of Science", "Computer Science"),
                      ("Bachelor of Science, Expected June 2027", "Computer Science | GPA 3.8"),
                      ("Computer Science", "Bachelor of Science")):
            with self.subTest(lines=lines):
                self.assertEqual(draft_of("EDUCATION", *lines)["majors"], ["computer_science"])

    def test_a_line_beside_a_bare_degree_that_is_not_only_a_major_is_ignored(self):
        for neighbour in ("Engineering Honor Society", "Relevant coursework: Statistics",
                          "School of the Art Institute of Chicago"):
            with self.subTest(neighbour=neighbour):
                self.assertEqual(draft_of("EDUCATION", "Bachelor of Science", neighbour)["majors"],
                                 [])


def valid_draft(**changes) -> dict:
    base = {"majors": ["physics"], "minors": [], "degree": "bachelor", "grad_year": 2027,
            "grad_month": 6, "skills": ["python"], "fields": ["electrical"],
            "evidence": {"study": "education", "grad": "education"}}
    return {**base, **changes}


class ValidateDraft(RefusalCase):
    """The parent rebuilding whatever the child sent (4.4.9)."""

    def test_a_good_draft_survives_unchanged(self):
        self.assertEqual(resume_parse.validate_draft(valid_draft()), valid_draft())

    def test_a_derived_draft_survives_unchanged(self):
        for name, lines, *_ in EXTRACTOR_ROWS:
            draft = resume_parse.derive_draft(resume(*lines), TODAY)
            with self.subTest(row=name):
                self.assertEqual(resume_parse.validate_draft(draft), draft)

    def test_unknown_non_string_and_repeated_ids_are_dropped(self):
        draft = resume_parse.validate_draft(valid_draft(
            majors=["physics", "astrology", 7, "physics"], skills=["python", None, "telepathy"],
            fields=["software", "pharmacy_school"], degree="wizard"))
        self.assertEqual(draft["majors"], ["physics"])
        self.assertEqual(draft["skills"], ["python"])
        self.assertEqual(draft["fields"], ["software"])
        self.assertIsNone(draft["degree"])

    def test_a_string_where_a_list_belongs_is_not_read_letter_by_letter(self):
        self.assertEqual(resume_parse.validate_draft(valid_draft(majors="physics"))["majors"], [])

    def test_true_is_not_a_year(self):
        draft = resume_parse.validate_draft(valid_draft(grad_year=True))
        self.assertIsNone(draft["grad_year"])

    def test_a_month_without_a_year_is_dropped(self):
        draft = resume_parse.validate_draft(valid_draft(grad_year=None, grad_month=6))
        self.assertIsNone(draft["grad_month"])

    def test_years_and_months_out_of_range_are_dropped(self):
        draft = resume_parse.validate_draft(valid_draft(grad_year=1999, grad_month=13))
        self.assertEqual((draft["grad_year"], draft["grad_month"]), (None, None))
        draft = resume_parse.validate_draft(valid_draft(grad_month=0))
        self.assertIsNone(draft["grad_month"])

    def test_unknown_keys_are_dropped(self):
        draft = resume_parse.validate_draft(valid_draft(name=CANARY[0], email=CANARY[1]))
        self.assertEqual(set(draft), set(valid_draft()))

    def test_list_caps_are_enforced(self):
        majors = [m.id for m in resume_lexicon.MAJORS]
        skills = [s.id for s in resume_lexicon.SKILLS]
        fields = ["software", "data_ml", "analytics", "security", "it", "electrical", "civil"]
        draft = resume_parse.validate_draft(valid_draft(
            majors=majors, minors=majors, skills=skills, fields=fields))
        self.assertEqual([len(draft[k]) for k in ("majors", "minors", "skills", "fields")],
                         [3, 2, 40, 6])

    def test_evidence_keeps_only_the_three_known_values(self):
        draft = resume_parse.validate_draft(valid_draft(
            evidence={"study": "resume", "grad": "text", "raw": CANARY[3]}))
        self.assertEqual(draft["evidence"], {"study": None, "grad": "text"})
        draft = resume_parse.validate_draft(valid_draft(evidence="education"))
        self.assertEqual(draft["evidence"], {"study": None, "grad": None})

    def test_something_that_is_not_a_dict_is_a_failed_worker(self):
        for junk in (None, [], "draft", 7):
            with self.subTest(junk=junk), self.refused("worker_failed"):
                resume_parse.validate_draft(junk)


class Refusals(unittest.TestCase):
    def test_an_unknown_reason_becomes_worker_failed(self):
        self.assertEqual(ResumeRefusal("nonsense").reason, "worker_failed")

    def test_the_reasons_are_exactly_the_upload_error_table(self):
        self.assertEqual(resume_parse.REASONS, {
            "too_big", "empty", "bad_type", "type_mismatch", "bad_magic", "no_pdf_support",
            "encrypted", "too_many_pages", "no_text", "corrupt", "zip_bomb", "xml_entity",
            "timeout", "worker_failed", "busy", "already_reading", "rate_limited", "paste_short"})


class SourceRules(unittest.TestCase):
    """What must never be in `resume_parse.py` (2.7), read with `ast`."""

    NETWORK = {"socket", "urllib", "http", "aiohttp", "requests", "discord"}

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((BOT / "resume_parse.py").read_text(encoding="utf-8"))

    def imported(self, nodes):
        for node in nodes:
            if isinstance(node, ast.Import):
                yield from (alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                yield node.module.split(".")[0]

    def test_pypdf_is_never_imported_at_module_scope(self):
        self.assertNotIn("pypdf", set(self.imported(self.tree.body)))

    def test_nothing_is_written_to_disk(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                self.assertNotIn(name, {"open", "write_bytes", "write_text"})
        self.assertNotIn("tempfile", set(self.imported(ast.walk(self.tree))))

    def test_no_network_module_is_imported(self):
        self.assertFalse(set(self.imported(ast.walk(self.tree))) & self.NETWORK)


if __name__ == "__main__":
    unittest.main()
