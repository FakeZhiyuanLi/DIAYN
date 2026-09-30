"""
hints.py
~~~~~~~~
What DIAYN's messages tell someone to type, or to turn on: a command spelled
with the Python that is actually running, the portal toggle the bot cannot log
in without, and a newer Python when this one is too old.

    hints.command("setup")      # '.venv/bin/python diayn.py setup'
    hints.install_hint()        # how to install requirements.txt for this Python
    hints.exit_if_old_python()  # a script's first check, before its own imports

The quick start installs into .venv and never activates it, and plenty of
hosts have no `python` at all (stock macOS, Ubuntu without python-is-python3),
so a hint that says bare `python` is one that cannot be copied. The running
interpreter is shown relative to the checkout when it is inside it, since
every command is typed from there, and absolute otherwise.

Importing this module does nothing. diayn.py, internship_poller.py and
resolve_boards.py import it before they check the Python version, so it uses
nothing Python 3.9 cannot parse and run.
"""

import os
import shlex
import sys

CHECKOUT = os.path.dirname(os.path.abspath(__file__))
ENTRY_POINT = "diayn.py"
REQUIREMENTS = "requirements.txt"
#: What to type when the running interpreter is unknown: sys.executable can be empty.
FALLBACK_PYTHON = "python3"
#: The venv the quick start makes, in the checkout.
VENV = ".venv"
#: Where the Server Members Intent is turned on. The bot asks for it, and Discord
#: refuses the login of a bot whose portal toggle is off.
INTENT_HOW = ("In the developer portal, open your application, then Bot, and under "
              "Privileged Gateway Intents turn on Server Members Intent.")
#: The oldest Python DIAYN runs on. On 3.9, macOS's own python3, the scraper and llm
#: die with a TypeError as they are imported, before anything could say why.
MIN_PYTHON = (3, 10)
#: A script's exit status when this Python is older than MIN_PYTHON: a failure.
OLD_PYTHON_EXIT = 1


def _inside(path, directory):
    """`path` relative to `directory` when it is inside it, else None."""
    try:
        if os.path.commonpath([path, directory]) != directory:
            return None
    except ValueError:
        return None
    return os.path.relpath(path, directory)


def interpreter(executable=None, checkout=CHECKOUT):
    """The running Python, or `executable`, as someone in `checkout` types it: relative
    when it is inside the checkout, absolute otherwise, and quoted for a shell."""
    executable = sys.executable if executable is None else executable
    if not executable:
        return FALLBACK_PYTHON
    path = os.path.abspath(executable)
    # A venv's python is a symlink to the Python it was made from, so only its
    # directory is resolved: a checkout reached through a symlink (macOS's /tmp
    # is /private/tmp) is still the checkout, and the venv stays the venv.
    resolved = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    for candidate, base in ((path, os.path.abspath(checkout)),
                            (resolved, os.path.realpath(checkout))):
        relative = _inside(candidate, base)
        if relative is not None:
            # With no slash, the shell would look the name up on PATH instead.
            return shlex.quote(relative if os.sep in relative else os.curdir + os.sep + relative)
    return shlex.quote(path)


def command(*args, executable=None, checkout=CHECKOUT):
    """`diayn.py` with `args`, as typed in the checkout with the running Python."""
    return " ".join([interpreter(executable, checkout), ENTRY_POINT, *args])


def python_refusal(version=None, executable=None, checkout=CHECKOUT):
    """Why this Python, or `version`, is too old for DIAYN, and which interpreter it is,
    in two lines; None when it is MIN_PYTHON or newer."""
    version = tuple(sys.version_info if version is None else version)[:3]
    if version[:2] >= MIN_PYTHON:
        return None
    wanted = ".".join(str(n) for n in MIN_PYTHON)
    shown = ".".join(str(n) for n in version)
    return (f"DIAYN needs Python {wanted} or newer; this is {shown}\n"
            f"{interpreter(executable, checkout)} is that Python. Make .venv with a newer "
            "one, as the README's quick start says.")


def exit_if_old_python(version=None):
    """A script's first check, before it imports anything that needs a newer Python:
    on one older than MIN_PYTHON, says why and exits OLD_PYTHON_EXIT. Nothing otherwise."""
    refusal = python_refusal(version)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        sys.exit(OLD_PYTHON_EXIT)


def install_hint(executable=None, checkout=CHECKOUT, in_venv=None):
    """How to install requirements.txt for the running Python, as a sentence. In a venv,
    its own pip. Outside one, the quick start's .venv: a system Python's pip refuses to
    install packages (PEP 668) on Debian, Ubuntu and Homebrew."""
    if in_venv is None:
        in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        return (f"Install the requirements: "
                f"{interpreter(executable, checkout)} -m pip install -r {REQUIREMENTS}")
    return (f"This Python is not a virtual environment. Make one, as the README's quick "
            f"start does: python3 -m venv {VENV} && {VENV}/bin/pip install -r "
            f"{REQUIREMENTS}, then run DIAYN as {VENV}/bin/python {ENTRY_POINT} ...")
