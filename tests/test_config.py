"""
Configuration: what the scraper reads, from where, and when.

    python3 -m unittest discover -s tests      # no install needed

The rule these pin is that settings are read once, in `main()`, after the
scraper's own .env has loaded — never while the module is imported, and never
from the working directory. The scraper used to read its environment at
import, after a `load_dotenv(override=True)`, so which values won depended on
who imported it first and from where; a process started in the bot's checkout
could pick up the bot's .env.

The in-process tests call the pure pieces (`configure`, `env_file_path`,
`config_lines`). The rest run the scraper in a child process, the way pm2
does, against a copy of the module in a temporary checkout — so the real
checkout's .env, if a box has one, is never at the path being tested and is
never read. python-dotenv is only on boxes that installed requirements.txt;
the tests that need it to load a file are skipped without it.
"""

import asyncio
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()

import internship_poller as poller  # noqa: E402

HAS_DOTENV = importlib.util.find_spec("dotenv") is not None
NEEDS_DOTENV = unittest.skipUnless(HAS_DOTENV, "python-dotenv is not installed")

# Every variable the scraper reads, named here rather than taken from the
# module, so a child process starts clean even if the module's list is wrong.
SCRAPER_VARIABLES = ("POLLER_ENV_FILE", "POSTINGS_DB", "BOARDS_FILE", "YC_CACHE",
                     "DISCORD_TOKEN")
SCRAPER_PREFIXES = ("POLL_", "GEMINI_", "LLM_", "DIAYN_", "FIT_")

# The child process: stub aiohttp if it is missing, then run the copied module
# as a script. argv is [tests dir, script, *args]. `block_dotenv` makes
# `import dotenv` fail, as it does on a box without python-dotenv.
RUN_SCRIPT = """
import runpy, sys
tests, script = sys.argv[1], sys.argv[2]
sys.path.insert(0, tests)
from aiohttp_stub import stub_aiohttp
stub_aiohttp()
if {block_dotenv}:
    sys.modules["dotenv"] = None
sys.argv = [script] + sys.argv[3:]
runpy.run_path(script, run_name="__main__")
"""

# The child process for the import check: everything the module imports is
# loaded first, so the only code running while `open` is watched is the
# module's own top level. zoneinfo among them: importing it reads the
# interpreter's build settings, which on macOS opens a system plist.
# Prints what it saw as JSON.
IMPORT_SCRIPT = """
import builtins, json, os, runpy, sys
tests, script = sys.argv[1], sys.argv[2]
sys.path.insert(0, tests)
from aiohttp_stub import stub_aiohttp
stub_aiohttp()
import aiohttp, zoneinfo
before = dict(os.environ)
opened = []
real_open = builtins.open
def watched_open(file, *args, **kwargs):
    opened.append(str(file))
    return real_open(file, *args, **kwargs)
builtins.open = watched_open
try:
    module = runpy.run_path(script, run_name="internship_poller_import_check")
finally:
    builtins.open = real_open
print(json.dumps({"opened": opened,
                  "environ_unchanged": dict(os.environ) == before,
                  "rpd": module["SETTINGS"].llm_rpd,
                  "contact": module["SETTINGS"].contact,
                  "boards": len(module["BOARDS"])}))
"""


def shown(output, label):
    """The value `config` printed for `label`, or None if it printed no such line."""
    for line in output.splitlines():
        if line.startswith(label + " "):
            return line[len(label):].strip()
    return None


class Configure(unittest.TestCase):
    def test_an_empty_environment_gives_the_code_defaults(self):
        settings = poller.configure({})
        self.assertEqual(settings, poller.Settings())
        self.assertEqual(settings.llm_rpd, 250)
        self.assertEqual(settings.llm_rpm, 5)
        self.assertEqual(settings.host_concurrency, 4)
        self.assertEqual(settings.host_min_interval, 0.12)
        self.assertIsNone(settings.gemini_key)
        self.assertEqual(settings.contact, "")

    def test_the_data_paths_default_to_the_checkout_s_data_directory(self):
        # Beside the code, never the working directory: the scraper and the
        # bot must agree on one file whatever directory each was started in.
        data = os.path.join(poller.CHECKOUT, "data")
        settings = poller.configure({})
        self.assertEqual(settings.postings_db, os.path.join(data, "postings.db"))
        self.assertEqual(settings.boards_file, os.path.join(data, "boards.json"))
        self.assertEqual(settings.yc_cache, os.path.join(data, "yc_cache.json"))

    def test_the_data_paths_come_from_the_environment(self):
        settings = poller.configure({"POSTINGS_DB": "/srv/data/postings.db",
                                     "BOARDS_FILE": "/srv/data/boards.json",
                                     "YC_CACHE": "/srv/data/yc_cache.json"})
        self.assertEqual(settings.postings_db, "/srv/data/postings.db")
        self.assertEqual(settings.boards_file, "/srv/data/boards.json")
        self.assertEqual(settings.yc_cache, "/srv/data/yc_cache.json")

    def test_an_empty_value_keeps_the_default(self):
        # `POSTINGS_DB=` in a .env is how a line gets switched off; it must not
        # become a database called "" in the working directory.
        settings = poller.configure({"POSTINGS_DB": "", "GEMINI_RPD": "",
                                     "GEMINI_API_KEY": ""})
        self.assertEqual(settings, poller.Settings())

    def test_numbers_are_read_as_numbers(self):
        settings = poller.configure({"GEMINI_RPD": "500", "GEMINI_RPM": " 15 ",
                                     "POLL_HOST_MIN_INTERVAL": "0.5",
                                     "POLL_CONTACT": "ops@example.org"})
        self.assertEqual(settings.llm_rpd, 500)
        self.assertEqual(settings.llm_rpm, 15)
        self.assertEqual(settings.host_min_interval, 0.5)
        self.assertEqual(settings.contact, "ops@example.org")

    def test_a_number_that_will_not_parse_names_its_variable(self):
        with self.assertRaises(poller.ConfigError) as caught:
            poller.configure({"GEMINI_RPD": "lots"})
        self.assertIn("GEMINI_RPD", str(caught.exception))

    def test_a_count_below_one_is_refused(self):
        # POLL_HOST_CONCURRENCY=0 is a semaphore nobody can acquire: every
        # request waits forever and the sweep never finishes, with no error.
        for var in ("POLL_HOST_CONCURRENCY", "GEMINI_BATCH", "GEMINI_RPD"):
            with self.subTest(var=var), self.assertRaises(poller.ConfigError) as caught:
                poller.configure({var: "0"})
            self.assertIn(var, str(caught.exception))

    def test_a_negative_interval_is_refused(self):
        with self.assertRaises(poller.ConfigError):
            poller.configure({"POLL_HOST_MIN_INTERVAL": "-1"})

    def test_the_quota_day_s_zone_must_be_a_real_zone(self):
        # Refused at start-up, not at the first --llm sweep or `stats`, where
        # an unknown zone would fail every time it was asked for today's date.
        self.assertEqual(poller.configure({"LLM_DAY_TZ": "UTC"}).llm_day_tz, "UTC")
        for raw in ("Mars/Olympus_Mons", "../../etc/passwd", "Pacific Time"):
            with self.subTest(raw=raw), self.assertRaises(poller.ConfigError) as caught:
                poller.configure({"LLM_DAY_TZ": raw})
            self.assertIn("LLM_DAY_TZ", str(caught.exception))

    def test_configure_reads_only_the_mapping_it_is_given(self):
        with mock.patch.dict(os.environ, {"GEMINI_RPD": "999"}):
            self.assertEqual(poller.configure({}).llm_rpd, 250)

    def test_settings_cannot_be_changed_once_bound(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            poller.configure({}).llm_rpd = 1

    def test_the_key_is_not_in_the_settings_repr(self):
        # A traceback or a debug print of the settings must not leak it.
        settings = poller.configure({"GEMINI_API_KEY": "sk-not-a-real-key-123"})
        self.assertEqual(settings.gemini_key, "sk-not-a-real-key-123")
        self.assertNotIn("sk-not-a-real-key-123", repr(settings))


class BotSettings(unittest.TestCase):
    """The Discord bot's settings, read from the same .env by the same configure()."""

    # Not a real token or a real id: shaped like them, so a leak would be seen.
    TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.not-a-real-token"
    OWNER = "112233445566778899"

    def test_the_bot_s_defaults(self):
        settings = poller.configure({})
        self.assertIsNone(settings.discord_token)
        self.assertEqual(settings.owner_ids, ())
        self.assertEqual(settings.tz, "UTC")
        self.assertEqual(settings.fit_batch, 15)
        self.assertEqual(settings.fit_rpd, 200)
        self.assertEqual(settings.fit_rpm, 10)

    def test_the_data_directory_defaults_to_the_checkout_s_never_the_working_directory(self):
        self.assertEqual(poller.configure({}).data_dir,
                         os.path.join(poller.CHECKOUT, "data"))
        self.assertEqual(poller.configure({}).data_dir, poller.DATA_DIR)

    def test_diayn_data_moves_every_data_file_that_is_not_named_on_its_own(self):
        settings = poller.configure({"DIAYN_DATA": "/srv/diayn"})
        self.assertEqual(settings.data_dir, "/srv/diayn")
        self.assertEqual(settings.postings_db, "/srv/diayn/postings.db")
        self.assertEqual(settings.boards_file, "/srv/diayn/boards.json")
        self.assertEqual(settings.yc_cache, "/srv/diayn/yc_cache.json")

    def test_a_data_file_named_on_its_own_still_wins(self):
        settings = poller.configure({"DIAYN_DATA": "/srv/diayn",
                                     "POSTINGS_DB": "/var/lib/postings.db"})
        self.assertEqual(settings.postings_db, "/var/lib/postings.db")
        self.assertEqual(settings.boards_file, "/srv/diayn/boards.json")
        self.assertEqual(settings.yc_cache, "/srv/diayn/yc_cache.json")

    def test_a_relative_data_directory_is_refused(self):
        # It would resolve against the working directory, which under pm2 is
        # wherever the process was started. `~` is not expanded by a .env.
        for raw in ("data", "./data", "~/diayn-data"):
            with self.subTest(raw=raw), self.assertRaises(poller.ConfigError) as caught:
                poller.configure({"DIAYN_DATA": raw})
            self.assertIn("DIAYN_DATA", str(caught.exception))

    def test_owner_ids_are_read_as_integers(self):
        self.assertEqual(poller.configure({"DIAYN_OWNER_IDS": self.OWNER}).owner_ids,
                         (112233445566778899,))
        self.assertEqual(
            poller.configure({"DIAYN_OWNER_IDS": " 11223344, 55667788 ,"}).owner_ids,
            (11223344, 55667788))

    def test_a_repeated_owner_id_counts_once(self):
        self.assertEqual(
            poller.configure({"DIAYN_OWNER_IDS": "11223344,55667788,11223344"}).owner_ids,
            (11223344, 55667788))

    def test_an_owner_id_that_is_not_a_discord_id_stops_the_start(self):
        bad = ("owner", "11223344x", "-11223344", "0", "1.5", "11223344;55667788",
               "11223344 55667788", str(2 ** 64), "\u00b2", "9" * 5000)
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(poller.ConfigError) as caught:
                poller.configure({"DIAYN_OWNER_IDS": raw})
            self.assertIn("DIAYN_OWNER_IDS", str(caught.exception))

    def test_a_refused_owner_id_is_not_repeated_in_the_message(self):
        # The message goes to a log; the entry is most likely an id with a typo.
        with self.assertRaises(poller.ConfigError) as caught:
            poller.configure({"DIAYN_OWNER_IDS": f"55667788,{self.OWNER}x"})
        self.assertNotIn(self.OWNER, str(caught.exception))
        self.assertIn("2", str(caught.exception))

    def test_the_bot_s_zone_must_be_a_real_zone(self):
        self.assertEqual(poller.configure({"DIAYN_TZ": "America/Los_Angeles"}).tz,
                         "America/Los_Angeles")
        for raw in ("Mars/Olympus_Mons", "Pacific Time"):
            with self.subTest(raw=raw), self.assertRaises(poller.ConfigError) as caught:
                poller.configure({"DIAYN_TZ": raw})
            self.assertIn("DIAYN_TZ", str(caught.exception))

    def test_the_fit_check_limits_are_counts(self):
        settings = poller.configure({"FIT_BATCH": "20", "FIT_RPD": "500",
                                     "FIT_RPM": "15"})
        self.assertEqual((settings.fit_batch, settings.fit_rpd, settings.fit_rpm),
                         (20, 500, 15))
        for var in ("FIT_BATCH", "FIT_RPD", "FIT_RPM"):
            for raw in ("0", "lots"):
                with self.subTest(var=var, raw=raw), \
                        self.assertRaises(poller.ConfigError) as caught:
                    poller.configure({var: raw})
                self.assertIn(var, str(caught.exception))

    def test_neither_the_token_nor_the_owners_are_in_the_settings_repr(self):
        settings = poller.configure({"DISCORD_TOKEN": self.TOKEN,
                                     "DIAYN_OWNER_IDS": self.OWNER})
        self.assertEqual(settings.discord_token, self.TOKEN)
        self.assertNotIn(self.TOKEN, repr(settings))
        self.assertNotIn(self.OWNER, repr(settings))


class EnvFilePath(unittest.TestCase):
    def test_poller_env_file_wins(self):
        self.assertEqual(
            poller.env_file_path({"POLLER_ENV_FILE": "/etc/diayn.env"}, "/srv/DIAYN"),
            "/etc/diayn.env")

    def test_otherwise_the_checkout_s_own_env(self):
        self.assertEqual(poller.env_file_path({}, "/srv/DIAYN"),
                         os.path.join("/srv/DIAYN", ".env"))
        self.assertEqual(poller.env_file_path({"POLLER_ENV_FILE": ""}, "/srv/DIAYN"),
                         os.path.join("/srv/DIAYN", ".env"))

    def test_the_default_is_beside_the_code_not_the_working_directory(self):
        self.assertEqual(poller.env_file_path({}),
                         os.path.join(poller.CHECKOUT, ".env"))


class ConfigLines(unittest.TestCase):
    def test_every_setting_is_shown_with_its_variable(self):
        settings = poller.configure({"GEMINI_RPD": "500",
                                     "POLL_CONTACT": "ops@example.org"})
        out = "\n".join(poller.config_lines(settings, "/srv/DIAYN/.env"))
        self.assertEqual(shown(out, "env file"), "/srv/DIAYN/.env")
        self.assertEqual(shown(out, "POSTINGS_DB"), settings.postings_db)
        self.assertEqual(shown(out, "BOARDS_FILE"), settings.boards_file)
        self.assertEqual(shown(out, "YC_CACHE"), settings.yc_cache)
        self.assertEqual(shown(out, "POLL_CONTACT"), "ops@example.org")
        self.assertEqual(shown(out, "POLL_HOST_CONCURRENCY"), "4")
        self.assertEqual(shown(out, "GEMINI_RPD"), "500")
        self.assertEqual(shown(out, "GEMINI_MODEL"), settings.gemini_model)

    def test_the_key_is_shown_as_set_never_as_its_value(self):
        settings = poller.configure({"GEMINI_API_KEY": "sk-not-a-real-key-123"})
        out = "\n".join(poller.config_lines(settings, None))
        self.assertNotIn("sk-not-a-real-key-123", out)
        self.assertEqual(shown(out, "GEMINI_API_KEY"), "set")

    def test_a_missing_key_and_contact_say_so(self):
        out = "\n".join(poller.config_lines(poller.configure({}), None))
        self.assertEqual(shown(out, "GEMINI_API_KEY"), "not set")
        self.assertEqual(shown(out, "POLL_CONTACT"), "(not set)")
        self.assertTrue(shown(out, "env file").startswith("none"))


class BotConfigLines(unittest.TestCase):
    TOKEN = BotSettings.TOKEN
    OWNER = BotSettings.OWNER

    def test_the_bot_s_settings_are_shown(self):
        settings = poller.configure({"DIAYN_TZ": "America/Los_Angeles",
                                     "DIAYN_DATA": "/srv/diayn", "FIT_RPD": "300"})
        out = "\n".join(poller.config_lines(settings, None))
        self.assertEqual(shown(out, "DIAYN_DATA"), "/srv/diayn")
        self.assertEqual(shown(out, "POSTINGS_DB"), "/srv/diayn/postings.db")
        self.assertEqual(shown(out, "DIAYN_TZ"), "America/Los_Angeles")
        self.assertEqual(shown(out, "FIT_BATCH"), "15")
        self.assertEqual(shown(out, "FIT_RPD"), "300")
        self.assertEqual(shown(out, "FIT_RPM"), "10")

    def test_the_token_is_shown_as_set_never_as_its_value(self):
        out = "\n".join(poller.config_lines(
            poller.configure({"DISCORD_TOKEN": self.TOKEN}), None))
        self.assertNotIn(self.TOKEN, out)
        self.assertEqual(shown(out, "DISCORD_TOKEN"), "set")
        out = "\n".join(poller.config_lines(poller.configure({}), None))
        self.assertEqual(shown(out, "DISCORD_TOKEN"), "not set")

    def test_the_owners_are_counted_never_listed(self):
        # `config` output is meant to be pasted into an issue as it stands.
        settings = poller.configure({"DIAYN_OWNER_IDS": f"{self.OWNER},55667788"})
        out = "\n".join(poller.config_lines(settings, None))
        self.assertNotIn(self.OWNER, out)
        self.assertNotIn("55667788", out)
        self.assertEqual(shown(out, "DIAYN_OWNER_IDS"), "set (2 ids)")
        out = "\n".join(poller.config_lines(poller.configure({}), None))
        self.assertEqual(shown(out, "DIAYN_OWNER_IDS"), "(not set)")


class UserAgent(unittest.TestCase):
    """Every request says who is asking, and how to reach them.

    The owners of these boards should be able to tell the traffic apart and
    write to someone about it. The agent names the project, and POLL_CONTACT
    when it is set; resolve_boards.py sends the same one rather than posing as
    a browser.
    """

    PROJECT = "https://github.com/FakeZhiyuanLi/DIAYN"

    def test_it_names_the_project_and_the_contact(self):
        self.assertEqual(poller.user_agent("ops@example.org"),
                         f"DIAYN/1.0 (+{self.PROJECT}; contact: ops@example.org)")

    def test_an_unset_contact_is_left_out(self):
        self.assertEqual(poller.user_agent(""), f"DIAYN/1.0 (+{self.PROJECT})")

    def test_every_sweep_sends_it_with_the_configured_contact(self):
        sessions = []

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        def polite_session(**kwargs):
            sessions.append(kwargs)
            return Session()

        settings = poller.configure({"POLL_CONTACT": "ops@example.org"})
        with mock.patch.object(poller, "SETTINGS", settings), \
                mock.patch.object(poller, "BOARDS", ()), \
                mock.patch.object(poller, "polite_session", polite_session):
            asyncio.run(poller.fetch_all())
        self.assertEqual(sessions[0]["headers"]["User-Agent"],
                         poller.user_agent("ops@example.org"))

    def test_resolve_boards_sends_the_same_agent(self):
        import resolve_boards
        # The contact comes from the scraper's own settings. load_env_file is
        # replaced, so no .env on this box is read.
        with mock.patch.object(poller, "load_env_file", return_value=None), \
                mock.patch.object(poller, "SETTINGS", poller.SETTINGS), \
                mock.patch.dict(os.environ, {"POLL_CONTACT": "ops@example.org"}):
            headers = resolve_boards.agent_headers(resolve_boards.boot_settings())
        self.assertEqual(headers["User-Agent"], poller.user_agent("ops@example.org"))
        with open(resolve_boards.__file__, encoding="utf-8") as f:
            self.assertNotIn("Mozilla", f.read())

    def test_resolve_boards_paces_its_requests_through_the_host_gate(self):
        # Many careers URLs on one host (Workday tenants, one company's pages)
        # would otherwise go out six at a time with no gap between them. The
        # gate reads its spacing from poller.SETTINGS, so those must be the
        # configured settings by the time a request goes out.
        import resolve_boards
        sessions, contacts = [], []

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        def polite_session(**kwargs):
            sessions.append(kwargs)
            return Session()

        async def resolve(sess, url, name=None):
            contacts.append(poller.SETTINGS.contact)
            return {"input": url, "ok": False, "why": "not fetched", "name": name}

        argv = ["resolve_boards.py", "https://careers.example.com"]
        with mock.patch.object(poller, "load_env_file", return_value=None), \
                mock.patch.object(poller, "SETTINGS", poller.SETTINGS), \
                mock.patch.object(poller, "polite_session", polite_session), \
                mock.patch.object(resolve_boards, "resolve", resolve), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"POLL_CONTACT": "ops@example.org"}), \
                contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(resolve_boards.main())
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["headers"]["User-Agent"],
                         poller.user_agent("ops@example.org"))
        self.assertEqual(contacts, ["ops@example.org"])


class Cli(unittest.TestCase):
    """The scraper run as a script, from a temporary checkout.

    The layout is `checkout/internship_poller.py` (a copy) and `work/`, the
    directory the child process starts in. Nothing else is there unless a test
    puts it there.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.checkout = os.path.join(self._tmp.name, "checkout")
        self.work = os.path.join(self._tmp.name, "work")
        os.mkdir(self.checkout)
        os.mkdir(self.work)
        self.script = shutil.copy(poller.__file__, self.checkout)

    def _write(self, path, text):
        with open(path, "w") as f:
            f.write(text)
        return path

    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in SCRAPER_VARIABLES and not k.startswith(SCRAPER_PREFIXES)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra)
        return env

    def _run(self, code, *args, **env):
        return subprocess.run(
            [sys.executable, "-B", "-c", code, TESTS, self.script, *args],
            cwd=self.work, env=self._env(**env), capture_output=True, text=True,
            timeout=60)

    def _config(self, block_dotenv=False, **env):
        return self._run(RUN_SCRIPT.format(block_dotenv=block_dotenv), "config", **env)

    def test_importing_the_module_is_inert(self):
        # A .env beside the code and one in the working directory, both of
        # which the old import-time load_dotenv would have applied.
        self._write(os.path.join(self.checkout, ".env"),
                    "GEMINI_RPD=999\nPOLL_CONTACT=checkout@example.org\n")
        self._write(os.path.join(self.work, ".env"),
                    "GEMINI_RPD=555\nPOLL_CONTACT=cwd@example.org\n")
        result = self._run(IMPORT_SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout)
        self.assertEqual(seen["opened"], [])
        self.assertTrue(seen["environ_unchanged"])
        self.assertEqual(seen["rpd"], 250)
        self.assertEqual(seen["contact"], "")
        self.assertEqual(seen["boards"], 0)
        self.assertEqual(sorted(os.listdir(self.checkout)),
                         [".env", "internship_poller.py"])
        self.assertEqual(os.listdir(self.work), [".env"])

    @NEEDS_DOTENV
    def test_config_reads_the_file_named_by_poller_env_file(self):
        env_file = self._write(
            os.path.join(self._tmp.name, "scraper.env"),
            "GEMINI_RPD=777\nPOLL_CONTACT=ops@example.org\n"
            "GEMINI_API_KEY=sk-not-a-real-key-123\n")
        result = self._config(POLLER_ENV_FILE=env_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shown(result.stdout, "env file"), env_file)
        self.assertEqual(shown(result.stdout, "GEMINI_RPD"), "777")
        self.assertEqual(shown(result.stdout, "POLL_CONTACT"), "ops@example.org")
        self.assertEqual(shown(result.stdout, "GEMINI_API_KEY"), "set")
        self.assertNotIn("sk-not-a-real-key-123", result.stdout + result.stderr)

    @NEEDS_DOTENV
    def test_config_reads_the_checkout_s_env_by_default(self):
        env_file = self._write(os.path.join(self.checkout, ".env"),
                               "GEMINI_RPD=321\n")
        result = self._config()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shown(result.stdout, "env file"), env_file)
        self.assertEqual(shown(result.stdout, "GEMINI_RPD"), "321")

    @NEEDS_DOTENV
    def test_the_process_environment_wins_over_the_file(self):
        # override=False: pm2's own env block, or an export in the shell, is
        # the operator's last word; the file only fills in what is unset.
        env_file = self._write(os.path.join(self._tmp.name, "scraper.env"),
                               "POLL_CONTACT=file@example.org\nGEMINI_RPD=777\n")
        result = self._config(POLLER_ENV_FILE=env_file,
                              POLL_CONTACT="process@example.org")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shown(result.stdout, "POLL_CONTACT"), "process@example.org")
        self.assertEqual(shown(result.stdout, "GEMINI_RPD"), "777")

    def test_the_working_directory_s_env_is_never_read(self):
        # The bot's checkout has a .env of its own. A scraper started there
        # must not take the bot's settings for its own.
        self._write(os.path.join(self.work, ".env"),
                    "GEMINI_RPD=555\nPOLL_CONTACT=cwd@example.org\n")
        result = self._config()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(shown(result.stdout, "env file").startswith("none"))
        self.assertEqual(shown(result.stdout, "GEMINI_RPD"), "250")
        self.assertEqual(shown(result.stdout, "POLL_CONTACT"), "(not set)")

    def test_a_bad_bot_setting_stops_the_start_naming_it(self):
        for var, raw in (("DIAYN_TZ", "Pacific Time"), ("DIAYN_OWNER_IDS", "owner"),
                         ("DIAYN_DATA", "data"), ("FIT_RPM", "0")):
            with self.subTest(var=var):
                result = self._config(**{var: raw})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(var, result.stderr)
                self.assertIsNone(shown(result.stdout, "FIT_RPD"))

    def test_config_shows_the_token_only_as_set(self):
        result = self._config(DISCORD_TOKEN=BotSettings.TOKEN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shown(result.stdout, "DISCORD_TOKEN"), "set")
        self.assertNotIn(BotSettings.TOKEN, result.stdout + result.stderr)

    def test_a_named_env_file_that_is_missing_stops_the_start(self):
        # Somebody set POLLER_ENV_FILE on purpose; carrying on with the code
        # defaults would sweep with settings nobody chose.
        missing = os.path.join(self._tmp.name, "missing.env")
        result = self._config(POLLER_ENV_FILE=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(missing, result.stderr)
        self.assertIsNone(shown(result.stdout, "GEMINI_RPD"))

    def test_an_env_file_without_python_dotenv_stops_the_start(self):
        # The file is there and cannot be read: the same settings-nobody-chose
        # problem, so it is an error rather than the old silent skip.
        env_file = self._write(os.path.join(self._tmp.name, "scraper.env"),
                               "GEMINI_RPD=777\n")
        result = self._config(block_dotenv=True, POLLER_ENV_FILE=env_file)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python-dotenv", result.stderr)
        self.assertIsNone(shown(result.stdout, "GEMINI_RPD"))


if __name__ == "__main__":
    unittest.main()
