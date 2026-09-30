"""
DIAYN's entry point.

    .venv/bin/python diayn.py <command> [options]

Every command of the scraper, `internship_poller.py`, runs through here with
the same arguments and the same exit codes: 0 done, 1 failed, 2 a usage error,
3 another sweeper holds the lock. `.venv/bin/python diayn.py sweep --init` and
`.venv/bin/python internship_poller.py sweep --init` are the same run.

DIAYN's own commands, for the Discord bot, are in BOT_COMMANDS:

    .venv/bin/python diayn.py import-legacy --from <stats.db>

copies the old `/internships ping` tracker's subscribers out of its bot's
stats.db, which it opens read-only, into DIAYN's users.db as profiles. It runs
once, and prints counts only.

    .venv/bin/python diayn.py grant --user <id>
    .venv/bin/python diayn.py grant --server <id>
    .venv/bin/python diayn.py revoke --user <id>
    .venv/bin/python diayn.py revoke --server <id>

let one person, or everyone in one server, use the bot, or take that away,
without Discord: a host can grant before the bot's first start. They write
users.db's access_grants, which the running bot reads on every check, and
print what they did, never an id.

    .venv/bin/python diayn.py run [--interval N] [--llm]

runs the Discord bot and the sweep loop together, in one process, until it is
stopped. It holds the sweeper lock for as long as it runs, so a second `run`,
or a `watch` beside it, exits 3 before anything logs in to Discord. The bot
reads postings.db only through the contract, on a read-only connection of its
own; the sweep loop is the scraper's own `watch`, on the writer connection. If
the sweep loop ever ends, the process exits 1, so that whatever runs it starts
it again. If Discord refuses the Server Members Intent, it exits 78 (EX_CONFIG),
which DEPLOY.md's units do not restart on. `internship_poller.py watch` still
runs the sweep loop alone, for a host that wants the two apart.

    .venv/bin/python diayn.py setup

gets a new host ready: it checks DISCORD_TOKEN with Discord and the Server
Members Intent, makes the data directory at mode 700, bootstraps postings.db
with a first sweep that records every open posting as seen, unless a
postings.db is already there, and prints the invite link. host_checks.py has
the steps.

    .venv/bin/python diayn.py doctor

checks the host again at any time: the Python version and the platform,
discord.py and pypdf, the settings, the token and the intent, the data
directory, postings.db and its last sweep, whether anything is sweeping,
POLL_CONTACT and the Gemini key. It changes nothing, and exits 1 when anything
is to fix.

Before anything else, main() checks that Python is 3.10 or newer, with
hints.python_refusal. The scraper and host_checks use 3.10's syntax, and on
3.9, macOS's own python3, they die with a TypeError as they are imported,
before doctor could say why. So this module, and hints.py, the one module of
the checkout it imports at the top, stay within what Python 3.9 parses and
runs; tests/test_diayn.py checks both. internship_poller.py and
resolve_boards.py, run on their own, make the same check before their imports.

Importing this module is inert. The scraper is imported only when a command,
or the list of commands, is asked for. The Discord client, and discord.py with
it, is imported only by `run`.
"""

import argparse
import asyncio
import os
import pathlib
import signal
import sqlite3
import sys
import time
import traceback

import hints

CHECKOUT = os.path.dirname(os.path.abspath(__file__))
# The finder's modules, which use bare imports with this directory on sys.path.
BOT_DIR = os.path.join(CHECKOUT, "bot")
IMPORT_LEGACY = "import-legacy"
GRANT, REVOKE = "grant", "revoke"
RUN = "run"
SETUP, DOCTOR = "setup", "doctor"
# DIAYN's own commands.
BOT_COMMANDS = (IMPORT_LEGACY, GRANT, REVOKE, RUN, SETUP, DOCTOR)
# What the scraper needs that only a POSIX system has: DIAYN runs on Linux and macOS.
POSIX_ONLY_MODULES = ("fcntl",)
HELP_FLAGS = ("-h", "--help")
# argparse's code for a usage error, which the scraper exits with too.
USAGE_EXIT = 2
# A failure: the scraper's code for a refusal or a bad setting.
FAILED_EXIT = 1
# sysexits.h's EX_CONFIG: `run` exits with it when Discord refuses the Server Members
# Intent. That is a toggle in the developer portal, which no restart changes, so
# DEPLOY.md's pm2 and systemd units never restart on it: a loop of refused logins can
# go on all day, and Discord resets the token of a bot that logs in too often.
CONFIG_EXIT = 78
# The gateway's close code for an intent the portal has not turned on.
DISALLOWED_INTENTS = 4014
# Seconds the bot has to stop once the sweep loop has ended and it has been cancelled.
# Logging out of Discord takes a second or two. A bot still stopping after this is
# left behind, and the process exits non-zero anyway, so that it is started again.
BOT_SHUTDOWN_S = 30


class IntentRefused(Exception):
    """Discord refused the Server Members Intent as the bot logged in."""


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


def host_checks():
    """setup's steps and doctor's checks, imported on first use."""
    import host_checks
    return host_checks


def access_module():
    """The bot's access module, with bot/ on sys.path, imported on first use."""
    _bot_path()
    import access
    return access


def usage(scraper_commands) -> str:
    return ("usage: diayn.py <command> [options]\n\n"
            f"DIAYN's commands: {', '.join(BOT_COMMANDS)}\n"
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


# ------------------------------------------------------------------ run

def discord_bot():
    """
    `run`'s bot: a function of the bound settings that returns the coroutine building
    bot/app.py's client and serving it until it stops. app, and discord.py with it, is
    imported here, so a box without discord.py is refused before anything is locked;
    the client is built inside the coroutine, after the sweep loop's connection has
    published the registry the bot's reader checks.
    """
    _bot_path()
    import app
    import discord

    async def serve(settings):
        try:
            await app.serve(app.build(), settings.discord_token)
        except Exception as error:
            if refuses_intent(error, discord):
                raise IntentRefused() from error
            raise
    return serve


def refuses_intent(error, discord) -> bool:
    """Whether `error`, or anything it was raised from or while handling, is discord.py
    refusing a privileged intent: PrivilegedIntentsRequired, or the gateway closing
    with 4014, the code discord.py turns into it."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, discord.PrivilegedIntentsRequired):
            return True
        if isinstance(error, discord.ConnectionClosed) and error.code == DISALLOWED_INTENTS:
            return True
        error = error.__cause__ or error.__context__
    return False


def _how_it_ended(task) -> str:
    """Why a finished task finished, for the log."""
    if task.cancelled():
        return "it was cancelled"
    error = task.exception()
    if error is None:
        return "it returned"
    return f"{type(error).__name__}: {error}"


async def run_together(bot, sweep) -> int:
    """
    Runs `bot` and `sweep`, two coroutines, as tasks in this loop until the bot stops;
    returns the exit code: 0 when the bot stopped, FAILED_EXIT when the sweep loop ended.

    The sweep loop is not meant to end: the scraper's cmd_watch outlives a failed
    sweep. If it ends anyway, a done-callback cancels the bot, first, so that nothing
    after it can leave the bot running, then logs why; and the process exits non-zero,
    so pm2 or systemd starts it again with its sweeps. Otherwise the bot would go on
    answering from a ledger nothing updates. The bot gets BOT_SHUTDOWN_S to stop; one
    still stopping then is left behind. When the bot stops first, the sweep loop is
    cancelled and waited for, so it is finished before its connection closes. An
    exception from the bot is raised from here. A stop from outside (Ctrl-C, or the
    SIGINT or SIGTERM pm2 and systemd send, through run_until_stopped) cancels each task
    once, and waits for both: the bot logs out of Discord, and nothing is logged. A sweep
    loop cancelled by anything else, as an event loop tearing every task down does, is
    not one that ended: the bot is cancelled once, left to log out, and nothing is logged.
    """
    bot_task = asyncio.create_task(bot, name="bot")
    sweep_task = asyncio.create_task(sweep, name="sweep loop")
    ended = []

    def on_sweep_done(task):
        # The bot stopped first and cancelled it; or something else cancelled it.
        # Neither is a sweep loop that ended by itself.
        if bot_task.done() or task.cancelled():
            return
        bot_task.cancel()
        ended.append(_how_it_ended(task))
        if not task.cancelled() and task.exception() is not None:
            traceback.print_exception(task.exception(), file=sys.stderr)
        scraper().log(f"sweep loop ended ({ended[0]}); stopping the bot, so that whatever "
                      "runs DIAYN starts both again", file=sys.stderr)

    sweep_task.add_done_callback(on_sweep_done)
    try:
        await asyncio.wait([bot_task, sweep_task], return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:      # the process is stopping: Ctrl-C, or pm2's SIGINT
        # Detached first: the sweep loop finishes stopping before the bot has logged
        # out, and the callback would cancel the bot a second time, cutting its logout
        # off, and log a sweep loop that ended by itself.
        sweep_task.remove_done_callback(on_sweep_done)
        bot_task.cancel()
        sweep_task.cancel()
        await asyncio.wait([bot_task, sweep_task])
        raise
    if ended:
        await _let_the_bot_stop(bot_task)
        return FAILED_EXIT
    if not bot_task.done():             # the sweep loop was cancelled from outside
        bot_task.cancel()
        await _let_the_bot_stop(bot_task)
        return FAILED_EXIT
    sweep_task.cancel()
    await asyncio.wait([sweep_task])
    bot_task.result()                   # raises what the bot raised
    return 0


async def _let_the_bot_stop(bot_task) -> None:
    """Waits up to BOT_SHUTDOWN_S for the cancelled bot, and says so when it has not
    stopped, or when it failed as it stopped. What it is left doing is cancelled again
    as the event loop closes."""
    done, _ = await asyncio.wait([bot_task], timeout=BOT_SHUTDOWN_S)
    if not done:
        scraper().log(f"the bot had not stopped {BOT_SHUTDOWN_S}s after it was told to; "
                      "exiting anyway", file=sys.stderr)
    elif not bot_task.cancelled() and bot_task.exception() is not None:
        scraper().log(f"the bot stopped with {_how_it_ended(bot_task)}", file=sys.stderr)


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def run_until_stopped(main):
    """
    Runs coroutine `main` to completion and returns its result, as asyncio.run does,
    except that SIGINT and SIGTERM cancel `main` rather than tear the loop down. A stop
    then raises KeyboardInterrupt, once `main` has finished stopping.

    Python 3.11's asyncio.run cancels the main task on SIGINT, so run_together stops in
    order: the bot logs out of Discord, then the loop closes. 3.10's lets the
    KeyboardInterrupt out of the loop and cancels every task at once, which cuts the
    bot's logout off. No version handles SIGTERM, which systemd sends by default. This
    gives every version 3.11's behaviour, for both signals.
    """
    loop = asyncio.new_event_loop()
    stopped = []
    try:
        asyncio.set_event_loop(loop)
        task = loop.create_task(main)

        def stop(signum):
            stopped.append(signum)
            task.cancel()

        for signum in STOP_SIGNALS:
            loop.add_signal_handler(signum, stop, signum)
        try:
            return loop.run_until_complete(task)
        except asyncio.CancelledError:
            if stopped:
                raise KeyboardInterrupt from None
            raise
    finally:
        for signum in STOP_SIGNALS:
            loop.remove_signal_handler(signum)
        _close_loop(loop)


def _close_loop(loop) -> None:
    """What asyncio.run does last: cancel what is left, finish async generators and the
    default executor, then close the loop."""
    try:
        left = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in left:
            t.cancel()
        if left:
            loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _run_arguments(poller, argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"diayn.py {RUN}",
        description="Run the Discord bot and the sweep loop together, until stopped.")
    parser.add_argument("--interval", type=int, default=poller.DEFAULT_INTERVAL_S,
                        help="seconds between sweeps, as for `watch` "
                             f"(default {poller.DEFAULT_INTERVAL_S}, "
                             f"at least {poller.MIN_INTERVAL_S})")
    parser.add_argument("--llm", action="store_true",
                        help="classify new postings with Gemini, as `watch --llm` does "
                             "(needs GEMINI_API_KEY)")
    args = parser.parse_args(argv)
    if args.interval < poller.MIN_INTERVAL_S:
        parser.error(f"--interval {args.interval}: sweeps are at least "
                     f"{poller.MIN_INTERVAL_S} seconds apart. "
                     f"Pass {poller.MIN_INTERVAL_S} or more.")
    return args


def _database_refusal(poller, error, path) -> str:
    """What `run` says when postings.db is refused: a missing file, or one with an empty
    ledger, points at setup alone, since `run` takes no --init."""
    if isinstance(error, poller.EmptyLedger):
        return (f"{path}: the seen ledger is empty, so the first sweep would record every "
                "open posting as new, and the bot would announce them all. `diayn.py "
                f"{RUN}` never bootstraps postings.db: `{hints.command(SETUP)}` says what to "
                "do with this file.")
    if not os.path.exists(path):
        return (f"{path}: no such database. `{hints.command(SETUP)}` makes one, with a "
                "first sweep that records every posting open now as seen, so that none is "
                "announced. If this box has one, check DIAYN_DATA and POSTINGS_DB: "
                f"`{hints.command('config')}` shows the path in use.")
    return (f"{error}\n`diayn.py {RUN}` never makes or bootstraps postings.db; "
            f"for a new one, `{hints.command(SETUP)}` does.")


def _serve_and_sweep(poller, settings, interval, bot, watch) -> int:
    """Takes the lock for life, then opens the writer, then runs both tasks."""
    with poller.sweeper_lock(settings.postings_db):
        conn = poller.open_for_sweeping(init=False, interval=interval)
        try:
            return run_until_stopped(run_together(bot(settings), watch(conn)))
        finally:
            conn.close()


def cmd_run(poller, argv, bot=None, watch=None) -> int:
    """
    `run [--interval N] [--llm]`: the Discord bot and the sweep loop in one process, until
    stopped; returns the exit code. The settings are bound (the scraper's boot()) before
    anything else, and the lock is taken before the bot is built or logs in.

    `bot`, a function of the settings returning the bot's coroutine, and `watch`, a
    function of the writer connection returning the sweep loop's, are for the tests.
    By default they are bot/app.py's client and the scraper's cmd_watch.
    """
    args = _run_arguments(poller, argv)
    try:
        poller.boot()
    except poller.ConfigError as e:
        return _refused(e, RUN)
    settings = poller.SETTINGS
    if not (settings.discord_token or "").strip():
        return _refused("DISCORD_TOKEN is not set: the bot's token, "
                        "from the Discord developer portal", RUN)
    if bot is None:
        try:
            bot = discord_bot()
        except ModuleNotFoundError as e:
            return _refused(f"the bot needs {e.name}, which is not installed. "
                            f"{hints.install_hint()}", RUN)
    if watch is None:
        def watch(conn):
            return poller.cmd_watch(conn, args.interval, use_llm=args.llm)
    try:
        return _serve_and_sweep(poller, settings, args.interval, bot, watch)
    except poller.LockHeld as e:
        print(f"diayn.py {RUN}: {e}", file=sys.stderr)
        return poller.LOCK_HELD_EXIT
    except poller.DatabaseRefused as e:
        return _refused(_database_refusal(poller, e, settings.postings_db), RUN)
    except poller.SchemaMismatch as e:
        return _refused(e, RUN)
    except IntentRefused:
        _refused(f"Discord refused the Server Members Intent, and the bot cannot log in "
                 f"without it. {hints.INTENT_HOW} Then start DIAYN again. Exiting "
                 f"{CONFIG_EXIT}, which DEPLOY.md's pm2 and systemd units do not restart "
                 "on: a restart would be refused the same way.", RUN)
        return CONFIG_EXIT
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


# ------------------------------------------------------------------ dispatch

def main(argv=None) -> int:
    """Run the command in `argv` (default: the command line); return its exit code.

    A scraper command that fails exits from inside the scraper, with the
    scraper's own code.
    """
    refusal = hints.python_refusal()
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return FAILED_EXIT
    argv = sys.argv[1:] if argv is None else list(argv)
    command = argv[0] if argv else None
    try:
        poller = scraper()
    except ModuleNotFoundError as e:
        if e.name in POSIX_ONLY_MODULES:
            print(f"diayn.py: DIAYN runs on Linux and macOS only. The sweeper lock needs "
                  f"{e.name}, which this platform ({sys.platform}) does not have.",
                  file=sys.stderr)
            return FAILED_EXIT
        print(f"diayn.py: the scraper needs {e.name}, which is not installed. "
              f"{hints.install_hint()}", file=sys.stderr)
        return FAILED_EXIT
    if command == IMPORT_LEGACY:
        return cmd_import_legacy(poller, argv[1:])
    if command in (GRANT, REVOKE):
        return cmd_access(poller, command, argv[1:])
    if command == RUN:
        return cmd_run(poller, argv[1:])
    if command == SETUP:
        return host_checks().cmd_setup(poller, argv[1:])
    if command == DOCTOR:
        return host_checks().cmd_doctor(poller, argv[1:])
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
