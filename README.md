# DIAYN

**Discord is all you need.** Pronounced "dine".

DIAYN is a Discord bot that finds internships for the people in your server.
It watches the public job boards of a few dozen companies, plus as many more as
you let it find, and sends each person a DM with the new roles that fit their
profile: any major, any level, filled in from a resume if they like. They can
also browse what is open, and ask about one role's salary and description.

You run it yourself, with a Discord bot of your own, on a Linux or macOS
machine. It is private by default: nobody but you can use it until you grant
access to a person or to a whole server.

The name is the reason it exists. The people it is for are already in a
Discord server, and should not have to refresh a hundred careers pages to hear
about a role.

- [Quick start](#quick-start) · [The Discord developer portal](#the-discord-developer-portal) · [Configuration](#configuration)
- [Who may use it](#who-may-use-it) · [Commands](#commands) · [Gemini](#gemini) · [Privacy](#privacy)
- [The politeness gate](#the-politeness-gate) · [Adding boards](#adding-boards) · [Linux and macOS only](#linux-and-macos-only)
- [DEPLOY.md](DEPLOY.md): running it for good, under pm2 or systemd, with backups and upgrades
- [CONTRACT.md](CONTRACT.md): the rules between the scraper and the bot
- [CLAUDE.md](CLAUDE.md): the rules for working in this repository

## Quick start

You need Python 3.10 or newer, on Linux or macOS, and a Discord account.

1. **In the [Discord developer portal](https://discord.com/developers/applications):**
   create an application, then open **Bot**, press **Reset Token** and copy the
   token. On the same page, turn on **Server Members Intent**, and turn
   **Public Bot** off unless others should be able to add it.
2. **Install:**

   ```sh
   git clone https://github.com/FakeZhiyuanLi/DIAYN.git
   cd DIAYN
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```

   The venv gets whichever Python `python3` is, and it must be 3.10 or newer.
   macOS's own `python3` is 3.9, and pip then fails to find python-dotenv
   without saying why. Install a newer Python (python.org or Homebrew) and make
   the venv with it, as in `python3.12 -m venv .venv`. `diayn.py` names both
   versions if the venv's Python is too old, and so do `internship_poller.py`
   and `resolve_boards.py` run on their own.

3. **Configure:** `cp example.env .env && chmod 600 .env`, then set
   `DISCORD_TOKEN` and `POLL_CONTACT` in it.
4. **Set up:** run `.venv/bin/python diayn.py setup`. It does four things:
   - checks the token with Discord, and the intent;
   - creates `data/`, at mode 700;
   - bootstraps `postings.db`: the first sweep records everything open now as
     seen, so nothing old is announced;
   - prints the invite link.
5. **Run:** invite the bot to your server with that link, then run
   `.venv/bin/python diayn.py run`. [DEPLOY.md](DEPLOY.md) has pm2 and systemd
   examples for keeping it running.
6. **Use it:** `/internships profile` to start. As the owner, `/diayn grant`
   lets others in.

`.venv/bin/python diayn.py doctor` checks everything again at any time: the
token, the intent, the data directory, the sweeper, the last sweep,
`POLL_CONTACT`, the Python version, the platform, and that discord.py and
pypdf are installed.

## The Discord developer portal

Everything the bot needs from Discord is set on the application's pages:

- **Bot → Reset Token** gives the token, which goes in `DISCORD_TOKEN`. Anyone
  with it can act as your bot: keep it in `.env`, which git ignores, and reset
  it if it ever leaks.
- **Bot → Privileged Gateway Intents → Server Members Intent: on.** The bot
  uses it to notice when someone has left every server it shares with them, and
  to tell, in a DM, whether someone belongs to a server that has access. It
  cannot log in without it: Discord refuses the connection. `setup` stops and
  `doctor` fails until it is on.
- **Presence Intent and Message Content Intent: off.** The bot has no use for
  either: every command is a slash command.
- **Bot → Public Bot: off**, unless other people should be able to add the bot
  to their servers. Even a public bot answers only its owner and those granted
  access.
- **The invite link**, which `setup` prints, asks for the scopes `bot` and
  `applications.commands` and no permission in the server. Every reply is an
  interaction response and every alert a DM, and neither needs one.

Discord delivers a DM only to someone who shares a server with the bot and
accepts DMs from that server's members.

## Configuration

Every setting is an environment variable, usually set in `.env`, and
[`example.env`](example.env) documents each one with the code's own default.

| Key | Required | What it does |
|---|---|---|
| `DISCORD_TOKEN` | yes | Your bot's token, from the developer portal. |
| `POLL_CONTACT` | strongly recommended | Goes in the User-Agent every job board sees, so a board's owner can reach you rather than block you. Use a project URL or a role mailbox, never a personal address. |
| `DIAYN_OWNER_IDS` | no | Comma-separated Discord user ids of the bot's owners. Default: the Discord application's owner, or its team's members. |
| `DIAYN_TZ` | no | The zone for alert hours and the daily housekeeping, named wherever an hour is shown. Default `UTC`. |
| `DIAYN_DATA` | no | The data directory, as an absolute path. Default: `data/` in the checkout, never the working directory. |
| `POSTINGS_DB`, `BOARDS_FILE`, `YC_CACHE` | no | One data file somewhere other than `DIAYN_DATA`, as an absolute path. |
| `GEMINI_API_KEY` | no | Turns on the [fit check](#the-fit-check), and lets a scraper command run with `--llm`. |
| `GEMINI_MODEL`, `FIT_BATCH`, `FIT_RPD`, `FIT_RPM` | no | The model both uses of the key share, and the fit check's batch size and budget. |
| `GEMINI_RPM`, `GEMINI_RPD`, `GEMINI_TPM` | no | The budget of `--llm`. |
| `LLM_DAY_TZ` | no | The zone of Gemini's quota day, separate from `DIAYN_TZ`. Default `America/Los_Angeles`, where the free tier resets. |
| `POLL_HOST_CONCURRENCY`, `POLL_HOST_MIN_INTERVAL` | no | The per-host [politeness gate](#the-politeness-gate). |
| `POLLER_ENV_FILE` | no | Where the `.env` is, when it is not in the checkout. Set it in the process environment, never in a `.env`. |

Three rules decide what a run actually uses:

- **The `.env` comes from `POLLER_ENV_FILE`, or else from the checkout**, beside
  `diayn.py`. Never from the working directory. `POLLER_ENV_FILE` naming a
  missing file stops the start.
- **The environment wins over the file.** The `.env` only fills in what is not
  already set, so a value in pm2's or systemd's environment, or a shell export,
  is the final word. An empty value keeps the default.
- **Settings are read once, when a command starts.** A bad value (a count below
  1, a misspelt time zone, a relative or `~` path in `DIAYN_DATA`,
  `POSTINGS_DB`, `BOARDS_FILE` or `YC_CACHE`) stops the start with a message
  naming the variable.

`.venv/bin/python diayn.py config` prints the `.env` it used and every setting
a run would use. It shows `GEMINI_API_KEY` and `DISCORD_TOKEN` only as set or
not set, and `DIAYN_OWNER_IDS` only as a count, so its output can be pasted
anywhere.

## Who may use it

The bot is private. Until you grant access, it answers only its owner:
`DIAYN_OWNER_IDS`, or else the Discord application's owner, or its team's
members. Someone may use it when they are:

- an owner;
- a user granted by id;
- running a command inside a server that has a grant;
- in a DM, a member of a server that has a grant.

Anyone else gets an ephemeral "This bot is private. Ask whoever runs it for
access.", which never names you. Access is checked in every command, every
button and menu, the `/internships info` autocomplete, the save step of every
form, and before every alert.

**Always open, with or without access:** anything that removes data or reduces
contact. `/internships delete`, the profile card's "Delete my data" and its
confirmation, and the alert buttons "Stop" and "Pause". Nobody is ever stuck
with their data or their alerts.

**Taking access away** stops alerts at the next delivery tick. A profile whose
owner has had no access for 30 days is deleted, as is one whose owner has left
every server the bot shares with them for 30 days.

Grant and revoke in Discord with `/diayn grant` and `/diayn revoke`, or from
the host with [`.venv/bin/python diayn.py grant`](#grant), which works before
the bot has ever started.

## Commands

### In Discord

| Command | Does |
|---|---|
| `/internships profile` | Sets up or edits a profile: majors, degree, graduation, the kinds of role and places wanted, alert times. Attach a resume to fill it in. |
| `/internships matches` | Roles that fit the profile, best or newest first, with why each one matched. |
| `/internships recent` | Browses recent roles by field, level and place. No profile needed. |
| `/internships info` | Salary, description and fit for one role. |
| `/internships ping` | Turns alert DMs on or off, or sets how often they come: hourly, daily or weekly. |
| `/internships delete` | Shows everything stored about you, and erases it. |
| `/internships help` | How the finder works and what it keeps. |
| `/diayn grant user` · `/diayn grant server` | Owner only. Lets one person, or everyone in this server, use the bot. |
| `/diayn revoke user` · `/diayn revoke server` | Owner only. Takes that away. |
| `/diayn access` | Owner only. Who has access: counts, and the servers by name. |
| `/diayn debug` | Owner only. Sweep health, delivery, the fit check's usage, coverage by field. |

### On the host

    .venv/bin/python diayn.py <command> [options]

Every command here is typed in the checkout, with the venv's Python, as the
quick start installed it: nothing needs the venv activated, and a bare
`python` is often not there at all. What DIAYN prints when it points at a
command is spelled the same way, with whichever Python is running it.

DIAYN's own commands are `setup`, `doctor`, `run`, `grant`, `revoke` and
`import-legacy`. The rest are the scraper's, which also run as
`.venv/bin/python internship_poller.py <command>`, with the same arguments and
exit codes: 0 done, 1 failed, 2 a usage error, 3 another sweeper holds the
lock. `.venv/bin/python diayn.py <command> --help` lists a command's options.

### `setup`

Gets a new host ready, and is safe to run again: nothing that exists is
changed, except that what others on the box could read is made private. In
order, stopping at the first failure, it:

- checks that discord.py is installed, since `run` cannot start the bot
  without it, and warns without pypdf, which reads PDF resumes; either way it
  says how to install them for the Python running it;
- checks `DISCORD_TOKEN` with Discord, and that the Server Members Intent is
  on: nothing is made until both are;
- makes the data directory at mode 700. One that is there already, and that
  other users on the box can read, it tightens to 700, and `users.db` and
  `postings.db`, with their `-journal`, `-wal` and `-shm`, to 600, and says
  what it tightened;
- bootstraps `postings.db`, holding the sweeper lock, with a first sweep that
  records every open posting as seen, so none of them is announced. It never
  bootstraps a file that is already there: one with a ledger is left as it is,
  and one with an empty ledger is refused, since only you can say whether it is
  new (`sweep --init`) or the wrong file;
- prints the invite link.

### `doctor`

Checks the host, and changes nothing: the Python version, the platform,
discord.py and pypdf, the settings, the token and the intent, the data
directory, `postings.db`, how long ago the last sweep began, whether anything
holds the sweeper lock, `POLL_CONTACT`, and whether a Gemini key is set. Each check prints `ok`,
`warn`, `note` or `fail`, every check runs whatever an earlier one found, and
it exits 1 when anything is to fix.

### `run`

    .venv/bin/python diayn.py run            # --interval N and --llm as for watch

Runs the Discord bot and the sweep loop together, in one process, until it is
stopped. This is what pm2 or systemd runs.

- **One at a time.** It takes the sweeper lock before it logs in to Discord and
  holds it for life, so a second `run`, or a `watch` beside it, exits 3 and
  never starts a second bot on the same token.
- **The sweep loop is `watch`'s own.** A failed sweep is logged and the next
  runs a full interval later. If the loop ever ends, the process exits 1, so
  that pm2 or systemd starts it again rather than leaving a bot with nothing
  sweeping behind it.
- **The bot only reads `postings.db`**, through the contract, on a read-only
  connection of its own, and keeps its own data in `users.db`.
- It needs `DISCORD_TOKEN`, and refuses to start without a `postings.db`: it
  never makes one. `setup` does.
- **Exit 78 is a setting, not a crash.** If Discord refuses the Server Members
  Intent, `run` says which portal toggle to turn on and exits 78 (EX_CONFIG),
  after one login, never retrying. DEPLOY.md's pm2 and systemd units do not
  restart on 78: a loop of refused logins can get the bot's token reset.

### `grant`

    .venv/bin/python diayn.py grant --user <id>     # or --server <id>

Lets one person, or everyone in one server, use the bot. The grant is written
into `users.db`, so it works before the bot has ever started, and the running
bot sees it on its next check. It prints what it did, never an id. A server's
id is its **Copy Server ID** in Discord, with Developer Mode on.

### `revoke`

    .venv/bin/python diayn.py revoke --user <id>    # or --server <id>

Takes a grant away. Someone keeps access only through another grant, or by
being an owner.

### `import-legacy`

    .venv/bin/python diayn.py import-legacy --from /path/to/old/stats.db

For a host that ran the older `/internships ping` tracker DIAYN grew out of:
copies its subscribers out of that bot's `stats.db` into `users.db`, as
profiles. It opens the old file read-only and leaves it as it was. It runs
once: a second run is refused, so nobody who has since deleted their data comes
back. It prints counts only, and exits 1 unless every subscriber was either
imported or already had a profile. An old file it cannot open, or a `users.db`
it cannot open or make, is refused in one line, with exit 1.

### The sweeper lock

Exactly one process may write `postings.db`. The commands that write hold
`<POSTINGS_DB>.lock` while they run, and `run` and `watch` hold it for as long
as they run. A writing command that finds the lock held **exits 3, having done
nothing**, so a log can tell "another sweeper is running" (3) from "failed"
(1). The commands that only read never wait for it.

| Command | Writes, so takes the lock | Opens `postings.db` |
|---|---|---|
| `verify` | no | no |
| `list` | only with `--llm` | only with `--llm` |
| `sweep`, `watch`, `run` | yes | yes; `sweep` and `watch` create it only with `--init` |
| `stats` | no | yes, read-only (`mode=ro`) |
| `prune` | yes | yes |
| `discover` | yes (`boards.json`, `yc_cache.json`) | no |
| `llm-diff` | yes (the Gemini cache) | yes |
| `upgrade-db` | yes | yes |
| `setup` | only to bootstrap | creates it when there is none |
| `doctor` | for an instant, only when nothing holds it | yes, read-only |
| `config` | no | no |

**No command creates `postings.db` unasked.** Only `setup`, `sweep --init` and
`watch --init` may, and `sweep`, `watch` and `run` refuse an existing file
whose `seen` ledger is empty. A new, empty ledger makes every open posting look
new, so its first sweep would announce the entire market; that is a bootstrap,
and only somebody who means one should get one.

### `verify`

Probes every board in the registry once and prints, per board, whether it
answered, how many jobs it lists and how many are recent technical
internships. Use it after adding boards. `--sector finance` narrows it to one
sector. It reads nothing from `postings.db` and writes nothing.

### `list`

Fetches every board live and prints the open internships, grouped by sector
and company, with near-duplicate postings collapsed into one role.

- `--us`: US and remote only.
- `--category swe|quant|hardware|data-ml|pm|other`, `--sector <sector>`.
- `--all-roles`: include non-technical internships.
- `--max-age N`: ignore postings older than N days (default 30; 0 for no
  limit). `--strict` also drops postings with no date, or only Workday's
  "30+ days ago".
- `--dupes`: show the collapsed duplicates under each role.
- `--llm`: classify with Gemini. This writes Gemini's verdicts to the cache in
  `postings.db`, so it needs the file to exist, and takes the lock.

### `sweep`

One sweep: fetch every board (sending ETags, so an unchanged Greenhouse, Lever
or Ashby board costs a `304`), record each posting not seen before, prune
`postings` to 30 days, refresh the contract tables, and print what was new.
All of a sweep's writes are one transaction: it lands whole or not at all.

- `--init`: create `postings.db` if it is missing, and allow a first sweep into
  an empty ledger. It never adopts an existing file that holds another schema,
  and no command opens a file whose `user_version` is not 2.
- `--llm`: classify the new postings with Gemini.
- `--interval N`: the gap between sweeps that `scraper_meta` tells the bot to
  expect (default 900, at least 60).

### `watch`

Sweeps every `--interval` seconds (default 900, 15 minutes) until stopped: the
sweep loop alone, without the bot. `run` runs this same loop beside the bot.
It holds the lock for life, and a second `watch` exits 3.

- **A failed sweep does not stop it.** The sweep is rolled back, the error is
  logged, and the next sweep runs a full interval later.
- **A restart does not sweep at once.** It first waits until one interval
  after the last sweep began, finished or not: each sweep records its start in
  the lock file before its first request. So a crash loop, even one that dies
  mid-sweep, cannot hit every job board on every restart.
- It logs one timestamped line per sweep.

`--init` and `--llm` work as for `sweep`.

### `stats`

Counts the retained postings and technical internships, the size of the `seen`
ledger, and the internships per sector. Read-only: it opens the file
`mode=ro`, takes no lock and changes nothing, so it is safe beside any sweeper.

### `prune`

Deletes `postings` rows older than `--max-age` days (default 30). `--dry-run`
only counts them. Every sweep already prunes at 30 days, so this is rarely
needed. **Anything below 30 is refused**, because the bot shows postings up to
30 days old. Rows with no date, and Workday's "30+ days ago", are kept, since
nothing proves they are old. `seen` is never touched, so a pruned role is never
announced again.

### `discover`

Finds more boards. It mines ATS links from public internship listings on
GitHub, checks each candidate against the live board, and writes every board
with at least `--min-interns N` (default 1) recent technical internships to
`BOARDS_FILE`, with sector `unknown`. The seed boards are merged in at load.

- `--workday`: also check the Workday boards it mines (slow).
- `--common-crawl`: also ask Common Crawl (often down; best effort).
- `--yc`: also probe Y Combinator's public company list for boards (slow).
  Results are cached in `YC_CACHE`; `--yc-limit N` probes at most N uncached
  companies, and `--yc-recheck` ignores the cache.

**It rewrites `boards.json` whole**, over any hand edits. What it finds changes
which boards every later sweep polls, and every posting on a new board is new
to the ledger, so the next sweep hands all of them to the bot at once.

### `llm-diff`

Classifies the most recent stored postings (`--limit`, default 200) with both
the regular expressions and Gemini, and prints where they disagree. It is how
to decide whether `--llm` is worth turning on. It spends Gemini quota and
writes the cache, so it takes the lock; it needs `GEMINI_API_KEY`.

### `upgrade-db`

Brings a `postings.db` written by an older scraper, from before the contract
tables, up to [CONTRACT.md](CONTRACT.md), in place: it switches the file to
WAL, adds the contract tables and writes `scraper_meta`. It refuses, having
changed nothing, a file that fails `PRAGMA integrity_check` or is not schema
version 2. It prints each table's row count and highest rowid before and
after, and exits non-zero if any moved. It is safe to run again.

### `config`

Prints the `.env` used and every setting a run would use, by variable name.
The Gemini key and the Discord token are shown only as set or not set, and the
owners' ids only as a count.

## Gemini

Optional, and strongly recommended. Without `GEMINI_API_KEY`, nothing is sent
to Gemini and everything else still works: alerts carry the rule-based matches
unchecked. With it, the key is used in two ways, each with a budget of its own,
and the two budgets together must fit the key's quota on your AI Studio
dashboard. Both send their requests through `llm.py`, share `GEMINI_MODEL`,
`GEMINI_MAX_ATTEMPTS` and `GEMINI_HTTP_TIMEOUT`, and count their day in
`LLM_DAY_TZ`.

### The fit check

Before an alert is sent, Gemini is asked whether each role the rule-based
matcher picked suits that person, and answers `fit`, `unsure` or `no_fit`
with a reason of at most 120 characters. A `no_fit` role is not sent, and the
alerts move on past it all the same; `fit` roles come first, then `unsure`, and
the DM shows each one's reason. At most `FIT_BATCH` roles go in one request
(default 15), at most `FIT_RPD` requests a day (200) and `FIT_RPM` a minute
(10). Verdicts are cached in `users.db` for 45 days, by a fingerprint of the
profile's labels and the role, so a role is asked about once per profile and
asked again after the profile changes. `/internships matches` shows the cached
verdicts and never makes a request.

The check never holds an alert back. Without a key, with the day's budget
spent, on an API error or an answer that does not parse, the alert goes out
with the rule-based matches unchecked and no reason lines. It keeps its own
time, whatever `GEMINI_HTTP_TIMEOUT` says (where 0 means no deadline): a request
gets 90 seconds, its retries included, and all the requests of one delivery tick
get three minutes together; whatever they do not reach goes out unchecked.
After a request fails or times out, the check asks nothing for ten minutes, so
an outage costs one alert a wait rather than every alert in turn. `/diayn debug` shows the day's requests
and tokens and the class of the last failure. Each person can turn the check
off from their profile card; it is on for everyone else. What it sends is
listed under [Privacy](#privacy).

### `--llm`

With `GEMINI_API_KEY` set, `--llm` classifies newly seen postings by title with
Gemini instead of the regular expressions, in batches of `GEMINI_BATCH`. A
sweep sends only the postings it has never seen, which is what keeps a busy day
inside a free-tier budget: tens of calls, not thousands. Verdicts are cached in
`postings.db`, usage is counted per day in `LLM_DAY_TZ` (the free tier resets
at midnight Pacific), and a request that keeps failing leaves its postings to
the regular expressions.

The limits default to the stricter free tier (`GEMINI_RPM=5`,
`GEMINI_RPD=250`) so a change of model cannot exceed one by surprise; check
your own dashboard before raising them. `run` classifies with the regular
expressions unless it is given `--llm`.

## Privacy

**What is stored**, all of it in the data directory. Whichever command makes
it first (`setup`, `grant`, `import-legacy` or `sweep --init`) makes it at mode
700, and each database in it is made at 600. `setup` tightens any of them that
is looser:

- `postings.db`, the scraper's ledger of public job postings. The bot only
  ever reads it.
- `users.db`, the bot's own: each person's profile (the labels they chose or
  confirmed), which roles they were sent or hid, their alert settings, the
  access grants, and the fit check's cache and budget. It holds Discord ids.
  Whoever runs the bot can read it; keep it, and its backups, private.

A resume given to the bot is read once and thrown away. It is read in a
separate, short-lived process that is given none of the bot's environment
variables (so no bot token or API key), cannot write a byte to any file or
leave a core dump, is held to 15 seconds of CPU and is killed after 20 seconds.
That is not a full sandbox: the process runs as the bot's own user, so it can
still read files that user can, `.env` included, and open network connections.
The bot process never parses, stores or logs the resume's text; only the
vocabulary the person confirms is kept. Whoever runs this bot can read what it
stores. A profile is deleted after a year unused, and 30 days after its owner
leaves every server the bot shares with them or loses access to the bot.
`/internships delete` erases everything at once.

**What the fit check sends to Google.** Only where the host has set
`GEMINI_API_KEY`, and only for someone who has not turned the check off:
before an alert, one request to Google's Gemini API holding that person's
profile as labels (majors, minors, degree, graduation date, kinds of role,
fields, skills, keywords, places and terms) and, for each role, its title,
company, location and term. Never the resume or any of its text, a name, a
Discord id, an email address or other contact details: the profile holds none
of those. Google's terms for the Gemini API govern what it receives. The
answers are kept in `users.db` for 45 days under a fingerprint of those labels,
never under a person's id. The consent screen says the same, whenever the host
has a key.

**What the job boards see**: requests from your machine, with DIAYN's
User-Agent and your `POLL_CONTACT`. Salaries and descriptions are fetched for
one role at a time, when someone asks with `/internships info`.

## The politeness gate

These are other people's public endpoints, and a burst of requests to one host
is what gets an address blocked. Every request passes through one gate, wrapped
around the HTTP session itself, so no adapter can forget it and a new one
inherits it:

- **Per host:** at most `POLL_HOST_CONCURRENCY` requests in flight (default 4),
  and at least `POLL_HOST_MIN_INTERVAL` seconds between the starts of two
  requests (default 0.12). Overall, a sweep fetches at most 20 boards at once.
- **Conditional requests:** Greenhouse, Lever and Ashby boards are fetched with
  their last ETag, so an unchanged board answers `304` with no body.
- **An honest User-Agent:** `DIAYN/1.0 (+https://github.com/FakeZhiyuanLi/DIAYN;
  contact: <POLL_CONTACT>)`, never a browser's, so a board's owner can tell
  this traffic apart and knows where to write. The bot's salary and description
  fetches, and `resolve_boards.py`, send the same one.
- **One sweeper, every 15 minutes.** The lock stops a second process from
  doubling the traffic, and a restarted sweep loop waits for its turn.

Gemini's API is the one exception: each of its two uses has a budget of its
own (see [Gemini](#gemini)).

## What it covers

The built-in registry, `SEED_BOARDS` in `internship_poller.py`, holds 63 boards
on Greenhouse, Lever, Ashby and Workday, each probed by hand. `discover` adds
more, into `boards.json`. There are also adapters for iCIMS, Eightfold and
Taleo, for boards added by hand.

Each posting is classified by title with regular expressions: is it an
internship, is it technical, its category (`swe`, `quant`, `hardware`,
`data-ml`, `pm`, `other`), its term (`Summer 2027`) and its region. Gemini can
classify instead (`--llm`). Boards carry a sector: `tech`, `finance`,
`healthcare`, `defense`, `industrial`, `retail`, `energy`, or `unknown` for a
board `discover` found.

Two tables do the remembering. `seen` holds every posting id DIAYN has ever
recorded, and is never pruned, so a role is announced once, however long it
stays open. `postings` holds the details for 30 days, and every sweep prunes
anything older.

The finder's place presets are Southern California's, and its major aliases
follow one university's catalogue. Both are data rather than code, and a later
release can generalise them.

## Blocking a company: `BLOCKED_COMPANIES`

To stop showing a company entirely, add its name to `BLOCKED_COMPANIES` in
`internship_poller.py`. Its boards are dropped at load, so they are never
polled, stored or announced, and the block is published to the bot, which
filters that company's stored rows out of everything it shows.

The match is on letters and digits only, and on the **start** of a name, because
the name a board is filed under is rarely the name anybody types: `Rocket Lab`
also blocks the `rocketlabusa` slug and the `rocketlab/wd1/...` Workday path.
The trap is the other direction: a short or common entry blocks every company
whose name begins with it. Keep entries long and distinctive. An entry that is
empty after normalising is ignored, since it would block everything.

Two consequences worth knowing:

- Rows stored before the block stay in `postings` until they age out, and every
  reader filters them in the meantime.
- **Unblocking a company hands its whole open board to the next sweep as new**,
  because its postings never entered `seen` while it was blocked.

## Adding boards

Every board is `[platform, slug, company, sector]`. A Workday slug is
`tenant/wdN/SitePath`, taken from the careers URL
(`https://capitalone.wd1.myworkdayjobs.com/Capital_One_Careers` is
`capitalone/wd1/Capital_One_Careers`); the site path cannot be guessed.

1. **Find the board.** Paste the careers URL you see in a browser into
   `resolve_boards.py`, which follows redirects, identifies the ATS, and emits
   only boards that return real jobs:

   ```sh
   .venv/bin/python resolve_boards.py https://careers.example.com
   .venv/bin/python resolve_boards.py --file careers_urls.txt --emit   # rows for SEED_BOARDS
   ```

   It never guesses slugs: guessed ones rarely exist, and an LLM's are
   confidently invented.
2. **Add it.** For your host only, add the row to `boards.json` in the data
   directory (a JSON list of rows). A malformed row is skipped with a warning
   and never stops a start. To add a board for everyone, add it to
   `SEED_BOARDS` in a pull request.
3. **Check it.** `.venv/bin/python diayn.py verify --sector <its sector>`.

A board added this way behaves like one `discover` found: its open postings are
all new to the ledger, and arrive at the bot together at the next sweep.

## Linux and macOS only

DIAYN runs on Linux and macOS only, for now. Three things stand in the way on
Windows:

- The sweeper lock uses `fcntl`, which Windows does not have.
- The resume reader limits its process with `resource`, which Windows does not
  have either. Without those limits, the promise that nothing it reads reaches
  a file would not hold.
- Time zones need a zone database, which Windows has only with the `tzdata`
  package.

On Windows, every command says so and stops. `doctor` checks the platform
along with everything else.

## Layout

```
diayn.py               the entry point: setup · doctor · run · grant · revoke · import-legacy, and every scraper command
host_checks.py         what setup does and doctor checks
discord_portal.py      what setup and doctor ask Discord about the bot
hints.py               the commands and install steps messages tell someone to type, spelled with the running Python
private_files.py       how every command makes the data directory (mode 700) and a database (600)
internship_poller.py   the scraper: sweep, watch, stats, prune, discover, upgrade-db, …
llm.py                 one request to Gemini, for the fit check and --llm
resolve_boards.py      finds the job board behind a careers page
bot/                   the Discord bot: app.py, access, and the finder's modules
contract/              the scraper's schema, and fixtures both halves are tested against
tests/  tests/bot/     standard-library unittest; no network, no real database
data/                  gitignored: postings.db · users.db · boards.json · yc_cache.json
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests     # with requirements.txt installed
python3 -m unittest discover -s tests              # bare: aiohttp is stubbed
```

Standard library only, no network, and no real `postings.db` or `users.db`:
every fixture is built in memory or in a temporary directory. A run without
python-dotenv skips the few tests that need it, and a run without discord.py
skips the bot's tests that need it; both are expected. CI runs Python 3.10 and
3.12, each with all of `requirements.txt`, with only the scraper's aiohttp and
python-dotenv, and with only python-dotenv, and guards the repository against
tracked data and secrets.

## Provenance

DIAYN began as the internship tracker inside BaronChairStair, a student club's Discord bot, and was moved out to stand on its own.

It is released under the [MIT License](LICENSE).
