"""
intern_views.py
~~~~~~~~~~~~~~~
The profile card, draft and saved, and everything reachable from it: the
matches list and its empty state, and the delete screen. The two edit modals
it opens live beside the upload modal in `intern_upload`.

**A draft is only in memory.** After a resume is parsed the card is a draft held
by its `DraftCardView` (D5): nothing is stored until Save, and a Cancel or the
view's timeout forgets it. **A saved card is persistent.** `ProfileCardView` has
fixed custom ids and is registered once in `on_ready`; the registered instance
answers every user, so its callbacks load the stored profile of
`interaction.user.id` and never trust the profile a card was drawn with.

**The bot is private.** Every button, select and submit here checks
`intern_ui.need_access` first, except the ways out: the card's Delete button,
the delete screen's two buttons and a draft's Cancel answer anyone.

**Coverage needs the window**, which can take seconds cold, so every path here
that draws a card or a list defers first and edits the deferred message.
Refusals (no profile, tracker down) are sent before the defer.
"""

import time
from collections.abc import Sequence

import discord

import intern_alert_views
import intern_clock
import intern_delivery
import intern_match
import intern_store
import intern_text
import intern_ui
import intern_vocab as vocab
from intern_profile import Profile, can_save, with_changes

_DAY_S = 86400
_STATE = "st:"
_PROBLEMS_SHOWN = 3                 # modal problem sentences on a card's feedback line
_DELETE_TIMEOUT_S = 300
_FIRST_DAYS = intern_text.FIRST_MATCH_DAYS


# ------------------------------------------------------------------ cards

async def _coverage(p: Profile):
    parts = await intern_ui.window_parts()
    return None if parts is None else intern_match.coverage(
        p, parts.cands, time.time(), supply=parts.supply, exclude=intern_ui.hidden(p.user_id),
        gmap=parts.gmap)


def _card(p: Profile, coverage, **text: object) -> str:
    return intern_text.card_text(p, coverage=coverage, companies=intern_ui.known_companies(), **text)


async def _edit(interaction, content: str, view: discord.ui.View | None) -> None:
    await interaction.edit_original_response(content=content, view=view,
                                             allowed_mentions=intern_ui.NO_MENTIONS)


async def show_card(interaction, *, feedback: str | None = None, edit: bool = False) -> None:
    """The saved card for interaction.user.id: edit=True edits the deferred message; else a
    first response defers then edits, and a later one is a new private message."""
    p = intern_store.load(intern_ui.db, interaction.user.id)
    if p is None:
        await intern_ui.refuse(interaction, intern_text.alert_reply("no_profile"))
        return
    followup = not edit and interaction.response.is_done()
    if not edit:
        await intern_ui.defer_reply(interaction)
    text = _card(p, await _coverage(p), mode="saved", feedback=feedback)
    if followup:
        await intern_ui.private_send(interaction)(text, view=ProfileCardView(p))
    else:
        await _edit(interaction, text, ProfileCardView(p))


async def _store_and_render(interaction, current: Profile, changed: Profile | None,
                            problems: Sequence[str] = ()) -> None:
    """Saves an edit (unless already stored: `changed` None) and redraws with "Saved.
    **N roles** match now (was M)." (J4). No cursor is passed to save, so a profile
    deleted meanwhile raises instead of being re-created."""
    ranked = None if changed is None else await _ranked(current, days=None)
    saved = intern_store.load(intern_ui.db, current.user_id) if changed is None else \
        intern_store.save(intern_ui.db, changed, now=time.time())
    coverage = await _coverage(saved)
    lines = [intern_text.feedback_line(None if ranked is None else len(ranked[0]),
                                       coverage.total if coverage else None),
             *problems[:_PROBLEMS_SHOWN]]
    await _edit(interaction, _card(saved, coverage, mode="saved", feedback="\n".join(lines)),
                ProfileCardView(saved))


async def show_draft(interaction, draft: Profile, *, evidence: str | None, header: str,
                     replacing: Profile | None, feedback: str | None = None) -> None:
    """Edits the deferred response into the draft card + DraftCardView(draft)."""
    text = _card(draft, await _coverage(draft), mode="draft", feedback=feedback,
                 evidence=evidence, header=header)
    await _edit(interaction, text, DraftCardView(draft, evidence=evidence, replacing=replacing,
                                                 header=header))


async def save_draft(interaction, draft: Profile) -> None:
    """J5: store, redraw as the saved card, the welcome DM unless alerts are off, then the
    best recent matches (recorded as sent so alerts do not repeat them)."""
    uid, now = draft.user_id, time.time()
    existing = intern_store.load(intern_ui.db, uid)
    saved = intern_store.save(intern_ui.db, draft, now=now, cursor=intern_delivery.horizon(now))
    if existing is not None and (draft.alerts, draft.alert_hour) != (existing.alerts, existing.alert_hour):
        saved = intern_store.set_alerts(intern_ui.db, uid, draft.alerts, draft.alert_hour, now,
                                        cursor=intern_delivery.horizon(now)) or saved
    await show_card(interaction, edit=True)
    # The welcome DM goes first: it is the check that DMs arrive, and when it does it
    # clears refused DMs, so what follows is written for the profile as it now is.
    if saved.alerts != "off" and await intern_alert_views.send_welcome(interaction, saved) \
            and saved.dm_failures >= intern_store.DM_FAILURE_LIMIT:
        saved = intern_store.load(intern_ui.db, uid) or saved
        await show_card(interaction, edit=True)      # the DM arrived: drop the "couldn't DM you" banner
    ranked = await _ranked(saved, days=_FIRST_DAYS)
    shown = ranked[0][:intern_match.FIRST_MATCHES] if ranked else []
    if ranked and not shown:
        await send_matches(interaction, saved, days=_FIRST_DAYS, sort="best")   # the empty state
    else:
        intern_store.record_sent(intern_ui.db, uid, [h for m in shown for h in m.ledger], now)
        for chunk in intern_text.saved_followup(shown, now, saved):
            await intern_ui.private_send(interaction)(chunk)


# ------------------------------------------------------------------ lists (J8, J9)

async def _ranked(p: Profile, *, days: int | None, sort: str = "best"):
    """(matches, candidates, gmap) over the last `days` (None: the whole window), hidden
    groups dropped; None with the tracker down."""
    parts = await intern_ui.window_parts()
    if parts is None:
        return None
    now = time.time()
    recent = parts.cands if days is None else [c for c in parts.cands if c.ts >= now - days * _DAY_S]
    return (intern_match.rank(p, recent, now, exclude=intern_ui.hidden(p.user_id), gmap=parts.gmap,
                              sort=sort), recent, parts.gmap)


async def _listing(p: Profile, *, days: int, sort: str):
    """(messages, relaxations): the matches packed (J9), or the empty state (J8) and the
    relaxations its buttons offer; None with the tracker down."""
    ranked = await _ranked(p, days=days, sort=sort)
    if ranked is None:
        return None
    (matches, recent, gmap), now = ranked, time.time()
    if matches:
        header = intern_ui.with_banner(p, intern_text.matches_header(len(matches), days=days, sort=sort))
        return intern_text.matches_messages(matches[:intern_match.MATCHES_MAX], now, header=header), []
    relax = intern_match.relaxations(p, recent, now, exclude=intern_ui.hidden(p.user_id), gmap=gmap)
    text = intern_text.empty_state(p, relax, pool=len(recent), days=days,
                                   companies=intern_ui.companies_watched(), now=now)
    return [intern_ui.with_banner(p, text)], relax


async def send_matches(interaction, p: Profile, *, days: int, sort: str) -> None:
    """J9 / J8 as new private messages. The interaction has been deferred."""
    listing = await _listing(p, days=days, sort=sort)
    if listing is None:
        await intern_ui.refuse(interaction, intern_text.disabled_tracker(intern_ui.pconn_error or ""))
        return
    chunks, relax = listing
    await intern_ui.refuse(interaction, chunks[0], view=RelaxView(p.user_id, relax, days=days, sort=sort)
                           if relax else None)
    for chunk in chunks[1:]:
        await intern_ui.private_send(interaction)(chunk)


class RelaxView(intern_ui.OwnedView):
    """One button per relaxation with a gain (J8): applies it and re-runs the list in place.
    With `draft`, the change goes to that draft in memory instead of the stored profile."""

    def __init__(self, owner_id: int, relax: Sequence[intern_match.Relaxation], *,
                 draft: "DraftCardView | None" = None, days: int = _FIRST_DAYS,
                 sort: str = "best") -> None:
        super().__init__(owner_id)
        self.draft, self.days, self.sort = draft, days, sort
        for r in tuple(relax)[:3]:
            button = discord.ui.Button(label=intern_text.relax_button_label(r))
            button.callback = self._apply_callback(r)
            self.add_item(button)

    def _apply_callback(self, relaxation: intern_match.Relaxation):
        async def callback(interaction) -> None:
            await self._apply(interaction, relaxation)
        return callback

    async def _apply(self, interaction, r: intern_match.Relaxation) -> None:
        if not await intern_ui.need_access(interaction):
            return
        now = time.time()
        if self.draft is not None:
            p = self.draft.draft = with_changes(self.draft.draft, now, **r.changes)
        else:
            current = await intern_ui.profile_for(interaction, tracker=True)
            if current is None:
                return
            p = intern_store.save(intern_ui.db, with_changes(current, now, **r.changes), now=now)
        await intern_ui.defer_update(interaction)
        self.stop()
        listing = await _listing(p, days=self.days, sort=self.sort)
        if listing is None:
            await intern_ui.refuse(interaction, intern_text.disabled_tracker(intern_ui.pconn_error or ""))
            return
        chunks, relax = listing
        await _edit(interaction, chunks[0], RelaxView(p.user_id, relax, draft=self.draft, days=self.days,
                                                      sort=self.sort) if relax else None)
        for chunk in chunks[1:]:
            await intern_ui.private_send(interaction)(chunk)


# ------------------------------------------------------------------ the delete screen (J11)

async def show_delete(interaction) -> None:
    """Everything stored, in as many messages as it takes, with the buttons under the question.
    `privacy_rows` is one quick read, so there is no defer."""
    uid = interaction.user.id
    rows = intern_store.privacy_rows(intern_ui.db, uid)
    if rows is None:
        intern_store.delete_user(intern_ui.db, uid)        # any row left without a profile
        await intern_ui.refuse(interaction, intern_text.nothing_held())
        return
    chunks = intern_text.delete_confirm(intern_text.privacy_text(rows))
    if len(chunks) == 1:
        await interaction.response.send_message(chunks[0], ephemeral=True,
                                                allowed_mentions=intern_ui.NO_MENTIONS,
                                                view=DeleteConfirmView(uid))
        return
    await interaction.response.send_message(chunks[0], ephemeral=True,
                                            allowed_mentions=intern_ui.NO_MENTIONS)
    send = intern_ui.private_send(interaction)
    for chunk in chunks[1:-1]:
        await send(chunk)
    await send(chunks[-1], view=DeleteConfirmView(uid))


class DeleteConfirmView(intern_ui.OwnedView):
    def __init__(self, owner_id: int) -> None:
        super().__init__(owner_id, timeout=_DELETE_TIMEOUT_S)

    @discord.ui.button(label="Delete everything", style=discord.ButtonStyle.danger)
    async def delete_all(self, interaction, button) -> None:
        if not await intern_ui.need_finder(interaction):
            return
        intern_store.delete_user(intern_ui.db, interaction.user.id)
        self.stop()
        await interaction.response.edit_message(content=intern_text.deleted_text(), view=None,
                                                allowed_mentions=intern_ui.NO_MENTIONS)

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button) -> None:
        self.stop()
        await interaction.response.edit_message(view=None, allowed_mentions=intern_ui.NO_MENTIONS)


# ------------------------------------------------------------------ the card's selects (J3)

def _alert_pairs(p: Profile | None) -> tuple[list[tuple[str, str]], str | None]:
    """The Alerts options and the current value; an hour set with `ping hour:` is added.
    Every hour names DIAYN_TZ, the zone alert hours are kept in."""
    zone = intern_clock.zone_label()
    pairs = list(vocab.alert_choices(zone=zone))
    if p is None:
        return pairs, None
    current = vocab.alert_choice_value(p.alerts, p.alert_hour if p.alerts in ("daily", "weekly") else 9)
    if current not in dict(pairs):
        pairs.append((current, vocab.alert_choice_label(p.alerts, p.alert_hour, zone=zone)))
    return pairs, current


def _card_select(kind: int, p: Profile | None, *, fields_min: int,
                 custom_id: str = discord.utils.MISSING) -> discord.ui.Select:
    """Card row `kind` + 1: fields, levels, where or alerts, current values preselected.
    st:XX tokens are never Where options: they are edited in More filters."""
    fields, levels = (p.fields, p.levels) if p else ((), ())
    presets = tuple(t for t in p.locations if not t.startswith(_STATE)) if p else ()
    alert_pairs, alert = _alert_pairs(p)
    placeholder, options, low, high = (
        ("Fields: pick all that fit (up to 6)", intern_ui.select_options(vocab.FIELDS, fields), fields_min,
         max(vocab.MAX_FIELDS, len(fields))),
        ("Looking for", intern_ui.select_options(vocab.LEVELS, levels), 1, len(vocab.LEVELS)),
        ("Where", intern_ui.select_options(vocab.LOCATION_PRESETS, presets), 1, len(vocab.LOCATION_PRESETS)),
        ("Alerts", intern_ui.select_options(alert_pairs, (alert,)), 1, 1))[kind]
    return discord.ui.Select(custom_id=custom_id, placeholder=placeholder, options=options,
                             min_values=low, max_values=high, row=kind)


def _select_changes(p: Profile, kind: int, values: list[str]) -> dict[str, object] | None:
    """What one of the four selects asks for, as with_changes keywords (None: nothing valid)."""
    if kind == 3:
        choice = vocab.parse_alert_choice(next(iter(values), ""))
        if choice is None:
            return None
        # Off carries no hour of its own: keep the stored one for when alerts come back.
        return {"alerts": choice[0], "alert_hour": p.alert_hour if choice[0] == "off" else choice[1]}
    states = tuple(t for t in p.locations if t.startswith(_STATE))
    return ({"fields": tuple(values)}, {"levels": tuple(values)},
            {"locations": tuple(values) + states})[kind]


def _wire(view: discord.ui.View, selects, on_change) -> None:
    for kind, select in enumerate(selects):
        async def callback(interaction, kind=kind, select=select) -> None:
            await on_change(interaction, kind, list(select.values))
        select.callback = callback
        view.add_item(select)


# ------------------------------------------------------------------ the saved card

class ProfileCardView(intern_ui.FinderView):
    """Persistent; __init__(p=None) builds the placeholder instance registered in on_ready.
    A card's own instance times out; the registered one then answers, identically."""

    def __init__(self, p: Profile | None = None) -> None:
        super().__init__(timeout=None if p is None else intern_ui.VIEW_TIMEOUT_S)
        _wire(self, (_card_select(0, p, fields_min=1, custom_id="intern:card:fields"),
                     _card_select(1, p, fields_min=1, custom_id="intern:card:levels"),
                     _card_select(2, p, fields_min=1, custom_id="intern:card:where"),
                     _card_select(3, p, fields_min=1, custom_id="intern:card:alerts")), _edit_saved)

    @discord.ui.button(label="Show my matches", style=discord.ButtonStyle.primary,
                       custom_id="intern:card:matches", row=4)
    async def matches(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction):
            return
        p = await intern_ui.profile_for(interaction, tracker=True)
        if p is not None:
            await intern_ui.defer_reply(interaction)
            await send_matches(interaction, p, days=_FIRST_DAYS, sort="best")

    @discord.ui.button(label="Edit details", custom_id="intern:card:details", row=4)
    async def details(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction):
            return
        p = await intern_ui.profile_for(interaction)
        if p is not None:
            await interaction.response.send_modal(_modals().DetailsModal(p, on_done=_saved_modal_done))

    @discord.ui.button(label="More filters", custom_id="intern:card:filters", row=4)
    async def filters(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction):
            return
        p = await intern_ui.profile_for(interaction)
        if p is not None:
            await interaction.response.send_modal(_modals().FiltersModal(p, on_done=_saved_modal_done))

    @discord.ui.button(label="Replace resume", custom_id="intern:card:upload", row=4)
    async def upload(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction):
            return
        p = await intern_ui.profile_for(interaction)
        if p is None:
            return
        await _modals().open_upload_with_consent(interaction)

    @discord.ui.button(label="Delete my data", style=discord.ButtonStyle.danger,
                       custom_id="intern:card:delete", row=4)
    async def delete(self, interaction, button) -> None:
        if await intern_ui.need_finder(interaction):
            await show_delete(interaction)


def _modals():
    """intern_upload, which holds every modal the finder opens. Imported when first
    needed: intern_upload imports this module (spec 6 import graph)."""
    import intern_upload
    return intern_upload


async def _edit_saved(interaction, kind: int, values: list[str]) -> None:
    if not await intern_ui.need_access(interaction):
        return
    p = await intern_ui.profile_for(interaction)
    if p is None:
        return
    changes = _select_changes(p, kind, values)
    if changes is None:
        await intern_ui.refuse(interaction, intern_text.generic_failure())
        return
    await intern_ui.defer_update(interaction)
    now = time.time()
    if "alerts" in changes:          # a saved profile's cadence goes through set_alerts (3.1)
        intern_store.set_alerts(intern_ui.db, p.user_id, changes["alerts"], changes["alert_hour"],
                                now, cursor=intern_delivery.horizon(now))
        await _store_and_render(interaction, p, None)
        return
    await _store_and_render(interaction, p, with_changes(p, now, **changes))


async def _saved_modal_done(interaction, changed: Profile, problems: tuple[str, ...]) -> None:
    current = await intern_ui.profile_for(interaction)
    if current is not None:
        await intern_ui.defer_update(interaction)
        await _store_and_render(interaction, current, changed, problems)


# ------------------------------------------------------------------ the draft card

class DraftCardView(intern_ui.OwnedView):
    """The draft card (J3): nothing is stored until Save; every change redraws the card."""

    def __init__(self, draft: Profile, *, evidence: str | None, replacing: Profile | None,
                 header: str | None = None) -> None:
        super().__init__(draft.user_id)
        self.draft, self.evidence, self.replacing, self.header = draft, evidence, replacing, header
        _wire(self, [_card_select(kind, draft, fields_min=0) for kind in range(4)], self._changed)
        self.save.disabled = not can_save(draft)

    async def _changed(self, interaction, kind: int, values: list[str]) -> None:
        if not await intern_ui.need_access(interaction):
            return
        changes = _select_changes(self.draft, kind, values)
        if changes is None:
            await intern_ui.refuse(interaction, intern_text.generic_failure())
            return
        await intern_ui.defer_update(interaction)
        await self._redraw(interaction, with_changes(self.draft, time.time(), **changes))

    async def _redraw(self, interaction, draft: Profile, feedback: str | None = None) -> None:
        self.stop()                  # before the new view is stored for the same message
        await show_draft(interaction, draft, evidence=self.evidence, header=self.header,
                         replacing=self.replacing, feedback=feedback)

    async def _modal_done(self, interaction, changed: Profile, problems: tuple[str, ...]) -> None:
        await intern_ui.defer_update(interaction)
        await self._redraw(interaction, changed, "\n".join(problems[:_PROBLEMS_SHOWN]) or None)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
            return
        if not can_save(self.draft):         # the button is disabled; a stale client is not
            await intern_ui.refuse(interaction, intern_text.generic_failure())
            return
        await intern_ui.defer_update(interaction)
        self.stop()
        await save_draft(interaction, self.draft)

    @discord.ui.button(label="Edit details", row=4)
    async def details(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction):
            return
        await interaction.response.send_modal(_modals().DetailsModal(self.draft, on_done=self._modal_done))

    @discord.ui.button(label="More filters", row=4)
    async def filters(self, interaction, button) -> None:
        if not await intern_ui.need_access(interaction):
            return
        await interaction.response.send_modal(_modals().FiltersModal(self.draft, on_done=self._modal_done))

    @discord.ui.button(label="Cancel", row=4)
    async def cancel(self, interaction, button) -> None:
        self.stop()
        await interaction.response.edit_message(content=intern_text.cancelled_draft(), view=None,
                                                allowed_mentions=intern_ui.NO_MENTIONS)
