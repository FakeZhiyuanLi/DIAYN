# DIAYN

**Discord is all you need.** Pronounced "dine".

DIAYN watches the public job boards of a few dozen companies, plus as many more
as you let it find, and records every internship posting it sees in a SQLite
file, `postings.db`. It never talks to Discord itself. The BaronChairStair
bot reads that file and does the talking: the alerts, `/internships` search, and the matches.

That split is the reason for the name. The people this is for are already in a
Discord server, and should not have to refresh a hundred careers pages to hear
about a role. DIAYN is the half that refreshes the pages; the bot is the half
that tells them.

- [Install](#install) · [Configuration](#configuration) · [Commands](#commands)
- [The politeness gate](#the-politeness-gate) · [Blocking a company](#blocking-a-company-blocked_companies) · [Adding boards](#adding-boards)
- [CONTRACT.md](CONTRACT.md): what the bot may assume about `postings.db`
- [DEPLOY.md](DEPLOY.md): running it on the server under pm2, backups, upgrades, and the one-time move from the bot
- [CLAUDE.md](CLAUDE.md): the rules for working in this repository

## What it covers

The built-in registry, `SEED_BOARDS` in `internship_poller.py`, holds 63 boards
on Greenhouse, Lever, Ashby and Workday, each probed by hand. `discover` adds
more, into `boards.json`. There are also adapters for iCIMS, Eightfold and
Taleo, for boards added by hand.

Each posting is classified by title with regular expressions: is it an
internship, is it technical, its category (`swe`, `quant`, `hardware`,
`data-ml`, `pm`, `other`), its term (`Summer 2027`) and its region. Gemini can
classify instead (`--llm`, below); nothing needs it. Boards carry a sector:
`tech`, `finance`, `healthcare`, `defense`, `industrial`, `retail`, `energy`,
or `unknown` for a board `discover` found.

Two tables do the remembering. `seen` holds every posting id DIAYN has ever
recorded, and is never pruned, so a role is announced once, however long it
stays open. `postings` holds the details for 30 days, and every sweep prunes
anything older.

Salaries and job descriptions are not fetched here. The bot fetches them
itself, for the one role a user asks about.

## Install

Python 3.10 or newer.

```sh
git clone https://github.com/FakeZhiyuanLi/DIAYN.git && cd DIAYN
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp example.env .env && chmod 600 .env     # then edit it; see below
.venv/bin/python internship_poller.py config
.venv/bin/python internship_poller.py sweep --init     # the first sweep of a new postings.db
```

On a laptop, the defaults put everything under `data/` in the checkout, which
git ignores. On the server, see [DEPLOY.md](DEPLOY.md).

## Configuration

Every setting is an environment variable, and [`example.env`](example.env)
documents each one with the code's own default. Three rules decide what a run
actually uses:

- **The `.env` comes from `POLLER_ENV_FILE`, or else from beside
  `internship_poller.py`.** Never from the working directory, so DIAYN started
  from the bot's checkout cannot read the bot's `.env`. `POLLER_ENV_FILE`
  naming a missing file stops the start.
- **The environment wins over the file.** The `.env` only fills in what is not
  already set, so a value in pm2's environment or a shell export is the final
  word. An empty value keeps the default.
- **Settings are read once, when a command starts,** and never while the
  module is imported. A bad value (a count below 1, a misspelt time zone)
  stops the start with a message naming the variable.

`python internship_poller.py config` prints the `.env` it used and every
setting a run would use. It shows `GEMINI_API_KEY` and `DISCORD_TOKEN` only as
set or not set, and `DIAYN_OWNER_IDS` only as a count, so its output can be
pasted anywhere.

`DIAYN_DATA` is the data directory, the checkout's own `data/` by default.
`postings.db`, `boards.json` and `yc_cache.json` live in it unless
`POSTINGS_DB`, `BOARDS_FILE` or `YC_CACHE` names one on its own. It must be an
absolute path.

## Commands

    python internship_poller.py <command> [options]
    python diayn.py <command> [options]          # the same, through DIAYN's entry point

`diayn.py` runs every command below with the same arguments and exit codes.
Of its own commands for the Discord bot, three are built:

    python diayn.py grant --user <id>        # or --server <id>
    python diayn.py revoke --user <id>       # or --server <id>

let one person, or everyone in one server, use the bot, or stop them. The bot
is private: only its owner (`DIAYN_OWNER_IDS`, or else the Discord
application's owner) may use it until someone is granted access. A server
grant covers anyone using the bot inside that server, and that server's
members anywhere, DMs included. These write the grant into `users.db`, so they
work before the bot has ever started, and the running bot sees the change on
its next check. They print what they did, never an id.

    python diayn.py import-legacy --from /path/to/old/stats.db

copies the subscribers of the old `/internships ping` tracker out of its bot's
`stats.db` into `users.db`, in the data directory, as profiles. It opens the
old file read-only and leaves it as it was. It runs once: a second run is
refused, so nobody who has since deleted their data comes back. It prints
counts only, and exits 1 unless every subscriber was either imported or
already had a profile. The others (`setup`, `doctor` and `run`) are not built
yet: each says so and exits 2.

**The sweeper lock.** Exactly one process may write `postings.db`. The commands
that write hold `<POSTINGS_DB>.lock` while they run, and `watch` holds it for
as long as it runs. A writing command that finds the lock held **exits 3,
having done nothing**, so a log can tell "another sweeper is running" (3) from
"failed" (1). The commands that only read never wait for it.

| Command | Writes, so takes the lock | Opens `postings.db` |
|---|---|---|
| `verify` | no | no |
| `list` | only with `--llm` | only with `--llm` |
| `sweep`, `watch` | yes | yes; creates it only with `--init` |
| `stats` | no | yes, read-only (`mode=ro`) |
| `prune` | yes | yes |
| `discover` | yes (`boards.json`, `yc_cache.json`) | no |
| `llm-diff` | yes (the Gemini cache) | yes |
| `upgrade-db` | yes | yes |
| `config` | no | no |

**No command creates `postings.db` unasked.** Only `sweep --init` and
`watch --init` may, and `sweep` and `watch` also refuse an existing file whose
`seen` ledger is empty. A new, empty ledger makes every open posting look new,
so its first sweep would announce the entire market; that is a bootstrap, and
only somebody who means one should get one.

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

Sweeps every `--interval` seconds (default 900, 15 minutes) until stopped.
This is what pm2 runs on the server. It holds the lock for life, and a second
`watch` exits 3.

- **A failed sweep does not stop it.** The sweep is rolled back, the error is
  logged, and the next sweep runs a full interval later.
- **A restart does not sweep at once.** It first waits until one interval
  after the last sweep began, finished or not: each sweep records its start in
  the lock file before its first request. So a crash loop under pm2, even one
  that dies mid-sweep, cannot hit every job board on every restart.
- It logs one timestamped line per sweep. The new postings themselves are the
  bot's to announce.

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

**It rewrites `boards.json` whole**, over any hand edits. On the server, what it
finds changes which boards every later sweep polls, and every posting on a new
board is new to the ledger, so the next sweep hands all of them to the bot at
once.

### `llm-diff`

Classifies the most recent stored postings (`--limit`, default 200) with both
the regular expressions and Gemini, and prints where they disagree. It is how
to decide whether `--llm` is worth turning on. It spends Gemini quota and
writes the cache, so it takes the lock; it needs `GEMINI_API_KEY`.

### `upgrade-db`

Brings a `postings.db` written by the older in-bot scraper up to
[CONTRACT.md](CONTRACT.md), in place: it switches the file to WAL, adds the
contract tables and writes `scraper_meta`. It refuses, having changed nothing,
a file that fails `PRAGMA integrity_check` or is not schema version 2. It prints
each table's row count and highest rowid before and after, and exits non-zero
if any moved. It is safe to run again.

### `config`

Prints the `.env` used and every setting a run would use, by variable name.
The Gemini key and the Discord token are shown only as set or not set, and the
owners' ids only as a count.

## Gemini (`--llm`)

Optional. With `GEMINI_API_KEY` set, `--llm` classifies newly seen postings by
title with Gemini instead of the regular expressions, in batches of
`GEMINI_BATCH`. A sweep sends only the postings it has never seen, which is what
keeps a busy day inside a free-tier budget: tens of calls, not thousands. Verdicts are
cached in `postings.db`, usage is counted per day in `LLM_DAY_TZ` (the free
tier resets at midnight Pacific), and a request that keeps failing leaves its
postings to the regular expressions.

The limits default to the stricter free tier (`GEMINI_RPM=5`, `GEMINI_RPD=250`)
so a change of model cannot exceed one by surprise; check your own AI Studio
dashboard before raising them. On the server, `watch` runs without `--llm`, and
turning it on is the owner's decision.

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
  this traffic apart and knows where to write. `resolve_boards.py` sends the
  same one, through the same gate. Set `POLL_CONTACT` to a project URL or a role mailbox, never a
  personal address.
- **One sweeper, every 15 minutes.** The lock stops a second process from
  doubling the traffic, and a restarted `watch` waits for its turn.

Gemini's API is the one exception: it has its own quota accounting.

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
   python resolve_boards.py https://careers.example.com
   python resolve_boards.py --file careers_urls.txt --emit    # rows for SEED_BOARDS
   ```

   It never guesses slugs: guessed ones rarely exist, and an LLM's are
   confidently invented.
2. **Add it.** For this box only, add the row to `boards.json` in the data
   directory (a JSON list of rows). A malformed row is skipped with a warning
   and never stops a start. To keep a board for everyone, add it to
   `SEED_BOARDS` in a pull request.
3. **Check it.** `python internship_poller.py verify --sector <its sector>`.

A board added this way behaves like one `discover` found: its open postings are
all new to the ledger, and arrive at the bot together at the next sweep.

## Privacy

A resume given to the Discord bot is read once and thrown away. It is read in a
separate, short-lived process that is given none of the bot's environment
variables (so no bot token or API key), cannot write a byte to any file or leave
a core dump, is held to 15 seconds of CPU and is killed after 20 seconds. That
is not a full sandbox: the process runs as the bot's own user, so it can still
read files that user can, `.env` included, and open network connections. The
bot process never parses, stores or logs the resume's text; only the vocabulary
the person confirms is kept. Whoever runs this bot can read what it stores.
A profile is deleted after a year unused, and 30 days after its owner leaves
every server the bot shares with them or loses access to the bot.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests     # with requirements.txt installed
python3 -m unittest discover -s tests              # bare: aiohttp is stubbed
```

Standard library only, no network, and no real `postings.db`: every fixture is
built in memory or in a temporary directory. A run without python-dotenv skips
the few tests that need it, and a run without discord.py skips the bot's
tests that need it; both are expected. CI runs Python 3.10 and 3.12, each with
all of `requirements.txt`, with only the scraper's aiohttp and python-dotenv,
and with only python-dotenv.

## Provenance

DIAYN began as the internship tracker inside the BaronChairStair Discord
bot, and was moved out so that the scraper can be run, released and fixed on its own,
with the bot reading its output through a written contract.

It was imported as it stood in BaronChairStair at 9130a2c, carrying the history
of `internship_poller.py`, `resolve_boards.py` and the blocklist tests from
9f00cd5 onward. Nothing earlier was brought over, on purpose. BaronChairStair's
history was rewritten once, so its commit ids from before the rewrite no longer
mean anything, and none is cited here. The imported code has the same author
as the rest, and the same [LICENSE](LICENSE).

The bot's half, the alerts, `/internships`, user profiles and the taxonomy
behind them, is moving into DIAYN as well, under `bot/`. Profiles live in
DIAYN's own `users.db`, and `import-legacy` (under *Commands*) copies the old
tracker's subscribers into it once.
