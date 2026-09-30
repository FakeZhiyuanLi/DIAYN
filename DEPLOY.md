# Running DIAYN for good

The README's [quick start](README.md#quick-start) gets the bot answering from a
terminal. This file keeps it running on a Linux or macOS host: a service
manager to start it at boot and restart it after a crash, daily backups, a
restore, and upgrades from one release tag to the next.

DIAYN is one process, `diayn.py run`: the Discord bot and the sweeper together.
It is the only process that writes `postings.db`, and it holds the sweeper lock
for as long as it runs.

Four rules hold everywhere below:

- **When a check goes red, stop and find out why. Do not restart through it.**
  A check that fails on your host usually means the host differs from what this
  file assumes, and that is worth understanding before anything else changes.
- **Stop and start DIAYN through its service manager, never by PID and never
  with `pkill -f`.** pm2 and systemd restart a process they manage the moment it
  dies, so killing one by hand either does nothing or ends with two.
- **Copy a database only with sqlite3's `.backup`, and restore it only with
  `.restore`.** Never `cp` over one, and never `VACUUM` `postings.db`: its rowids
  are the bot's autocomplete values and must not change
  ([CONTRACT.md](CONTRACT.md), P3), and a `cp` beside a live `-wal` file can
  hand SQLite a torn database.
- **One running copy per bot token.** Two processes on one token answer every
  command twice. The lock stops a second `run` on the same data directory, but
  it cannot see a copy on another machine.

## Where things live

The commands below use these names:

```sh
D=$HOME/DIAYN/data          # the data directory: DIAYN_DATA, if you set it
B=$HOME/diayn-backups       # daily copies of both databases
```

| What | Where |
|---|---|
| The checkout, detached at a release tag | `~/DIAYN` |
| The settings, `chmod 600` | `~/DIAYN/.env` |
| The scraper's ledger, its WAL sidecars and the sweeper lock | `$D/postings.db`, `-wal`, `-shm`, `.lock` |
| The bot's own database: profiles, access grants, the fit check's cache | `$D/users.db` |
| The boards `discover` found, and its Y Combinator cache | `$D/boards.json`, `$D/yc_cache.json` |
| Daily backups, kept 14 days | `$B/postings-YYYY-MM-DD.db`, `$B/users-YYYY-MM-DD.db` |

`$D` and `$B` are both `chmod 700`: `users.db` holds Discord ids and everyone's
profile, and so does every copy of it. The default `$D`, `data/` in the
checkout, is ignored by git and untouched by an upgrade. Set `DIAYN_DATA` to an
absolute path to keep it anywhere else. Nothing under `$D` belongs in git, and
CI fails if any of it is ever tracked.

## Installing

Python 3.10 or newer, git, the `sqlite3` shell, and `flock`: part of
util-linux on Linux, and `brew install flock` on macOS. Then pm2 or systemd,
below.

```sh
git clone https://github.com/FakeZhiyuanLi/DIAYN.git ~/DIAYN
cd ~/DIAYN && git checkout --detach v1.0.0
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests          # must end OK
cp example.env .env && chmod 600 .env                    # then fill it in
.venv/bin/python diayn.py setup
mkdir -p "$B" && chmod 700 "$B"
```

A host always runs a tag, never a branch, so what runs is exactly what was
released. `setup` checks the token, makes `$D` at mode 700 (or tightens it,
and the databases in it, if others on the box can read them), bootstraps
`postings.db` and prints the invite link; the README says what each step does.

## The `.env`

[`example.env`](example.env) documents every setting. A host needs
`DISCORD_TOKEN` and `POLL_CONTACT`; `DIAYN_TZ`, `DIAYN_DATA`, `DIAYN_OWNER_IDS`
and `GEMINI_API_KEY` are worth a look.

- **Setting `GEMINI_API_KEY` turns on the fit check**, not only `--llm`. From
  the next start, before each alert, that person's profile labels go to Google
  (majors, minors, degree, graduation date, kinds of role, fields, skills,
  keywords, places and terms, with each role's title, company, location and
  term), once they have been shown the notice, and every request counts
  against the key's quota (`FIT_RPD`, `FIT_RPM`). Leave it empty and nothing
  goes to Google. The README's *Gemini* and *Privacy* sections say exactly
  what is sent.
- **The process environment wins over the file.** DIAYN loads its `.env`
  without overriding what is already set, so a value in pm2's daemon
  environment or in a systemd `Environment=` line beats the file.
  `.venv/bin/python diayn.py config` shows what a run from your shell would
  use, and shows the token and the key only as set or not set.
- **Settings are read once, at the start.** After editing `.env`, restart the
  service.
- **Never paste the `.env` anywhere.** Paste `config`'s output instead.

## Running it as a service

Use one of the two below, not both. Either way, `run` is the whole of DIAYN.
`watch` is the sweep loop without the bot, and beside `run` it exits 3.

### pm2

pm2 runs on Linux and macOS. Put this in `~/diayn.config.cjs`, outside the
checkout, so an upgrade never touches it:

```js
module.exports = {
  apps: [{
    name: "diayn",
    script: "/home/<user>/DIAYN/diayn.py",
    args: "run",
    interpreter: "/home/<user>/DIAYN/.venv/bin/python",
    cwd: "/home/<user>/DIAYN",
    env: { PYTHONUNBUFFERED: "1" },
    autorestart: true,
    watch: false,
    exp_backoff_restart_delay: 2000,
    stop_exit_codes: [3, 78],
    kill_timeout: 10000,
  }],
};
```

- **Absolute paths.** pm2 does not expand `~`. On macOS the home directory is
  `/Users/<user>`.
- **`watch: false`**, so checking out a new tag never restarts it halfway
  through an upgrade.
- **`exp_backoff_restart_delay`**, so a crash loop backs off. The sweep loop
  also waits, on every start, until an interval has passed since the last
  sweep began, finished or not, so even a crash loop sweeps the job boards at
  most once an interval.
- **`stop_exit_codes: [3, 78]`.** Exit 3 means another `run` or `watch` holds
  the lock. Restarting into it only loops; find the other process instead.
  Exit 78 means Discord refused the Server Members Intent, a toggle in the
  developer portal, or refused `DISCORD_TOKEN` in `.env`. No restart changes
  either, and a loop of refused logins can get the bot's token reset. `run`
  asks Discord's REST API about both before it logs in, so even a service
  manager that restarts it on 78 anyway only repeats that cheap REST call,
  never a gateway login. The log line says what to fix; then
  `pm2 restart diayn`.
- **`kill_timeout`.** pm2 stops a process with SIGINT, which `run` treats as
  Ctrl-C, on every supported Python: it logs out of Discord and exits 0. This gives it ten seconds before
  SIGKILL.

```sh
pm2 start ~/diayn.config.cjs && pm2 save
pm2 startup                          # once: prints the command that starts pm2 at boot
pm2 restart diayn
pm2 stop diayn && pm2 save           # stays stopped across a reboot
pm2 logs diayn --lines 50 --nostream
```

### systemd

On Linux, as `/etc/systemd/system/diayn.service`:

```ini
[Unit]
Description=DIAYN, a Discord internship finder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/DIAYN
ExecStart=/home/<user>/DIAYN/.venv/bin/python /home/<user>/DIAYN/diayn.py run
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=3 78
KillSignal=SIGINT
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- **`Restart=on-failure`** restarts it after a crash, and after it exits 1
  because its sweep loop ended, so the sweeps come back with it.
- **`RestartPreventExitStatus=3 78`**: another `run` or `watch` holds the lock
  (3), or Discord refused the Server Members Intent or the token (78), as for
  pm2 above. Fix what the log line names, then `sudo systemctl restart diayn`.
- **`KillSignal=SIGINT`**, the signal pm2 sends too, so both units stop `run` the
  same way: it logs out of Discord and exits 0. `run` treats SIGTERM, systemd's
  default, the same, so a unit without this line also stops cleanly.
- **`UMask=0077`**, so every file it creates is readable by its own user alone.

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now diayn
systemctl status diayn
sudo systemctl restart diayn
sudo systemctl stop diayn            # `disable` too, to stay stopped across a reboot
journalctl -u diayn -n 50 --no-pager
```

### Commands by hand while it runs

The commands that only read (`stats`, `config`, `verify`, `list` without
`--llm`, `doctor`) are safe at any time. The ones that write (`sweep`, `prune`,
`discover`, `llm-diff`, `list --llm`, `upgrade-db`) exit 3 while the service
holds the lock: stop the service first, and start it again after.
`grant` and `revoke` write only `users.db`, and are safe at any time.

## Checking it runs

1. `pm2 list` or `systemctl status diayn` shows it online, with a restart count
   that is not climbing.
2. `pgrep -f 'diayn[.]py run' | wc -l` prints 1. It counts PIDs, which
   pgrep prints the same way on Linux and macOS, whereas its `-a` flag does not
   travel: on Linux it lists command lines, on macOS it adds ancestors to the
   match. The `[.]` keeps the pattern from matching a shell that runs this very
   line. To see the command line as well: `pgrep -lf` on macOS, and
   `pgrep -f --list-full` on Linux.
3. The log shows `DIAYN is logged in as …`, then `watching N boards every 900s`,
   perhaps `next sweep due in Ns`, and then one `sweep: …` line every 15
   minutes. One `sweep failed, rolled back: …` is a board having a bad moment;
   every sweep failing is red.
4. `.venv/bin/python diayn.py doctor` ends `doctor: nothing to fix`, and its
   `sweeper` line says a `run` or a `watch` holds the lock.
5. In Discord, `/diayn debug` shows a last sweep less than 16 minutes old.
6. What the running process took effect with, read-only:

   ```sh
   sqlite3 "file:$D/postings.db?mode=ro" \
     "SELECT key, value FROM scraper_meta; SELECT datetime(MAX(started), 'unixepoch') FROM sweeps;"
   ```

   `scraper_version` is the tag you deployed, `db_path` is `$D/postings.db`
   and `sweep_interval_s` is 900. The time is UTC.

## Backups

Once a day, a consistent copy of both databases, kept 14 days. Save this as
`~/diayn-backup.sh`:

```sh
#!/bin/sh
set -eu
D=$HOME/DIAYN/data
B=$HOME/diayn-backups
day=$(date +%F)
sqlite3 "file:$D/postings.db?mode=ro" ".backup '$B/postings-$day.db'"
sqlite3 "file:$D/users.db?mode=ro" ".backup '$B/users-$day.db'"
find "$B" \( -name 'postings-2*.db' -o -name 'users-2*.db' \) -mtime +14 -delete
```

and run it from cron at 04:15 (`crontab -e`):

```
15 4 * * * /bin/sh $HOME/diayn-backup.sh
```

- `.backup` takes a consistent copy of a live database, which `cp` cannot.
- `mode=ro`, so a wrong path fails instead of leaving an empty database behind.
- The `-2*` patterns match only the dated daily copies, so a named copy such as
  `postings-pre-v1.1.0.db` stays until you delete it on purpose.
- **Keep the retention short.** A copy of `users.db` still holds the profile of
  everyone who has since used `/internships delete`. Fourteen days bounds how
  long that lasts.

The next morning, check that it ran: `ls -la "$B" | tail -3` shows today's two
files, and neither is empty.

## Restoring

For a damaged or lost file, never to undo something.

- **`postings.db`** rewinds the `seen` ledger to the day of the backup, so every
  posting first seen since then is recorded again, as new, by the next sweep.
- **`users.db`** rewinds profiles, grants and alert settings to that day.
  Anyone who deleted their data since then is back. Restore it only when the
  file is lost or damaged, and from the newest copy.

Stop the service (`pm2 stop diayn`, or `sudo systemctl stop diayn`), then:

```sh
pgrep -f 'diayn[.]py run' | wc -l                             # 0
ls -la "$B"                                                   # choose the day
sqlite3 "file:$B/postings-YYYY-MM-DD.db?mode=ro" 'PRAGMA integrity_check'   # ok
flock -n "$D/postings.db.lock" \
  sqlite3 "$D/postings.db" ".timeout 5000" ".restore '$B/postings-YYYY-MM-DD.db'"
sqlite3 "file:$D/postings.db?mode=ro" \
  "PRAGMA integrity_check; SELECT COUNT(*) FROM seen; SELECT MAX(rowid) FROM postings;"
```

and for `users.db`, the same way:

```sh
sqlite3 "file:$B/users-YYYY-MM-DD.db?mode=ro" 'PRAGMA integrity_check'      # ok
flock -n "$D/postings.db.lock" \
  sqlite3 "$D/users.db" ".timeout 5000" ".restore '$B/users-YYYY-MM-DD.db'"
```

Then start the service, and go through *Checking it runs*.

- `flock -n` takes the lock `run` holds for life (both use `flock(2)`), so the
  restore refuses, and changes nothing, if DIAYN is still running.
- `.restore` writes through SQLite into the live file and keeps its `-wal` and
  `-shm` consistent, which copying a file over it would not.
- If a database is gone altogether, the same `.restore` line creates it from
  the backup. That is the one time a new file there is right: it arrives with
  its ledger. Never let `setup` or `sweep --init` make a new `postings.db` in
  its place: an empty ledger makes every open posting look new.

## Upgrading by tag

DIAYN is released as `vMAJOR.MINOR.PATCH` tags. Within one `MAJOR` the
databases' formats do not change, so an upgrade needs no migration and a
rollback opens the files as they are. The bot and the scraper are released
together, from one repository, so they always agree.

```sh
cd ~/DIAYN
PREV_TAG=$(git describe --tags --exact-match)      # what runs now
git fetch --tags origin
git log --oneline "$PREV_TAG"..vX.Y.Z               # what changed
```

Read the release's notes, then:

```sh
sqlite3 "file:$D/postings.db?mode=ro" ".backup '$B/postings-pre-vX.Y.Z.db'"
sqlite3 "file:$D/users.db?mode=ro" ".backup '$B/users-pre-vX.Y.Z.db'"
git checkout --detach vX.Y.Z && git status --short         # clean
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests             # must end OK
```

Restart the service, run `.venv/bin/python diayn.py doctor`, and go through
*Checking it runs*; `scraper_version` must now be `X.Y.Z`. The bot syncs its
slash commands as it starts, and a Discord client may need a reload (Ctrl-R) to
show a new one. Delete the two `pre-vX.Y.Z` copies once the new version has run
for a few days.

**Rolling an upgrade back:**

```sh
cd ~/DIAYN && git checkout --detach "$PREV_TAG"
.venv/bin/pip install -r requirements.txt
```

and restart the service. Restore the `pre-vX.Y.Z` copies only if the new
version damaged the data itself.
