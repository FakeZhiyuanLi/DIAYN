"""
host_checks.py
~~~~~~~~~~~~~~
`diayn.py setup`, which gets a new host ready, from a filled-in .env to an
invite link, and `diayn.py doctor`, which checks it again at any time.

    python diayn.py setup
    python diayn.py doctor

**setup** goes in this order, and stops at the first failure:

1. **The token.** DISCORD_TOKEN must be set, and Discord must accept it
   (discord_portal). Nothing is made until it does.
2. **The intent.** The Server Members Intent being off is a warning: the bot
   runs without it, but cannot notice someone leaving every server it shares
   with them, and a DM cannot tell who is a member of a granted server.
3. **The data directory**, DIAYN_DATA, made at mode 700. One that exists keeps
   the mode it has, with a warning if others on the box can read it.
4. **postings.db**, bootstrapped as `sweep --init` would: a first sweep that
   records every posting open now as seen, so none of them is announced. A file
   that is already there is never bootstrapped again: with a ledger it is left
   as it is, and with an empty one it is left alone and refused, since only
   the operator can say whether it is new (`sweep --init`) or the wrong file.
5. **The invite link**, with the scopes the bot needs and no permission.

**doctor** checks the Python version, the platform, the settings, the token
and the intent, the data directory, postings.db and its last sweep, whether
anything holds the sweeper lock, POLL_CONTACT, and the Gemini key. Every check
runs, whatever an earlier one found, except those that need settings that
would not load. It makes nothing and writes nothing. To see whether a sweeper
is running it takes the lock for an instant, and only when nothing holds it.

Each check prints one line, `ok`, `warn`, `note` or `fail`, and a failure goes
to stderr. Exit codes are the scraper's: 0 done (warnings included), 1 failed
or something to fix, 3 another sweeper holds the lock (setup only). Nothing
printed carries the token or the Gemini key.

Importing this module does nothing.
"""

import argparse
import asyncio
import dataclasses
import importlib.util
import os
import sqlite3
import stat
import sys
import time

import discord_portal as portal

OK, WARN, NOTE, FAIL = "ok", "warn", "note", "fail"
FAILED_EXIT = 1
#: The data directory's mode: users.db in it holds Discord ids and profiles.
PRIVATE_MODE = 0o700
#: The commands a finding may point at, spelled as the README spells them.
SETUP_COMMAND = "python diayn.py setup"
RUN_COMMAND = "python diayn.py run"
INIT_COMMAND = "python diayn.py sweep --init"
CONFIG_COMMAND = "python diayn.py config"
MIN_PYTHON = (3, 10)
#: What DIAYN needs that only a POSIX system has, and what for.
POSIX_MODULES = (("fcntl", "the sweeper lock"), ("resource", "the resume reader's limits"))
PLATFORM_NAMES = {"linux": "Linux", "darwin": "macOS"}
#: B6: a sweep this many intervals late is reported, as /diayn debug reports it.
STALE_SWEEPS = 3
INTENT_HOW = ("In the developer portal, open your application, then Bot, and under "
              "Privileged Gateway Intents turn on Server Members Intent.")


@dataclasses.dataclass(frozen=True)
class Finding:
    """One line of what setup (or doctor) found: its level, what it is about, and what it says."""
    level: str
    what: str
    said: str

    @property
    def failed(self) -> bool:
        return self.level == FAIL


def report(finding: Finding) -> None:
    """Prints `finding` as one line, to stderr when it is a failure."""
    print(f"{finding.level:<5} {finding.what}: {finding.said}",
          file=sys.stderr if finding.failed else sys.stdout, flush=True)


# ------------------------------------------------------------------ Discord

def discord_findings(poller, settings, fetch_application=None):
    """([findings], the application), or ([the failure], None) when the token is missing or
    refused. Discord is asked only when there is a token to ask about."""
    if not (settings.discord_token or "").strip():
        return [Finding(FAIL, "DISCORD_TOKEN", "not set. Put the bot's token in .env: in the "
                        "developer portal, your application, then Bot, then Reset Token.")], None
    fetch = fetch_application or portal.fetch_application
    agent = portal.user_agent(poller.PROJECT_URL, poller.__version__)
    try:
        app = asyncio.run(fetch(settings.discord_token, user_agent=agent))
    except portal.PortalError as error:
        return [Finding(FAIL, "DISCORD_TOKEN", str(error))], None
    found = [Finding(OK, "DISCORD_TOKEN", f"Discord accepts it, for the bot {app.bot_name} "
                     f"of the application {app.name!r}.")]
    if app.members_intent:
        found.append(Finding(OK, "Server Members Intent", "on."))
    else:
        found.append(Finding(WARN, "Server Members Intent", "off. The bot runs without it, but "
                             "cannot tell when someone has left every server it shares with "
                             "them, or who in a DM belongs to a server with access. "
                             + INTENT_HOW))
    if app.public:
        found.append(Finding(NOTE, "Public Bot", "on, so anyone with the invite link can add "
                             "this bot to a server. It still answers only you and those you "
                             "grant access. To add it yourself only, turn Public Bot off on "
                             "the Bot page."))
    return found, app


# ------------------------------------------------------------------ the data directory

def _loose(path: str, mode: int) -> Finding:
    return Finding(WARN, "data directory", f"{path} is mode {mode:o}, so other users on this "
                   f"box can read what it holds, users.db's profiles among them. "
                   f"chmod 700 {path}")


def _make_private(path: str) -> Finding:
    try:
        os.makedirs(path, mode=PRIVATE_MODE)
    except OSError as error:
        return Finding(FAIL, "data directory", f"{path} could not be made: "
                       f"{type(error).__name__}: {error}")
    try:
        os.chmod(path, PRIVATE_MODE)
    except OSError:
        return Finding(WARN, "data directory", f"{path} made, but this filesystem would not "
                       "set its mode to 700.")
    return Finding(OK, "data directory", f"{path} made, mode 700.")


def data_directory(path: str, make: bool = False) -> Finding:
    """What the data directory at `path` is like. With `make`, a missing one is made at
    mode 700; an existing one is never changed."""
    if not os.path.isdir(path):
        if os.path.exists(path):
            return Finding(FAIL, "data directory", f"{path} is not a directory. "
                           "Check DIAYN_DATA.")
        if make:
            return _make_private(path)
        return Finding(FAIL, "data directory", f"{path} does not exist. "
                       f"`{SETUP_COMMAND}` makes it.")
    if not os.access(path, os.W_OK | os.X_OK):
        return Finding(FAIL, "data directory", f"{path} is not writable by this user.")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        return _loose(path, mode)
    return Finding(OK, "data directory", f"{path}, mode {mode:o}.")


# ------------------------------------------------------------------ postings.db

def _seen(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]


def _refused(error) -> Finding:
    return Finding(FAIL, "postings.db", str(error))


@dataclasses.dataclass(frozen=True)
class Ledger:
    """What doctor and setup read from postings.db."""
    seen: int                   # rows in the seen ledger
    last_sweep: float | None    # when the last recorded sweep began
    interval: int               # the seconds between sweeps scraper_meta names


def _interval(poller, conn) -> int:
    """scraper_meta's sweep_interval_s, or the scraper's default for a file without one."""
    try:
        row = conn.execute("SELECT value FROM scraper_meta "
                           "WHERE key = 'sweep_interval_s'").fetchone()
    except sqlite3.OperationalError:        # a file from before the contract tables
        row = None
    try:
        interval = int(row[0]) if row else 0
    except (TypeError, ValueError):
        interval = 0
    return interval if interval > 0 else poller.DEFAULT_INTERVAL_S


def read_ledger(poller, path: str):
    """The Ledger of the postings.db at `path`, which exists, opened read-only; or the
    Finding that refuses it."""
    try:
        conn = poller.db_read_only()
    except (poller.DatabaseRefused, poller.SchemaMismatch) as error:
        return _refused(error)
    except sqlite3.Error as error:
        return _refused(f"{path}: {type(error).__name__}: {error}")
    try:
        return Ledger(seen=_seen(conn),
                      last_sweep=conn.execute("SELECT MAX(started) FROM sweeps").fetchone()[0],
                      interval=_interval(poller, conn))
    except sqlite3.Error as error:
        return _refused(f"{path}: {type(error).__name__}: {error}")
    finally:
        conn.close()


def _empty(path: str) -> Finding:
    return _refused(
        f"{path} exists, but its seen ledger is empty, so `{RUN_COMMAND}` would refuse "
        "it, and setup never bootstraps a file that is already there. If setup made it "
        f"and its first sweep failed, `{INIT_COMMAND}` finishes the bootstrap. If this "
        f"box should have a ledger, this is the wrong file: `{CONFIG_COMMAND}` shows the "
        "path in use.")


def existing_ledger(poller, path: str) -> Finding:
    """What setup says of the postings.db at `path`, which exists; it is never changed."""
    ledger = read_ledger(poller, path)
    if isinstance(ledger, Finding):
        return ledger
    if not ledger.seen:
        return _empty(path)
    return Finding(OK, "postings.db", f"{path} exists, with {ledger.seen} postings seen, and "
                   "is left as it is: a ledger is never bootstrapped twice.")


def _first_sweep(poller, path: str) -> Finding:
    interval = poller.DEFAULT_INTERVAL_S
    conn = poller.open_for_sweeping(init=True, interval=interval)
    try:
        try:
            result = asyncio.run(poller.cmd_sweep(conn, quiet=True, interval=interval))
        except Exception as error:      # rolled back when the connection closes uncommitted
            return _refused(f"the first sweep failed ({type(error).__name__}: {error}). "
                            f"{path} was made, with no ledger yet: `{INIT_COMMAND}` tries "
                            "the first sweep again.")
        seen = _seen(conn)
    finally:
        conn.close()
    if not seen:
        return _refused(f"the first sweep recorded no posting ({result.summary}). Check "
                        f"that this box can reach the job boards, then `{INIT_COMMAND}` "
                        "tries again.")
    return Finding(OK, "postings.db", f"bootstrapped: {seen} postings recorded as seen. "
                   f"{result.summary}")


def bootstrap(poller, settings) -> tuple[int, Finding]:
    """(exit code, finding): postings.db bootstrapped with a first sweep, unless a file is
    already there, which is only looked at. The sweeper lock is held for the sweep."""
    path = settings.postings_db
    if os.path.exists(path):
        found = existing_ledger(poller, path)
        return (FAILED_EXIT if found.failed else 0), found
    print("...   postings.db: bootstrapping. The first sweep records every posting open now "
          "as seen, so none of them is announced. It takes a minute or two.", flush=True)
    try:
        with poller.sweeper_lock(path, create=True):
            found = (existing_ledger(poller, path) if os.path.exists(path)
                     else _first_sweep(poller, path))
    except poller.LockHeld as error:
        return poller.LOCK_HELD_EXIT, _refused(error)
    except (poller.DatabaseRefused, poller.SchemaMismatch) as error:
        return FAILED_EXIT, _refused(error)
    except (sqlite3.Error, OSError) as error:
        return FAILED_EXIT, _refused(f"{path}: {type(error).__name__}: {error}")
    return (FAILED_EXIT if found.failed else 0), found


# ------------------------------------------------------------------ setup

def _invite(app) -> None:
    print("\nInvite the bot to your server with this link:\n\n"
          f"    {portal.invite_url(app.id)}\n\n"
          f"Then start it: {RUN_COMMAND}   (DEPLOY.md has pm2 and systemd examples)\n"
          "In Discord, /internships profile starts a profile, and as the owner, "
          "/diayn grant lets others in.")


def _steps(poller, settings, fetch_application) -> int:
    found, app = discord_findings(poller, settings, fetch_application)
    for finding in found:
        report(finding)
    if app is None:
        return FAILED_EXIT
    directory = data_directory(settings.data_dir, make=True)
    report(directory)
    if directory.failed:
        return FAILED_EXIT
    code, ledger = bootstrap(poller, settings)
    report(ledger)
    if code:
        return code
    _invite(app)
    return 0


def cmd_setup(poller, argv, fetch_application=None) -> int:
    """
    `setup`: checks the token and the intent, makes the data directory and bootstraps
    postings.db, then prints the invite link; returns the exit code. The settings are
    bound first (the scraper's boot()), so the paths are the ones `run` will use.
    `fetch_application` stands in for discord_portal's, for the tests.
    """
    argparse.ArgumentParser(
        prog="diayn.py setup",
        description="Get this host ready: check the bot's token and intent, make the data "
                    "directory, bootstrap postings.db and print the invite link. "
                    "Safe to run again: nothing that exists is changed.").parse_args(argv)
    try:
        poller.boot()
    except poller.ConfigError as error:
        report(Finding(FAIL, "settings", str(error)))
        return FAILED_EXIT
    return _steps(poller, poller.SETTINGS, fetch_application)


# ------------------------------------------------------------------ doctor

def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def python_version(version=None) -> Finding:
    """Python `version` (default: this one), which must be 3.10 or newer."""
    version = tuple(version or sys.version_info[:3])
    shown = ".".join(str(n) for n in version[:3])
    if version[:2] < MIN_PYTHON:
        return Finding(FAIL, "Python", f"{shown}, but DIAYN needs 3.10 or newer.")
    return Finding(OK, "Python", f"{shown}.")


def platform_support(os_name=None, platform=None) -> Finding:
    """Whether this platform (or the one named) has what DIAYN needs: Linux or macOS."""
    os_name, platform = os_name or os.name, platform or sys.platform
    name = PLATFORM_NAMES.get(platform, platform)
    if os_name != "posix":
        return Finding(FAIL, "platform", f"{name}. DIAYN runs on Linux and macOS only: it "
                       "needs fcntl for the sweeper lock and resource for the resume "
                       "reader's limits.")
    missing = [f"{module} (for {what})" for module, what in POSIX_MODULES
               if not _has_module(module)]
    if missing:
        return Finding(FAIL, "platform", f"{name}, but this Python has no "
                       f"{' or '.join(missing)}. DIAYN runs on Linux and macOS only.")
    return Finding(OK, "platform", f"{name}, with fcntl for the sweeper lock and resource "
                   "for the resume reader's limits.")


def _ago(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'}"
    n = max(0, int(seconds))
    return f"{n} second{'' if n == 1 else 's'}"


def last_sweep(ledger: Ledger, now: float) -> Finding:
    """How long ago the last sweep began: late beyond STALE_SWEEPS intervals."""
    every = _ago(ledger.interval)
    if ledger.last_sweep is None:
        return Finding(WARN, "last sweep", f"none recorded yet. `{RUN_COMMAND}` sweeps "
                       f"every {every}.")
    age = max(0.0, now - ledger.last_sweep)
    if age > STALE_SWEEPS * ledger.interval:
        return Finding(WARN, "last sweep", f"began {_ago(age)} ago, more than "
                       f"{STALE_SWEEPS} intervals of {every}. The bot warns of the same in "
                       "/diayn debug; the process's log says why.")
    return Finding(OK, "last sweep", f"began {_ago(age)} ago; one is due every {every}.")


def sweeper(poller, path: str) -> Finding:
    """Whether a `run` or a `watch` holds the sweeper lock. Taken for an instant, and
    only when nothing holds it; a lock file that is not there is not made."""
    lock = poller.lock_path(path)
    idle = f"so nothing is sweeping. `{RUN_COMMAND}` runs the bot and its sweeps."
    if not os.path.exists(lock):
        return Finding(WARN, "sweeper", f"there is no {lock} yet, {idle}")
    try:
        with poller.sweeper_lock(path):
            pass
    except poller.LockHeld:
        return Finding(OK, "sweeper", "a `run` or a `watch` holds the lock, so postings.db "
                       "is being swept.")
    except (OSError, poller.DatabaseRefused) as error:
        return Finding(FAIL, "sweeper", f"{lock}: {type(error).__name__}: {error}")
    return Finding(WARN, "sweeper", f"nothing holds {lock}, {idle}")


def database_findings(poller, path: str, now: float) -> list:
    """postings.db, its last sweep and its sweeper. Nothing is made: a missing file is
    only reported."""
    if not os.path.exists(path):
        return [_refused(f"{path} does not exist. `{SETUP_COMMAND}` makes one, with a first "
                         "sweep that records every open posting as seen.")]
    ledger = read_ledger(poller, path)
    if isinstance(ledger, Finding):
        return [ledger]
    held = (_empty(path) if not ledger.seen
            else Finding(OK, "postings.db", f"{path}, with {ledger.seen} postings seen."))
    return [held, last_sweep(ledger, now), sweeper(poller, path)]


def contact(settings) -> Finding:
    if settings.contact:
        return Finding(OK, "POLL_CONTACT", f"{settings.contact}, in the User-Agent every job "
                       "board sees.")
    return Finding(WARN, "POLL_CONTACT", "not set. It goes in the User-Agent every job board "
                   "sees, so a board's owner can reach whoever runs this rather than block "
                   "it. Use a project URL or a role mailbox, never a personal address.")


def gemini(settings) -> Finding:
    if settings.gemini_key:
        return Finding(OK, "GEMINI_API_KEY", "set. Before an alert, Gemini checks each match "
                       "for everyone who has not turned the check off, up to "
                       f"FIT_RPD={settings.fit_rpd} requests a day; that budget and --llm's "
                       "together must fit the key's quota.")
    return Finding(NOTE, "GEMINI_API_KEY", "not set, which is fine: it is optional. With a "
                   "key, Gemini checks each match before an alert is sent, leaves out the "
                   "ones that do not suit the person and says why for the rest. Without one, "
                   "alerts carry the rule-based matches unchecked. The README's Gemini "
                   "section says what it sends.")


def _settings_finding(poller, env_file) -> Finding:
    if env_file:
        return Finding(OK, "settings", f"from {env_file}.")
    return Finding(OK, "settings", "from the environment alone, since there is no "
                   f"{poller.env_file_path()}.")


def _verdict(findings: list) -> int:
    failed = sum(f.failed for f in findings)
    warned = sum(f.level == WARN for f in findings)
    warnings = f", {warned} warning{'' if warned == 1 else 's'}" if warned else ""
    print(f"\ndoctor: {failed} to fix{warnings}." if failed
          else f"\ndoctor: nothing to fix{warnings}.")
    return FAILED_EXIT if failed else 0


def cmd_doctor(poller, argv, fetch_application=None, now=None) -> int:
    """
    `doctor`: every check, each reported as it is made; returns 1 when anything is to fix
    and 0 otherwise. `fetch_application` stands in for discord_portal's and `now` for the
    clock, for the tests.
    """
    argparse.ArgumentParser(
        prog="diayn.py doctor",
        description="Check this host: the token and intent, the data directory, "
                    "postings.db, the sweeper, and the settings. Changes nothing.").parse_args(argv)
    findings = []

    def check(*found):
        for finding in found:
            report(finding)
            findings.append(finding)

    check(python_version(), platform_support())
    try:
        env_file = poller.boot()
    except poller.ConfigError as error:
        check(Finding(FAIL, "settings", str(error)))
        return _verdict(findings)
    settings = poller.SETTINGS
    check(_settings_finding(poller, env_file))
    check(*discord_findings(poller, settings, fetch_application)[0])
    check(data_directory(settings.data_dir))
    check(*database_findings(poller, settings.postings_db, time.time() if now is None else now))
    check(contact(settings), gemini(settings))
    return _verdict(findings)
