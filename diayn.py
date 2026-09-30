"""
DIAYN's entry point.

    python diayn.py <command> [options]

Every command of the scraper, `internship_poller.py`, runs through here with
the same arguments and the same exit codes: 0 done, 1 failed, 2 a usage error,
3 another sweeper holds the lock. `python diayn.py sweep --init` and
`python internship_poller.py sweep --init` are the same run.

DIAYN's own commands, for the Discord bot, are in PLANNED_COMMANDS until they
are built. Each says so and exits 2, without importing the scraper or touching
a file.

Importing this module is inert. The scraper is imported only when a scraper
command, or the list of commands, is asked for, so the planned commands work
on a box without aiohttp.
"""

import sys

# DIAYN's own commands, each built in a later change.
PLANNED_COMMANDS = ("setup", "doctor", "run", "import-legacy", "grant")
HELP_FLAGS = ("-h", "--help")
# argparse's code for a usage error, which the scraper exits with too.
USAGE_EXIT = 2
# A failure: the scraper's code for a refusal or a bad setting.
FAILED_EXIT = 1


def scraper():
    """internship_poller, imported on first use."""
    import internship_poller
    return internship_poller


def usage(scraper_commands) -> str:
    return ("usage: diayn.py <command> [options]\n\n"
            f"DIAYN's commands (not built yet): {', '.join(PLANNED_COMMANDS)}\n"
            f"The scraper's commands: {', '.join(scraper_commands)}\n"
            "`diayn.py <scraper command> --help` lists that command's options.")


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
