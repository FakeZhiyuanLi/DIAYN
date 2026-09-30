"""
The resume vocabulary: every major and skill the finder can read off a resume.

    python3 -m unittest discover -s tests      # no install needed

`resume_lexicon` is the only way a word from a resume becomes something stored.
A major or a skill that is not in these two tables cannot reach a profile, so
most of what is pinned here is that the tables are whole and consistent: ids
unique, every field they point at real, every field reachable. The rest is the
lookups the worker and the Edit details modal share, where the rule that
matters is "longest alias wins": `business economics` is one major, not
Business plus Economics.

Pure module, so this file needs nothing installed.
"""

import dataclasses
import re
import unittest

import intern_vocab
import resume_lexicon as lexicon

#: The three one- or two-letter skills that are ordinary words in prose, and so
#: only count as a whole item on a Skills line, spelled exactly.
STRICT_SKILLS = {"c", "go", "r"}


class LexiconShape(unittest.TestCase):
    """The tables themselves (4.4.7, 4.4.8)."""

    def test_there_are_63_majors_with_unique_ids(self):
        ids = [major.id for major in lexicon.MAJORS]
        self.assertEqual(len(ids), 63)
        self.assertEqual(len(set(ids)), 63)

    def test_there_are_105_skills_with_unique_ids(self):
        ids = [skill.id for skill in lexicon.SKILLS]
        self.assertEqual(len(ids), 105)
        self.assertEqual(len(set(ids)), 105)

    def test_every_field_a_major_or_skill_names_is_a_real_field(self):
        known = set(intern_vocab.FIELD_IDS)
        for entry in (*lexicon.MAJORS, *lexicon.SKILLS):
            with self.subTest(entry=entry.id):
                self.assertTrue(entry.fields)
                self.assertLessEqual(set(entry.fields), known)

    def test_every_field_is_reached_by_some_major(self):
        # A field no major reaches is one a resume can never suggest.
        reached = {field for major in lexicon.MAJORS for field in major.fields}
        self.assertEqual(reached, set(intern_vocab.FIELD_IDS))

    def test_every_field_is_reached_by_some_skill(self):
        reached = {field for skill in lexicon.SKILLS for field in skill.fields}
        self.assertEqual(reached, set(intern_vocab.FIELD_IDS))

    def test_every_skill_pattern_compiles(self):
        for skill in lexicon.SKILLS:
            with self.subTest(skill=skill.id):
                re.compile(skill.pattern)

    def test_the_strict_skills_are_c_go_and_r(self):
        strict = {skill.id for skill in lexicon.SKILLS if skill.strict}
        self.assertEqual(strict, STRICT_SKILLS)

    def test_aliases_are_lower_case_and_spell_and_rather_than_an_ampersand(self):
        # Text is lower-cased and "&" becomes "and" before aliases are matched,
        # so an alias written any other way can never match anything.
        for major in lexicon.MAJORS:
            for alias in major.aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(alias, alias.lower())
                    self.assertNotIn("&", alias)

    def test_every_generic_alias_is_a_real_alias(self):
        # A generic alias that no major carries is an exclusion guarding nothing.
        aliases = {alias for major in lexicon.MAJORS for alias in major.aliases}
        self.assertLessEqual(lexicon.GENERIC_ALIASES, aliases)

    def test_the_lookups_index_every_entry(self):
        self.assertEqual(set(lexicon.MAJOR_BY_ID), {m.id for m in lexicon.MAJORS})
        self.assertEqual(set(lexicon.SKILL_BY_ID), {s.id for s in lexicon.SKILLS})
        self.assertEqual(lexicon.MAJOR_BY_ID["physics"].label, "Physics")

    def test_entries_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lexicon.MAJORS[0].label = "Changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lexicon.SKILLS[0].strict = True


class MajorsIn(unittest.TestCase):
    """Naming every major in a phrase (4.4.3 step 2)."""

    def test_two_majors_joined_by_and_are_both_found(self):
        self.assertEqual(lexicon.majors_in("Biology and Chemistry"),
                         ("biological_sciences", "chemistry"))

    def test_an_ampersand_reads_as_and(self):
        self.assertEqual(lexicon.majors_in("Criminology, Law & Society"), ("criminology",))

    def test_the_longest_alias_wins(self):
        self.assertEqual(lexicon.majors_in("Business Economics"), ("business_economics",))

    def test_majors_come_back_in_the_order_they_appear(self):
        self.assertEqual(lexicon.majors_in("Mathematics and Computer Science"),
                         ("mathematics", "computer_science"))

    def test_an_alias_inside_a_longer_word_is_not_a_hit(self):
        self.assertEqual(lexicon.majors_in("Mathematical modelling for Martian habitats"), ())

    def test_skip_generic_ignores_the_everyday_words(self):
        # "Biology" alone is also how people describe a course or a club.
        self.assertEqual(lexicon.majors_in("Biology student", skip_generic=True), ())
        self.assertEqual(lexicon.majors_in("UCI - Business Economics", skip_generic=True),
                         ("business_economics",))

    def test_nothing_is_found_in_nothing(self):
        self.assertEqual(lexicon.majors_in(""), ())
        self.assertEqual(lexicon.majors_in(None), ())

    def test_a_major_named_twice_keeps_the_position_of_its_longest_alias(self):
        self.assertEqual(lexicon.majors_in("Math and Computer Science and Applied Mathematics"),
                         ("computer_science", "mathematics"))

    def test_a_label_names_its_own_major(self):
        self.assertEqual(lexicon.majors_in("B.A. Languages & Cultures"), ("languages",))

    def test_match_major_is_the_first_major_named(self):
        self.assertEqual(lexicon.match_major("B.S. Mathematics and Computer Science"),
                         "mathematics")
        self.assertIsNone(lexicon.match_major("Astrobiology"))


class MajorsAlone(unittest.TestCase):
    """A line that is a major and nothing else (4.4.3, the line beside a bare degree)."""

    def test_a_line_of_only_majors_names_them(self):
        self.assertEqual(lexicon.majors_alone("Computer Science"), ("computer_science",))
        self.assertEqual(lexicon.majors_alone(" Biology & Chemistry, "),
                         ("biological_sciences", "chemistry"))
        self.assertEqual(lexicon.majors_alone("Physics and Mathematics"),
                         ("physics", "mathematics"))

    def test_a_line_with_anything_else_on_it_names_nothing(self):
        for text in ("Engineering Honor Society", "Relevant coursework: Statistics",
                     "University of California, Irvine", "Dean's List", "", None):
            with self.subTest(text=text):
                self.assertEqual(lexicon.majors_alone(text), ())


class FindSkills(unittest.TestCase):
    """Counting skills (4.4.5)."""

    def test_a_skill_is_counted_anywhere_in_the_text(self):
        self.assertEqual(lexicon.find_skills("Built a rig in SolidWorks", ()), ("solidworks",))

    def test_javascript_is_not_also_java(self):
        self.assertEqual(lexicon.find_skills("JavaScript", ()), ("javascript",))

    def test_strict_skills_count_only_on_a_skills_line(self):
        prose = "Go to C Building. R is my initial."
        self.assertEqual(lexicon.find_skills(prose, ()), ())
        self.assertEqual(lexicon.find_skills(prose, ("Python, C, Go and R",)),
                         ("c", "go", "r"))

    def test_strict_skills_are_case_sensitive(self):
        self.assertEqual(lexicon.find_skills("", ("go, r, c",)), ())

    def test_more_mentions_come_first_then_lexicon_order(self):
        text = "Python. Excel models. Excel dashboards. SQL."
        self.assertEqual(lexicon.find_skills(text, ()), ("excel", "python", "sql"))

    def test_at_most_40_skills_are_kept(self):
        text = ", ".join(skill.label for skill in lexicon.SKILLS)
        self.assertEqual(len(lexicon.find_skills(text, ())), intern_vocab.MAX_SKILLS)


class FieldsFor(unittest.TestCase):
    """The draft's fields (4.4.6)."""

    def test_fields_are_the_majors_fields_primary_first(self):
        self.assertEqual(lexicon.fields_for(("computer_science", "mathematics"), ()),
                         ("software", "data_ml", "analytics", "quant"))

    def test_fields_are_capped_at_six(self):
        majors = ("informatics", "statistics", "business_administration")
        self.assertEqual(len(lexicon.fields_for(majors, ())), intern_vocab.MAX_FIELDS)

    def test_with_no_major_a_field_needs_three_skills_voting_for_it(self):
        skills = ("python", "java", "sql", "react", "aws", "docker", "git")
        self.assertEqual(lexicon.fields_for((), skills), ("software",))

    def test_with_no_major_at_most_two_fields_come_from_skills(self):
        # data_ml 4 votes, analytics 4, software 3: the tie goes to field order,
        # and software, with enough votes of its own, is the third and is cut.
        skills = ("python", "pandas", "ml_frameworks", "machine_learning", "java", "cpp",
                  "excel", "tableau", "power_bi")
        self.assertEqual(lexicon.fields_for((), skills), ("data_ml", "analytics"))

    def test_two_skills_are_not_enough_to_suggest_a_field(self):
        self.assertEqual(lexicon.fields_for((), ("python", "java")), ())

    def test_unknown_ids_are_ignored_rather_than_raising(self):
        self.assertEqual(lexicon.fields_for(("nope",), ("nope",)), ())


class ResolveMajorWords(unittest.TestCase):
    """What the Edit details modal does with the Major(s) box."""

    def test_majors_minors_and_unknown_parts_are_separated(self):
        majors, minors, unknown = lexicon.resolve_major_words(
            "Mechanical Engineering; minor Mathematics; Astrobiology")
        self.assertEqual(majors, ("mechanical_engineering",))
        self.assertEqual(minors, ("mathematics",))
        self.assertEqual(unknown, ("Astrobiology",))

    def test_every_minor_spelling_feeds_minors(self):
        for text in ("minor in Chemistry", "Minors: Chemistry", "minor: Chemistry"):
            with self.subTest(text=text):
                self.assertEqual(lexicon.resolve_major_words(text)[1], ("chemistry",))

    def test_newlines_separate_parts_and_blank_parts_are_ignored(self):
        majors, minors, unknown = lexicon.resolve_major_words("Physics\n\n ;Economics")
        self.assertEqual((majors, minors, unknown), (("physics", "economics"), (), ()))

    def test_a_minor_that_is_already_a_major_is_not_repeated(self):
        majors, minors, _ = lexicon.resolve_major_words("Mathematics; minor Mathematics")
        self.assertEqual((majors, minors), (("mathematics",), ()))

    def test_caps_are_three_majors_and_two_minors(self):
        majors, minors, _ = lexicon.resolve_major_words(
            "Physics; Chemistry; Economics; Philosophy; minors: History, English, Dance")
        self.assertEqual(majors, ("physics", "chemistry", "economics"))
        self.assertEqual(minors, ("history", "english"))

    def test_an_unknown_part_is_cut_to_30_characters(self):
        _, _, unknown = lexicon.resolve_major_words("  " + "x" * 50 + "  ")
        self.assertEqual(unknown, ("x" * 30,))

    def test_every_label_the_modal_prefills_reads_back_as_its_major(self):
        # Edit details prefills the box with labels, and "minor " before a
        # minor. A label that does not read back is dropped on submit, so a
        # member who only changed their graduation date loses a major.
        for major in lexicon.MAJORS:
            with self.subTest(major=major.id):
                self.assertEqual(lexicon.resolve_major_words(major.label),
                                 ((major.id,), (), ()))
                self.assertEqual(lexicon.resolve_major_words(f"minor {major.label}"),
                                 ((), (major.id,), ()))

    def test_a_full_prefilled_box_reads_back_unchanged(self):
        # Three majors and two minors, "; "-joined exactly as the modal writes them.
        ids = [major.id for major in lexicon.MAJORS]
        for start in range(0, len(ids), 5):
            majors, minors = ids[start:start + 3], ids[start + 3:start + 5]
            text = "; ".join([lexicon.MAJOR_BY_ID[m].label for m in majors]
                             + [f"minor {lexicon.MAJOR_BY_ID[m].label}" for m in minors])
            with self.subTest(text=text):
                self.assertEqual(lexicon.resolve_major_words(text),
                                 (tuple(majors), tuple(minors), ()))


class ResolveSkillWords(unittest.TestCase):
    """What the Edit details modal does with the Skills box."""

    def test_labels_match_without_case_except_the_strict_ones(self):
        skills, unknown = lexicon.resolve_skill_words("Python, solidworks, lab safety, R")
        self.assertEqual(skills, ("python", "solidworks", "r"))
        self.assertEqual(unknown, ("lab safety",))

    def test_a_lower_case_strict_label_is_not_that_skill(self):
        self.assertEqual(lexicon.resolve_skill_words("r"), ((), ("r",)))

    def test_a_part_fully_matching_a_pattern_is_that_skill(self):
        self.assertEqual(lexicon.resolve_skill_words("pivot tables; C++\nJavaScript"),
                         (("excel", "cpp", "javascript"), ()))

    def test_a_part_that_only_contains_a_skill_is_unknown(self):
        self.assertEqual(lexicon.resolve_skill_words("python scripting"),
                         ((), ("python scripting",)))

    def test_repeats_are_kept_once(self):
        self.assertEqual(lexicon.resolve_skill_words("Python, python , PYTHON"),
                         (("python",), ()))

    def test_at_most_40_skills_are_kept(self):
        text = ", ".join(skill.label for skill in lexicon.SKILLS)
        skills, _ = lexicon.resolve_skill_words(text)
        self.assertEqual(len(skills), intern_vocab.MAX_SKILLS)


if __name__ == "__main__":
    unittest.main()
