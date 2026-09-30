# Working in this repository

DIAYN is the internship scraper that the BaronChairStair Discord bot reads
from. This file holds the rules. What the commands do is in
[`README.md`](README.md), running it on the server is in
[`DEPLOY.md`](DEPLOY.md), and what the bot may assume about `postings.db` is in
[`CONTRACT.md`](CONTRACT.md). Follow DEPLOY.md in the order it gives: the order
is load-bearing.

Where this file and one of those differ, **do the stricter thing and say so in
your report.**

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

Two things are expected, and are not red:

- A run without python-dotenv skips the few tests that need it
  (`OK (skipped=N)`). Skips are fine; a failure or an error is not.
- A run without aiohttp imports the scraper through `tests/aiohttp_stub.py`.

## Never

- **Commit `*.db`, its `-journal`, `-wal` or `-shm`, `*.db.lock`, `.env` or
  `.env.*`, `data/`, `boards.json` or `yc_cache.json`.** Treat this repository
  as public. `.gitignore` covers all of them and CI fails if any is tracked, but
  `git add -A` or `git add -f` gets them in anyway. Stage files by name.
- **`VACUUM`, rebuild, or `INSERT OR REPLACE` into `postings`.** That includes
  `VACUUM INTO`, a `.dump` and reload, and plain `REPLACE`. The rowids are the
  bot's autocomplete values and cache keys, and part of the contract (P3);
  `tests/test_contract.py` fails on any `VACUUM` or `REPLACE INTO postings` in
  the source. Copy the file only with `.backup` and restore it only with
  `.restore`.
- **Run a second sweeper.** Exactly one process writes `postings.db`, and
  `<POSTINGS_DB>.lock` enforces it. A writing command that finds the lock held
  exits 3: that is the lock doing its job, not a failure to retry, and never a
  reason to delete the lock file. On the server the sweeper is pm2's `diayn`;
  do not run `sweep`, `watch`, `prune`, `discover`, `llm-diff` or `list --llm`
  by hand against the live file.
- **Stop a process by PID, or with `pkill -f`.** Stop DIAYN through pm2
  (`pm2 stop diayn`), which otherwise restarts whatever you killed, and the bot
  through whatever runs it, as BaronChairStair's DEPLOY.md says.
- **Load a `.env` from anywhere but `POLLER_ENV_FILE` or the checkout.** Never
  from the working directory or the data directory, and never the bot's. Do not
  open, print or copy a `.env`; `internship_poller.py config` shows what took
  effect, and prints the Gemini key only as set or not set.
- **Create `postings.db` to get past a refusal.** `--init` is for a database
  that is meant to be new. On the server, `no such database` or
  `the seen ledger is empty` is a red check: an empty ledger makes every open
  posting look new, and the bot would announce them all.
- **Cite a BaronChairStair commit id from before its history rewrite**, in a
  document, a commit message, a comment or a pull request. The ones cited here
  are 9f00cd5 and 9130a2c. `tests/test_docs.py` rejects any other commit-shaped
  id in the Markdown files; add one to its list only once you are sure it is
  from after the rewrite.
- **Get round the politeness gate.** Every request to a job board goes through
  `polite_session`, with DIAYN's own User-Agent, never a browser's. These are
  other people's servers.

## A contract change

Anything in `CONTRACT.md` or `contract/` is a contract change, and follows
CONTRACT.md's *Versioning*: an additive change needs no bump; removing or
renaming something, or breaking a promise, bumps `contract_version` and the
major version, and **the bot ships first**, with a release that accepts both.
The bot vendors `contract/` at a named DIAYN tag.

When `tests/test_contract.py` goes red, the code and the contract disagree.
Work out which one is wrong. Do not regenerate the fixture to match the code.

An adapter that changes the shape of the URLs it stores is a contract change
too: `contract/sample_urls.json` pins them, because the bot's salary and
description fetcher matches them with its own patterns.

## Couplings that are easy to miss

- **`POSTINGS_DB` is spelled identically in DIAYN's `.env` and the bot's.** The
  lock is that path plus `.lock`, and the bot refuses a file whose
  `scraper_meta.db_path` names another.
- **The bot's window is 30 days.** `PRUNE_DAYS` and `prune --max-age` never go
  below it, and the bot checks `prune_days` when it opens the file.
- **`seen` is never pruned.** The bot's bootstrap guard reads
  `MIN(first_seen)` from it.
- **Unblocking a company, or adding a board, hands its whole open board to the
  next sweep as new**, because none of its postings is in `seen` yet.
- **Settings are read once, in `main()`, after the `.env` loads**, and never
  at import. Importing the module must stay inert: no file read, no change to
  `os.environ`. `tests/test_config.py` pins it.

## Tests

Standard-library `unittest` only. No network, and no real `postings.db` or
`.env`: every fixture is built in memory or in a
`tempfile.TemporaryDirectory()`. Keep the aiohttp stub pattern
(`tests/aiohttp_stub.py`), so the suite runs on a bare `python3`. Write the
test first and watch it fail.

## Decisions that are not yours to make

Report these and stop; do not act on them unasked.

- **Turning on Gemini (`--llm`) on the server**, or putting `GEMINI_API_KEY`
  in its `.env`. It spends a budget and sends postings to a third party.
- **Purging data.** Deleting rows from `postings.db` beyond the built-in 30-day
  prune, purging `llm_cache`, deleting a backup, or a `VACUUM`, even one that
  would finish a purge.
- **Changing the repository's visibility**, owner or name, rewriting its
  history, or changing the history roots CI pins.
- **Running `discover` against the server's data directory, or unblocking a
  company.** Either hands the bot a burst of postings as new.
- **A contract version bump.**
- **Rotating a secret.**
