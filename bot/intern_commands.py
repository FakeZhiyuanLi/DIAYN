"""
intern_commands.py
~~~~~~~~~~~~~~~~~~
`/internships` itself: the seven subcommands (spec 1.3) and their
autocompletes, plus every name `app.py` wires in (spec 7.1): the group, the
delivery loop, the membership hooks and the persistent views. The loop and
hooks are written in `intern_alert_views`, beside the alerts they keep correct,
and exported from here so the wiring has one module to import.

Each command sends its refusals first (no access to this bot, finder off,
tracker off, not whoever runs this bot, a field nobody could have picked),
then defers if it will read the window, and answers privately. The bot is
private: every subcommand but `help` and `delete` checks `intern_ui.need_access`
before anything else, and so do the role suggestions, which would otherwise
list postings and the user's own matches to anyone. The Gemini, database and
sweep diagnostics are built here (`debug_report`), read-only, for the owner's
`/diayn debug` (`diayn_commands`).

Nothing here reads the scraper: the blocklist, the quota, the file's path and
the sweeper's name come from `intern_ui.source`, the contract tables the
scraper publishes (`postings_source`).
"""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands

import intern_alert_views
import intern_clock
import intern_delivery
import intern_fit
import intern_match
import intern_store
import intern_text
import intern_ui
import intern_upload
import intern_views
import intern_vocab as vocab
import posting_details
import postings_source
import resume_parse
from intern_location import describe_locations, location_fit, state_choices
from intern_places import STATE_BY_NAME, US_STATES
from intern_profile import Profile
from intern_taxonomy import bucket
from message_pack import MAX_CHUNK, pack

DESC_SNIPPET_MAX = 1200     # description excerpt length for `info`
FIND_SCAN_MAX = 25          # rows `info` scans past blocked and non-job ones for a match
_SQL_SUGGESTIONS = 100      # rows a cold-window autocomplete reads
_CHOICES_MAX, _TOP_MATCHES, _CHOICE_NAME_MAX = 25, 10, 100
_POSTING = "rowid, platform, external_id, company, title, location, url, published, first_seen"
_COMPANY_AT, _TITLE_AT = 3, 4
_ANY_PLACE = ("us", "remote_us", "abroad", "unlisted")       # a place label for anyone
_LEVEL_CHOICES = (("Internships & co-ops", "intern_coop"), ("Internships", "intern"),
                  ("Co-ops & placements", "coop"), ("New grad & programs", "new_grad"),
                  ("Entry-level", "entry"), ("Apprenticeships", "apprentice"),
                  ("Part-time & hourly", "hourly"), ("Level not stated", "unspecified"))
_LEVEL_LABELS = {value: name for name, value in _LEVEL_CHOICES}

# The delivery loop and the membership hooks, written beside the alerts they keep
# correct and exported under the names app.py wires in (7.1).
intern_delivery_loop = intern_alert_views.intern_delivery_loop
member_left = intern_alert_views.member_left
member_joined = intern_alert_views.member_joined
DELIVERY_MINUTES = intern_alert_views.DELIVERY_MINUTES


def persistent_views() -> list[discord.ui.View]:
    """Registered once at start-up so buttons sent before a restart keep working."""
    return [intern_views.ProfileCardView(), intern_alert_views.AlertControlsView(),
            intern_alert_views.DmCheckView()]


internships = app_commands.Group(
    name="internships",
    description="Internships, co-ops and new-grad roles matched to you, for any major",
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True,
                                                    private_channel=False))


# ------------------------------------------------------------------ profile, matches, delete, help

@internships.command(name="profile",
                     description="Set up or edit your internship profile. Attach a resume to fill it in.")
@app_commands.describe(resume="PDF, Word (.docx) or .txt, up to 2 MB. Read once, never saved.")
async def internships_profile(interaction: discord.Interaction,
                              resume: discord.Attachment | None = None) -> None:
    if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
        return
    intern_ui.touch(interaction.user.id)
    if resume is not None:
        await intern_upload.begin_upload(interaction, resume)
    elif intern_store.load(intern_ui.db, interaction.user.id) is None:
        await intern_ui.show_start(interaction)
    else:
        await intern_views.show_card(interaction)


@internships.command(name="matches",
                     description="Roles that fit your profile, best first, with why each one matched.")
@app_commands.describe(sort="Best first, or newest first", days="How many days back to look (default 14)")
@app_commands.choices(sort=[app_commands.Choice(name="Best first", value="best"),
                            app_commands.Choice(name="Newest first", value="newest")])
async def internships_matches(interaction: discord.Interaction,
                              sort: app_commands.Choice[str] | None = None,
                              days: app_commands.Range[int, 1, 30] = 14) -> None:
    if (not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction)
            or not await intern_ui.need_tracker(interaction)):
        return
    p = intern_store.load(intern_ui.db, interaction.user.id)
    if p is None:
        await intern_ui.show_start(interaction, extra=intern_text.matches_no_profile())
        return
    intern_ui.touch(p.user_id)
    await intern_ui.defer_reply(interaction)
    await intern_views.send_matches(interaction, p, days=days, sort=sort.value if sort else "best")


@internships.command(name="delete",
                     description="See everything the internship finder stores about you, and erase it.")
async def internships_delete(interaction: discord.Interaction) -> None:
    if await intern_ui.need_finder(interaction):
        await intern_views.show_delete(interaction)


@internships.command(name="help", description="How the internship finder works and what it keeps about you.")
async def internships_help(interaction: discord.Interaction) -> None:
    intern_ui.touch(interaction.user.id)
    chunks = intern_text.help_text(pdf_ok=resume_parse.pdf_supported(),
                                   companies=intern_ui.companies_watched(),
                                   gemini=intern_fit.available())
    await intern_ui.refuse(interaction, chunks[0])
    for chunk in chunks[1:]:
        await intern_ui.private_send(interaction)(chunk)


# ------------------------------------------------------------------ recent (no profile)

def _resolve_field(typed: str | None) -> str | None:
    """A field id, or a FIELDS label typed exactly (any case) -> its id; else None."""
    text = (typed or "").strip()
    by_label = {label.lower(): fid for fid, label in vocab.FIELDS}
    found = vocab.valid_ids("field", [text, by_label.get(text.lower())])
    return found[0] if found else None


def _resolve_where(typed: str | None) -> str | None:
    """A preset id or label, `st:XX`, `State: {Name}`, a state name or code -> its token."""
    text = " ".join((typed or "").split())
    low = text.lower()
    presets = {label.lower(): pid for pid, label in vocab.LOCATION_PRESETS}
    name = low[len("state:"):].strip() if low.startswith("state:") else low
    candidates = [low if low in vocab.LOCATION_PRESET_IDS else None,
                  f"st:{text[3:].upper()}" if low.startswith("st:") else None,
                  presets.get(low),
                  f"st:{STATE_BY_NAME[name]}" if name in STATE_BY_NAME else None,
                  f"st:{text.upper()}" if text.upper() in US_STATES else None]
    found = vocab.valid_ids("location", candidates)
    return found[0] if found else None


@internships.command(name="recent", description="Browse recent roles by field, level and place. No profile needed.")
@app_commands.describe(field="A field: start typing and pick one", level="Which kind of role",
                       where="A place: start typing and pick one (default anywhere in the US)",
                       days="How many days back to look (default 7)")
@app_commands.choices(level=[app_commands.Choice(name=n, value=v) for n, v in _LEVEL_CHOICES])
async def internships_recent(interaction: discord.Interaction, field: str | None = None,
                             level: app_commands.Choice[str] | None = None, where: str | None = None,
                             days: app_commands.Range[int, 1, 30] = 7) -> None:
    if not await intern_ui.need_access(interaction) or not await intern_ui.need_tracker(interaction):
        return
    field_id, where_id = _resolve_field(field) if field else None, _resolve_where(where) if where else "us"
    if field and field_id is None:
        await intern_ui.refuse(interaction, intern_text.unknown_choice("field", field))
        return
    if where_id is None:
        await intern_ui.refuse(interaction, intern_text.unknown_choice("where", where))
        return
    intern_ui.touch(interaction.user.id)
    await intern_ui.defer_reply(interaction)
    level_id, now = level.value if level else "intern_coop", time.time()
    levels = ("intern", "coop") if level_id == "intern_coop" else (level_id,)
    found = intern_match.browse(await intern_ui.window(), field=field_id, levels=levels,
                                locations=(where_id, "unlisted"), days=days, now=now,
                                gmap=intern_ui.window_gmap(), limit=None)
    send = intern_ui.private_send(interaction)
    if not found:
        await send(intern_text.browse_empty(days=days, companies=intern_ui.companies_watched()))
        return
    header = intern_text.browse_header(len(found), field=field_id, level_label=_LEVEL_LABELS[level_id],
                                       where_label=describe_locations((where_id,)), days=days)
    for chunk in intern_text.matches_messages(found[:intern_match.BROWSE_MAX], now, header=header,
                                              with_why=False):
        await send(chunk)


def _suggestions(context: str, build) -> list[app_commands.Choice[str]]:
    """`build()`'s choices; an autocomplete must never raise, so a failure is logged and empty."""
    try:
        return build()[:_CHOICES_MAX]
    except Exception as error:
        intern_ui.log_failure(context, error)
        return []


def _field_choices(current: str) -> list[app_commands.Choice[str]]:
    tokens = vocab.norm_text(current).lower().split()
    return [app_commands.Choice(name=label, value=fid) for fid, label in vocab.FIELDS
            if all(t in label.lower() for t in tokens)]


@internships_recent.autocomplete("field")
async def _field_autocomplete(interaction: discord.Interaction, current: str):
    return _suggestions("field autocomplete", lambda: _field_choices(current))


@internships_recent.autocomplete("where")
async def _where_autocomplete(interaction: discord.Interaction, current: str):
    return _suggestions("place autocomplete", lambda: [
        app_commands.Choice(name=label, value=value) for label, value in state_choices(current)])


# ------------------------------------------------------------------ ping

@dataclass(frozen=True)
class PingPlan:
    action: str                 # "on" | "off" | "set", as ping_reply takes it
    alerts: str | None          # the cadence for set_alerts; None: leave it (lifting a pause)
    alert_hour: int
    unpause: bool
    welcome: bool               # send the welcome DM, which doubles as the DM check


def _ping_plan(p: Profile, cadence: str | None, hour: int | None, now: float) -> PingPlan:
    """J12. A bare ping turns alerts on (from off, refused DMs or a pause) or off; it never
    turns a paused user's alerts off."""
    if cadence is not None or hour is not None:
        alerts = cadence or p.alerts
        keep = hour is not None and alerts in ("daily", "weekly")
        return PingPlan("set", alerts, hour if keep else p.alert_hour, False, False)
    if p.alerts == "off":
        return PingPlan("on", "daily", p.alert_hour, False, True)
    if p.dm_failures >= intern_store.DM_FAILURE_LIMIT:
        return PingPlan("on", p.alerts, p.alert_hour, False, True)
    if p.paused_until is not None and p.paused_until > now:
        return PingPlan("on", None, p.alert_hour, True, False)
    return PingPlan("off", "off", p.alert_hour, False, False)


@internships.command(name="ping", description="Turn internship DMs on or off, or set how often they come.")
@app_commands.describe(cadence="How often alerts come",
                       hour="Hour of day, in this bot's time zone, for daily and weekly alerts")
@app_commands.choices(cadence=[app_commands.Choice(name=n, value=v) for n, v in (
    ("Hourly", "hourly"), ("Daily", "daily"), ("Weekly (Mondays)", "weekly"), ("Off", "off"))])
async def internships_ping(interaction: discord.Interaction,
                           cadence: app_commands.Choice[str] | None = None,
                           hour: app_commands.Range[int, 0, 23] | None = None) -> None:
    if not await intern_ui.need_access(interaction) or not await intern_ui.need_finder(interaction):
        return
    db, uid, now = intern_ui.db, interaction.user.id, time.time()
    p = intern_store.load(db, uid)
    if p is None:
        await intern_ui.show_start(interaction)
        return
    intern_ui.touch(uid)
    plan = _ping_plan(p, cadence.value if cadence else None, hour, now)
    if plan.unpause:
        intern_store.set_paused_until(db, uid, None, now)
    if plan.alerts is not None:
        intern_store.set_alerts(db, uid, plan.alerts, plan.alert_hour, now,
                                cursor=intern_delivery.horizon(now))
    updated = intern_store.load(db, uid) or p
    await intern_ui.refuse(interaction, intern_ui.with_banner(
        updated, intern_text.ping_reply(updated, action=plan.action, now=now)))
    if plan.welcome:
        await intern_alert_views.send_welcome(interaction, updated)


# ------------------------------------------------------------------ info

def _find_posting(role: str) -> tuple | None:
    """`rowid` from autocomplete, else every typed word in company + title; blocked
    companies and non-job postings skipped, never resolved-then-refused."""
    pconn, source, role = intern_ui.pconn, intern_ui.source, role.strip()
    def findable(row) -> bool:
        return not source.is_blocked(row[_COMPANY_AT]) and bucket(row[_TITLE_AT]) != "excluded"
    if role.isdigit():
        row = pconn.execute(f"SELECT {_POSTING} FROM postings WHERE rowid = ?", (int(role),)).fetchone()
        if row and findable(row):
            return row
    tokens = role.lower().split()
    if not tokens:
        return None
    cond = " AND ".join(["(company || ' ' || title) LIKE ?"] * len(tokens))
    rows = pconn.execute(f"SELECT {_POSTING} FROM postings WHERE {cond} "
                         f"ORDER BY COALESCE(published, first_seen) DESC LIMIT {FIND_SCAN_MAX}",
                         [f"%{t}%" for t in tokens]).fetchall()
    return next((r for r in source.drop_blocked(rows, company_at=_COMPANY_AT) if findable(r)), None)


async def _details(c: intern_match.Candidate) -> dict:
    try:
        # An iCIMS board may sit on the company's own careers host; the registry says which.
        return await posting_details.fetch_details(
            c.platform, c.url, c.external_id, icims_hosts=intern_ui.source.icims_hosts()) or {}
    except Exception as error:       # a closed posting or a slow board: show what we have
        intern_ui.log_failure("fetching a posting's details", error)
        return {}


def _info_blocks(c: intern_match.Candidate, details: dict, p: Profile | None, now: float) -> list[str]:
    match, reason = intern_match.explain(p, c, now) if p else (None, None)
    plain = intern_match.Match(cand=c, score=0, band=None, why=(), group_key=c.rk, more=0,
                               place=location_fit(c.loc, _ANY_PLACE)[1], ledger=(c.rk_hash, c.ck_hash))
    desc = (details.get("description") or "").strip()
    if len(desc) > DESC_SNIPPET_MAX:
        desc = desc[:DESC_SNIPPET_MAX].rsplit(" ", 1)[0] + " …"
    block = intern_ui.with_banner(p, intern_text.match_block(match or plain, now, with_why=False))
    blocks = intern_text.info_blocks(block, details.get("salary"), desc)
    return blocks + ([intern_text.fit_line(match, reason)] if p else [])


@internships.command(name="info", description="Salary, description and fit for one posting.")
@app_commands.describe(role="Start typing a company or title and pick a suggestion")
async def internships_info(interaction: discord.Interaction, role: str) -> None:
    if not await intern_ui.need_access(interaction) or not await intern_ui.need_tracker(interaction):
        return
    row = _find_posting(role)
    if row is None:
        await intern_ui.refuse(interaction, intern_text.unknown_choice("role", role))
        return
    intern_ui.touch(interaction.user.id)
    await intern_ui.defer_reply(interaction)
    (c,) = intern_match.tag_rows([row])
    details = await _details(c)
    p = intern_store.load(intern_ui.db, interaction.user.id) if intern_ui.intern_error is None else None
    for chunk in pack(_info_blocks(c, details, p, time.time()), MAX_CHUNK, "\n\n"):
        await intern_ui.private_send(interaction)(chunk)


def _role_picks(user_id: int, tokens: list[str]) -> list[tuple[int, str, str]]:
    """(rowid, company, title): the user's top matches then the newest early-career roles
    from a warm window; from SQL alone when the window is cold (autocomplete cannot wait)."""
    cached = intern_ui.cached_window()
    if cached is None:
        conds = " AND ".join(["(company || ' ' || title) LIKE ?"] * len(tokens)) or "1 = 1"
        rows = intern_ui.pconn.execute(
            f"SELECT rowid, company, title FROM postings WHERE {conds} "
            f"ORDER BY COALESCE(published, first_seen) DESC LIMIT {_SQL_SUGGESTIONS}",
            [f"%{t}%" for t in tokens]).fetchall()
        return [r for r in intern_ui.source.drop_blocked(rows, company_at=1)
                if bucket(r[2]) in vocab.EARLY_CAREER]
    p = intern_store.load(intern_ui.db, user_id) if intern_ui.intern_error is None else None
    top = intern_match.rank(p, cached, time.time(), exclude=intern_ui.hidden(user_id),
                            gmap=intern_ui.window_gmap())[:_TOP_MATCHES] if p else []
    newest = sorted((c for c in cached if c.level in vocab.EARLY_CAREER), key=lambda c: -c.ts)
    return [(c.rowid, c.company, c.title) for c in (*(m.cand for m in top), *newest)]


def _role_choices(user_id: int, guild_id: int | None, current: str) -> list[app_commands.Choice[str]]:
    """Suggestions for `info`'s role: none for anyone this bot is not open to, here."""
    if not intern_ui.may_use(user_id, guild_id):
        return []
    if intern_ui.pconn is None or intern_ui.source is None:
        return []
    tokens, choices = current.lower().split(), {}
    for rowid, company, title in _role_picks(user_id, tokens):
        label = " ".join(f"{company} — {title}".split())[:_CHOICE_NAME_MAX]
        if rowid not in choices and all(t in label.lower() for t in tokens):
            choices[rowid] = app_commands.Choice(name=label, value=str(rowid))
        if len(choices) == _CHOICES_MAX:
            break
    return list(choices.values())


@internships_info.autocomplete("role")
async def _role_autocomplete(interaction: discord.Interaction, current: str):
    return _suggestions("role autocomplete",
                        lambda: _role_choices(interaction.user.id, interaction.guild_id, current))


# ------------------------------------------------------------------ the debug report (/diayn debug)

def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def _db_size(path: Path | None) -> str:
    """Size of a sqlite database including its -wal/-shm sidecars, which can
    hold megabytes of not-yet-checkpointed data."""
    total = 0
    for suffix in ("", "-wal", "-shm") if path else ():
        f = Path(str(path) + suffix)
        if f.exists():
            total += f.stat().st_size
    return _human_bytes(total) if total else "missing"


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    d = max(0, time.time() - ts)
    if d < 90:
        return f"{d:.0f}s ago"
    if d < 5400:
        return f"{d / 60:.0f}m ago"
    if d < 172800:
        return f"{d / 3600:.1f}h ago"
    return f"{d / 86400:.1f}d ago"


def _gemini_lines(pconn: sqlite3.Connection, source: postings_source.Source) -> list[str]:
    """Quota the scraper's CLI spends (`sweep --llm`, `list --llm`, `llm-diff`), from
    postings.db; the fit check's own is `_fit_lines`. Keyed by the day the source names,
    which SQL date('now') (UTC) is not."""
    quota = source.quota()
    row = pconn.execute("SELECT n, COALESCE(prompt_tokens,0), COALESCE(output_tokens,0) "
                        "FROM llm_usage WHERE day=?", (quota.today,)).fetchone()
    used, ptok, otok = row if row else (0, 0, 0)
    cap = quota.rpd
    pct = 100 * used / cap if cap else 0
    # Clamped: a lowered GEMINI_RPD can leave today's count above the cap.
    filled = min(10, int(pct // 10))
    bar = "█" * filled + "░" * (10 - filled)
    lines = ["**Gemini quota (today)**",
             f"`{bar}` {used}/{cap} requests ({pct:.0f}%) · model `{quota.model}`",
             f"tokens: {ptok:,} in · {otok:,} out · {ptok + otok:,} total" if ptok or otok
             else "tokens: not recorded yet (counted from the next Gemini call)",
             f"limits: {quota.rpm} req/min · {quota.tpm:,} tok/min · "
             f"{cap} req/day · resets at midnight {intern_clock.zone_label(quota.zone)}"]
    hist = pconn.execute("SELECT day, n FROM llm_usage ORDER BY day DESC LIMIT 7").fetchall()
    if len(hist) > 1:
        lines.append("last 7 days: " + " · ".join(f"{d[5:]} {n}" for d, n in hist))
    return lines


def _db_path(db: sqlite3.Connection | None) -> Path | None:
    """The file behind a connection: users.db, for the finder's. None in memory."""
    rows = db.execute("PRAGMA database_list").fetchall() if db is not None else []
    main = next((row[2] for row in rows if row[1] == "main"), "")
    return Path(main) if main else None


def _sweep_lines(pconn: sqlite3.Connection, source: postings_source.Source) -> list[str]:
    """Who sweeps, how often, and (B6) a warning when the last sweep is long overdue."""
    lines = [f"{source.sweeper_label} sweeps every {source.sweep_interval_s // 60}m"]
    stale = postings_source.stale_for(pconn, source, time.time())
    if stale is not None:
        lines.append(f"**no sweep for {stale / 3600:.1f}h** — check that the sweeper is running")
    return lines


def _store_lines(pconn: sqlite3.Connection, source: postings_source.Source) -> list[str]:
    seen_n = pconn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    post_n = pconn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    cache_n = pconn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
    db = intern_ui.db
    profiles = intern_store.summary(db)["profiles"] if intern_ui.intern_error is None else "?"
    sweep = pconn.execute("SELECT started, duration, errors, new_rows FROM sweeps "
                          "ORDER BY started DESC LIMIT 1").fetchone()
    return ["", "**Databases**",
            f"`postings.db` {_db_size(Path(source.db_path))} — {post_n:,} postings · "
            f"{seen_n:,} seen (dedup ledger) · {cache_n:,} cached verdicts",
            f"`users.db` {_db_size(_db_path(db))} — {profiles} internship profile(s)",
            "", "**Sweeps**", *_sweep_lines(pconn, source),
            f"last recorded sweep: {_ago(sweep[0])} · {sweep[1]:.0f}s · {sweep[2]} errors · "
            f"{sweep[3]} new" if sweep else "last recorded sweep: none yet"]


def _fit_lines(db: sqlite3.Connection) -> list[str]:
    """The Gemini fit check's day, from users.db (`intern_fit`): requests and tokens
    against FIT_RPD, the cache, and the class of the last fallback to unchecked."""
    now, limits = time.time(), intern_fit.limits()
    today = intern_fit.usage(db, intern_fit.quota_day(now))
    return intern_text.fit_debug_lines(
        key=intern_fit.available(), model=limits.model, requests=today.requests,
        prompt_tokens=today.prompt_tokens, output_tokens=today.output_tokens, rpd=limits.rpd,
        rpm=limits.rpm, batch=limits.batch, zone=intern_clock.zone_label(limits.zone),
        cached=intern_fit.cached_count(db), opted_out=intern_store.summary(db)["fit_off"],
        last_error=intern_fit.last_error, now=now)


async def _finder_lines() -> list[str]:
    db = intern_ui.db
    if intern_ui.intern_error is not None or db is None:
        return [intern_text.disabled_finder(intern_ui.intern_error or "not started")]
    parts = await intern_ui.window_parts()
    return intern_text.debug_lines(
        intern_store.summary(db), {key: intern_store.get_meta(db, key) for key in intern_alert_views.REPORT_KEYS},
        parts.supply if parts else {}, pdf_ok=resume_parse.pdf_supported(),
        migrated=intern_store.get_meta(db, intern_store.LEGACY_IMPORT_KEY))


async def debug_report() -> list[str]:
    """`/diayn debug`'s lines, for the owner only (`diayn_commands` asks). Counts only.
    Read-only: it reports what the last sweep and tick did rather than running either,
    so it never changes the numbers it exists to report. It reads the window, so the
    caller defers first."""
    finder = await _finder_lines()
    if intern_ui.intern_error is None and intern_ui.db is not None:
        finder = finder + [""] + _fit_lines(intern_ui.db)
    intern_ui.ensure_postings()                 # read after the await: a reopen may have run
    pconn, source = intern_ui.pconn, intern_ui.source
    return finder + [""] + (
        _gemini_lines(pconn, source) + _store_lines(pconn, source)
        if pconn is not None and source is not None
        else [intern_text.disabled_tracker(intern_ui.pconn_error or "")])


@internships.error
async def _on_internships_error(interaction: discord.Interaction, error: Exception) -> None:
    name = getattr(interaction.command, "name", "?")
    await intern_ui.fail_softly(interaction, f"/internships {name}", error)

