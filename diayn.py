"""
DIAYN's entry point.

    python diayn.py <command> [options]

Every command of the scraper, `internship_poller.py`, runs through here with
the same arguments and the same exit codes: 0 done, 1 failed, 2 a usage error,
3 another sweeper holds the lock. `python diayn.py sweep --init` and
`python internship_poller.py sweep --init` are the same run.

DIAYN's own commands, for the Discord bot, are in BOT_COMMANDS:

    python diayn.py import-legacy --from <stats.db>

copies the old `/internships ping` tracker's subscribers out of its bot's
stats.db, which it opens read-only, into DIAYN's users.db as profiles. It runs
once, and prints counts only.

    python diayn.py grant --user <id>      python diayn.py revoke --user <id>
    python diayn.py grant --server <id>    python diayn.py revoke --server <id>

let one person, or everyone in one server, use the bot, or take that away,
without Discord: a host can grant before the bot's first start. They write
users.db's access_grants, which the running bot reads on every check, and
print what they did, never an id.

The rest are in PLANNED_COMMANDS until they are built. Each says so and exits
2, without importing the scraper or touching a file.

Importing this module is inert. The scraper is imported only when a scraper
command, a bot command, or the list of commands is asked for, so the planned
commands work on a box without aiohttp.
"""

import argparse
import os
import pathlib
import sqlite3
import sys
import time

CHECKOUT = os.path.dirname(os.path.abspath(__file__))
# The finder's modules, which use bare imports with this directory on sys.path.
BOT_DIR = os.path.join(CHECKOUT, "bot")
IMPORT_LEGACY = "import-legacy"
GRANT, REVOKE = "grant", "revoke"
# DIAYN's own commands that are built.
BOT_COMMANDS = (IMPORT_LEGACY, GRANT, REVOKE)
# DIAYN's own commands, each built in a later change.
PLANNED_COMMANDS = ("setup", "doctor", "run")
HELP_FLAGS = ("-h", "--help")
# argparse's code for a usage error, which the scraper exits with too.
USAGE_EXIT = 2
# A failure: the scraper's code for a refusal or a bad setting.
FAILED_EXIT = 1


def scraper():
    """internship_poller, imported on first use."""
    import internship_poller
    return internship_poller


def _bot_path() -> None:
    if BOT_DIR not in sys.path:
        sys.path.insert(0, BOT_DIR)


def finder():
    """The finder's store and delivery modules, with bot/ on sys.path, imported on first use."""
    _bot_path()
    import intern_delivery
    import intern_store
    return intern_store, intern_delivery


def access_module():
    """The bot's access module, with bot/ on sys.path, imported on first use."""
    _bot_path()
    import access
    return access


def usage(scraper_commands) -> str:
    return ("usage: diayn.py <command> [options]\n\n"
            f"DIAYN's commands: {', '.join(BOT_COMMANDS)}\n"
            f"DIAYN's commands (not built yet): {', '.join(PLANNED_COMMANDS)}\n"
            f"The scraper's commands: {', '.join(scraper_commands)}\n"
            "`diayn.py <command> --help` lists that command's options.")


# ------------------------------------------------------------------ import-legacy

def open_legacy(path) -> sqlite3.Connection:
    """The old bot's stats.db at `path`, opened read-only (mode=ro), so nothing
    can be written to it. A path that is not a file is refused with
    FileNotFoundError rather than created."""
    real = pathlib.Path(os.path.realpath(path))
    if not real.is_file():
        raise FileNotFoundError(f"{path}: no such file")
    return sqlite3.connect(real.as_uri() + "?mode=ro", uri=True)


def _refused(reason, command=IMPORT_LEGACY) -> int:
    print(f"diayn.py {command}: {reason}", file=sys.stderr)
    return FAILED_EXIT


def _read_legacy(store, source):
    """(the legacy rows of the file at `source`, None), or (None, why there are none)."""
    try:
        old = open_legacy(source)
    except OSError as e:
        return None, str(e)
    try:
        return store.read_legacy(old), None
    except store.LegacyImportError as e:
        return None, str(e)
    except sqlite3.Error as e:
        return None, f"{source}: {type(e).__name__}: {e}"
    finally:
        old.close()


def import_legacy(source, users_path, now) -> int:
    """
    Copies the old tracker's subscribers from the stats.db at `source` into the
    users.db at `users_path`; returns the exit code. The old file is only read.
    users.db is made only once the old file has been read, and a refusal leaves
    it as it was. Prints counts and reasons, never an id.
    """
    store, delivery = finder()
    rows, reason = _read_legacy(store, source)
    if reason is not None:
        return _refused(reason)
    os.makedirs(os.path.dirname(users_path), exist_ok=True)
    users = sqlite3.connect(users_path)
    try:
        store.init_db(users)
        counts = store.write_migrated(users, rows, now, cursor=delivery.horizon(now))
    except store.LegacyImportError as e:
        return _refused(e)
    except sqlite3.Error as e:
        return _refused(f"users.db: {type(e).__name__}: {e}")
    finally:
        users.close()
    print(f"{IMPORT_LEGACY}: {counts.legacy} subscribers in the old tracker; "
          f"{counts.written} imported, {counts.already} already had a profile.")
    return 0


def cmd_import_legacy(poller, argv) -> int:
    """`import-legacy --from <stats.db>`, with the scraper's settings for where users.db is."""
    parser = argparse.ArgumentParser(
        prog=f"diayn.py {IMPORT_LEGACY}",
        description="Copy the old tracker's subscribers into users.db, once.")
    parser.add_argument("--from", dest="source", required=True, metavar="STATS_DB",
                        help="the old bot's stats.db, which is opened read-only")
    args = parser.parse_args(argv)
    settings, reason = _settings(poller)
    if reason is not None:
        return _refused(reason)
    return import_legacy(args.source, settings.users_db, time.time())


def _settings(poller):
    """(the scraper's settings, None), or (None, why they are refused): the .env is loaded
    as the scraper loads it, so users.db is the one the bot opens."""
    try:
        poller.load_env_file()
        return poller.configure(os.environ), None
    except poller.ConfigError as e:
        return None, e


# ------------------------------------------------------------------ grant and revoke

#: What each command prints, by (command, kind, whether it changed anything).
_ACCESS_SAID = {
    (GRANT, "user", True): "that user may use this bot now.",
    (GRANT, "user", False): "that user already had a grant; nothing changed.",
    (GRANT, "guild", True): "everyone in that server, and its members anywhere, may use this bot now.",
    (GRANT, "guild", False): "that server already had a grant; nothing changed.",
    (REVOKE, "user", True): "that user's grant is gone. They keep access only if they run this "
                            "bot or are in a server that has a grant.",
    (REVOKE, "user", False): "that user had no grant of their own; nothing changed.",
    (REVOKE, "guild", True): "that server's grant is gone. Its members keep access only through "
                             "a grant of their own or another server's.",
    (REVOKE, "guild", False): "that server had no grant; nothing changed.",
}


def discord_id(text: str) -> int:
    """A Discord id as typed on the command line: digits, as Discord's Copy ID gives them."""
    if not text.isdigit() or not 0 < int(text) < 2 ** 63:
        raise argparse.ArgumentTypeError("expected a Discord id: the number Discord's "
                                         "Copy ID gives")
    return int(text)


def _write_access(access, command, kind, target, users_path, now) -> bool:
    """Makes users.db and its grants table if need be, then grants or revokes."""
    os.makedirs(os.path.dirname(users_path), exist_ok=True)
    users = sqlite3.connect(users_path)
    try:
        access.init_db(users)
        if command == GRANT:
            return access.grant(users, kind, target, granted_by=None, now=now)
        return access.revoke(users, kind, target)
    finally:
        users.close()


def change_access(command, kind, target, users_path, now) -> int:
    """
    Grants or revokes access for one user or one server (`kind` "user" or "guild") in
    the users.db at `users_path`; returns the exit code. A grant from here has no
    granter. A revoke never creates users.db: without one there is nothing to take
    back. Prints what it did, never the id.
    """
    access = access_module()
    if command == REVOKE and not os.path.exists(users_path):
        changed = False
    else:
        try:
            changed = _write_access(access, command, kind, target, users_path, now)
        except (sqlite3.Error, OSError) as e:
            return _refused(f"users.db: {type(e).__name__}: {e}", command)
    print(f"{command}: {_ACCESS_SAID[command, kind, changed]}")
    return 0


def cmd_access(poller, command, argv) -> int:
    """`grant` or `revoke`, `--user <id>` or `--server <id>`, into the users.db the settings name."""
    verb = "Let" if command == GRANT else "Stop"
    parser = argparse.ArgumentParser(
        prog=f"diayn.py {command}",
        description=f"{verb} one person, or everyone in one server, "
                    f"{'use' if command == GRANT else 'using'} the bot.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user", type=discord_id, metavar="ID", help="a Discord user's id")
    target.add_argument("--server", type=discord_id, metavar="ID", help="a Discord server's id")
    args = parser.parse_args(argv)
    settings, reason = _settings(poller)
    if reason is not None:
        return _refused(reason, command)
    kind, target_id = ("user", args.user) if args.user is not None else ("guild", args.server)
    return change_access(command, kind, target_id, settings.users_db, time.time())


# ------------------------------------------------------------------ dispatch

def main(argv=None) -> int:
    """Run the command in `argv` (default: the command line); return its exit code.

    A scraper command that fails exits from inside the scraper, with the
    scraper's own code.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    command = argv[0] if argv else None
    if command in PLANNED_COMMANDS:
        print(f"diayn.py {command}: not built yet", file=sys.stderr)
        return USAGE_EXIT
    try:
        poller = scraper()
    except ModuleNotFoundError as e:
        print(f"diayn.py: the scraper needs {e.name}, which is not installed. "
              "Install the requirements: pip install -r requirements.txt",
              file=sys.stderr)
        return FAILED_EXIT
    if command == IMPORT_LEGACY:
        return cmd_import_legacy(poller, argv[1:])
    if command in (GRANT, REVOKE):
        return cmd_access(poller, command, argv[1:])
    if command in HELP_FLAGS:
        print(usage(poller.COMMANDS))
        return 0
    if command is None:
        print(usage(poller.COMMANDS), file=sys.stderr)
        return USAGE_EXIT
    if not command.startswith("-") and command not in poller.COMMANDS:
        print(f"diayn.py: unknown command {command!r}\n\n{usage(poller.COMMANDS)}",
              file=sys.stderr)
        return USAGE_EXIT
    poller.main(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
