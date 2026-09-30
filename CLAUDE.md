# Working in this repository

DIAYN is a self-hosted Discord bot that finds internships: a scraper that
sweeps public job boards into `postings.db`, and a bot that matches what it
finds to each person's profile and DMs them. `.venv/bin/python diayn.py run`
runs both, in one process. This file holds the rules. What the commands do is in
[`README.md`](README.md), running it on a host is in [`DEPLOY.md`](DEPLOY.md),
and what the bot may assume about `postings.db` is in
[`CONTRACT.md`](CONTRACT.md). Follow DEPLOY.md in the order it gives: the order
is load-bearing.

Where this file and one of those differ, **do the stricter thing and say so in
your report.**

## The layout

- `diayn.py` is the entry point: `setup`, `doctor`, `run`, `grant`, `revoke`,
  `import-legacy`, and every command of the scraper, `internship_poller.py`.
  `host_checks.py` and `discord_portal.py` are what `setup` and `doctor` do.
  `hints.py` spells every command a message tells someone to type with the
  Python that is running (`.venv/bin/python diayn.py setup`), never bare
  `python`, which stock macOS and Ubuntu do not have. It also holds the Python
  version check that `diayn.py`, `internship_poller.py` and `resolve_boards.py`
  make before their own imports, so it must stay within what Python 3.9 runs;
  `tests/test_diayn.py` checks that.
- `bot/` is the Discord bot: `app.py`, the client; `access.py`, who may use it;
  `intern_fit.py`, the Gemini fit check; and the finder's other modules. They
  use bare imports with `bot/` on `sys.path`, and `bot/` has no `__init__.py`
  ([`bot/README.md`](bot/README.md) says why). Their tests are in `tests/bot/`.
- `llm.py` is the one Gemini request, shared by the fit check and `--llm`.
- The data directory (`DIAYN_DATA`, default `data/`) holds `postings.db`, the
  scraper's ledger, and `users.db`, the bot's own: profiles, the sent and
  hidden ledgers, access grants, and the fit check's cache and budget.
- `private_files.py` is how the data directory is made at mode 700 and a
  database at 600, by whichever command makes it first: `setup`, `grant`,
  `import-legacy`, `run` (users.db), `sweep --init` or `watch --init`. Anything
  new that can make either goes through it; a plain `os.makedirs` or
  `sqlite3.connect` makes them readable by everyone on the box. `setup`
  tightens what is already there; `doctor` only reports it.

## When a check goes red, stop and report

Do not restart through it, retry until it passes, or loosen the check. A check
that is red on a box where CI was green usually means the box differs from CI
in a way worth understanding, and a check that has gone red once has usually
caught something.

Run the suite both ways; both must end `OK`:

```sh
.venv/bin/python -m unittest discover -s tests      # with requirements.txt installed
python3 -m unittest discover -s tests               # bare
```

Three things are expected, and are not red:

- A run without python-dotenv skips the few tests that need it
  (`OK (skipped=N)`). Skips are fine; a failure or an error is not.
- A run without discord.py skips the bot's tests that need it. With
  requirements.txt installed, none of them may skip.
- A run without aiohttp imports the scraper through `tests/aiohttp_stub.py`.

## Never

- **Commit `*.db`, its `-journal`, `-wal` or `-shm`, `*.db.lock`, `.env` or
  `.env.*`, `data/`, `boards.json` or `yc_cache.json`.** This repository is
  public, and `users.db` holds Discord ids and everyone's profile.
  `.gitignore` covers all of them and CI fails if any is tracked, but
  `git add -A` or `git add -f` gets them in anyway. Stage files by name.
- **Open a real `users.db` or `postings.db` to look at its contents, or print a
  Discord id.** Tests build their own in a temporary directory. `grant`,
  `revoke` and `import-legacy` print counts and outcomes, never an id; keep
  them that way. A resume's text is never stored, logged or printed.
- **`VACUUM`, rebuild, or `INSERT OR REPLACE` into `postings`.** That includes
  `VACUUM INTO`, a `.dump` and reload, and plain `REPLACE`. The rowids are the
  bot's autocomplete values and cache keys, and part of the contract (P3);
  `tests/test_contract.py` fails on any `VACUUM` or `REPLACE INTO postings` in
  the source. Copy a database only with `.backup` and restore it only with
  `.restore`.
- **Run a second sweeper.** Exactly one process writes `postings.db`, and
  `<POSTINGS_DB>.lock` enforces it. A writing command that finds the lock held
  exits 3: that is the lock doing its job, not a failure to retry, and never a
  reason to delete the lock file. On a host the sweeper is the service running
  `diayn.py run`; do not run `sweep`, `watch`, `prune`, `discover`, `llm-diff`
  or `list --llm` by hand against its live file. Two copies of the bot on one
  token answer every command twice, and the lock cannot see a copy on another
  machine.
- **Stop a process by PID, or with `pkill -f`.** Stop DIAYN through its service
  manager (`pm2 stop diayn`, `systemctl stop diayn`), which otherwise restarts
  whatever you killed.
- **Make a service manager restart `run` on exit 78.** It means Discord refused
  the Server Members Intent, a portal toggle only the host can turn on, or
  `DISCORD_TOKEN`, which only the host can replace; a loop of refused logins
  can get the bot's token reset. `run` asks Discord's REST API about both
  before it logs in (`check_before_login`), so even a service manager that
  restarts it anyway only repeats that REST call, never a gateway login; keep
  that check ahead of the login. DEPLOY.md's units list 78 beside 3 as codes
  never to restart on; keep them there.
- **Load a `.env` from anywhere but `POLLER_ENV_FILE` or the checkout.** Never
  from the working directory or the data directory. Do not open, print or copy
  a `.env`; `diayn.py config` shows what took effect, and prints the Discord
  token and the Gemini key only as set or not set.
- **Create `postings.db` to get past a refusal.** `setup` and `--init` are for
  a database that is meant to be new, and `setup` never touches one that
  exists. On a host, `no such database` or `the seen ledger is empty` is a red
  check: an empty ledger makes every open posting look new, and the bot would
  announce them all.
- **Cite a commit id from before the history rewrite of the project DIAYN grew
  out of**, in a document, a commit message, a comment or a pull request.
  `tests/test_docs.py` rejects any commit-shaped id in the Markdown files that
  is not on its list; add one only once you are sure it is from after the
  rewrite.
- **Get round the politeness gate.** Every request to a job board goes through
  `polite_session`, with DIAYN's own User-Agent, never a browser's. These are
  other people's servers.

## A contract change

Anything in `CONTRACT.md` or `contract/` is a contract change, and follows
CONTRACT.md's *Changing the contract*: both halves change in the same commit,
with both halves' tests. An additive change needs no bump; removing or
renaming something, or breaking a promise, bumps `contract_version` and the
major version.

When `tests/test_contract.py` goes red, the code and the contract disagree.
Work out which one is wrong. Do not regenerate the fixture to match the code.

An adapter that changes the shape of the URLs it stores is a contract change
too: `contract/sample_urls.json` pins them, because the bot's salary and
description fetcher matches them with its own patterns.

## Couplings that are easy to miss

- **Both halves take their settings from the scraper's `SETTINGS`**, which
  holds the code defaults (UTC, no contact, the checkout's `data/`) until
  `boot()` binds them. `run`, `setup` and `doctor` call `boot()` before
  anything else; code in `bot/` that reads a setting earlier gets a default.
  The bot takes `postings.db`'s path from there too, and refuses a file whose
  `scraper_meta.db_path` names another.
- **The taxonomy keys are ledger keys.** `intern_taxonomy.role_key`,
  `clone_key`, `group_hash` and `company_norm` key everyone's sent and hidden
  ledgers, and the fit check's cache, in `users.db`. Keep their output
  byte-identical: a changed key re-sends every role people were told about and
  brings back every role they hid. `tests/bot/test_intern_taxonomy.py` pins
  them.
- **Access is checked in each callback, not in the shared views.** A new
  command, button, select or modal submit needs its own check. The ways out
  (`/internships delete`, "Delete my data" and its confirmation, "Stop" and
  "Pause") stay open without access, and must not gain one.
- **The fit check never holds an alert back.** Without a key, with the budget
  spent, or on any failure, the alert goes out unchecked. What it sends is
  pinned by `tests/bot/test_intern_fit.py`: profile labels and posting fields,
  never an id, a name, contact details or resume text. README's *Privacy* and
  the consent screen say the same, and must change with it.
- **The fit check never runs before its notice.** A profile is checked only
  once `fit_notice_at` says its owner was shown what the check sends
  (`intern_fit.enabled`). The start card, the consent screen and
  `/internships help` record it where they show it, a new profile's draft
  records it because one of the first two came before it, and anyone else is
  told in their next alert, which goes out unchecked. A new screen that shows
  the notice records it (`intern_ui.note_fit_notice`); a new way to make a
  profile says whether the notice came first.
- **The bot's window is 30 days.** `PRUNE_DAYS` and `prune --max-age` never go
  below it, and the bot checks `prune_days` when it opens the file.
- **`seen` is never pruned.** The bot's bootstrap guard reads
  `MIN(first_seen)` from it.
- **Unblocking a company, or adding a board, hands its whole open board to the
  next sweep as new**, because none of its postings is in `seen` yet.
- **Settings are read once, when a command starts, after the `.env` loads**,
  and never at import. Importing a module must stay inert: no file read, no
  change to `os.environ`, and `diayn.py` imports discord.py only for `run`.
  `tests/test_config.py` pins it for the scraper.

## Tests

Standard-library `unittest` only. No network, and no real `postings.db`,
`users.db` or `.env`: every fixture is built in memory or in a
`tempfile.TemporaryDirectory()`, and Discord and Gemini are faked. Keep the
aiohttp stub pattern (`tests/aiohttp_stub.py`), so the suite runs on a bare
`python3`. Write the test first and watch it fail.

## Decisions that are not yours to make

Report these and stop; do not act on them unasked.

- **Putting `GEMINI_API_KEY` in a host's `.env`, or turning on `--llm` there.**
  Setting `GEMINI_API_KEY` alone turns on the fit check, with no flag: from
  the next start it sends each person's profile labels to Google before their
  alerts. `--llm` sends posting titles and locations. Either spends a budget
  and sends data to a third party.
- **Changing what the fit check sends to Gemini**, or what the bot stores
  about a person.
- **Granting or revoking access on a real host**, with `grant`, `revoke` or
  `/diayn`, or running `import-legacy` against a real database.
- **Purging data.** Deleting rows from `postings.db` beyond the built-in 30-day
  prune, or from `users.db` beyond the finder's own expiry and a person's own
  delete, purging `llm_cache`, deleting a backup, or a `VACUUM`, even one that
  would finish a purge.
- **Changing the repository's visibility**, owner or name, rewriting its
  history, or changing the history roots CI pins.
- **Running `discover` against a host's data directory, or unblocking a
  company.** Either hands the bot a burst of postings as new.
- **A contract version bump.**
- **Rotating a secret.**
