"""
discord_portal.py
~~~~~~~~~~~~~~~~~
What `diayn.py setup` and `diayn.py doctor` ask Discord about the host's own
application, before the bot ever logs in: whether DISCORD_TOKEN is a bot token
Discord accepts, whether the Server Members Intent is on, and whether anyone
may add the bot to a server. And the link that invites it.

    app = await discord_portal.fetch_application(token, user_agent=...)
    discord_portal.invite_url(app.id)

Two GETs with the bot's token, over aiohttp: `/users/@me`, the bot's own user,
and `/oauth2/applications/@me`, its application. Nothing is written to
Discord and nothing connects to the gateway, so discord.py is not needed.
These are Discord's own servers, not a job board's, so the politeness gate
does not apply.

**Nothing here repeats the token.** A failure is named by its HTTP status or
its class, never by a message that could carry a header. The invite link holds
the application's id, which is the bot's public identity: Discord shows it to
anyone the link is given to. It is kept out of the repr all the same.

Importing this module does nothing.
"""

import asyncio
import dataclasses
from urllib.parse import urlencode

import aiohttp

API = "https://discord.com/api/v10"
USER_PATH = "/users/@me"
APPLICATION_PATH = "/oauth2/applications/@me"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
#: The Server Members Intent: GATEWAY_GUILD_MEMBERS (1 << 14) once a bot is
#: verified, GATEWAY_GUILD_MEMBERS_LIMITED (1 << 15) until then. The portal's
#: toggle sets one of them.
MEMBERS_INTENT_FLAGS = (1 << 14) | (1 << 15)
INVITE_SCOPES = ("bot", "applications.commands")
#: No permission in the server at all: every reply the bot makes is an
#: interaction response, and every alert a DM, and neither needs one.
INVITE_PERMISSIONS = 0
#: Seconds both requests together may take: one deadline for the pair, and
#: aiohttp's own limit on each request besides.
TIMEOUT_S = 15
#: What Discord answers for a discriminator a username no longer carries.
NO_DISCRIMINATOR = ("", "0", None)


class PortalError(Exception):
    """Discord did not confirm the token and the application. The text says why, and
    never carries the token."""


@dataclasses.dataclass(frozen=True)
class Application:
    """The host's application, as setup and doctor report it."""
    id: str = dataclasses.field(repr=False)     # for the invite link
    name: str
    bot_name: str
    members_intent: bool
    public: bool                                # anyone with the link may add the bot


def user_agent(project_url: str, version: str) -> str:
    """The User-Agent Discord asks a bot's own HTTP requests to carry."""
    return f"DiscordBot ({project_url}, {version})"


def invite_url(application_id: str) -> str:
    """The link that adds the bot, with its commands, to a server, asking for no permission."""
    query = urlencode({"client_id": application_id, "scope": " ".join(INVITE_SCOPES),
                       "permissions": INVITE_PERMISSIONS})
    return f"{AUTHORIZE_URL}?{query}"


def _status_error(status: int, path: str) -> PortalError:
    if status == 401:
        return PortalError(
            "Discord refused DISCORD_TOKEN (HTTP 401): it is not a bot token Discord "
            "accepts. In the developer portal, open your application, then Bot, then "
            "Reset Token, and put the new token in .env.")
    if status == 429:
        return PortalError("Discord is rate limiting this address (HTTP 429). "
                           "Try again in a minute.")
    return PortalError(f"Discord answered HTTP {status} for {path}.")


async def _get(session, path: str, headers: dict) -> object:
    async with session.get(API + path, headers=headers) as response:
        if response.status != 200:
            raise _status_error(response.status, path)
        try:
            return await response.json()
        except (ValueError, aiohttp.ClientError):
            raise _unparsed(path) from None


def _unparsed(path: str) -> PortalError:
    return PortalError(f"Discord's answer for {path} did not parse.")


def _bot_name(user: object) -> str:
    if not isinstance(user, dict) or not isinstance(user.get("username"), str):
        raise _unparsed(USER_PATH)
    tag = user.get("discriminator")
    return user["username"] if tag in NO_DISCRIMINATOR else f"{user['username']}#{tag}"


def _application(app: object, bot_name: str) -> Application:
    if not isinstance(app, dict):
        raise _unparsed(APPLICATION_PATH)
    app_id, flags = app.get("id"), app.get("flags") or 0
    if not (isinstance(app_id, str) and app_id.isascii() and app_id.isdigit()
            and isinstance(flags, int)):
        raise _unparsed(APPLICATION_PATH)
    return Application(id=app_id, name=str(app.get("name") or ""), bot_name=bot_name,
                       members_intent=bool(flags & MEMBERS_INTENT_FLAGS),
                       public=bool(app.get("bot_public")))


async def _ask(session, headers: dict) -> Application:
    user = await _get(session, USER_PATH, headers)
    app = await _get(session, APPLICATION_PATH, headers)
    return _application(app, _bot_name(user))


async def fetch_application(token: str | None, *, user_agent: str,
                            session=None) -> Application:
    """
    The application `token` belongs to, as Discord reports it; PortalError when the
    token is missing, refused, or Discord cannot be reached or understood. `session`
    is an aiohttp session, for the tests; by default one is made, and closed, here.
    """
    token = (token or "").strip()
    if not token:
        raise PortalError("DISCORD_TOKEN is not set: the bot's token, from the developer "
                          "portal (your application, then Bot, then Reset Token).")
    headers = {"Authorization": f"Bot {token}", "User-Agent": user_agent}
    try:
        if session is not None:
            return await asyncio.wait_for(_ask(session, headers), TIMEOUT_S)
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as own:
            return await asyncio.wait_for(_ask(own, headers), TIMEOUT_S)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as error:
        raise PortalError(f"could not reach Discord: {type(error).__name__}") from None
