"""
diayn_commands.py
~~~~~~~~~~~~~~~~~
`/diayn`, the commands of whoever runs this bot: who may use it, and how it
is doing.

    /diayn grant user:<user>     /diayn grant server     let one person, or everyone in this server, in
    /diayn revoke user:<user>    /diayn revoke server    take that away again
    /diayn access                                         who may use it: counts, and servers by name
    /diayn debug                                          sweep health, delivery and coverage by field

Every one answers only the owner (`access.is_owner`), asked before anything
else; anyone else, a granted user included, is told it is not for them in
words that name nobody. Every reply is private and mentions nobody.

A grant or a revoke is one row of users.db's `access_grants`, the rows
`diayn.py grant` and `revoke` write without Discord. The finder reads them on
every check, so the next command sees the change, and a revoke stops alerts at
the next delivery tick and starts the 30 days after which the profile goes.
`debug` is the report `intern_commands.debug_report` builds.
"""

import time

import discord
from discord import app_commands

import access
import intern_commands
import intern_text
import intern_ui
from message_pack import MAX_CHUNK, pack

diayn = app_commands.Group(
    name="diayn", description="For whoever runs this bot: who may use it, and how it is doing",
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True,
                                                    private_channel=False))
grant = app_commands.Group(name="grant", description="Let someone use this bot", parent=diayn)
revoke = app_commands.Group(name="revoke", description="Stop someone using this bot", parent=diayn)


async def need_owner(interaction) -> bool:
    """False, after refusing privately, for anyone but whoever runs this bot."""
    if access.is_owner(interaction.user.id):
        return True
    await intern_ui.refuse(interaction, intern_text.owner_only())
    return False


def _server_name(interaction) -> str:
    return getattr(getattr(interaction, "guild", None), "name", None) or "this server"


async def _send_all(interaction, lines: list[str]) -> None:
    chunks = pack(lines, MAX_CHUNK, "\n")
    await intern_ui.refuse(interaction, chunks[0])
    for chunk in chunks[1:]:
        await intern_ui.private_send(interaction)(chunk)


# ------------------------------------------------------------------ grant and revoke

@grant.command(name="user", description="Let one person use this bot, anywhere")
@app_commands.describe(user="Who may use it")
async def grant_user(interaction: discord.Interaction, user: discord.User) -> None:
    if not await need_owner(interaction) or not await intern_ui.need_finder(interaction):
        return
    added = access.grant(intern_ui.db, "user", user.id, granted_by=interaction.user.id,
                         now=time.time())
    await intern_ui.refuse(interaction, intern_text.granted_user(user.mention, added=added))


@grant.command(name="server", description="Let everyone in this server use this bot")
async def grant_server(interaction: discord.Interaction) -> None:
    if not await need_owner(interaction) or not await intern_ui.need_finder(interaction):
        return
    if interaction.guild_id is None:
        await intern_ui.refuse(interaction, intern_text.needs_a_server("grant"))
        return
    added = access.grant(intern_ui.db, "guild", interaction.guild_id,
                         granted_by=interaction.user.id, now=time.time())
    await intern_ui.refuse(interaction, intern_text.granted_server(_server_name(interaction),
                                                                   added=added))


@revoke.command(name="user", description="Take away one person's own access")
@app_commands.describe(user="Whose access to take away")
async def revoke_user(interaction: discord.Interaction, user: discord.User) -> None:
    if not await need_owner(interaction) or not await intern_ui.need_finder(interaction):
        return
    removed = access.revoke(intern_ui.db, "user", user.id)
    still = intern_ui.may_use(user.id)          # as the owner, or through a granted server
    await intern_ui.refuse(interaction, intern_text.revoked_user(user.mention, removed=removed,
                                                                 still=still))


@revoke.command(name="server", description="Take away this server's access")
async def revoke_server(interaction: discord.Interaction) -> None:
    if not await need_owner(interaction) or not await intern_ui.need_finder(interaction):
        return
    if interaction.guild_id is None:
        await intern_ui.refuse(interaction, intern_text.needs_a_server("revoke"))
        return
    removed = access.revoke(intern_ui.db, "guild", interaction.guild_id)
    await intern_ui.refuse(interaction, intern_text.revoked_server(_server_name(interaction),
                                                                   removed=removed))


# ------------------------------------------------------------------ access and debug

@diayn.command(name="access", description="Who may use this bot: counts, and the servers by name")
async def diayn_access(interaction: discord.Interaction) -> None:
    if not await need_owner(interaction) or not await intern_ui.need_finder(interaction):
        return
    granted, client = access.grants(intern_ui.db), intern_ui.bot
    guilds = [client.get_guild(gid) if client is not None else None for gid in sorted(granted.guilds)]
    names = [guild.name for guild in guilds if guild is not None]
    await _send_all(interaction, intern_text.access_summary(
        owners=len(access.owner_ids()), users=len(granted.users), servers=names,
        gone=len(guilds) - len(names)))


@diayn.command(name="debug", description="Sweep health, delivery and coverage by field")
async def diayn_debug(interaction: discord.Interaction) -> None:
    """Counts only, and read-only (`intern_commands.debug_report`)."""
    if not await need_owner(interaction):
        return
    await intern_ui.defer_reply(interaction)
    for chunk in pack(await intern_commands.debug_report(), MAX_CHUNK, "\n"):
        await intern_ui.private_send(interaction)(chunk)


@diayn.error
async def _on_diayn_error(interaction: discord.Interaction, error: Exception) -> None:
    name = getattr(interaction.command, "qualified_name", "diayn")
    await intern_ui.fail_softly(interaction, f"/{name}", error)
