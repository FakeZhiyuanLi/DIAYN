"""
app.py
~~~~~~
DIAYN's Discord client: the one place the internship finder is wired to
Discord. Everything the finder does lives in `intern_*.py`; this module decides
whether any of it runs.

    client = app.build()                 # both databases open, nothing contacted
    await app.serve(client, token)       # logs in and runs until stopped

`build()` reads the scraper's bound settings, `internship_poller.SETTINGS`, so
the scraper's boot() must have run first: the finder's clock (DIAYN_TZ), its
User-Agent (POLL_CONTACT) and its owner check (DIAYN_OWNER_IDS) read the same
settings, and the files opened here must be the ones they describe.

**Two databases, and each fails alone.** postings.db is the scraper's: it is
opened read-only under the contract (`postings_source`), never created, and a
file that is missing or wrong turns off the tracker, not the bot. users.db is
the finder's own (`intern_store`): one that cannot be opened or made turns off
the finder, and `/internships recent` and `info` still work.

**Two command groups**: the finder's `/internships`, and the owner's `/diayn`
(`diayn_commands`), which grants and revokes access and carries the debug
report. Both are added whenever the client is built.

**Once, after login** (`setup_hook`): the application's owners are recorded
for `access`, the global commands are synced (keeping Discord's own Entry
Point command), and, with the finder on, the persistent buttons are registered
and the delivery loop starts. Nothing here sweeps: the scraper does, in a task
of its own.

**What Discord is asked for.** The default intents plus members, which is
privileged: the finder uses it to notice someone leaving every server it shares
with them. No message content and no presences; there are no prefix commands.

**No link previews.** Job listings carry links, and Discord would draw a card
for each, burying the text. Every message this client sends defaults to
`suppress_embeds=True` (`install_send_defaults`), scoped to this client:
anything sent for another client is left alone.

Importing this module opens nothing, builds nothing and patches nothing.
"""

import dataclasses
import functools
import sqlite3
import sys
import weakref

import discord
from discord import app_commands

import access
import diayn_commands
import intern_commands
import intern_fit
import intern_store
import intern_ui
import postings_source
import private_files

#: Discord's own command for an application with Activities: the entry the app
#: launcher shows. discord.py has no notion of it (see sync_global_commands).
ENTRY_POINT_COMMAND_TYPE = 4
#: The three ways the finder sends: a DM, a first reply, and a followup.
SEND_PATHS = ((discord.abc.Messageable, "send"),
              (discord.InteractionResponse, "send_message"),
              (discord.Webhook, "send"))
_WRAPPED = "_diayn_no_embeds"
#: The clients whose messages default to no link previews.
_suppressing: "weakref.WeakSet[discord.Client]" = weakref.WeakSet()


# ------------------------------------------------------------------ what Discord is asked for

def intents() -> discord.Intents:
    """The default intents plus members (privileged; the portal must enable it)."""
    wanted = discord.Intents.default()
    wanted.members = True
    wanted.message_content = False
    wanted.presences = False
    return wanted


# ------------------------------------------------------------------ no link previews

def _client_of(target) -> "discord.Client | None":
    """The client a Messageable, an InteractionResponse or a Webhook belongs to, or None.
    Reads discord.py's connection state, as of 2.7.1."""
    state = getattr(target, "_state", None)
    if state is None:
        state = getattr(getattr(target, "_parent", None), "_state", None)
    get_client = getattr(state, "_get_client", None)
    return get_client() if callable(get_client) else None


def _no_embeds(send):
    @functools.wraps(send)
    async def wrapper(self, *args, **kwargs):
        if (not kwargs.get("embed") and not kwargs.get("embeds")
                and _client_of(self) in _suppressing):
            kwargs.setdefault("suppress_embeds", True)
        return await send(self, *args, **kwargs)
    setattr(wrapper, _WRAPPED, True)
    return wrapper


def install_send_defaults(client: discord.Client) -> None:
    """Every message `client` sends defaults to no link previews. A caller can still pass
    suppress_embeds=False, and a message with an embed of its own is left alone. The send
    paths are wrapped once, however many clients ask."""
    for cls, name in SEND_PATHS:
        send = getattr(cls, name)
        if not getattr(send, _WRAPPED, False):
            setattr(cls, name, _no_embeds(send))
    _suppressing.add(client)


# ------------------------------------------------------------------ the two databases

@dataclasses.dataclass(frozen=True)
class Stores:
    """What `build` opened, and why anything it could not open is off."""
    db: sqlite3.Connection | None           # users.db, the finder's own
    intern_error: str | None                # why the finder is off; None when it is on
    pconn: sqlite3.Connection | None        # postings.db, read-only
    source: postings_source.Source | None
    pconn_error: str | None                 # why the tracker is off; None when it is on
    postings_path: str                      # where the finder reopens postings.db from


def _open_users(path: str) -> tuple[sqlite3.Connection | None, str | None]:
    """(users.db, None), or (None, why) after saying so. The finder's tables, the
    access grants and the fit check's cache and budget are made here; a failure turns
    off only the finder (W1). A users.db that is not there yet is made at mode 600: on
    a host that went from setup straight to run, this is what makes it."""
    db = None
    try:
        with private_files.private_umask():
            db = sqlite3.connect(path)
        intern_store.init_db(db)
        access.init_db(db)
        intern_fit.init_db(db)
        return db, None
    except sqlite3.Error as e:
        postings_source.close_quietly(db)
        error = f"{type(e).__name__}: {e}"
        print(f"internship finder disabled: {error}", file=sys.stderr)
        return None, error


def open_stores(settings) -> Stores:
    """Opens postings.db (read-only, never created) and users.db, at the paths `settings`
    name. Never raises: whatever cannot be opened is off, and the log says why."""
    pconn, source, pconn_error = postings_source.open_from_env(settings)
    if pconn_error:
        print(f"internship tracker disabled: {pconn_error}", file=sys.stderr)
    db, intern_error = _open_users(settings.users_db)
    return Stores(db=db, intern_error=intern_error, pconn=pconn, source=source,
                  pconn_error=pconn_error, postings_path=settings.postings_db)


def wire(client: discord.Client, stores: Stores) -> None:
    """Hands the finder what it shares. intern_ui never imports this module, which would
    be a cycle and would build a client, so the client hands it over (W10)."""
    intern_ui.db = stores.db
    intern_ui.pconn = stores.pconn
    intern_ui.pconn_error = stores.pconn_error
    intern_ui.source = stores.source
    intern_ui.postings_path = stores.postings_path
    intern_ui.bot = client
    intern_ui.intern_error = stores.intern_error


# ------------------------------------------------------------------ commands

async def sync_global_commands(client: discord.Client) -> None:
    """
    Bulk-syncs the global commands, keeping Discord's own Entry Point command. Discord
    creates it for an application with Activities; a plain tree.sync() leaves it out of
    the bulk payload, which Discord reads as a request to delete it, and it refuses the
    whole update (error 50240).

    Reaches into discord.py internals, because 2.7.1 has no public way to do this:
    `_get_all_commands`, `get_translated_payload` and `to_dict` have all changed shape
    across the 2.x line, so a dependency bump can break this. Written against 2.7.1.
    """
    tree = client.tree
    commands = tree._get_all_commands(guild=None)
    translator = tree.translator
    if translator:
        payload = [await c.get_translated_payload(tree, translator) for c in commands]
    else:
        payload = [c.to_dict(tree) for c in commands]
    existing = await client.http.get_global_commands(client.application_id)
    payload += [c for c in existing if c.get("type") == ENTRY_POINT_COMMAND_TYPE]
    await client.http.bulk_upsert_global_commands(client.application_id, payload=payload)


# ------------------------------------------------------------------ the client

class DiaynBot(discord.Client):
    """The finder's client. Built with the stores it runs on; logs in only in `serve`."""

    def __init__(self, stores: Stores) -> None:
        super().__init__(intents=intents())
        self.stores = stores
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(intern_commands.internships)
        self.tree.add_command(diayn_commands.diayn)
        install_send_defaults(self)

    async def setup_hook(self) -> None:
        # Once per process, after login and before the gateway connects: on_ready fires
        # again on every reconnect, and global command writes are rate limited.
        access.set_application_owners(access.application_owners(self.application))
        try:
            await sync_global_commands(self)
        except Exception as error:      # stale commands are no reason to stop the finder
            print(f"command sync failed: {type(error).__name__}; "
                  "commands may be stale until restart", file=sys.stderr)
        # Buttons on messages sent before a restart stay clickable (W9). The delivery
        # loop needs only the finder's own tables: with postings.db down it still runs
        # the housekeeping that deletes idle and departed users' profiles (W3).
        if self.stores.intern_error is None:
            for view in intern_commands.persistent_views():
                self.add_view(view)
            if not intern_commands.intern_delivery_loop.is_running():
                intern_commands.intern_delivery_loop.start()

    async def on_ready(self) -> None:
        print(f"DIAYN is logged in as {self.user}", file=sys.stderr)

    # Leaving the last server the bot shares pauses a person's alerts and starts the
    # 30-day clock on their profile; coming back stops it (W8). intern_commands decides
    # which applies, and does nothing for someone without a profile.
    async def on_member_remove(self, member: discord.Member) -> None:
        if self.stores.intern_error is None and not member.bot:
            intern_commands.member_left(member)

    async def on_member_join(self, member: discord.Member) -> None:
        if self.stores.intern_error is None and not member.bot:
            intern_commands.member_joined(member)


def build() -> DiaynBot:
    """The client, with both databases open and the finder handed them; not logged in.
    Reads the scraper's bound settings, so its boot() must have run (module docstring)."""
    import internship_poller
    stores = open_stores(internship_poller.SETTINGS)
    client = DiaynBot(stores)
    wire(client, stores)
    return client


async def serve(client: DiaynBot, token: str | None) -> None:
    """Logs `client` in with the bot's token and runs it until it is stopped. A missing
    token is refused before anything is contacted."""
    if not token or not token.strip():
        raise ValueError("DISCORD_TOKEN is not set: the bot's token, from the developer portal")
    async with client:
        await client.start(token.strip())
