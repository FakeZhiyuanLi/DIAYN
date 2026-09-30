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

import ast
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

import diayn  # noqa: E402
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
    "example.env", "internship_poller.py", "resolve_boards.py", "diayn.py",
    "README.md",
    "CONTRACT.md", "DEPLOY.md", "CLAUDE.md", "requirements.txt", ".gitignore",
    ".github/workflows/ci.yml", "contract/postings_v1.sql",
    "contract/company_norm_cases.json", "contract/sample_urls.json",
    "tests/test_contract.py", "tests/aiohttp_stub.py", "LICENSE",
    "bot/README.md", "tests/bot/__init__.py", "tests/bot/test_bot_path.py",
    ".gitleaks.toml",
)

# The fake secrets the tests use, each saying so in its own value: the gitleaks
# allowlist must pass every one. And values a narrow allowlist must still refuse,
# because they only come near the marker.
FAKE_SECRETS = (
    "not-a-real-token", "not-a-real-token.for-these-tests", "not-a-real-gemini-key",
    "sk-not-a-real-key-123", "MTIzNDU2Nzg5MDEyMzQ1Njc4.not-a-real-token",
    "test-key-not-real",
)
NEAR_MISSES = ("a-real-value", "notarealvalue", "not-a-realm", "really-not-realistic")


# The quick start, in the order a stranger types it.
QUICK_START = (
    "git clone https://github.com/FakeZhiyuanLi/DIAYN.git",
    "cd DIAYN",
    "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
    "cp example.env .env",
    ".venv/bin/python diayn.py setup",
    ".venv/bin/python diayn.py run",
)
# The settings every host has to know about, whatever else the table holds.
ESSENTIAL_SETTINGS = ("DISCORD_TOKEN", "POLL_CONTACT", "DIAYN_OWNER_IDS", "DIAYN_TZ",
                      "DIAYN_DATA", "GEMINI_API_KEY")
# The project DIAYN grew out of. The README names it once, in one unlinked line.
PROVENANCE_NAME = "BaronChairStair"
# How the bot's command modules declare their slash commands, and what each is
# called in Discord: `@internships.command(name="matches"` is /internships matches.
SLASH_COMMANDS = {
    os.path.join("bot", "intern_commands.py"): {"internships": "/internships"},
    os.path.join("bot", "diayn_commands.py"): {"diayn": "/diayn", "grant": "/diayn grant",
                                               "revoke": "/diayn revoke"},
}
DECLARED = re.compile(r'^@(\w+)\.command\(name="([a-z-]+)"', re.M)
# One of DIAYN's scripts run with bare `python`: stock macOS and Ubuntu have no such
# command, and where there is one it is not the venv the quick start installs into.
BARE_PYTHON = re.compile(r"(?<![\w./-])python (diayn|internship_poller|resolve_boards)\.py\b")


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

    def test_a_copy_leaves_exactly_the_two_blanks_every_host_fills_in(self):
        # `cp example.env .env`, then fill in these two, as the quick start says.
        # Everything else stays commented out, following the code's default.
        text = read("example.env")
        assignments = [ln for ln in text.splitlines() if ASSIGNMENT.match(ln)]
        uncommented = [ln for ln in assignments if not ln.startswith("#")]
        self.assertEqual(uncommented, ["DISCORD_TOKEN=", "POLL_CONTACT="])
        self.assertEqual(assignments[:2], uncommented)

    def test_is_written_for_one_bot_on_its_own_host(self):
        # Not for a scraper beside another bot, with an .env of its own.
        text = read("example.env").lower()
        for gone in ("the bot's .env", "bot's checkout", "server's .env"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, text)


class Requirements(unittest.TestCase):
    def test_pins_exactly_the_runtime_dependencies(self):
        # The scraper's two, and the Discord bot's two: discord.py, and pypdf
        # to read a PDF resume.
        lines = [ln.strip() for ln in read("requirements.txt").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        names = sorted(ln.split("==")[0].lower() for ln in lines)
        self.assertEqual(names, ["aiohttp", "discord.py", "pypdf", "python-dotenv"])
        for line in lines:
            with self.subTest(line=line):
                self.assertRegex(line, r"^[a-z][a-z.-]*==\d+(\.\d+)+$")


def section(text, heading) -> str:
    """The body of the `## heading` section of `text`, up to the next `## `."""
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


class Readme(unittest.TestCase):
    """The README, written for a stranger who has only the repository."""

    @classmethod
    def setUpClass(cls):
        cls.readme = read("README.md")

    def test_the_layout_names_every_module_at_the_root(self):
        # A module the others import, left off the map, is one a stranger cannot place.
        block = self.readme.split("\n## Layout\n", 1)[1].split("```", 2)[1]
        listed = {line.split()[0] for line in block.splitlines() if line.strip()}
        for name in sorted(n for n in os.listdir(ROOT) if n.endswith(".py")):
            with self.subTest(module=name):
                self.assertIn(name, listed)

    def test_every_command_has_a_section(self):
        # The scraper's commands, and DIAYN's own.
        commands = next(a for a in poller.arguments()._actions if a.dest == "cmd").choices
        for cmd in tuple(commands) + diayn.BOT_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIn(f"### `{cmd}`", self.readme)

    def test_the_quick_start_is_the_path_in_order(self):
        quick_start, at = section(self.readme, "Quick start"), -1
        for step in QUICK_START:
            with self.subTest(step=step):
                found = quick_start.find(step, at + 1)
                self.assertGreater(found, at)
                at = found

    def test_the_configuration_table_names_only_variables_the_code_reads(self):
        read_by_code = {var for _, var, _ in poller.SETTINGS_FROM_ENV} | {ENV_FILE_VARIABLE}
        named = self.configuration_table()
        self.assertEqual(named - read_by_code, set())
        self.assertEqual(set(ESSENTIAL_SETTINGS) - named, set())

    def configuration_table(self) -> set:
        """Every variable named in the first column of the Configuration table."""
        named = set()
        for line in section(self.readme, "Configuration").splitlines():
            if line.startswith("| `"):
                named.update(re.findall(r"`([A-Z][A-Z0-9_]*)`", line.split("|")[1]))
        return named

    def test_every_slash_command_is_listed(self):
        for module, groups in SLASH_COMMANDS.items():
            for group, name in DECLARED.findall(read(module)):
                with self.subTest(command=f"{groups[group]} {name}"):
                    self.assertIn(f"`{groups[group]} {name}", self.readme)

    def test_says_what_the_discord_portal_needs(self):
        for needed in ("Server Members Intent", "Public Bot", "applications.commands",
                       "Reset Token"):
            with self.subTest(needed=needed):
                self.assertIn(needed, self.readme)

    def test_says_it_runs_on_linux_and_macos_only(self):
        self.assertIn("Linux and macOS only", self.readme)

    def test_names_the_project_it_grew_out_of_once_without_a_link(self):
        lines = [ln for ln in self.readme.splitlines() if PROVENANCE_NAME in ln]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].count(PROVENANCE_NAME), 1)
        self.assertNotIn("http", lines[0])
        self.assertNotIn("](", lines[0])


def documents() -> list:
    """Every document a reader of the repository meets: the Markdown files at the root and
    in bot/, and example.env."""
    names = sorted(n for n in os.listdir(ROOT) if n.endswith(".md"))
    names += sorted(os.path.join("bot", n) for n in os.listdir(os.path.join(ROOT, "bot"))
                    if n.endswith(".md"))
    return names + ["example.env"]


def modules() -> list:
    """Every module of the code, outside tests/: the scripts at the root and bot/'s. Their
    docstrings are read by whoever opens them, and resolve_boards prints its own as its
    usage text."""
    names = sorted(n for n in os.listdir(ROOT) if n.endswith(".py"))
    return names + sorted(os.path.join("bot", n) for n in os.listdir(os.path.join(ROOT, "bot"))
                          if n.endswith(".py"))


class Documents(unittest.TestCase):
    def test_every_command_names_the_venvs_python(self):
        for name in documents():
            for number, line in enumerate(read(name).splitlines(), 1):
                with self.subTest(document=name, line=number):
                    self.assertIsNone(BARE_PYTHON.search(line), line)

    def test_every_module_docstring_names_the_venvs_python_too(self):
        for name in modules():
            docstring = ast.get_docstring(ast.parse(read(name))) or ""
            for line in docstring.splitlines():
                with self.subTest(module=name, line=line.strip()):
                    self.assertIsNone(BARE_PYTHON.search(line), line)

    def test_the_modules_scanned_include_the_scripts_people_run(self):
        # A scan over no files would pass on every run and protect nothing.
        self.assertTrue({"diayn.py", "internship_poller.py", "resolve_boards.py",
                         "host_checks.py"} <= set(modules()))

    def test_only_the_readme_names_the_project_it_grew_out_of(self):
        # DIAYN stands on its own: the README's provenance line is the one mention.
        for name in documents():
            if name == "README.md":
                continue
            with self.subTest(document=name):
                self.assertNotIn(PROVENANCE_NAME.lower(), read(name).lower())


class ClaudeMd(unittest.TestCase):
    """CLAUDE.md: the rules for working here, for this repository's layout."""

    @classmethod
    def setUpClass(cls):
        cls.claude = read("CLAUDE.md")

    def test_keeps_its_sections_of_rules(self):
        for heading in ("When a check goes red, stop and report", "Never",
                        "Couplings that are easy to miss", "Tests",
                        "Decisions that are not yours to make"):
            with self.subTest(heading=heading):
                self.assertIn(f"\n## {heading}\n", self.claude)

    def test_covers_the_bot_and_its_data(self):
        for named in ("bot/", "diayn.py", "users.db", "access", "fit check", "role_key"):
            with self.subTest(named=named):
                self.assertIn(named, self.claude)


def shell_commands(text) -> list:
    """Every line of `text`, with a line ending in a backslash joined to the next, as the
    shell joins them."""
    return re.sub(r"\s*\\\n\s*", " ", text).splitlines()


class Deploy(unittest.TestCase):
    """DEPLOY.md: running DIAYN for good, on any Linux or macOS host."""

    @classmethod
    def setUpClass(cls):
        cls.deploy = read("DEPLOY.md")
        cls.commands = [c.strip() for c in shell_commands(cls.deploy)]

    def test_pm2_runs_the_one_process(self):
        self.assertRegex(self.deploy, r'script: "[^"]*/diayn\.py"')
        self.assertIn('args: "run"', self.deploy)
        # 3 is another sweeper holding the lock, and 78 Discord refusing the Server
        # Members Intent: restarting into either only loops, and a loop of refused
        # logins can get the bot's token reset.
        # The config line itself, not the prose that quotes it.
        self.assertRegex(self.deploy, r"(?m)^\s+stop_exit_codes: \[%d, %d\],$"
                         % (poller.LOCK_HELD_EXIT, diayn.CONFIG_EXIT))

    def test_systemd_runs_the_one_process(self):
        self.assertRegex(self.deploy, r"(?m)^ExecStart=/\S+/python /\S+/diayn\.py run$")
        # run stops cleanly on SIGINT, as on Ctrl-C; SIGTERM would kill it outright.
        self.assertIn("KillSignal=SIGINT", self.deploy)
        self.assertIn(f"RestartPreventExitStatus={poller.LOCK_HELD_EXIT} {diayn.CONFIG_EXIT}\n",
                      self.deploy)

    def test_says_why_78_is_never_restarted(self):
        for said in (f"Exit {diayn.CONFIG_EXIT}", "Server Members Intent", "token"):
            with self.subTest(said=said):
                self.assertIn(said, self.deploy)

    def test_the_process_check_works_on_linux_and_macos(self):
        # pgrep -a lists command lines on Linux, and on macOS means "ancestors too".
        # A count of the PIDs reads the same on both.
        for command in self.commands:
            with self.subTest(command=command):
                self.assertNotRegex(command, r"\bpgrep\s+-\w*a")
        self.assertIn("pgrep -f 'diayn[.]py run' | wc -l", self.deploy)

    def test_backs_up_both_databases_with_backup(self):
        for db in ("postings.db", "users.db"):
            with self.subTest(db=db):
                self.assertTrue(any(f'"file:$D/{db}?mode=ro" ".backup ' in c
                                    for c in self.commands))

    def test_restores_both_databases_with_restore_under_the_lock(self):
        restores = [c for c in self.commands if '".restore ' in c or "'.restore " in c]
        for db in ("postings.db", "users.db"):
            with self.subTest(db=db):
                self.assertTrue(any(f'"$D/{db}"' in c for c in restores))
        for command in restores:
            with self.subTest(command=command):
                self.assertTrue(command.startswith('flock -n "$D/postings.db.lock" sqlite3 '))

    def test_never_copies_a_database_as_a_file(self):
        for command in self.commands:
            with self.subTest(command=command):
                self.assertNotRegex(command, r"^(cp|mv|rsync|scp)\b.*\.db\b")

    def test_upgrades_by_tag(self):
        self.assertIn("git fetch --tags", self.deploy)
        self.assertIn("git checkout --detach vX.Y.Z", self.deploy)

    def test_the_move_from_an_in_process_sweep_is_gone(self):
        for gone in ("INTERN_SWEEP", "in-process", "Stage 1", "Stage 2", "Stage 3",
                     PROVENANCE_NAME):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.deploy)


class Contract(unittest.TestCase):
    """CONTRACT.md: the rules between the scraper and the bot, inside this repository.
    Runtime messages and docstrings cite its promises by number (P5, B7), so every
    number stays."""

    PROMISES = tuple(f"P{n}" for n in range(1, 9)) + tuple(f"B{n}" for n in range(1, 8))

    @classmethod
    def setUpClass(cls):
        cls.contract = read("CONTRACT.md")

    def test_keeps_every_promise_as_a_row(self):
        for promise in self.PROMISES:
            with self.subTest(promise=promise):
                self.assertRegex(self.contract, rf"(?m)^\| {promise} \| \S")

    def test_names_the_version_both_halves_check(self):
        declared = re.compile(r'^CONTRACT_VERSION = "(\d+)"', re.M)
        bot_version = declared.search(read(os.path.join("bot", "postings_contract.py"))).group(1)
        title = re.search(r"^# .* contract, v(\d+)$", self.contract, re.M)
        self.assertIsNotNone(title)
        self.assertEqual({title.group(1), bot_version}, {poller.CONTRACT_VERSION})

    def test_is_about_one_repository(self):
        # The two halves were once in two repositories, one vendoring the other's
        # fixtures at a tag, with a staged move between them. None of that is left.
        text = self.contract.lower()
        for gone in ("vendor", "client/fixtures", "another repository", "both repositories",
                     "stage 3", "`external` mode", PROVENANCE_NAME.lower()):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, text)


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



def ci_job(name) -> str:
    """The text of one job of CI's workflow, up to the next job or the end."""
    ci = read(os.path.join(".github", "workflows", "ci.yml"))
    start = ci.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z][\w-]*:\n", ci[start + 1:])
    return ci[start:start + 1 + following.start()] if following else ci[start:]


class Gitleaks(unittest.TestCase):
    """CI's secret scan: every commit of the history, by a pinned binary whose checksum is
    checked before it runs, with the default rules and one narrow allowlist."""

    @classmethod
    def setUpClass(cls):
        cls.guards = ci_job("guards")
        cls.config = read(".gitleaks.toml")

    def test_pins_a_version_and_its_checksum(self):
        self.assertRegex(self.guards, r"GITLEAKS_VERSION: \d+\.\d+\.\d+\n")
        self.assertRegex(self.guards, r"GITLEAKS_SHA256: [0-9a-f]{64}\n")
        self.assertNotIn("latest", self.guards)

    def test_checks_the_download_before_unpacking_or_running_it(self):
        checked = self.guards.index("sha256sum --check --strict")
        self.assertLess(self.guards.index("curl "), checked)
        self.assertLess(checked, self.guards.index("tar -xzf"))
        self.assertLess(checked, self.guards.index('/gitleaks" git '))

    def test_scans_the_whole_history_redacted_with_this_config(self):
        self.assertIn('/gitleaks" git --config .gitleaks.toml --redact ', self.guards)
        self.assertIn("fetch-depth: 0", self.guards)

    def test_the_config_keeps_every_default_rule(self):
        self.assertRegex(self.config, r"(?m)^\[extend\]\nuseDefault = true$")
        # No rule of its own, and no allowlist by path, commit or rule: those would
        # hide real findings. The allowlist's regexes see the secret alone.
        self.assertNotRegex(self.config, r"(?m)^\[\[rules")
        self.assertNotRegex(self.config,
                            r"(?m)^(paths|commits|stopwords|targetRules|regexTarget) *=")

    def test_the_allowlist_passes_the_fake_secrets_and_nothing_near_them(self):
        allowed = [re.compile(r) for r in re.findall(r"'''(.+?)'''", self.config)]
        self.assertTrue(allowed)
        for fake in FAKE_SECRETS:
            with self.subTest(fake=fake):
                self.assertTrue(any(r.search(fake) for r in allowed))
        for near in NEAR_MISSES:
            with self.subTest(near=near):
                self.assertFalse(any(r.search(near) for r in allowed))


if __name__ == "__main__":
    unittest.main()
