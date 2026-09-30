"""
access.py
~~~~~~~~~
Who runs this bot. The owner may use the owner's commands (for now,
`/internships debug`), and will be the one who grants others access.

The owner is whoever DIAYN_OWNER_IDS names. When it names nobody, it is the
owner of the Discord application the bot logs in as: its single owner, or the
admins and developers of the team that owns it, the same people discord.py's
own `Bot.is_owner` accepts. `app.py` records those at start-up, once it has
logged in and read the application (`set_application_owners`). Until then, and
with neither known, nobody is the owner: this check fails closed.

DIAYN_OWNER_IDS is read from the scraper's settings, `internship_poller.SETTINGS`,
which its configure() checked and its boot() bound, on every call, so a test
changes it by replacing SETTINGS.

Importing this module reads nothing, and it never imports discord: the
application is read by attribute, so the tests hand it plain objects.
"""

from collections.abc import Iterable

#: Team roles that own the application, as discord.TeamMemberRole spells them.
_OWNING_ROLES = frozenset({"admin", "developer"})

#: The application's owners, recorded at start-up; empty until then.
_application_owners: frozenset[int] = frozenset()


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
