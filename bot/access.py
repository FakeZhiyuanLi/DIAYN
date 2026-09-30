"""
access.py
~~~~~~~~~
Who may use this bot. It is private: the owner may, and so may whoever the
owner grants access to. There is no open mode.

**The owner** is whoever DIAYN_OWNER_IDS names. When it names nobody, it is the
owner of the Discord application the bot logs in as: its single owner, or the
admins and developers of the team that owns it, the same people discord.py's
own `Bot.is_owner` accepts. `app.py` records those at start-up, once it has
logged in and read the application (`set_application_owners`). Until then, and
with neither known, nobody is the owner: this check fails closed. The owner's
own commands are `/diayn`'s, and only the owner grants.

**A grant** is one row of users.db's `access_grants`: one user, by id, or one
whole server. `allowed` is the whole policy (plan 3.3). A person may use the
bot when they are the owner, when they were granted by id, when they are using
it inside a granted server, or, in a DM, when they are a member of a granted
server, which is where that member's alerts arrive. Inside a server with no
grant, membership of another server lets nobody in.

The policy is pure. It reads a snapshot of the grants (`grants`) and is handed
the membership lookup, which only the caller can do, because it needs the
Discord client's member cache; it is asked only when nothing else settles it.

DIAYN_OWNER_IDS is read from the scraper's settings, `internship_poller.SETTINGS`,
which its configure() checked and its boot() bound, on every call, so a test
changes it by replacing SETTINGS.

Importing this module reads nothing, and it never imports discord: the
application is read by attribute, so the tests hand it plain objects.
"""

import dataclasses
import sqlite3
from collections.abc import Callable, Iterable

#: Team roles that own the application, as discord.TeamMemberRole spells them.
_OWNING_ROLES = frozenset({"admin", "developer"})

#: The application's owners, recorded at start-up; empty until then.
_application_owners: frozenset[int] = frozenset()
#: What one grant covers: one user, or everyone in (or a member of) one server.
KINDS = ("user", "guild")
#: The largest id SQLite's INTEGER holds; a Discord id (a snowflake) is far below it.
_MAX_ID = 2 ** 63 - 1

_GRANTS_DDL = """
    CREATE TABLE IF NOT EXISTS access_grants (
        kind       TEXT    NOT NULL CHECK (kind IN ('user','guild')),
        id         INTEGER NOT NULL,
        granted_by INTEGER,
        granted_at REAL    NOT NULL,
        PRIMARY KEY (kind, id)
    )
"""


def _is_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ids(values: Iterable[object]) -> frozenset[int]:
    return frozenset(v for v in values if _is_id(v))


def application_owners(app) -> frozenset[int]:
    """The owners of the application `app` (a discord.AppInfo) describes: the admins and
    developers of its team when a team owns it, else its one owner."""
    team = getattr(app, "team", None)
    if team is not None:
        return _ids(m.id for m in team.members
                    if getattr(m.role, "value", m.role) in _OWNING_ROLES)
    owner = getattr(app, "owner", None)
    return _ids((owner.id,)) if owner is not None else frozenset()


def set_application_owners(ids: Iterable[object]) -> None:
    """Records the application's owners (`application_owners`); `app.py` calls it at start-up."""
    global _application_owners
    _application_owners = _ids(ids)


def owner_ids() -> frozenset[int]:
    """DIAYN_OWNER_IDS when it names anyone, else the application's owners."""
    import internship_poller
    configured = internship_poller.SETTINGS.owner_ids
    return _ids(configured) if configured else _application_owners


def is_owner(user_id: object) -> bool:
    """True for whoever runs this bot (module docstring); False for anyone else, and for
    anything that is not a Discord user id."""
    return _is_id(user_id) and user_id in owner_ids()


# ------------------------------------------------------------------ grants

@dataclasses.dataclass(frozen=True)
class Grants:
    """Who has been granted access, as users.db holds it at one moment."""
    users: frozenset[int] = frozenset()
    guilds: frozenset[int] = frozenset()


#: Whether a person is a member of any of the servers given; the caller builds one per person.
MemberOf = Callable[[frozenset[int]], bool]


def _check(kind: object, target: object) -> None:
    if kind not in KINDS:
        raise ValueError(f"a grant is for one of {KINDS}, not {kind!r}")
    if not _is_id(target) or not 0 < target <= _MAX_ID:
        raise ValueError(f"a {kind} grant needs a Discord id, not {target!r}")


def init_db(db: sqlite3.Connection) -> None:
    """Creates `access_grants` in users.db if absent. Safe on every boot; raises sqlite3.Error."""
    db.execute(_GRANTS_DDL)
    db.commit()


def grant(db: sqlite3.Connection, kind: str, target: int, *, granted_by: int | None,
          now: float) -> bool:
    """
    Lets `target` in: one user, or everyone in one server (`kind` "user" or "guild").
    `granted_by` is the owner who did it, or None from the command line. True when
    this made a new grant; False when there already was one, which is left as it was.
    """
    _check(kind, target)
    if granted_by is not None and not _is_id(granted_by):
        raise ValueError(f"granted_by is a Discord id or None, not {granted_by!r}")
    with db:
        return db.execute("INSERT OR IGNORE INTO access_grants (kind, id, granted_by, granted_at) "
                          "VALUES (?, ?, ?, ?)", (kind, target, granted_by, now)).rowcount == 1


def revoke(db: sqlite3.Connection, kind: str, target: int) -> bool:
    """Takes a grant away. True when there was one to take."""
    _check(kind, target)
    with db:
        return db.execute("DELETE FROM access_grants WHERE kind = ? AND id = ?",
                          (kind, target)).rowcount == 1


def grants(db: sqlite3.Connection | None) -> Grants:
    """Every grant in users.db, read now; none without users.db. Raises sqlite3.Error."""
    if db is None:
        return Grants()
    rows = db.execute("SELECT kind, id FROM access_grants").fetchall()
    return Grants(users=_ids(i for kind, i in rows if kind == "user"),
                  guilds=_ids(i for kind, i in rows if kind == "guild"))


def user_granted(db: sqlite3.Connection | None, user_id: int) -> bool:
    """Whether `user_id` holds a grant by id: the owner's record, which a person deleting
    their profile does not delete. False without users.db. Raises sqlite3.Error."""
    if db is None:
        return False
    return db.execute("SELECT 1 FROM access_grants WHERE kind = 'user' AND id = ?",
                      (user_id,)).fetchone() is not None


def allowed(granted: Grants, user_id: object, guild_id: object,
            member_of_granted_guild: MemberOf) -> bool:
    """
    Whether `user_id` may use this bot, here: inside the server `guild_id`, or in a DM
    (None). The owner and anyone granted by id may, anywhere; inside a server, anyone
    may when that server has a grant; in a DM, a member of a granted server may.
    `member_of_granted_guild` is asked, with the granted servers, only in a DM and only
    when nothing else has settled it.
    """
    if not _is_id(user_id):
        return False
    if is_owner(user_id) or user_id in granted.users:
        return True
    if guild_id is not None:
        return _is_id(guild_id) and guild_id in granted.guilds
    return bool(granted.guilds) and bool(member_of_granted_guild(granted.guilds))
