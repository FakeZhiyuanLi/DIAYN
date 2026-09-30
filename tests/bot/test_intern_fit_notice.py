"""
Where the Gemini fit check's notice is shown, and so recorded, before anything
is sent to Google.

    .venv/bin/python -m unittest discover -s tests      # skips without discord.py

A profile is checked only once its owner has been shown what the check sends
(`intern_fit.enabled`, `fit_notice_at`). The notice is on the start card and
the consent screen, one of which comes before every new profile, and in
`/internships help`. `test_intern_fit` holds the rest: the alert that carries
it to anyone these never reached, and that no request is made before it.

The ids here are made up.
"""

import dataclasses
import types
import unittest
from unittest import mock

import access
import intern_profile
import intern_store
import internship_poller as poller
from test_access_gates import (GRANTED, NOW, OWNER, REAL_DISCORD, Attachment, _GateCase,
                               interaction, needs_discord, on)

if REAL_DISCORD:
    import intern_commands
    import intern_ui
    import intern_upload
    import intern_views

KEY = "test-key-not-real"


@needs_discord
class _NoticeCase(_GateCase):
    """users.db in memory, GRANTED let in by id, the host with a Gemini key, and every draft
    the finder would draw caught instead of drawn."""

    def setUp(self):
        super().setUp()
        self.grant("user", GRANTED)
        self.drafts = []

        async def show_draft(interaction, draft, **kw):
            self.drafts.append(draft)
        for target, name, value in (
                (poller, "SETTINGS", self.settings(GEMINI_API_KEY=KEY)),
                (intern_views, "show_draft", show_draft)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def settings(**environ):
        return poller.configure({"DIAYN_OWNER_IDS": str(OWNER), **environ})

    def without_a_key(self):
        return mock.patch.object(poller, "SETTINGS", self.settings())

    def told_at(self, uid=GRANTED):
        return intern_store.load(self.db, uid).fit_notice_at


class ANewProfileWasShownIt(_NoticeCase):
    """No profile yet means the start card or the consent screen came first, and both carry
    the notice where the host has a key: the draft records it."""

    def test_pick_by_hand_from_the_start_card(self):
        i = interaction(GRANTED, component=True)
        self.drive(on(lambda: intern_upload.StartView(GRANTED),
                      lambda v, i: v.manual.callback(i))(i))

        (draft,) = self.drafts
        self.assertIsNotNone(draft.fit_notice_at)

    def test_a_parsed_resume(self):
        self.drive(intern_upload._show_parsed(interaction(GRANTED, component=True),
                                              {"fields": ["software"]}, source="resume"))

        (draft,) = self.drafts
        self.assertIsNotNone(draft.fit_notice_at)

    def test_without_a_key_there_was_nothing_to_show(self):
        with self.without_a_key():
            self.drive(intern_upload.start_manual(interaction(GRANTED, component=True)))
            self.drive(intern_upload._show_parsed(interaction(GRANTED, component=True),
                                                  {"fields": ["software"]}, source="resume"))

        self.assertEqual([d.fit_notice_at for d in self.drafts], [None, None])

    def test_a_refused_file_before_any_consent_screen_showed_nothing(self):
        # `/internships profile resume:` with a file refused on sight comes before the
        # consent screen, so its "Pick by hand" follows no notice at all.
        for consented, told in ((False, False), (True, True)):
            with self.subTest(consented=consented):
                self.drafts.clear()
                i = interaction(GRANTED, component=True)
                self.drive(on(lambda: intern_upload.ResumeFailView(GRANTED, consented=consented),
                              lambda v, i: v.manual.callback(i))(i))
                (draft,) = self.drafts
                self.assertEqual(draft.fit_notice_at is not None, told)

    def test_a_replaced_resume_keeps_what_the_profile_had(self):
        self.enrol(GRANTED)                       # never told
        self.drive(intern_upload._show_parsed(interaction(GRANTED, component=True),
                                              {"fields": ["software"]}, source="resume"))

        (draft,) = self.drafts
        self.assertIsNone(draft.fit_notice_at)


class AProfileThatSeesItIsRecorded(_NoticeCase):
    def test_the_consent_screen_before_an_upload(self):
        self.enrol(GRANTED)                       # consented to no disclosure yet
        i = interaction(GRANTED, component=True)

        self.drive(intern_upload.open_upload_with_consent(i))

        self.assertIn("Gemini checks your alerts", i.response.sent[0][0])
        self.assertIsNotNone(self.told_at())

    def test_the_consent_screen_before_a_file_is_read(self):
        self.enrol(GRANTED)
        i = interaction(GRANTED)

        self.drive(intern_upload.begin_upload(i, Attachment()))

        self.assertIn("Gemini checks your alerts", i.response.sent[0][0])
        self.assertIsNotNone(self.told_at())

    def test_no_consent_screen_shown_records_nothing(self):
        self.enrol(GRANTED, consent_version=intern_profile.DISCLOSURE_VERSION)
        i = interaction(GRANTED, component=True)

        self.drive(intern_upload.open_upload_with_consent(i))

        self.assertEqual(type(i.response.modals[0]).__name__, "UploadModal")
        self.assertIsNone(self.told_at())

    def test_help(self):
        self.enrol(GRANTED)
        i = interaction(GRANTED)

        self.drive(intern_commands.internships_help.callback(i))

        self.assertIn("Gemini checks your alerts", i.followup.sent[-1][0])
        self.assertIsNotNone(self.told_at())

    def test_the_first_time_is_the_one_kept(self):
        self.enrol(GRANTED, fit_notice_at=NOW)

        self.drive(intern_commands.internships_help.callback(interaction(GRANTED)))

        self.assertEqual(self.told_at(), NOW)

    def test_without_a_key_nothing_about_gemini_was_shown(self):
        self.enrol(GRANTED)
        with self.without_a_key():
            self.drive(intern_commands.internships_help.callback(interaction(GRANTED)))
            self.drive(intern_upload.open_upload_with_consent(interaction(GRANTED, component=True)))

        self.assertIsNone(self.told_at())

    def test_help_for_someone_with_no_profile_writes_nothing(self):
        self.drive(intern_commands.internships_help.callback(interaction(GRANTED)))

        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM intern_profiles").fetchone(), (0,))

    def test_help_with_the_finder_off_still_answers(self):
        with mock.patch.object(intern_ui, "intern_error", "OperationalError: x"), \
                mock.patch.object(intern_ui, "db", None):
            i = interaction(GRANTED)
            self.drive(intern_commands.internships_help.callback(i))

        self.assertIn("/internships", i.response.sent[0][0])


if __name__ == "__main__":
    unittest.main()
