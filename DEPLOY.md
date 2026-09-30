# Deploying DIAYN

On the server, one pm2 app, **`diayn`**, runs `internship_poller.py watch` from
`~/DIAYN`, and it is the only process that writes `postings.db`. The
BaronChairStair bot reads the same file, read-only. Anything about the bot
itself, its `.env`, how it is restarted and how to check it, is in the
bot's own DEPLOY.md, and this file does not repeat it.

Three rules hold everywhere below:

- **When a check goes red, stop and report. Do not restart through it.** A check
  that fails on the server usually means the server differs from what this file
  assumes, and that is worth understanding before anything else changes.
- **Stop and start DIAYN through pm2, never by PID and never with `pkill -f`.**
  pm2 restarts a process it manages the moment it dies, so killing one by hand
  either does nothing or ends with two.
- **Copy `postings.db` only with sqlite3's `.backup`, and restore it only with
  `.restore`.** Never `cp` over it and never `VACUUM` it. Its rowids are the
  bot's autocomplete values and must not change ([CONTRACT.md](CONTRACT.md),
  P3), and a `cp` beside a live `-wal` file can hand SQLite a torn database.

## Where things live

The commands below use these names. `<user>` is the Unix user both processes
run as, and `<bot>` is the bot's pm2 app name (`pm2 list`).

```sh
D=$HOME/internship-data         # the data directory, shared with the bot
B=$HOME/backups/internship      # backups of postings.db
BOT=$HOME/BaronChairStair       # the bot's checkout; confirm with: pm2 describe <bot>
```

| What | Where |
|---|---|
| The checkout, detached at a release tag | `~/DIAYN` |
| DIAYN's settings, `chmod 600` | `~/DIAYN/.env` |
| The database, its WAL sidecars and the sweeper lock | `$D/postings.db`, `-wal`, `-shm`, `.lock` |
| The board list and the YC probe cache | `$D/boards.json`, `$D/yc_cache.json` |
| Daily backups, kept 14 days | `$B/postings-YYYY-MM-DD.db` |
| The pm2 app definitions (the server's own file, in no repository) | `ecosystem.config.cjs`, beside the bot's app |

Both `$D` and `$B` are `chmod 700`. Nothing under `$D` belongs in git, and CI
fails if any of it is ever tracked.

## Installing

Python 3.10 or newer, the `sqlite3` shell, pm2, and `flock` (util-linux).

```sh
git clone https://github.com/FakeZhiyuanLi/DIAYN.git ~/DIAYN    # while private: the deploy key's SSH URL
cd ~/DIAYN && git checkout --detach v1.0.0
python3 --version                                               # 3.10 or newer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests                  # must end OK
mkdir -p "$D" "$B" && chmod 700 "$D" "$B"
```

The server always runs a tag, never a branch, so what runs is exactly what was
reviewed and released.

## The `.env`

Write `~/DIAYN/.env` by hand, from [`example.env`](example.env), then:

```sh
chmod 600 ~/DIAYN/.env
```

It needs at least these, with absolute paths:

```sh
POSTINGS_DB=/home/<user>/internship-data/postings.db
BOARDS_FILE=/home/<user>/internship-data/boards.json
YC_CACHE=/home/<user>/internship-data/yc_cache.json
POLL_CONTACT=<a project URL or a role mailbox, never a personal address>
```

- **`POSTINGS_DB` is spelled exactly as in the bot's `.env`.** The lock is this
  path plus `.lock`, and the bot refuses a file whose `scraper_meta.db_path`
  names another path.
- **Never copy the bot's `.env`, whole or in part.** Copy a value you need by
  its name, and never let `DISCORD_TOKEN` or any `GITHUB_*` key in.
- **Leave `GEMINI_API_KEY` out** while `--llm` stays off. Turning Gemini on is
  the owner's decision.
- **The process environment wins over the file.** DIAYN loads its `.env`
  without overriding what is already set, the opposite of the bot, so a stale
  value in pm2's daemon environment beats the file. `config` shows what a run
  from your shell would use; `scraper_meta` (under *Checking it runs*) shows
  what the running app actually took.

Then check it:

```sh
cd ~/DIAYN && .venv/bin/python internship_poller.py config
```

It must name `/home/<user>/DIAYN/.env` as the env file, show the three paths
under `$D`, the contact and the limits you expect, and `GEMINI_API_KEY` as
`not set`.

## The pm2 app

Add this block to the `apps` list in the server's `ecosystem.config.cjs`:

```js
{
  name: "diayn",
  script: "/home/<user>/DIAYN/internship_poller.py",
  args: "watch --interval 900",
  interpreter: "/home/<user>/DIAYN/.venv/bin/python",
  cwd: "/home/<user>/DIAYN",
  autorestart: true,
  watch: false,
  exp_backoff_restart_delay: 2000,
},
```

- **Absolute paths.** pm2 does not expand `~`. (Building them with
  `require("os").homedir()` in the `.cjs` works too.)
- **`watch: false`**, so checking out a new tag never restarts the app halfway
  through an upgrade.
- **`exp_backoff_restart_delay`**, so a crash loop backs off. `watch` also
  waits, on every start, until an interval has passed since the last sweep
  began, finished or not: each sweep records its start in `postings.db.lock`
  before its first request. So even a crash loop that dies mid-sweep sweeps
  the job boards at most once an interval.
- **No `--init`.** A missing or empty database must stop the app, not be
  replaced by a new one: that would announce every open posting as new. If the
  log says `no such database` or `the seen ledger is empty`, that is a red
  check. Find the file; do not add `--init`.
- **No `--llm`**, unless the owner has decided to turn Gemini on.

Day-to-day commands:

```sh
pm2 start /path/to/ecosystem.config.cjs --only diayn && pm2 save
pm2 restart diayn
pm2 stop diayn && pm2 save          # stays stopped across a reboot
pm2 logs diayn --lines 50 --nostream
```

## Checking it runs

1. `pm2 list` shows `diayn` online, with a restart count that is not climbing.
2. `pgrep -af 'internship_poller.py watch'` prints exactly one line.
3. The log opens with `watching N boards every 900s`, perhaps
   `next sweep due in Ns`, and then shows one `sweep: …` line every 15 minutes.
   One `sweep failed, rolled back: …` is a board having a bad moment; every
   sweep failing is red.
4. What the running app took effect with:

   ```sh
   sqlite3 "file:$D/postings.db?mode=ro" \
     "SELECT key, value FROM scraper_meta; SELECT datetime(MAX(started), 'unixepoch') FROM sweeps;"
   ```

   `scraper_version` is the tag you deployed, `db_path` is `$D/postings.db`,
   `sweep_interval_s` is 900, the Gemini limits are the ones you set, and the
   last sweep is less than 16 minutes old (the time is UTC).
5. `ls $D` shows `postings.db`, `postings.db-wal`, `postings.db-shm` and
   `postings.db.lock`.
6. The bot's `/internships debug` says `DIAYN <version> sweeps every 15m`.
   BaronChairStair's DEPLOY.md, *Verifying the bot*, covers the rest of the
   bot's side.

## Backups

One cron line (`crontab -e`) takes a copy at 04:15 every day and keeps 14 days:

```
15 4 * * * sqlite3 "file:$HOME/internship-data/postings.db?mode=ro" ".backup '$HOME/backups/internship/postings-$(date +\%F).db'" && find $HOME/backups/internship -name 'postings-2*.db' -mtime +14 -delete
```

- `.backup` takes a consistent copy of a live WAL database, which `cp` cannot.
- `mode=ro`, so a wrong path fails instead of leaving an empty database behind.
  The `sqlite3` shell accepts `file:` names as they are; it has no `-uri` flag.
- `\%`, because cron reads a bare `%` as the end of the command.
- `postings-2*.db` matches only the dated daily copies, so a named copy such as
  `postings-pre-split.db` stays until someone deletes it on purpose.

The next morning, check that it ran: `ls -la $B | tail -3` shows today's file,
and it is not empty.

## Restoring

For a damaged or lost file, never to undo a sweep. A restore rewinds the `seen`
ledger to the day of the backup, so every posting first seen since then is
recorded again, as new, by the next sweep, and handed to the bot again.

```sh
pm2 stop diayn
pgrep -af internship_poller                     # prints nothing
ls -la "$B"                                     # choose the copy
sqlite3 "file:$B/postings-YYYY-MM-DD.db?mode=ro" 'PRAGMA integrity_check'    # ok
flock -n "$D/postings.db.lock" \
  sqlite3 "$D/postings.db" ".timeout 5000" ".restore '$B/postings-YYYY-MM-DD.db'"
sqlite3 "file:$D/postings.db?mode=ro" \
  "PRAGMA integrity_check; SELECT COUNT(*) FROM seen; SELECT MAX(rowid) FROM postings;"
pm2 start diayn
```

- `flock -n` takes the same lock DIAYN takes (both use `flock(2)`), so the
  restore refuses, and changes nothing, if anything is still sweeping.
- `.restore` writes through SQLite into the live file and keeps its `-wal` and
  `-shm` consistent, which copying a file over it would not.
- If `$D/postings.db` is gone altogether, the same `.restore` line creates it
  from the backup. That is the one time a new file there is right: it arrives
  with its ledger.
- The bot notices the restored file by itself. Check `/internships debug`
  afterwards.

## Upgrading by tag

DIAYN is released as `vMAJOR.MINOR.PATCH` tags. `MAJOR` moves only with the
contract version, and a contract change is deployed to the bot first
([CONTRACT.md](CONTRACT.md), *Versioning*).

```sh
cd ~/DIAYN
PREV_TAG=$(git describe --tags --exact-match)      # what runs now
git fetch --tags origin
git diff --stat "$PREV_TAG" vX.Y.Z -- CONTRACT.md contract/
```

If that diff shows anything, stop until the bot release that accepts the new
contract is deployed, following BaronChairStair's DEPLOY.md. Then:

```sh
sqlite3 "file:$D/postings.db?mode=ro" ".backup '$B/postings-pre-vX.Y.Z.db'"
git checkout --detach vX.Y.Z && git status --short         # clean
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests             # must end OK
.venv/bin/python internship_poller.py config
pm2 restart diayn
```

Then go through *Checking it runs*; `scraper_version` must now be `X.Y.Z`.
Delete the `pre-vX.Y.Z` copy once the new version has run for a few days.

**Rolling an upgrade back:**

```sh
cd ~/DIAYN && git checkout --detach "$PREV_TAG"
.venv/bin/pip install -r requirements.txt && pm2 restart diayn
```

Within one `MAJOR` the file format does not change, so the older tag opens the
file as it is. Restore the `pre-vX.Y.Z` copy only if the new version damaged
the data itself.

---

## The one-time move from the bot

Until this move, the bot swept `postings.db` itself, in its own process, from a
file inside its own checkout. The move takes three stages, days apart, and each
has an entry condition, steps, checks and a rollback. Two keys in the bot's
`.env` select its mode; BaronChairStair's DEPLOY.md, *Transition:
`INTERN_SWEEP` and `POSTINGS_DB`*, explains them from the bot's side.

| After | `INTERN_SWEEP` (bot) | `POSTINGS_DB` (bot) | Who sweeps | How the bot opens the file |
|---|---|---|---|---|
| Before stage 1 | unset | unset: `$BOT/postings.db` | the bot | as a writer |
| Stage 1, the data move | unset | `$D/postings.db` | the bot | as a writer |
| Stage 2, the switch | `external` | `$D/postings.db` | DIAYN | read-only |
| Stage 3, the cleanup | removed | `$D/postings.db` | DIAYN | read-only |

Throughout:

- **Stop the bot, start it and restart it the way BaronChairStair's DEPLOY.md,
  *Restarting, and making a new command appear*, says**, and check it with
  *Verifying the bot*. On this box it is the pm2 app `<bot>`.
- **Never run the bot checkout's own `internship_poller.py`** for `sweep`,
  `watch`, `list --llm`, `llm-diff`, `discover` or `prune`. That copy is frozen
  and takes no lock.
- **Write down counts only**, never ids, in the migration issue.
- **Edit a `.env` in an editor**, one line at a time, and never print one.

### The read-only survey

Run this before each stage. It changes nothing.

1. `pm2 list`, then `pm2 describe <bot> | grep -iE 'watch|interpreter|exec cwd|script'`.
   If pm2's `watch` is on for the bot, a `git pull` restarts it mid-procedure:
   turn it off in `ecosystem.config.cjs` first.
2. `pgrep -af discord_bot.py` prints exactly one line, and
   `pgrep -af internship_poller` prints the sweeper you expect for this stage
   (none before stage 2). A `watch` nobody expected is red: find what runs it
   and stop it through that.
3. The bot's checkout and its scraper files:

   ```sh
   git -C "$BOT" fetch origin && git -C "$BOT" rev-parse HEAD && git -C "$BOT" status --short
   ls -la "$BOT"/postings.db* "$BOT"/boards.json "$BOT"/yc_cache.json
   ```

   Record the `HEAD` as `PREV`.
4. `grep -oE '^(GEMINI|POLL)_[A-Z_]+' "$BOT/.env"`: the names only, never the
   values.
5. `id -un`, `sqlite3 --version`, and the bot interpreter's `--version`.
6. The file's counts, read-only:

   ```sh
   sqlite3 "file:$BOT/postings.db?mode=ro" "PRAGMA user_version; PRAGMA journal_mode;
     SELECT COUNT(*) FROM seen; SELECT COUNT(*) FROM postings; SELECT MAX(rowid) FROM postings;
     SELECT MIN(first_seen) FROM seen; SELECT MAX(started) FROM sweeps;"
   ```

   After stage 1, point it at `$D/postings.db` instead.

### Stage 1: move the data out of the bot's checkout (the bot still sweeps)

**Entry:** the bot runs a release that reads `INTERN_SWEEP` and `POSTINGS_DB`,
and has for 3 days without errors. The owner has decided whether any cached
Gemini verdicts are purged first (owner decision 3 in the migration plan).

**Steps.** The bot is down for about two minutes, from step 2 to step 9.

1. Run the survey, items 1-3, and record `PREV`. Then
   `mkdir -p "$D" "$B" && chmod 700 "$D" "$B"`.
2. Stop the bot.
3. Check the file is idle and intact, and record the counts again (survey
   item 6):

   ```sh
   test ! -e "$BOT/postings.db-journal" && sqlite3 "file:$BOT/postings.db?mode=ro" 'PRAGMA integrity_check'   # ok
   ```

4. **Only if the owner approved a purge.** Run it now, with the bot stopped and
   before any copy exists, so no copy ever holds the purged rows. The steps
   and their checks are in the migration plan, not in this public runbook.
   Afterwards, re-run step 3's `integrity_check` and record the counts again.
5. Make the two copies, then compare the counts and `MAX(rowid)` of each with
   step 3:

   ```sh
   sqlite3 "file:$BOT/postings.db?mode=ro" ".backup '$B/postings-pre-split.db'"
   sqlite3 "file:$BOT/postings.db?mode=ro" ".backup '$D/postings.db'"
   ```

6. Move the board files, keeping the originals in `$B`, and remove the old
   database once its copies are verified, with the lock file the bot's
   in-process sweep left beside it:

   ```sh
   for f in boards.json yc_cache.json; do
     [ -e "$BOT/$f" ] && cp -p "$BOT/$f" "$D/" && mv "$BOT/$f" "$B/"
   done
   rm "$BOT/postings.db" && rm -f "$BOT/postings.db.lock"
   ```

7. In the bot's `.env`, add three lines, with absolute paths.
   `INTERN_SWEEP` stays unset.

   ```sh
   POSTINGS_DB=/home/<user>/internship-data/postings.db
   BOARDS_FILE=/home/<user>/internship-data/boards.json
   YC_CACHE=/home/<user>/internship-data/yc_cache.json
   ```

8. Install the backup cron (*Backups*, above).
9. Start the bot.

**Checks:**

- The bot's start-up log has no `internship tracker disabled`, and
  `/internships debug` shows the postings and seen counts from step 5 (or more,
  if a sweep has run since). They match
  `sqlite3 "file:$D/postings.db?mode=ro" "SELECT COUNT(*) FROM postings; SELECT COUNT(*) FROM seen;"`,
  so the bot reads the file under `$D`. Debug does not print the path itself.
- Within 15 minutes, `MAX(started)` in `$D/postings.db` advances.
- `SELECT COUNT(*) FROM seen WHERE first_seen > <the restart, in epoch seconds>`
  is in the tens, not the thousands.
- `ls "$BOT"/postings.db*` finds nothing: nothing recreated the old file.
- `git -C "$BOT" status --short` is clean, and `pgrep -af discord_bot.py` prints
  one line.
- The next morning, the cron has written that day's backup.

**Rollback** (about a minute):

1. Stop the bot.
2. `sqlite3 "file:$D/postings.db?mode=ro" ".backup '$BOT/postings.db'"`. The
   file is still in the bot's old journal mode, so nothing more is needed, and
   the rows swept since the move come back with it.
3. Move `boards.json` and `yc_cache.json` back from `$B`.
4. Remove the three `.env` lines, and start the bot.

### Stage 2: install DIAYN and switch the sweeper

**Entry:** stage 1 has run for at least a day. The `v1.0.0` tag can be fetched
from the server. It is a quiet hour: `/internships` is unavailable for a few
minutes, while the puzzle commands stay up.

**Steps:**

1. Run the survey, items 1-2. Record `PREV` and `T0=$(date +%s)`.
2. Install DIAYN (*Installing*, above), at `v1.0.0`.
3. Write `~/DIAYN/.env` (*The `.env`*, above) with the same three paths as the
   bot's. Copy each `POLL_HOST_*` line and each `GEMINI_*` limit from the bot's
   `.env` by name, and leave `GEMINI_API_KEY` out. `config` must show this env
   file, the paths under `$D` and the limits you expect.
4. Add the `diayn` block to `ecosystem.config.cjs` (*The pm2 app*, above).
   **Do not start it yet.**
5. **The bot stops sweeping first.** Add `INTERN_SWEEP=external` to the bot's
   `.env` and restart the bot. It now opens `$D/postings.db` read-only and
   never sweeps. Until step 6 the file has no contract tables, so the start-up
   log's `internship tracker disabled: …` line, naming the missing contract, is
   expected; the bot retries on every delivery tick. `pgrep -af discord_bot.py`
   prints one line, and the puzzle commands work.
6. With nothing sweeping (`pgrep -af internship_poller` prints nothing), take a
   copy and bring the file up to the contract:

   ```sh
   sqlite3 "file:$D/postings.db?mode=ro" ".backup '$B/postings-pre-upgrade.db'"
   cd ~/DIAYN && .venv/bin/python internship_poller.py upgrade-db
   ```

   It prints `integrity_check: ok`, then `journal_mode: delete -> wal` (or
   `wal -> wal`, if it already was), then one line per table whose count and
   highest rowid are the same before and after, and last
   `scraper_meta: contract_version 1, db_path /home/<user>/internship-data/postings.db`.
   If it refuses the file (a failed integrity check, or the wrong schema
   version), nothing was changed: stop and report. If it names tables that
   moved, stop, and restore `postings-pre-upgrade.db` (*Restoring*, above)
   before anything sweeps.
7. Start the scraper:

   ```sh
   pm2 start /path/to/ecosystem.config.cjs --only diayn && pm2 save
   ```

   It takes the lock, publishes the contract tables, and sweeps once the last
   recorded sweep plus 900 seconds has passed, so the gap with no sweeper is at
   most one interval.
8. Within five minutes, the bot's next delivery tick opens the contract, and
   `/internships debug` says `DIAYN 1.0.0 sweeps every 15m`.

**Why this order is safe:**

- **No double sweep.** The bot stops sweeping (step 5) before DIAYN starts
  (step 7), and the lock would refuse a second sweeper in any case.
- **No false bootstrap.** The ledger is not empty, so the bot's bootstrap guard
  only records a floor at `MIN(first_seen)`, which predates the move.
- **No lost rows.** DIAYN stamps what it finds with its own `now`, above every
  delivery cursor, so those rows are delivered once they settle. While the
  tracker waits for the contract, the bot leaves its cursors alone.

**Checks:**

- `pgrep -af discord_bot.py` prints one line, `pgrep -af 'internship_poller.py watch'`
  prints one, and `pm2 list` shows both online with no restart loop.
- `/internships debug` says `DIAYN 1.0.0 sweeps every 15m`, and shows a last
  sweep less than 16 minutes old and the Gemini limits from `scraper_meta`. The
  contract opening at all means the bot's `POSTINGS_DB` is the file DIAYN
  sweeps (B2), and `config` showed that file under `$D` in step 3.
- `sqlite3 "file:$D/postings.db?mode=ro" "PRAGMA journal_mode; SELECT COUNT(*) FROM seen WHERE first_seen > $T0;"`
  prints `wal` and a count in the tens.
- `find ~/BaronChairStair ~/DIAYN -name 'postings.db*'` finds nothing, and
  `ls $D` shows `postings.db`, `-wal`, `-shm` and `.lock`.
- `/internships recent`; `/internships matches` with a **made-up** profile,
  never a real person's; `/internships info` with its details; the role and
  company autocomplete.
- Over the next two delivery ticks, alerts arrive at their usual rate: no
  flood, and no silence.
- `git status --short` is clean in both checkouts.

**Rollback** (seconds, no copying):

1. `pm2 stop diayn && pm2 save`, so a reboot or `pm2 resurrect` cannot bring
   it back holding the lock, which would make the bot skip every sweep.
2. In the bot's `.env`, set `INTERN_SWEEP=in-process`, or delete the line.
3. Restart the bot.

The bot opens the same file and sweeps again: it is in WAL, still schema
version 2, and the bot ignores the extra tables. That holds only while the
ledger tables have exactly the v1.0.0 columns, because the bot's frozen copy
of the old scraper inserts into them by position. CONTRACT.md forbids adding
a column to them until stage 3 is complete; if one has been added anyway, do
not roll back: stop and report. If the rollback becomes permanent, run
`PRAGMA journal_mode=DELETE` on `$D/postings.db` with both processes stopped.

**Soak:** 14 days. Each day, `/internships debug` shows no `no sweep for …h`
warning, the day's backup is in `$B`, and `diayn`'s restart count in `pm2 list`
has not moved.

### Stage 3: retire the bot's in-process sweep

**Entry:** 14 days with `INTERN_SWEEP=external` and no rollback. The freeze
check is empty: no scraper change has landed in the bot's frozen copy since the
move, so DIAYN has everything.

```sh
git -C "$BOT" log --oneline <pr-a-merge>..origin/main -- internship_poller.py resolve_boards.py   # prints nothing
```

`<pr-a-merge>` is the BaronChairStair commit where the data-paths change
merged, recorded in the migration issue.

**Steps:**

1. Deploy the BaronChairStair release that removes the in-process sweep,
   following its DEPLOY.md. The box is already `external`, so nothing about
   the tracker changes.
2. In the bot's `.env`, remove `INTERN_SWEEP`, `BOARDS_FILE`, `YC_CACHE`, every
   `GEMINI_*` and every `POLL_HOST_*` line, and keep `POSTINGS_DB`. DIAYN's
   `.env` already holds those values. Restart the bot.
3. On the date fixed at entry (14 days later, recorded in the migration issue),
   delete `$B/postings-pre-split.db`. `$B/boards.json` may stay: it is public
   board data. Any other old copy of the database, such as a laptop's, is
   deleted or purged as the owner decided.

**Checks:**

- `/internships debug`, `recent` and `info` work.
- `pgrep` shows one bot and one `watch`.
- `git status --short` is clean in both checkouts.

**Rollback:**

1. `git -C "$BOT" switch -c rollback/pr-c <PREV>`.
2. Put back the removed `.env` lines, copying each value by name from DIAYN's
   `.env`.
3. Follow stage 2's rollback.
4. Open a revert pull request in BaronChairStair. The activity's `git pull`
   needs `git switch main` first.
