# The postings.db read contract, v1

DIAYN writes `postings.db`. The BaronChairStair Discord bot reads it, from
another repository and another process. This file is everything the two may
assume about each other. Neither side's code is a substitute for it: when the
code and this file disagree, one of them has a bug.

The machine-readable half lives in [`contract/`](contract/). The bot vendors
those files into `client/fixtures/`, each with a header line naming the DIAYN
tag it came from, and tests its own code against them. DIAYN's
`tests/test_contract.py` and `tests/test_cli.py` pin the scraper to the same
files.

| File | What it pins |
|---|---|
| [`contract/postings_v1.sql`](contract/postings_v1.sql) | The whole schema `db_init()` creates. The bot builds its fixtures from it. |
| [`contract/company_norm_cases.json`](contract/company_norm_cases.json) | `_norm` and the blocklist prefix rule, case by case (B7). |
| [`contract/sample_urls.json`](contract/sample_urls.json) | One `postings.url` per platform, in the exact shape its adapter stores. The bot's `posting_details` URL regexes must keep matching these. |

## File-level rules

- SQLite, `PRAGMA user_version = 2`, `journal_mode = WAL`. The scraper sets
  both: `db_init()` switches a file to WAL if it is not WAL already, and waits
  up to 5 s (`busy_timeout = 5000`) on another connection's lock.
- The file's absolute path is `POSTINGS_DB`. The variable has the same name in
  both repositories' `.env` files.
- The sweeper lock is `<POSTINGS_DB>.lock`, taken with `fcntl.flock` as
  `LOCK_EX|LOCK_NB`.
- `postings` has no `INTEGER PRIMARY KEY`, so its rowids are SQLite's own, and
  they are part of this contract (P3).

## What the bot may read, and nothing else

| Table | Columns |
|---|---|
| `postings` | `rowid`, `platform`, `external_id`, `company`, `title`, `location`, `url`, `published`, `first_seen`, `unbounded` |
| `seen` | `platform`, `external_id`, `first_seen` |
| `sweeps` | `started`, `duration`, `errors`, `new_rows` |
| `llm_usage` | `day`, `n`, `prompt_tokens`, `output_tokens` |
| `llm_cache` | `COUNT(*)` only |
| `scraper_meta` | `key`, `value` |
| `boards` | `platform`, `slug`, `company`, `sector` |
| `blocked_companies` | `name` |

Times (`first_seen`, `published`, `started`) are epoch seconds. `published` may
be NULL. `unbounded = 1` marks Workday's "30+ days ago" bucket, a floor rather
than a date.

Every other table and column (`etags`, `postings.sector`, `category`, `term`,
`region`, `is_intern`, `is_tech`, the rest of `sweeps`, the rows of
`llm_cache`) is the scraper's own and may change without notice.

### The contract tables

`scraper_meta`, `boards` and `blocked_companies` are created with
`CREATE TABLE IF NOT EXISTS`, so a scraper from before them opens the file
unharmed. `publish_registry()` rewrites all three, whole, at start-up and
inside every sweep's transaction (P8).

**`scraper_meta(key TEXT PRIMARY KEY, value TEXT)`**: every value is text.

| Key | Value |
|---|---|
| `contract_version` | `1`, this contract |
| `scraper_version` | the release, `MAJOR.MINOR.PATCH` |
| `db_path` | the `realpath` of the file the scraper opened, symlinks resolved |
| `prune_days` | the retention of `postings`, in days, never below 30 |
| `sweep_interval_s` | the seconds `watch` waits between sweeps |
| `gemini_model` | the model `--llm` classifies with |
| `llm_rpd`, `llm_rpm`, `llm_tpm` | its daily request cap, per-minute request cap and per-minute token cap |
| `llm_day_tz` | the zone of `llm_usage.day`, default `America/Los_Angeles` |
| `started_at` | when the scraper process started, in epoch seconds |

**`boards(platform, slug, company, sector, PRIMARY KEY(platform, slug))`**: the
registry the scraper polls, after the blocklist.

**`blocked_companies(name TEXT PRIMARY KEY)`**: the blocklist as written. A
name whose normalised form is empty is never published: the empty string is a
prefix of every name, so it would block everything.

## What the scraper promises

| # | Promise | Why the bot depends on it |
|---|---|---|
| P1 | `first_seen` is written once, at insert, and is identical in `seen` and `postings`. Nothing awaits between taking `now` and the commit in `cmd_sweep`, and rows are committed well within 300 s of their `first_seen`. | Delivery offers `cursor < first_seen <= now - SETTLE_S`, and the bot's window cache lasts 300 s. |
| P2 | `seen` is never pruned. `postings` is pruned only by `prune()`, with `prune_days >= 30`. `prune --max-age` below 30 is refused. | The bootstrap guard reads `MIN(seen.first_seen)`, and the bot's window is 30 days. |
| P3 | Rowids stay stable: no `VACUUM`, no `VACUUM INTO`, no `.dump` rebuild, and no `INSERT OR REPLACE` or `REPLACE` into `postings`. | The rowid is the `/internships info` autocomplete value and the bot's group-map key. |
| P4 | A failed sweep rolls back, and the process carries on. | Rows committed later would carry a stale `first_seen` below users' cursors, and would never be alerted. |
| P5 | Exactly one sweeper. `watch` holds the lock for its whole life; `sweep`, `prune` and `upgrade-db` hold it while they run. | Prevents double traffic to the job boards and a second writer. |
| P6 | The scraper never creates a database silently. `sweep` and `watch` refuse a missing file, or an empty `seen`, unless given `--init`. No other command creates one at all. | A new, empty file is a false bootstrap: its first sweep records every open posting as new. |
| P7 | `llm_usage.day` is the local date in `llm_day_tz`. | The bot's quota panel says the budget "resets at midnight Pacific". |
| P8 | The contract tables are refreshed at start-up and inside every sweep's transaction. | Keeps the bot's copy of the blocklist and the board registry current. |

## What the bot promises

| # | Promise |
|---|---|
| B1 | In `external` mode it opens `file:<abs path>?mode=ro` (`uri=True`, `timeout=1`) and never writes. In either mode it never creates `postings.db`. |
| B2 | When it opens the file it checks `user_version == 2`, `contract_version == 1`, the required columns, `realpath(POSTINGS_DB) == scraper_meta.db_path` and `prune_days >= WINDOW_DAYS`. Any failure raises `ContractError`, which turns off only the tracker. The open is retried on every delivery tick, and at most once a minute from commands. It also reopens when the file's inode changes, for example after a restore. |
| B3 | **The bootstrap guard.** At the start of every delivery tick, before `run_tick`, it reads `MIN(first_seen) FROM seen`. If that value exists, and either `cursor_floor` is missing or the value is above the floor, it calls `advance_all_cursors(db, min_first_seen)`. |
| B4 | It invalidates its window cache when `PRAGMA data_version` changes, checked in `window()` and on each tick. |
| B5 | A tick that cannot read `postings.db` skips alerts and leaves every cursor alone. |
| B6 | Heartbeat: if `MAX(sweeps.started)` is older than 3 × `sweep_interval_s`, it warns in `/internships debug` and logs at most once an hour. |
| B7 | It applies the blocklist as a normalised prefix, with its own `company_norm`. The scraper's `_norm` and the bot's `company_norm` are pinned equal by `contract/company_norm_cases.json`. |

The B3 guard is safe for four reasons:

- `advance_all_cursors` raises cursors with `MAX(cursor, ?)` and records the
  floor with `MAX`, so it never moves a cursor back.
- A bootstrap stamps every seed row with the same `now`.
- `seen` is never pruned (P2).
- The comparison `cursor < first_seen` is strict.

## Versioning

- **Additive changes need no bump**: a new table, a new nullable column, a new
  `scraper_meta` key.
- **Removing or renaming** a contract table or column, or breaking any of
  P1-P8, bumps `contract_version`. A bot release that accepts both the old and
  the new value ships and deploys first.
- **`user_version` never changes** without a coordinated plan and a rollback
  that keeps the live file readable.
- The scraper is released as `vMAJOR.MINOR.PATCH` tags, and `v*` tags are
  protected. `MAJOR` is bumped with `contract_version`: v1.x.y keeps contract 1.
- A change to anything in `contract/` is a contract change: it follows these
  rules, and the bot re-vendors the file from the new tag.

## Upgrading a file from before the contract

`python internship_poller.py upgrade-db` brings a v2 `postings.db` written by
an older scraper up to this contract, in place. It refuses, having changed
nothing, a file that fails `PRAGMA integrity_check` or is not `user_version`
2. Otherwise it switches the file to WAL, creates the contract tables and
writes `scraper_meta`, then prints each table's row count and `MAX(rowid)`
before and after. Those must be equal, and it exits non-zero if they are not.
It is safe to run again.
