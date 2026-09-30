"""
`discord_portal`: what `diayn.py setup` and `doctor` ask Discord about the
host's own application, and the link that invites its bot to a server.

    python3 -m unittest discover -s tests      # no install needed

Nothing here reaches Discord. Every request goes to a fake session that
answers from a table and records what it was asked, so the tests can check the
token went only where it should, and never into what is printed.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiohttp_stub import stub_aiohttp  # noqa: E402

stub_aiohttp()

import aiohttp  # noqa: E402

import discord_portal as portal  # noqa: E402

TOKEN = "not-a-real-token.for-these-tests"
AGENT = "DiscordBot (https://example.invalid/diayn, 9.9.9)"
APP_ID = "123456789012345678"
USER = {"id": "223456789012345678", "username": "diayn-test", "discriminator": "0420",
        "bot": True}
#: The portal's Server Members Intent toggle, as the flags show it for an unverified bot.
MEMBERS_LIMITED = 1 << 15
#: The same intent, for a verified bot.
MEMBERS_VERIFIED = 1 << 14


def application(flags=MEMBERS_LIMITED, public=False, **extra):
    return {"id": APP_ID, "name": "DIAYN test", "flags": flags, "bot_public": public, **extra}


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def json(self):
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers GETs from `answers`, {path: (status, body) or an exception to raise}."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def get(self, url, headers=None):
        self.requests.append((url, dict(headers or {})))
        answer = self.answers[url[len(portal.API):]]
        if isinstance(answer, BaseException):
            raise answer
        return FakeResponse(*answer)


class LateResponse(FakeResponse):
    """A FakeResponse that arrives `delay` seconds after it is asked for."""

    def __init__(self, status, body, delay):
        super().__init__(status, body)
        self.delay = delay

    async def __aenter__(self):
        await asyncio.sleep(self.delay)
        return self


class SlowSession(FakeSession):
    """A FakeSession whose every answer is `delay` seconds late."""

    def __init__(self, answers, delay):
        super().__init__(answers)
        self.delay = delay

    def get(self, url, headers=None):
        answer = super().get(url, headers)
        return LateResponse(answer.status, answer.body, self.delay)


def answers(user=(200, USER), app=None):
    return {"/users/@me": user,
            "/oauth2/applications/@me": app if app is not None else (200, application())}


def fetch(session, token=TOKEN):
    return asyncio.run(portal.fetch_application(token, user_agent=AGENT, session=session))


class TheRequests(unittest.TestCase):
    def test_asks_for_the_bot_user_and_the_application_with_the_bot_token(self):
        session = FakeSession(answers())
        fetch(session)
        self.assertEqual(sorted(url for url, _ in session.requests),
                         [portal.API + "/oauth2/applications/@me", portal.API + "/users/@me"])
        for _, headers in session.requests:
            self.assertEqual(headers["Authorization"], f"Bot {TOKEN}")
            self.assertEqual(headers["User-Agent"], AGENT)

    def test_the_token_is_stripped_of_the_whitespace_a_paste_brings(self):
        session = FakeSession(answers())
        fetch(session, token=f"  {TOKEN}\n")
        self.assertEqual({h["Authorization"] for _, h in session.requests}, {f"Bot {TOKEN}"})

    def test_an_empty_token_is_refused_without_a_request(self):
        session = FakeSession(answers())
        for token in (None, "", "   "):
            with self.subTest(token=token), self.assertRaises(portal.PortalError) as caught:
                fetch(session, token=token)
            self.assertIn("DISCORD_TOKEN", str(caught.exception))
        self.assertEqual(session.requests, [])

    def test_the_api_is_discords_own_over_https(self):
        self.assertTrue(portal.API.startswith("https://discord.com/api/v"))

    def test_the_user_agent_is_the_form_discord_asks_bots_for(self):
        self.assertEqual(portal.user_agent("https://example.invalid/diayn", "9.9.9"), AGENT)


class WhatItReads(unittest.TestCase):
    def test_reads_the_application_and_its_bot(self):
        found = fetch(FakeSession(answers()))
        self.assertEqual(found.id, APP_ID)
        self.assertEqual(found.name, "DIAYN test")
        self.assertEqual(found.bot_name, "diayn-test#0420")
        self.assertTrue(found.members_intent)
        self.assertFalse(found.public)

    def test_a_bot_without_a_discriminator_is_named_by_its_username(self):
        user = dict(USER, discriminator="0")
        self.assertEqual(fetch(FakeSession(answers(user=(200, user)))).bot_name, "diayn-test")

    def test_the_members_intent_is_on_with_either_flag_and_off_with_neither(self):
        for flags, on in ((MEMBERS_LIMITED, True), (MEMBERS_VERIFIED, True),
                          (MEMBERS_LIMITED | MEMBERS_VERIFIED, True), (0, False),
                          ((1 << 12) | (1 << 18), False)):
            with self.subTest(flags=flags):
                found = fetch(FakeSession(answers(app=(200, application(flags=flags)))))
                self.assertIs(found.members_intent, on)

    def test_missing_flags_read_as_no_intent(self):
        app = application()
        del app["flags"]
        self.assertFalse(fetch(FakeSession(answers(app=(200, app)))).members_intent)

    def test_a_public_bot_is_reported_as_public(self):
        self.assertTrue(fetch(FakeSession(answers(app=(200, application(public=True))))).public)

    def test_the_id_is_never_in_the_repr(self):
        # The invite link needs the id; a stray print of the object does not.
        self.assertNotIn(APP_ID, repr(fetch(FakeSession(answers()))))


class WhenDiscordSaysNo(unittest.TestCase):
    def refusal(self, session) -> str:
        with self.assertRaises(portal.PortalError) as caught:
            fetch(session)
        text = str(caught.exception)
        self.assertNotIn(TOKEN, text)
        return text

    def test_a_refused_token_says_where_to_get_a_new_one(self):
        text = self.refusal(FakeSession(answers(user=(401, {"message": "401: Unauthorized"}))))
        self.assertIn("401", text)
        self.assertIn("Reset Token", text)

    def test_a_rate_limit_says_to_wait(self):
        text = self.refusal(FakeSession(answers(app=(429, {"retry_after": 3}))))
        self.assertIn("429", text)
        self.assertIn("minute", text)

    def test_any_other_status_is_named(self):
        self.assertIn("HTTP 503", self.refusal(FakeSession(answers(app=(503, {})))))

    def test_no_network_is_named_by_its_class(self):
        for error in (asyncio.TimeoutError(), aiohttp.ClientError(TOKEN), OSError(TOKEN)):
            with self.subTest(error=type(error).__name__):
                text = self.refusal(FakeSession(answers(user=error)))
                self.assertIn("could not reach Discord", text)
                self.assertIn(type(error).__name__, text)

    def test_an_answer_that_does_not_parse_is_refused(self):
        for body in (ValueError("not json"), ["a", "list"], {"name": "no id"},
                     {"id": "12ab", "flags": 0}, {"id": APP_ID, "flags": "lots"}):
            with self.subTest(body=body):
                text = self.refusal(FakeSession(answers(app=(200, body))))
                self.assertIn("did not parse", text)


class TheDeadline(unittest.TestCase):
    def test_both_requests_together_must_finish_within_it(self):
        # Each answer alone is inside the limit; the two together are not.
        with mock.patch.object(portal, "TIMEOUT_S", 0.3), \
                self.assertRaises(portal.PortalError) as caught:
            fetch(SlowSession(answers(), delay=0.2))
        self.assertEqual(str(caught.exception), "could not reach Discord: TimeoutError")

    def test_answers_inside_it_are_read(self):
        with mock.patch.object(portal, "TIMEOUT_S", 1.0):
            self.assertEqual(fetch(SlowSession(answers(), delay=0.01)).id, APP_ID)


class TheInviteLink(unittest.TestCase):
    def test_asks_for_the_bot_and_its_commands_and_no_server_permission(self):
        link = urlsplit(portal.invite_url(APP_ID))
        self.assertEqual((link.scheme, link.netloc, link.path),
                         ("https", "discord.com", "/oauth2/authorize"))
        self.assertEqual(parse_qs(link.query),
                         {"client_id": [APP_ID], "scope": ["bot applications.commands"],
                          "permissions": ["0"]})

    def test_the_scopes_are_joined_as_discord_expects(self):
        self.assertIn("scope=bot+applications.commands", portal.invite_url(APP_ID))

    def test_the_permissions_are_none_at_all(self):
        # Every reply is an interaction response and every alert a DM, and
        # neither needs a permission in the server.
        self.assertEqual(portal.INVITE_PERMISSIONS, 0)


if __name__ == "__main__":
    unittest.main()
