"""
The documents that describe the code, held to the code.

    python3 -m unittest discover -s tests      # no install needed

Each of these has already drifted once somewhere. BaronChairStair's
example.env told operators GEMINI_RPM=15 and GEMINI_RPD=500 while the code
defaulted to 5 and 250, and documented neither POLL_HOST_* setting at all. A
guard in CI whose pattern had a typo would pass on every run and protect
nothing. And a commit id from before BaronChairStair's history rewrite, cited
in a public document, points at objects that must never be looked up again.
Nothing here reads a .env, a database or the network: only the tracked
documents, and the module's own tables.
"""

import os
import re
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()

import internship_poller as poller  # noqa: E402

# A line of example.env that sets one variable, commented out or not:
# `# GEMINI_RPD=250` or `POLL_CONTACT=`. Prose that happens to hold an `=`
# has more than one word after it, and does not match.
ASSIGNMENT = re.compile(r"^#?\s?([A-Z][A-Z0-9_]*)=(\S*)\s*$")

# Read from the environment before any .env loads, because it names the .env;
# so it is documented, though no Settings field holds it.
ENV_FILE_VARIABLE = "POLLER_ENV_FILE"

# Shown with an example rather than the default, because the default depends
# on where the checkout is, or is nobody's address.
DATA_PATHS = frozenset({"DIAYN_DATA", "POSTINGS_DB", "BOARDS_FILE", "YC_CACHE"})
EXAMPLE_VALUED = DATA_PATHS | {"POLL_CONTACT"}

# The only BaronChairStair commits any document may name: both are from after
# its history rewrite. Anything else that looks like a commit id is refused.
CITABLE_COMMITS = ("9f00cd5", "9130a2c")
COMMIT_ID = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")

# Paths the tracked-file guard must refuse, and tracked paths it must allow.
FORBIDDEN_PATHS = (
    "postings.db", "postings.db-journal", "postings.db-wal", "postings.db-shm",
    "postings.db.lock", "data/postings.db", "data/boards.json", "stats.db",
    ".env", ".env.local", "tests/.env", "boards.json", "yc_cache.json",
)
ALLOWED_PATHS = (
    "example.env", "internship_poller.py", "resolve_boards.py", "README.md",
    "CONTRACT.md", "DEPLOY.md", "CLAUDE.md", "requirements.txt", ".gitignore",
    ".github/workflows/ci.yml", "contract/postings_v1.sql",
    "contract/company_norm_cases.json", "contract/sample_urls.json",
    "tests/test_contract.py", "tests/aiohttp_stub.py", "LICENSE",
)


def read(name) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def documented(text) -> dict:
    """{variable: [value, ...]} for every assignment line in `text`."""
    found = {}
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            found.setdefault(match.group(1), []).append(match.group(2))
    return found


class ExampleEnv(unittest.TestCase):
    """example.env names every variable the scraper reads, at its real default."""

    @classmethod
    def setUpClass(cls):
        cls.found = documented(read("example.env"))
        cls.defaults = poller.Settings()

    def test_every_variable_the_code_reads_is_there_exactly_once(self):
        expected = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {ENV_FILE_VARIABLE}
        for var in sorted(expected):
            with self.subTest(var=var):
                self.assertEqual(len(self.found.get(var, [])), 1)

    def test_nothing_is_documented_that_the_code_does_not_read(self):
        read_by_code = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {ENV_FILE_VARIABLE}
        self.assertEqual(set(self.found) - read_by_code, set())

    def test_every_tunable_shows_the_code_default(self):
        # GEMINI_RPD=250, not the 500 BaronChairStair's file used to say.
        for name, var, kind in poller.SETTINGS_FROM_ENV:
            if var in EXAMPLE_VALUED or var in poller.SECRET_VARIABLES:
                continue
            with self.subTest(var=var):
                shown = self.found[var][0]
                parsed = shown if kind in (str, poller.ZoneInfo) else kind(shown)
                self.assertEqual(parsed, getattr(self.defaults, name))

    def test_data_paths_are_shown_absolute(self):
        # A relative POSTINGS_DB resolves against the working directory, which
        # under pm2 is wherever the process was started from.
        for var in sorted(DATA_PATHS):
            with self.subTest(var=var):
                self.assertTrue(self.found[var][0].startswith("/"))

    def test_secrets_are_shown_without_a_value(self):
        for var in sorted(poller.SECRET_VARIABLES):
            with self.subTest(var=var):
                self.assertEqual(self.found[var], [""])


class Requirements(unittest.TestCase):
    def test_pins_exactly_the_two_runtime_dependencies(self):
        lines = [ln.strip() for ln in read("requirements.txt").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        names = sorted(ln.split("==")[0].lower() for ln in lines)
        self.assertEqual(names, ["aiohttp", "python-dotenv"])
        for line in lines:
            with self.subTest(line=line):
                self.assertRegex(line, r"^[a-z-]+==\d+(\.\d+)+$")


class Readme(unittest.TestCase):
    def test_every_command_has_a_section(self):
        readme = read("README.md")
        commands = next(a for a in poller.arguments()._actions if a.dest == "cmd").choices
        for cmd in commands:
            with self.subTest(cmd=cmd):
                self.assertIn(f"### `{cmd}`", readme)


class CitedCommits(unittest.TestCase):
    def test_documents_cite_only_post_rewrite_commits(self):
        for name in sorted(n for n in os.listdir(ROOT) if n.endswith(".md")):
            for cited in COMMIT_ID.findall(read(name)):
                with self.subTest(document=name, commit=cited):
                    self.assertTrue(any(cited.startswith(c) or c.startswith(cited)
                                        for c in CITABLE_COMMITS))


class TrackedFileGuard(unittest.TestCase):
    """The CI step that fails when runtime data or a secret is tracked."""

    @classmethod
    def setUpClass(cls):
        match = re.search(r"git ls-files \| grep -E '([^']+)'",
                          read(os.path.join(".github", "workflows", "ci.yml")))
        cls.pattern = re.compile(match.group(1)) if match else None

    def test_ci_has_the_guard(self):
        self.assertIsNotNone(self.pattern)

    def test_refuses_runtime_data_and_secrets(self):
        for path in FORBIDDEN_PATHS:
            with self.subTest(path=path):
                self.assertIsNotNone(self.pattern.search(path))

    def test_allows_what_the_repository_tracks(self):
        for path in ALLOWED_PATHS:
            with self.subTest(path=path):
                self.assertIsNone(self.pattern.search(path))


if __name__ == "__main__":
    unittest.main()
