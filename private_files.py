"""
private_files.py
~~~~~~~~~~~~~~~~
How DIAYN makes the directories and the databases that hold its data: each
directory at mode 700 and each database at mode 600, so that nobody else on the
box can read them. users.db holds Discord ids and everyone's profile.

Whichever command is first to make the data directory or a database makes it
through here: `setup`, `grant` and `import-legacy`, `run` for users.db, and the
scraper's `sweep --init` and `watch --init` (and `discover`, for the directory
its files go in). sqlite gives a database's -journal, -wal and -shm the
database's own mode as it makes them, so they follow it.

A directory or a file that is already there keeps its mode, except through
tighten, which is setup's: it takes away what others on the box could read in
the data directory, users.db and postings.db that an earlier command, or an
earlier version, left loose. doctor only reports it.

Importing this module does nothing. It stays within what Python 3.9 runs, as
hints.py does, since the scraper imports it.
"""

import contextlib
import os
import stat
import threading

#: A directory only its owner can list or enter.
DIRECTORY_MODE = 0o700
#: Every permission bit for the group and for others.
GROUP_AND_OTHERS = 0o077
#: What the umask takes away while something is made: all of those. sqlite makes a
#: database at 644 less the umask, so 600.
PRIVATE_UMASK = GROUP_AND_OTHERS
#: The files sqlite keeps beside a database, which take its mode as it makes them.
SIDECARS = ("-journal", "-wal", "-shm")

# The umask belongs to the process, not to a thread. One block at a time changes
# it, so that a block ending puts back the umask from before any began; reentrant,
# so that make_directory can run inside a block.
_UMASK_LOCK = threading.RLock()


@contextlib.contextmanager
def private_umask():
    """
    For the block, umask 077: whatever it makes is its owner's alone. sqlite3.connect
    makes a database that is not there as it opens it, so a connect in the block makes
    one at mode 600. The umask from before is put back however the block ends.
    """
    with _UMASK_LOCK:
        before = os.umask(PRIVATE_UMASK)
        try:
            yield
        finally:
            os.umask(before)


def make_directory(path) -> bool:
    """
    Makes the directory `path`, and each parent of it that is missing, at mode 700;
    returns whether it made `path`. One that is already there is left as it is.
    Raises OSError, FileExistsError among them when a file is in the way.

    The chmod after makedirs is for a parent whose default ACL would otherwise
    decide the mode, which the umask does not.
    """
    if os.path.isdir(path):
        return False
    with private_umask():
        os.makedirs(path, mode=DIRECTORY_MODE, exist_ok=True)
    os.chmod(path, DIRECTORY_MODE)
    return True


def database_files(path) -> tuple:
    """The database at `path`, then the files sqlite keeps beside it."""
    return (path,) + tuple(path + suffix for suffix in SIDECARS)


def tighten(path):
    """
    Takes every permission for the group and for others off the directory or the file
    at `path`, when it is there and has any; returns the mode it had, or None when
    there was nothing to take. It never adds a bit: 755 becomes 700, 644 becomes 600,
    and 444 becomes 400. Raises OSError when the chmod is refused.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return None
    if not mode & GROUP_AND_OTHERS:
        return None
    os.chmod(path, mode & ~GROUP_AND_OTHERS)
    return mode
