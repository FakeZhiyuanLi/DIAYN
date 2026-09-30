"""
The resume upload path driven end to end with fakes: which screen comes first,
who pays for a failed read, and what the refusals say.

    .venv/bin/python -m unittest discover -s tests

`test_intern_surface.py` pins the order of calls by reading the source; this
file presses the buttons. It exists because the source rules were green while
a refused upload could still reach the upload modal without the disclosure
ever being shown, and store a consent the member never gave. The worker is
replaced by a fake, so nothing here spawns a process or reads a real file.
Skipped where discord.py is not installed.
"""

import asyncio
import copy
import dataclasses
import sqlite3
import types
import unittest
from unittest import mock

try:
    import discord
except ModuleNotFoundError as missing:  # pragma: no cover - depends on the environment
    if missing.name != "discord":
        raise
    discord = None

#: See test_intern_surface.REAL_DISCORD: a stub module has no __file__.
REAL_DISCORD = discord is not None and getattr(discord, "__file__", None) is not None
if REAL_DISCORD:
    import access
    import intern_store
    import intern_text
    import intern_ui
    import intern_upload
    import intern_views
    import rate_limit
    import resume_parse
    from intern_profile import DISCLOSURE_VERSION, new_profile

needs_discord = unittest.skipUnless(REAL_DISCORD, "discord.py is not installed")

UID = 4242
RESUME = (b"Education\nUniversity of Somewhere\n"
          b"Bachelor of Science in Mechanical Engineering, Expected June 2028\n"
          b"Skills\nPython, SolidWorks, MATLAB\n")


class Response:
    def __init__(self) -> None:
        self.sent, self.modals, self.edits, self.done = [], [], [], False

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))
        self.done = True

    async def send_modal(self, modal):
        self.modals.append(modal)
        self.done = True

    async def defer(self, **kw):
        self.done = True

    async def edit_message(self, **kw):
        self.edits.append(kw)
        self.done = True


class Attachment:
    def __init__(self, name: str, content_type: str, data: bytes, size: int | None = None) -> None:
        self.filename, self.content_type, self._data = name, content_type, data
        self.size = len(data) if size is None else size
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        return self._data


def interaction():
    i = types.SimpleNamespace(response=Response(), user=types.SimpleNamespace(id=UID), edited=[],
                              type=discord.InteractionType.component)

    async def edit_original_response(**kw):
        i.edited.append(kw)
    i.edit_original_response = edit_original_response
    return i


def texts(i) -> list[str]:
    return [c or "" for c, _ in i.response.sent] + [e.get("content") or "" for e in i.edited]


@needs_discord
class UploadTestCase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        intern_store.init_db(self.db)
        # The member pressing these buttons may use the bot; who may is test_access_gates'.
        access.init_db(self.db)
        access.grant(self.db, "user", UID, granted_by=None, now=1.0)
        patches = [mock.patch.object(intern_ui, "db", self.db, create=True),
                   mock.patch.object(intern_ui, "intern_error", None, create=True),
                   mock.patch.object(intern_ui, "upload_limiter", rate_limit.RateLimiter(limit=5, window=3600)),
                   mock.patch.object(intern_views, "show_draft", self.fake_show_draft)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.drafts = []

    async def fake_show_draft(self, interaction, profile, **kw):
        self.drafts.append(profile)

    def consent_on_record(self):
        p = new_profile(UID, 1.0, source="manual", cursor=0.0)
        intern_store.save(self.db, dataclasses.replace(p, fields=("software",),
                                                       consent_version=DISCLOSURE_VERSION), now=1.0,
                          cursor=0.0)

    def slots_used(self) -> int:
        return intern_ui.upload_limiter.used(UID)


class ARefusedUploadNeverSkipsTheDisclosure(UploadTestCase):
    def refused_before_consent(self):
        i = interaction()
        asyncio.run(intern_upload.begin_upload(i, Attachment("cv.doc", "application/msword", b"x" * 10)))
        content, kw = i.response.sent[0]
        return content, kw.get("view")

    def test_a_metadata_refusal_for_someone_who_never_consented_still_offers_the_way_forward(self):
        content, view = self.refused_before_consent()

        self.assertIn(".doc", content)
        self.assertIsInstance(view, intern_upload.ResumeFailView)

    def test_its_paste_button_shows_the_disclosure_before_any_modal(self):
        _, view = self.refused_before_consent()
        i = interaction()

        asyncio.run(view.paste.callback(i))

        self.assertEqual(i.response.modals, [])
        content, kw = i.response.sent[0]
        self.assertIn(intern_text.disclosure_text(), content)
        self.assertIsInstance(kw["view"], intern_upload.ConsentView)

    def test_continuing_from_that_disclosure_opens_the_upload_modal(self):
        _, view = self.refused_before_consent()
        first = interaction()
        asyncio.run(view.paste.callback(first))
        consent = first.response.sent[0][1]["view"]
        second = interaction()

        asyncio.run(consent.proceed.callback(second))

        self.assertIsInstance(second.response.modals[0], intern_upload.UploadModal)

    def test_someone_who_already_consented_goes_straight_to_the_modal(self):
        self.consent_on_record()
        i = interaction()
        asyncio.run(intern_upload.begin_upload(i, Attachment("cv.doc", "application/msword", b"x" * 10)))
        view = i.response.sent[0][1]["view"]
        press = interaction()

        asyncio.run(view.paste.callback(press))

        self.assertIsInstance(press.response.modals[0], intern_upload.UploadModal)


class AFailedReadStillCosts(UploadTestCase):
    def upload(self, outcome):
        async def worker(data, kind, today, **kw):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        self.consent_on_record()
        with mock.patch.object(intern_upload.resume_worker, "run_worker", worker):
            asyncio.run(intern_upload.start_read(interaction(), Attachment("cv.txt", "text/plain", RESUME),
                                                 "txt", source="resume"))

    def test_a_file_that_crashes_or_outlasts_the_worker_spends_its_slot(self):
        for reason in ("worker_failed", "timeout"):
            with self.subTest(reason=reason):
                intern_ui.upload_limiter = rate_limit.RateLimiter(limit=5, window=3600)

                self.upload(resume_parse.ResumeRefusal(reason))

                self.assertEqual(self.slots_used(), 1)

    def test_a_busy_worker_or_a_missing_pdf_reader_gives_the_slot_back(self):
        for reason in ("busy", "no_pdf_support"):
            with self.subTest(reason=reason):
                intern_ui.upload_limiter = rate_limit.RateLimiter(limit=5, window=3600)

                self.upload(resume_parse.ResumeRefusal(reason))

                self.assertEqual(self.slots_used(), 0)

    def test_a_download_that_fails_on_discords_side_gives_the_slot_back(self):
        class Broken(Attachment):
            async def read(self):
                raise asyncio.TimeoutError()
        self.consent_on_record()

        asyncio.run(intern_upload.start_read(interaction(), Broken("cv.txt", "text/plain", RESUME),
                                             "txt", source="resume"))

        self.assertEqual(self.slots_used(), 0)


class OneReadPerPersonAtATime(UploadTestCase):
    def test_a_second_upload_while_the_first_is_being_read_is_refused_without_a_worker(self):
        self.consent_on_record()
        started, release, calls = asyncio.Event(), asyncio.Event(), []

        async def worker(data, kind, today, **kw):
            calls.append(kind)
            started.set()
            await release.wait()
            return {"majors": [], "fields": []}

        async def both():
            first = interaction()
            task = asyncio.ensure_future(intern_upload.start_read(
                first, Attachment("a.txt", "text/plain", RESUME), "txt", source="resume"))
            await asyncio.wait_for(started.wait(), 5)      # a first read that never starts fails, not hangs
            second = interaction()
            await intern_upload.start_read(second, Attachment("b.txt", "text/plain", RESUME), "txt",
                                           source="resume")
            release.set()
            await asyncio.wait_for(task, 5)
            return second

        with mock.patch.object(intern_upload.resume_worker, "run_worker", worker):
            second = asyncio.run(both())

        self.assertEqual(calls, ["txt"])
        self.assertIn("still reading", " ".join(texts(second)))
        self.assertEqual(self.slots_used(), 1)


@needs_discord
class TheLimitIsFiveReadsAnHour(unittest.TestCase):
    def test_a_sixth_read_within_the_hour_is_refused_and_the_next_hour_is_not(self):
        limiter = copy.deepcopy(intern_ui.upload_limiter)      # the real one, left untouched

        self.assertIsInstance(limiter, rate_limit.RateLimiter)
        self.assertEqual([limiter.take(UID, now=0) for _ in range(6)], [True] * 5 + [False])
        self.assertTrue(limiter.take(UID, now=3600))


class TheRefusalsSayWhatHappened(UploadTestCase):
    def test_a_file_just_over_the_limit_is_never_called_2_0_mb(self):
        i = interaction()
        big = Attachment("cv.pdf", "application/pdf", b"%PDF-", size=resume_parse.MAX_BYTES + 1)

        asyncio.run(intern_upload.begin_upload(i, big))

        self.assertNotIn("2.0 MB", texts(i)[0])
        self.assertIn("2.1 MB", texts(i)[0])

    def test_replace_resume_consent_survives_a_dismissed_modal(self):
        async def build():
            return intern_upload.ConsentView(UID, None, "", then_modal=True)
        view = asyncio.run(build())
        first, second = interaction(), interaction()

        asyncio.run(view.proceed.callback(first))
        asyncio.run(view.proceed.callback(second))

        self.assertFalse(view.is_finished())
        self.assertIsInstance(second.response.modals[0], intern_upload.UploadModal)

    def test_cancelling_a_consent_with_no_file_does_not_mention_a_file(self):
        async def build():
            return intern_upload.ConsentView(UID, None, "", then_modal=True)
        view = asyncio.run(build())
        i = interaction()

        asyncio.run(view.cancel.callback(i))

        self.assertNotIn("file", i.response.edits[0]["content"])


if __name__ == "__main__":
    unittest.main()
