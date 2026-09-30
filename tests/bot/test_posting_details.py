"""
The one request the bot makes to a job board itself: salary and description
for a single posting, when somebody runs `/internships info`.

    python3 -m unittest discover -s tests      # no install needed

The URL it starts from is whatever the scraper stored, and once the scraper
runs as DIAYN's own process that is another repository's data. So the rules
pinned here are the ones that keep a stored URL from choosing where the bot
connects: https only, a host that belongs to the posting's own platform, and
for iCIMS the board's own host as the registry publishes it. A URL that fails
them is refused before any session is opened.

`contract/sample_urls.json` is the fixture the scraper's own tests require
every adapter to store exactly, so the regexes below are tested against what
the scraper really writes rather than against examples kept here.

`posting_details` imports aiohttp at module scope and a bare `python3` has
none, so it is stubbed with the surface the import touches. Nothing here makes
a request: every session is a fake.
"""

import asyncio
import json
import pathlib
import sys
import types
import unittest
from unittest import mock


def _stub_aiohttp() -> None:
    """Fakes `aiohttp`, unless the real one is installed."""
    try:
        import aiohttp  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    aiohttp.ClientTimeout = lambda **kwargs: None
    aiohttp.ClientSession = object
    aiohttp.TCPConnector = lambda **kwargs: None
    sys.modules["aiohttp"] = aiohttp


_stub_aiohttp()

import posting_details as details  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "contract"
SAMPLES = json.loads((FIXTURES / "sample_urls.json").read_text(encoding="utf-8"))["platforms"]
FETCHED = ("greenhouse", "lever", "ashby", "workday", "icims")
ICIMS_HOSTS = frozenset({SAMPLES["icims"]["slug"], "careers.rivian.com"})


class _Response:
    headers: dict = {}

    def __init__(self, payload, status=200):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._payload


class _Session:
    """Records every GET and answers each with `payload`."""

    def __init__(self, payload, opened: list, **kwargs):
        self.payload, self.kwargs, self.urls = payload, kwargs, []
        opened.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _Response(self.payload)


def fetch(platform, url, external_id="1", payload=None):
    """fetch_details against a fake session; (result, sessions opened)."""
    opened = []
    factory = lambda **kwargs: _Session(payload, opened, **kwargs)  # noqa: E731
    with mock.patch.object(details.aiohttp, "ClientSession", factory):
        out = asyncio.run(details.fetch_details(platform, url, external_id,
                                                icims_hosts=ICIMS_HOSTS))
    return out, opened


class TheStoredUrlsStillMatch(unittest.TestCase):
    def test_every_fetched_platforms_sample_is_allowed(self):
        for platform in FETCHED:
            with self.subTest(platform=platform):
                self.assertTrue(details.fetchable(platform, SAMPLES[platform]["url"], ICIMS_HOSTS))

    def test_the_regexes_match_what_the_adapters_store(self):
        for platform, regex, groups in (
                ("greenhouse", details.GH_JOB_URL_RE, ("acme", "4567890")),
                ("lever", details.LEVER_JOB_URL_RE, ("acme", "0f8a4c7e-2b1d-4e6a-9c3f-5d7b8a9e1f20")),
                ("ashby", details.ASHBY_JOB_URL_RE, ("acme",)),
                ("workday", details.WD_JOB_URL_RE,
                 ("acme", "wd1", "Acme_Careers", "/job/Irvine-CA/Software-Engineering-Intern_R12345"))):
            with self.subTest(platform=platform):
                found = regex.search(SAMPLES[platform]["url"])
                self.assertIsNotNone(found)
                self.assertEqual(found.groups(), groups)

    def test_platforms_without_a_detail_fetch_are_never_fetched(self):
        # Eightfold and Taleo have no branch: nothing to ask for, so no request.
        for platform in set(SAMPLES) - set(FETCHED):
            with self.subTest(platform=platform):
                self.assertFalse(details.fetchable(platform, SAMPLES[platform]["url"], ICIMS_HOSTS))
                out, opened = fetch(platform, SAMPLES[platform]["url"])
                self.assertEqual((out["description"], opened), (None, []))


class RefusedBeforeAnyRequest(unittest.TestCase):
    REFUSED = (
        ("lever", "https://evil.example/acme/0f8a4c7e-2b1d-4e6a-9c3f-5d7b8a9e1f20"),
        ("lever", "http://jobs.lever.co/acme/0f8a4c7e-2b1d-4e6a-9c3f-5d7b8a9e1f20"),
        ("lever", "https://jobs.lever.co.evil.example/acme/0f8a4c7e-2b1d-4e6a-9c3f-5d7b8a9e1f20"),
        ("greenhouse", "https://greenhouse.io.evil.example/acme/jobs/4567890"),
        ("greenhouse", "https://evil.example/job-boards.greenhouse.io/acme/jobs/4567890"),
        ("ashby", "https://jobs.lever.co/acme/6b1e2f3a-4c5d-4e7f-8a9b-0c1d2e3f4a5b"),
        ("workday", "https://acme.wd1.myworkdayjobs.com:8443/Acme_Careers/job/X_R1"),
        ("icims", "https://169.254.169.254/latest/meta-data/"),
        ("icims", "https://intranet.example/jobs/12345/intern/job"),
        ("icims", "file:///etc/passwd"),
        ("icims", ""),
        ("icims", None),
    )

    def test_a_host_outside_the_platform_opens_no_session(self):
        for platform, url in self.REFUSED:
            with self.subTest(platform=platform, url=url):
                self.assertFalse(details.fetchable(platform, url, ICIMS_HOSTS))
                out, opened = fetch(platform, url)
                self.assertEqual(opened, [])
                self.assertEqual((out["salary"], out["description"]), (None, None))

    def test_a_userinfo_trick_is_judged_by_the_real_host(self):
        url = "https://jobs.lever.co@evil.example/acme/0f8a4c7e-2b1d-4e6a-9c3f-5d7b8a9e1f20"
        self.assertFalse(details.fetchable("lever", url, ICIMS_HOSTS))

    def test_an_icims_board_on_its_own_careers_host_is_allowed(self):
        # The adapter supports a company careers origin fronting iCIMS; the
        # registry publishes that host, so its postings keep their details.
        self.assertTrue(details.fetchable("icims", "https://careers.rivian.com/jobs/1/intern/job",
                                          ICIMS_HOSTS))
        self.assertFalse(details.fetchable("icims", "https://careers.rivian.com/jobs/1/intern/job",
                                           frozenset()))


class WhatIsFetched(unittest.TestCase):
    def test_greenhouse_asks_its_api_for_the_parsed_slug_and_says_who_is_asking(self):
        payload = {"content": "&lt;p&gt;Build things.&lt;/p&gt;",
                   "pay_input_ranges": [{"min_cents": 3000_00, "max_cents": 4500_00,
                                         "currency_type": "USD", "title": "Hourly"}]}
        out, (session,) = fetch("greenhouse", SAMPLES["greenhouse"]["url"], payload=payload)
        self.assertEqual(session.urls, ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/4567890"])
        self.assertIn("DIAYN", session.kwargs["headers"]["User-Agent"])
        self.assertEqual(out, {"salary": "$3,000–$4,500 (Hourly)", "description": "Build things.",
                               "salary_certain": True})

    def test_icims_fetches_the_stored_page_in_its_server_rendered_view(self):
        out, (session,) = fetch("icims", SAMPLES["icims"]["url"],
                                payload="<div>Pays $25 - $30 per hour</div>")
        self.assertEqual(session.urls, [SAMPLES["icims"]["url"] + "?in_iframe=1"])
        self.assertEqual((out["salary"], out["salary_certain"]), ("$25 - $30 per hour", False))

    def test_workday_builds_its_api_from_the_parsed_url(self):
        out, (session,) = fetch("workday", SAMPLES["workday"]["url"],
                                payload={"jobPostingInfo": {"jobDescription": "<p>Hi</p>"}})
        self.assertEqual(session.urls, [
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Acme_Careers"
            "/job/Irvine-CA/Software-Engineering-Intern_R12345"])
        self.assertEqual(out["description"], "Hi")


class RedirectsAreJudgedLikeTheFirstRequest(unittest.TestCase):
    """The allowlist decides every hop, not just the first: a board could answer with a
    redirect to a private address, and its body would become the description users see."""

    def fetch_icims(self, answers):
        calls = []

        class Session(_Session):
            def get(self, url, **kwargs):
                calls.append((url, kwargs.get("allow_redirects", True)))
                status, location, body = answers[len(calls) - 1]
                response = _Response(body, status)
                response.headers = {"Location": location} if location else {}
                return response
        factory = lambda **kwargs: Session(None, [], **kwargs)  # noqa: E731
        with mock.patch.object(details.aiohttp, "ClientSession", factory):
            out = asyncio.run(details.fetch_details("icims", SAMPLES["icims"]["url"], "1",
                                                    icims_hosts=ICIMS_HOSTS))
        return out, calls

    def test_a_redirect_off_the_list_is_not_followed(self):
        out, calls = self.fetch_icims([(302, "http://169.254.169.254/latest/meta-data", None)])

        self.assertIsNone(out["description"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1])                       # aiohttp never followed it either

    def test_a_redirect_to_an_allowed_host_is_followed_by_hand(self):
        allowed = SAMPLES["icims"]["url"].split("?")[0] + "?in_iframe=1&hop=2"
        out, calls = self.fetch_icims([(302, allowed, None), (200, None, "<p>Build rockets</p>")])

        self.assertEqual(out["description"], "Build rockets")
        self.assertEqual(len(calls), 2)

    def test_the_fixed_api_hosts_never_follow_redirects(self):
        seen = []

        class Spy(_Session):
            def get(self, url, **kwargs):
                seen.append(kwargs.get("allow_redirects", True))
                return _Response({}, 200)
        with mock.patch.object(details.aiohttp, "ClientSession", lambda **k: Spy(None, [], **k)):
            asyncio.run(details.fetch_details("greenhouse", SAMPLES["greenhouse"]["url"], "1"))
        self.assertEqual(seen, [False])


class AtMostTwoAtOnce(unittest.TestCase):
    def test_concurrent_fetches_share_two_slots(self):
        inside, peak = [0], [0]

        class Slow(_Session):
            def get(self, url, **kwargs):
                outer = self

                class Response(_Response):
                    async def __aenter__(self):
                        inside[0] += 1
                        peak[0] = max(peak[0], inside[0])
                        await asyncio.sleep(0.01)
                        return self

                    async def __aexit__(self, *exc):
                        inside[0] -= 1
                        return False

                outer.urls.append(url)
                return Response({"jobPostingInfo": {}})

        async def five():
            return await asyncio.gather(*(details.fetch_details(
                "workday", SAMPLES["workday"]["url"], "1") for _ in range(5)))

        with mock.patch.object(details.aiohttp, "ClientSession",
                               lambda **kwargs: Slow(None, [], **kwargs)):
            asyncio.run(five())
        self.assertEqual(peak[0], details.CONCURRENT_FETCHES)
        self.assertEqual(details.CONCURRENT_FETCHES, 2)


if __name__ == "__main__":
    unittest.main()
