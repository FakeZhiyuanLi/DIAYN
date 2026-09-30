"""
intern_upload.py
~~~~~~~~~~~~~~~~
How a resume gets from a Discord user to the draft card: the start card's
buttons, the upload modal, the consent screen, and the single function that
downloads an attachment.

Split out because this is the one path that handles a resume, and its order is
the privacy promise (spec 2): metadata is checked (`resume_parse.sniff`)
before anything is downloaded; a user who has not consented sees the
disclosure before the download; the bytes go straight to the worker process
(`resume_worker.run_worker`) and are never held, logged or written here;
failures are logged by reason only. `read_and_parse` is the only caller of
`attachment.read()` and takes the kind `sniff` returned, so there is no path
that reads first and checks later.

The bot is private: every button and submit here checks
`intern_ui.need_access` first, except Cancel. A refused consent screen drops
the file it was holding, as any answer does.

The card's two edit modals (J4) live here too, so every modal the finder opens
is in one module; `intern_views` opens them.
"""

import asyncio
import calendar
import contextlib
import math
import sys
import time
from collections.abc import Awaitable, Callable, Sequence

import aiohttp
import discord

import intern_delivery
import intern_store
import intern_text
import intern_ui
import intern_views
import intern_vocab as vocab
import resume_parse
import resume_worker
from intern_profile import (DISCLOSURE_VERSION, Profile, from_draft, new_profile, parse_details_form,
                            parse_filters_form, upcoming_terms, with_changes)
from resume_lexicon import MAJOR_BY_ID, SKILL_BY_ID

#: Refusals that give the rate-limit slot back: nothing ran, or the bot is at fault.
#: A file that crashes or outlasts the worker is not on this list: it spent real
#: CPU, and refunding it would let one member keep both worker slots busy forever.
#: A failed download is refunded where it happens (`read_and_parse`).
_REFUNDED = frozenset({"busy", "no_pdf_support", "already_reading"})
#: Refusals that come with no way forward: waiting is the only answer.
_NO_VIEW = frozenset({"rate_limited", "already_reading"})
_KIND_LABELS = {"pdf": "PDF", "docx": "Word", "txt": "text"}
_MAX_PASTE = 4000
_MIB = 1024 * 1024
_CONSENT_TIMEOUT_S = 600            # the Attachment is held in memory no longer than this
_DOWNLOAD_ERRORS = (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError)
_STATE = "st:"
#: Members with a resume being read right now: one at a time each, so a single
#: member cannot hold every worker slot (`resume_worker.MAX_CONCURRENT`).
_reading: set[int] = set()
OnDone = Callable[[discord.Interaction, Profile, tuple[str, ...]], Awaitable[None]]


# ------------------------------------------------------------------ refusals

def _fail_view(reason: str, owner_id: int, consented: bool):
    """Every failure offers "paste instead / pick by hand", except the ones only waiting
    fixes (J2d). `consented` is whether this path already showed the disclosure."""
    return None if reason in _NO_VIEW else ResumeFailView(owner_id, consented=consented)


def _minutes_left(user_id: int) -> int:
    return max(1, -(-intern_ui.upload_limiter.opens_in(user_id) // 60))


async def _refused(interaction, reason: str, *, consented: bool = True, **ctx: object) -> None:
    """A refusal as the first private reply, or in place of the deferred message."""
    text = intern_text.upload_error(reason, **ctx)
    view = _fail_view(reason, interaction.user.id, consented)
    if not interaction.response.is_done():
        await intern_ui.refuse(interaction, text, view=view)
    elif view is None:
        await interaction.edit_original_response(content=text, allowed_mentions=intern_ui.NO_MENTIONS)
    else:
        await interaction.edit_original_response(content=text, view=view,
                                                 allowed_mentions=intern_ui.NO_MENTIONS)


async def _parse_failed(interaction, reason: str, kind: str, *, refund: bool | None = None) -> None:
    print(f"resume parse failed: {reason}", file=sys.stderr)
    if reason in _REFUNDED if refund is None else refund:
        intern_ui.upload_limiter.refund(interaction.user.id)
    await _refused(interaction, reason, kind_label=_KIND_LABELS.get(kind))


def _size_mb(size: int) -> str:
    """Rounded up, so a file over the limit never reads as exactly the limit ("2.0 MB")."""
    return f"{math.ceil(size / _MIB * 10) / 10:.1f}"


async def _sniff_refused(interaction, refusal, attachment, *, consented: bool = True) -> None:
    await _refused(interaction, refusal.reason, consented=consented,
                   size_mb=_size_mb(attachment.size or 0))


def needs_consent(user_id: int) -> bool:
    """No profile, or one that consented to an older disclosure (2.1)."""
    p = intern_store.load(intern_ui.db, user_id)
    return p is None or p.consent_version < DISCLOSURE_VERSION


async def open_upload_with_consent(interaction) -> None:
    """The upload modal, behind the disclosure when this member has not seen the current one.
    Either way the modal (or the disclosure) is the first response to the click."""
    if not needs_consent(interaction.user.id):
        await open_upload(interaction)
        return
    await interaction.response.send_message(
        intern_text.disclosure_text(), ephemeral=True, allowed_mentions=intern_ui.NO_MENTIONS,
        view=ConsentView(interaction.user.id, None, "", then_modal=True))


@contextlib.contextmanager
def _one_read(user_id: int):
    """Hold this member's only reading slot, or refuse while an earlier read runs."""
    if user_id in _reading:
        raise resume_parse.ResumeRefusal("already_reading")
    _reading.add(user_id)
    try:
        yield
    finally:
        _reading.discard(user_id)


# ------------------------------------------------------------------ the three entry paths

async def begin_upload(interaction, attachment: discord.Attachment) -> None:
    """`/internships profile resume:` (J2b). Sniff (refusing before any defer), then the
    consent screen when there is no consent on record (2.1), else start_read."""
    uid = interaction.user.id
    consented = not needs_consent(uid)
    try:
        kind = resume_parse.sniff(attachment.filename, attachment.content_type, attachment.size)
    except resume_parse.ResumeRefusal as refusal:
        # Refused before the consent screen: its buttons must not skip it.
        await _sniff_refused(interaction, refusal, attachment, consented=consented)
        return
    if not consented:
        await interaction.response.send_message(
            intern_text.consent_text(attachment.filename), ephemeral=True,
            allowed_mentions=intern_ui.NO_MENTIONS, view=ConsentView(uid, attachment, kind))
        return
    await start_read(interaction, attachment, kind, source="resume")


async def start_read(interaction, attachment: discord.Attachment, kind: str, *, source: str) -> None:
    """After consent (J2c): take a rate-limit slot, defer, then read_and_parse."""
    uid = interaction.user.id
    if not intern_ui.upload_limiter.take(uid):
        await _refused(interaction, "rate_limited", minutes=_minutes_left(uid))
        return
    await intern_ui.defer_update(interaction)
    await read_and_parse(interaction, attachment, kind, source=source)


async def _parsed(data: bytes, kind: str) -> dict:
    """The worker's validated draft. The bytes go no further than its stdin."""
    if len(data) > resume_parse.MAX_BYTES:
        raise resume_parse.ResumeRefusal("too_big")
    return await resume_worker.run_worker(data, kind, intern_ui.today())


async def read_and_parse(interaction, attachment: discord.Attachment, kind: str, *, source: str) -> None:
    """The ONLY caller of attachment.read(); `kind` comes from sniff. The interaction has
    been deferred. Download -> worker -> draft card, or a refusal with ResumeFailView."""
    try:
        with _one_read(interaction.user.id):
            try:
                data = await attachment.read()
            except _DOWNLOAD_ERRORS as error:
                # Discord's side failed, not the file: the member keeps their slot.
                intern_ui.log_failure("downloading a resume", error)
                await _parse_failed(interaction, "worker_failed", kind, refund=True)
                return
            # Handed straight to the worker: no name here holds the bytes past this call.
            draft = await _parsed(data, kind)
            del data
    except resume_parse.ResumeRefusal as refusal:
        await _parse_failed(interaction, refusal.reason, kind)
        return
    await _show_parsed(interaction, draft, source=source)


async def begin_paste(interaction, text: str) -> None:
    """The paste box (J2a): length check, rate limit, defer, then the worker as a .txt."""
    uid = interaction.user.id
    pasted = (text or "").strip()[:_MAX_PASTE]
    if len(pasted) < resume_parse.MIN_PASTE_CHARS:
        await _refused(interaction, "paste_short")
        return
    if not intern_ui.upload_limiter.take(uid):
        await _refused(interaction, "rate_limited", minutes=_minutes_left(uid))
        return
    await intern_ui.defer_update(interaction)
    try:
        with _one_read(uid):
            draft = await resume_worker.run_worker(pasted.encode("utf-8"), "txt",
                                                   intern_ui.today())
    except resume_parse.ResumeRefusal as refusal:
        await _parse_failed(interaction, refusal.reason, "txt")
        return
    await _show_parsed(interaction, draft, source="pasted")


async def _show_parsed(interaction, draft: dict, *, source: str) -> None:
    """The worker's draft as a draft card; a replacement keeps the stored filters (J3)."""
    uid, now = interaction.user.id, time.time()
    existing = intern_store.load(intern_ui.db, uid)
    profile = from_draft(uid, draft, now, source=source, cursor=intern_delivery.horizon(now),
                         today=intern_ui.today(), existing=existing)
    header = intern_text.draft_header(profile, found_field=bool(draft.get("fields")),
                                      replacing=existing)
    await intern_views.show_draft(interaction, profile, evidence=intern_text.evidence_line(draft),
                                  header=header, replacing=existing)


async def open_upload(interaction) -> None:
    """send_modal(UploadModal()): it must be the first response of the calling interaction."""
    await interaction.response.send_modal(UploadModal())


async def start_manual(interaction) -> None:
    """Pick by hand: an empty draft to fill in. Someone with a profile gets their card instead,
    so a stray press can never save an empty profile over theirs."""
    uid, now = interaction.user.id, time.time()
    if intern_store.load(intern_ui.db, uid) is not None:
        await intern_ui.defer_update(interaction)
        await intern_views.show_card(interaction, edit=True)
        return
    draft = new_profile(uid, now, source="manual", cursor=intern_delivery.horizon(now))
    await intern_ui.defer_update(interaction)
    await intern_views.show_draft(interaction, draft, evidence=None, replacing=None,
                                  header=intern_text.draft_header(draft, found_field=False, replacing=None))


# ------------------------------------------------------------------ components

class StartView(intern_ui.OwnedView):
    """Under the start card (J1)."""

    @discord.ui.button(label="Upload or paste my resume", style=discord.ButtonStyle.primary)
    async def upload(self, interaction, button) -> None:
        if await intern_ui.need_access(interaction) and await intern_ui.need_finder(interaction):
            await open_upload(interaction)

    @discord.ui.button(label="Pick by hand", style=discord.ButtonStyle.secondary)
    async def manual(self, interaction, button) -> None:
        if await intern_ui.need_access(interaction) and await intern_ui.need_finder(interaction):
            await start_manual(interaction)


class UploadModal(intern_ui.FinderModal):
    """J2a: one file or pasted text; a file wins."""

    def __init__(self) -> None:
        super().__init__(title="Your resume")
        self.file = discord.ui.FileUpload(custom_id="intern:upload:file", required=False,
                                          min_values=0, max_values=1)
        self.text = discord.ui.TextInput(custom_id="intern:upload:text",
                                         style=discord.TextStyle.paragraph, required=False,
                                         max_length=_MAX_PASTE)
        self.add_item(discord.ui.TextDisplay(intern_text.upload_modal_note()))
        self.add_item(discord.ui.Label(text="Resume file (PDF, .docx or .txt, up to 2 MB)",
                                       component=self.file))
        self.add_item(discord.ui.Label(
            text="...or paste your Education and Skills text",
            description="Paste as much or as little as you like, up to 4000 characters.",
            component=self.text))

    async def on_submit(self, interaction) -> None:
        if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
            return
        files, text = list(self.file.values), self.text.value or ""
        if files:
            attachment = files[0]
            try:
                kind = resume_parse.sniff(attachment.filename, attachment.content_type,
                                          attachment.size)
            except resume_parse.ResumeRefusal as refusal:
                await _sniff_refused(interaction, refusal, attachment)
                return
            await start_read(interaction, attachment, kind, source="resume")
        elif text.strip():
            await begin_paste(interaction, text)
        else:
            await intern_ui.refuse(interaction, intern_text.upload_empty())


class ConsentView(intern_ui.OwnedView):
    """
    The consent screen (J2b). Holds the Attachment in memory for at most 600 s and
    drops it on any answer or on timeout. With `then_modal` (Replace resume with an
    older consent, or a refused upload's "paste instead") there is no file yet:
    Continue opens the upload modal, and stays pressable in case the member closes
    the modal without submitting it.
    """

    def __init__(self, owner_id: int, attachment, kind: str, *, then_modal: bool = False) -> None:
        super().__init__(owner_id, timeout=_CONSENT_TIMEOUT_S)
        self.attachment, self.kind, self.then_modal = attachment, kind, then_modal
        for item in ((self.read_resume, self.pick) if then_modal else (self.proceed,)):
            self.remove_item(item)

    def _take(self):
        attachment, self.attachment = self.attachment, None
        self.stop()
        return attachment

    async def on_timeout(self) -> None:
        self.attachment = None

    @discord.ui.button(label="Read my resume", style=discord.ButtonStyle.primary)
    async def read_resume(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction):
            self._take()                 # dropped, as any answer drops it
            return
        attachment = self._take()
        if attachment is None or not await intern_ui.need_finder(interaction):
            return
        await start_read(interaction, attachment, self.kind, source="resume")

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def proceed(self, interaction, button) -> None:
        # Not _take(): a modal closed without submitting must leave Continue working.
        if await intern_ui.need_access(interaction) and await intern_ui.need_finder(interaction):
            await open_upload(interaction)

    @discord.ui.button(label="Pick by hand instead", style=discord.ButtonStyle.secondary)
    async def pick(self, interaction, button) -> None:
        allowed = await intern_ui.need_access(interaction)
        self._take()
        if allowed and await intern_ui.need_finder(interaction):
            await start_manual(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button) -> None:
        self._take()
        await interaction.response.edit_message(
            content=intern_text.cancelled_consent(had_file=not self.then_modal), view=None,
            allowed_mentions=intern_ui.NO_MENTIONS)


class ResumeFailView(intern_ui.OwnedView):
    """Under a failed upload (J2d). `consented` is False only for a refusal that came
    before the consent screen, whose "paste instead" must show the disclosure first."""

    def __init__(self, owner_id: int, *, consented: bool = True) -> None:
        super().__init__(owner_id)
        self.consented = consented

    @discord.ui.button(label="Paste the text instead", style=discord.ButtonStyle.primary)
    async def paste(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
            return
        if self.consented:
            await open_upload(interaction)
        else:
            await open_upload_with_consent(interaction)

    @discord.ui.button(label="Pick by hand", style=discord.ButtonStyle.secondary)
    async def manual(self, interaction, button) -> None:
        if await intern_ui.need_access(interaction) and await intern_ui.need_finder(interaction):
            await start_manual(interaction)


# ------------------------------------------------------------------ the two modals (J4)

def _joined(items: Sequence[str], sep: str, limit: int) -> str | None:
    """Whole items, `sep`-joined, while they fit a text box of `limit` characters."""
    kept: list[str] = []
    for item in items:
        if len(sep.join([*kept, item])) > limit:
            break
        kept.append(item)
    return sep.join(kept) or None


def _text(limit: int, *, paragraph: bool = False, default: str | None = None,
          placeholder: str | None = None) -> discord.ui.TextInput:
    style = discord.TextStyle.paragraph if paragraph else discord.TextStyle.short
    return discord.ui.TextInput(style=style, max_length=limit, required=False,
                                default=default, placeholder=placeholder)


def _labelled(modal: discord.ui.Modal, rows) -> None:
    for text, component in rows:
        modal.add_item(discord.ui.Label(text=text, component=component))


class DetailsModal(intern_ui.FinderModal):
    """Edit details: major(s), degree, graduation, skills, extra keywords."""

    def __init__(self, p: Profile, *, on_done: OnDone) -> None:
        super().__init__(title="Your details")
        self.p, self.on_done = p, on_done
        study = [MAJOR_BY_ID[m].label for m in p.majors if m in MAJOR_BY_ID] + [
            f"minor {MAJOR_BY_ID[m].label}" for m in p.minors if m in MAJOR_BY_ID]
        month = f"{calendar.month_name[p.grad_month]} " if p.grad_month else ""
        degrees = (*vocab.DEGREES, ("none", "Prefer not to say"))
        self.majors = _text(200, default=_joined(study, "; ", 200))
        self.degree = discord.ui.Select(options=intern_ui.select_options(degrees, (p.degree or "none",)),
                                        min_values=0, max_values=1, required=False)
        self.grad = _text(30, default=f"{month}{p.grad_year}" if p.grad_year else None,
                          placeholder="June 2028")
        self.skills = _text(1000, paragraph=True, default=_joined(
            [SKILL_BY_ID[s].label for s in p.skills if s in SKILL_BY_ID], ", ", 1000))
        self.keywords = _text(330, default=_joined(p.keywords, ", ", 330),
                              placeholder="e.g. turbomachinery, urban planning")
        _labelled(self, (("Major(s)", self.majors), ("Degree", self.degree),
                         ("Graduation", self.grad), ("Skills", self.skills),
                         ("Extra keywords (optional)", self.keywords)))

    async def on_submit(self, interaction) -> None:
        if not await intern_ui.need_access(interaction):
            return
        changes, problems = parse_details_form(
            self.majors.value or "", next(iter(self.degree.values), None), self.grad.value or "",
            self.skills.value or "", self.keywords.value or "",
            today=intern_ui.today(), current=self.p)
        await self.on_done(interaction, with_changes(self.p, time.time(), **changes), problems)


class FiltersModal(intern_ui.FinderModal):
    """More filters: other states, terms, hidden and only-these companies, alert threshold."""

    def __init__(self, p: Profile, *, on_done: OnDone) -> None:
        super().__init__(title="More filters")
        self.p, self.on_done = p, on_done
        names = intern_ui.known_companies()
        terms = tuple(dict.fromkeys((*upcoming_terms(intern_ui.today()), *p.terms)))
        states = [t[len(_STATE):] for t in p.locations if t.startswith(_STATE)]
        self.states = _text(200, default=_joined(states, ", ", 200), placeholder="e.g. WA, Oregon, NY")
        self.terms = discord.ui.Select(options=intern_ui.select_options(((t, t) for t in terms), p.terms),
                                       placeholder="Any time", min_values=0,
                                       max_values=min(vocab.MAX_TERMS, len(terms)), required=False)
        self.hide = _text(1000, paragraph=True, default=_joined(
            [names.get(n, n) for n in p.companies_hidden], ", ", 1000))
        self.only = _text(1000, paragraph=True, default=_joined(
            [names.get(n, n) for n in p.companies_only], ", ", 1000))
        self.min_score = discord.ui.Select(options=intern_ui.select_options(
            ((str(score), label) for score, label in vocab.MIN_SCORE_CHOICES), (str(p.min_score),)))
        _labelled(self, (("Other US states", self.states), ("Terms", self.terms),
                         ("Hide these companies", self.hide),
                         ("Only these companies (optional)", self.only),
                         ("Alert me about", self.min_score)))

    async def on_submit(self, interaction) -> None:
        if not await intern_ui.need_access(interaction):
            return
        changes, problems = parse_filters_form(
            self.states.value or "", list(self.terms.values), self.hide.value or "",
            self.only.value or "", next(iter(self.min_score.values), str(self.p.min_score)),
            today=intern_ui.today(), current=self.p,
            known_companies=intern_ui.known_companies())
        await self.on_done(interaction, with_changes(self.p, time.time(), **changes), problems)
