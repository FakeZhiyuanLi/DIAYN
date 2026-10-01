# Running DIAYN for good

The README's [quick start](README.md#quick-start) gets the bot answering from a
terminal. This file keeps it running on a Linux or macOS host: a fresh VPS
made ready for it, a service manager to start it at boot and restart it after a
crash, a box shared with another bot, a takeover from an older tracker, a move
from another machine, daily backups, a restore, and upgrades from one release
tag to the next.

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

## A fresh VPS

From a new server to *Installing*, on Ubuntu 24.04 or 22.04, or Debian 12.
Hetzner Cloud is the example here; any provider's VPS goes the same way. On a
box that already runs another bot, most of this is done: check each step
rather than repeat it, and read *Sharing the box with another bot* before
*Installing*.

**The server.** In Hetzner's Cloud Console, add a server with the Ubuntu 24.04
image and your SSH key. A small instance, 2 GB of memory for example, is
enough. DIAYN is one Python process, plus at most two resume-reader children at
a time while people upload resumes, each killed after 20 seconds and, on Linux,
held to 1 GiB of address space. Once it runs, `systemctl status diayn` or
`pm2 list` shows what it uses.

**A user with sudo, not root.** Hetzner's images let you in as root, with your
key. Make a normal user, give it sudo and your key, and do everything after
this as that user. On Debian, `apt install -y sudo` first if there is no
`sudo`.

```sh
ssh root@<server-ip>
adduser <user> && usermod -aG sudo <user>
install -d -m 700 -o <user> -g <user> /home/<user>/.ssh
install -m 600 -o <user> -g <user> ~/.ssh/authorized_keys /home/<user>/.ssh/
exit
ssh <user>@<server-ip>
sudo -v                                   # asks for <user>'s password, then prints nothing
```

**The packages.** `flock` is part of util-linux, which each of these systems
already has.

```sh
sudo apt update && sudo apt install -y git sqlite3 python3-venv
python3 --version                         # 3.10 or newer: 3.12 on 24.04, 3.10 on 22.04, 3.11 on Debian 12
sqlite3 --version && git --version && flock --version    # three versions, no error
```

**The clock.** Sweeps are spaced by it, and alert hours are read from it.

```sh
timedatectl                               # System clock synchronized: yes, and NTP service: active
```

If it says `no`, `sudo timedatectl set-ntp true` and look again in a minute. If
that fails, the box has no time service, and
`sudo apt install -y systemd-timesyncd` adds one. The box can stay in UTC:
`DIAYN_TZ` sets the zone for alert hours and the daily housekeeping, whatever
the box's own. The backups' cron line, below, runs at the box's own 04:15.

**The firewall.** DIAYN listens on no port. It only connects out: HTTPS to the
job boards, to Discord's API and, with a key, to Gemini, and a WebSocket to
Discord's gateway, all on port 443. Nothing needs opening for it. On a box that
already serves something, as one shared with another bot may, turning ufw on is
optional and not part of DIAYN's setup: nothing below depends on it. If ufw is
already active, leave it as it is: DIAYN needs nothing added.

To turn ufw on, which Ubuntu has and Debian gets with `sudo apt install -y ufw`,
look first at what the box already serves: turning ufw on closes every port it
was not told to allow, and on a box shared with another bot, that bot may be
serving on one. Allow SSH, and every public port that list shows, before
turning it on, or it can cut off the session you are typing in, or the other
bot:

```sh
sudo ufw status                           # inactive: nothing is filtered yet
sudo ss -tlnp                             # what listens now: sshd on 22, and whatever else the box serves
sudo ufw allow OpenSSH
sudo ufw allow <port>/tcp                 # one line per public port ss showed: 80 and 443, say, for another bot's web server
sudo ufw enable                           # asks before it turns on: see below
sudo ufw status                           # Status: active, OpenSSH ALLOW, and each port you allowed
```

`sudo ufw enable` asks `Command may disrupt existing ssh connections. Proceed
with operation (y|n)?`. Answer `y` only once `sudo ufw allow OpenSSH` has run;
otherwise answer `n`, and allow it first. A port `ss` shows on `127.0.0.1` or
`[::1]` is reachable only from the box itself, and needs no rule.

A Hetzner Cloud Firewall needs no inbound rule for DIAYN either: keep the one
for SSH, TCP 22, and any the box's other services need, and leave outbound
open, as it is while the firewall has no outbound rule. DIAYN itself needs only
DNS, TCP 443 and NTP (UDP 123) going out, but apt and the other software on the
box need more, so restricting outbound traffic is outside this guide.

**The two names.** As that user, add the two names *Where things live* uses to
`~/.bashrc`, so that every later shell has them, after a logout too:

```sh
cat >> ~/.bashrc <<'EOF'
D=$HOME/DIAYN/data
B=$HOME/diayn-backups
EOF
```

Then open a new shell, by logging out and back in, and check that
`echo "$D" "$B"` prints both paths. If you will set `DIAYN_DATA`, make `D` that
path instead. Then go through *Installing* from `git clone` on. A host taking
over from an older tracker, or taking DIAYN over from another machine, stops
before `setup`: *Taking over from an older tracker* and *Moving DIAYN from
another machine* say why.

## Where things live

The commands below use these names:

```sh
D=$HOME/DIAYN/data          # the data directory: DIAYN_DATA, if you set it
B=$HOME/diayn-backups       # daily copies of both databases
```

*A fresh VPS* puts both in `~/.bashrc`. On any other host, set them in each new
shell that runs these commands, or add them to your shell's startup file too.

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
util-linux on Linux, and `brew install flock` on macOS. On a new VPS, *A fresh
VPS*, above, installs them. Then pm2 or systemd, below.

Before `setup`, give DIAYN its own application in the Discord developer
portal, as the README's [quick start](README.md#quick-start), step 1, and
[*The Discord developer portal*](README.md#the-discord-developer-portal) say:
create the application, press **Reset Token** for `DISCORD_TOKEN`, turn
**Server Members Intent** on, set **Install Link** to **None**, and turn
**Public Bot** off.

```sh
git clone https://github.com/FakeZhiyuanLi/DIAYN.git ~/DIAYN
cd ~/DIAYN && git checkout --detach v1.0.0
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests          # must end OK
cp example.env .env && chmod 600 .env                    # then fill it in
.venv/bin/python diayn.py setup
mkdir -p "$B" && chmod 700 "$B"
```

Then invite the bot to your server with the link `setup` printed.

A host always runs a tag, never a branch, so what runs is exactly what was
released. `setup` checks the token, makes `$D` at mode 700 (or tightens it,
and the databases in it, if others on the box can read them), bootstraps
`postings.db` and prints the invite link; the README says what each step does.

**On macOS**, use Homebrew's `sqlite3`, not Apple's `/usr/bin/sqlite3`. Apple's
refuses to open a `.backup` copy of a WAL database read-only, as
`file:…?mode=ro`, with `unable to open database file (14)`, and this file
checks every copy it makes that way. Homebrew keeps its own off the `PATH`:

```sh
brew install sqlite flock
export PATH="$(brew --prefix sqlite)/bin:$PATH"      # add this line to ~/.zshrc too, for every new shell
command -v sqlite3                                    # Homebrew's, not /usr/bin/sqlite3
```

On Linux, the system's own `sqlite3` opens such a copy, and needs nothing more.

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
  without overriding what is already set, so a value in the environment pm2
  starts it with, or in a systemd `Environment=` line, beats the file. The pm2
  config below filters every variable DIAYN reads out of what pm2 would hand
  it (`filter_env`), and systemd starts it with a clean environment.
  `.venv/bin/python diayn.py config` shows what a run from your shell would
  use, and shows the token and the key only as set or not set.
- **Settings are read once, at the start.** After editing `.env`, restart the
  service.
- **Never paste the `.env` anywhere.** Paste `config`'s output instead.

## Choosing pm2 or systemd

On a Linux VPS, use systemd. Its `RestartPreventExitStatus=3 78` holds on every
start, the first after a reboot included, and the unit below adds
`UMask=0077`, `NoNewPrivileges` and `PrivateTmp`, which the pm2 config does not
set. journald keeps and rotates its log.

pm2 is fine where the box already runs its other apps under pm2, as a box
shared with another bot may, and that pm2 belongs to the user DIAYN will run
as: one tool for everything on the box. If another user's pm2 runs them, use
systemd; *Sharing the box with another bot* says how to tell. pm2 has one
caveat.
[pm2 issue #5601](https://github.com/Unitech/pm2/issues/5601) reports
`stop_exit_codes` ignored after `pm2 resurrect`, which is how pm2 brings its
apps back at boot, so after a reboot pm2 may restart DIAYN on exit 3 or 78.
DIAYN stays safe: `run` asks Discord's REST API about the token and the Server
Members Intent before it logs in, so a restart on 78 repeats that REST call and
never a gateway login, unless the check itself could not be made, and a
restart on 3 meets the lock again, exits 3 again, and backs off. Still, check
`pm2 list`'s restart count after a reboot (*After a reboot*, below).

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
    filter_env: ["DISCORD_TOKEN", "GEMINI_", "POLL_", "DIAYN_", "FIT_", "LLM_",
                 "POSTINGS_DB", "BOARDS_FILE", "YC_CACHE", "POLLER_ENV_FILE"],
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
- **`filter_env`**, so DIAYN never inherits another bot's settings. pm2 hands
  an app the environment of the shell that ran `pm2 start`, saves it with the
  app for every `pm2 resurrect`, and that environment wins over DIAYN's `.env`:
  a `DISCORD_TOKEN` exported there for another bot would log DIAYN in as that
  bot. pm2 drops from it every variable whose name contains one of these
  strings, anywhere in the name, and between them they cover every variable
  DIAYN reads. `env:` is applied after the filter, so `PYTHONUNBUFFERED`
  still arrives, and so would a setting you added there. Restart DIAYN by
  name, `pm2 restart diayn`, never through the config file and never with
  `--update-env`: once `diayn` is in `pm2 list`, even stopped, a `pm2 start`,
  `pm2 restart` or `pm2 reload` of `~/diayn.config.cjs` restarts it with the
  shell's whole environment merged back in, filter or not. To apply an edited
  config, run `pm2 delete diayn && pm2 start ~/diayn.config.cjs && pm2 save`,
  as *After a reboot* does. systemd starts DIAYN with a clean environment, so
  the unit below needs nothing like this.
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
  never a gateway login, unless the check itself could not be made. The log
  line says what to fix; then `pm2 restart diayn`.
- **`kill_timeout`.** pm2 stops a process with SIGINT, which `run` treats as
  Ctrl-C, on every supported Python: it logs out of Discord and exits 0. This gives it ten seconds before
  SIGKILL.

**Start it now:**

The first time only, while `pm2 list` has no `diayn`. After that, it is
`pm2 restart diayn`, as `filter_env` above says.

```sh
pm2 start ~/diayn.config.cjs
pm2 list                             # diayn online, and every other app as it should be after a reboot
pm2 save                             # what pm2 brings back at boot: the list as it stands
```

Then, only if pm2 is not yet set up to start at boot for this user, set it up,
once. On Linux, see whether it is:

```sh
systemctl is-enabled "pm2-$USER"     # enabled: already set up, so skip pm2 startup
```

and on macOS, whether `~/Library/LaunchAgents/pm2.$USER.plist` exists. If it
is not set up:

```sh
pm2 startup                          # prints one command, starting with sudo: run it as printed
```

**Later**, one line at a time, as you need it:

- `pm2 restart diayn`, after editing `.env` or upgrading.
- `pm2 stop diayn && pm2 save`, to stop it and keep it stopped across a
  reboot. `pm2 restart diayn && pm2 save` brings it back.
- `pm2 logs diayn --lines 50 --nostream`, its last 50 lines.

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

**Start it now:**

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now diayn
systemctl status diayn               # active (running)
```

**Later**, one line at a time, as you need it:

- `sudo systemctl restart diayn`, after editing `.env` or upgrading.
- `sudo systemctl stop diayn`, to stop it until the next boot, or
  `sudo systemctl disable --now diayn` to keep it stopped across one.
  `sudo systemctl enable --now diayn` brings it back.
- `journalctl -u diayn -n 50 --no-pager`, its last 50 lines.

### Commands by hand while it runs

The commands that only read (`stats`, `config`, `verify`, `list` without
`--llm`, `doctor`) are safe at any time. The ones that write (`sweep`, `prune`,
`discover`, `llm-diff`, `list --llm`, `upgrade-db`) exit 3 while the service
holds the lock: stop the service first, and start it again after.
`grant` and `revoke` write only `users.db`, and are safe at any time.

## Sharing the box with another bot

DIAYN runs beside another bot on one VPS as long as the two share nothing but
the machine.

**First, find out whose pm2 runs the other bot**, before any pm2 command.
pm2 keeps one daemon per user, and a user's pm2 commands reach only their own.
Run as any other user, `pm2 list` silently starts a second, empty daemon for
that user and shows an empty list, and a `pm2 save` or `pm2 startup` there does
nothing for the other bot.

```sh
ps -eo user:32,args | grep '[G]od Daemon' # one line per pm2 daemon: its owner, then its PM2_HOME in parentheses
systemctl list-unit-files 'pm2-*'         # pm2-<owner>.service, for each user whose pm2 starts at boot
```

If that owner is not the user DIAYN will run as, or no pm2 daemon runs at all,
run DIAYN under systemd, as *Choosing pm2 or systemd* recommends anyway,
instead of pm2, and leave the other bot's pm2 alone.

- **Its own checkout, `.env` and token.** DIAYN lives in `~/DIAYN`, with a
  `.env` of its own and a `DISCORD_TOKEN` from its own application in the
  developer portal. Never reuse the other bot's token: two processes on one
  token answer every command twice, and DIAYN's start would replace that bot's
  slash commands with its own.
- **Its own data and backups.** `$D` and `$B`, as in *Where things live*, apart
  from the other bot's files, with DIAYN's own `~/diayn-backup.sh` and cron
  line.
- **Nothing of the other bot's in DIAYN's environment.** The environment wins
  over DIAYN's `.env` (*The `.env`*, above), and an app pm2 starts inherits the
  environment of the shell it was started from. A `DISCORD_TOKEN` exported
  there for the other bot would log DIAYN in as that bot. The pm2 config's
  `filter_env` keeps every variable DIAYN reads out of it, and systemd hands
  DIAYN none of them. The log's `DIAYN is logged in as …` names the bot it
  logged in as.
- **One service manager for DIAYN.** DIAYN under systemd beside another bot
  under pm2 is fine. DIAYN under both is two copies on one token.

Under pm2, as the user whose pm2 already runs the other bot:

- **Its own app name and config file:** `name: "diayn"`, in
  `~/diayn.config.cjs`, apart from the other bot's config. Check `pm2 list`
  first: no other app may be called `diayn` already.
- **DIAYN by name in every command:** `pm2 restart diayn`, `pm2 stop diayn`,
  `pm2 logs diayn`. Never `pm2 restart all` or `pm2 stop all`, which restart or
  stop the other bot too, and never `pm2 kill`, which stops pm2 itself and
  every app it runs.
- **`pm2 save` saves every app in `pm2 list`**, as the list stands, and a
  reboot brings back exactly that. Check the list before saving:

  ```sh
  pm2 list                  # both bots, each online or stopped as it should be after a reboot
  pm2 save
  ```

- **`pm2 startup` once per user.** It writes the systemd unit, `pm2-<user>`,
  that brings pm2 and its saved apps back at boot. If the other bot set it up,
  it is done: do not run it again.

  ```sh
  systemctl is-enabled "pm2-$USER"          # enabled: already set up
  ```

- **Logs.** pm2 keeps each app's log under `~/.pm2/logs`, and never trims it.
  `pm2 install pm2-logrotate`, once for the whole pm2 daemon, rotates every
  app's log, both bots' included. If the other bot installed it, `pm2 list`
  shows it among its modules, and there is nothing to do.

Under systemd, journald keeps DIAYN's log and rotates it itself:
`journalctl -u diayn`.

**A user of its own.** One user for both bots is the simplest. A separate Unix
user for DIAYN is stronger isolation: the other bot's process cannot read
DIAYN's `.env` or `users.db`, and DIAYN's resume reader, which can read
whatever its user can, reaches the other bot's files only as far as their
modes let anyone. The extra steps:

```sh
sudo adduser --disabled-password diayn    # Enter through its questions; no password, reached only through sudo
sudo -iu diayn                            # a shell as diayn, in /home/diayn
```

Then everything from *Installing* on as `diayn`, with `/home/diayn` in the
paths, except what needs `sudo`, which `diayn` does not have: do that from your
own user. systemd suits it best: `User=diayn` in the unit, which you write and
start with `sudo` from your own user, and no second pm2. The other bot's pm2
belongs to another user, so the first step above says systemd too.

Look at it as `diayn`, too. After `sudo -iu diayn`, in `~/DIAYN`, run
`.venv/bin/python diayn.py doctor`, and, under pm2, `pm2 list` and
`pm2 logs diayn --lines 50 --nostream`: your own user cannot read `diayn`'s
`.env` or data, and your own pm2 is another daemon, which shows none of it.
Under systemd, read its log from your own user with
`sudo journalctl -u diayn -n 50 --no-pager`.

## Taking over from an older tracker

For a host where another bot used to sweep the job boards into a `postings.db`
of its own, and kept its `/internships ping` subscribers in its own `stats.db`.
DIAYN takes over the ledger, so nothing already announced is announced again,
and the subscribers, as profiles. The commands use one more name:

```sh
OLD=/path/to/the/old/data      # the directory holding the old bot's postings.db and stats.db
```

If the two files are in different directories, use each one's own path where
the commands below say `$OLD`.

1. **Stop the old bot first, entirely.** Stop its whole service, not only its
   sweep, and keep it stopped until step 10. Two sweepers double the traffic
   to every job board, since DIAYN's lock cannot see the old bot's, and two
   bots alert the same people about the same roles. Stop it through whatever
   runs it, by its own name. Under pm2, as the user whose pm2 runs it (the
   first step of *Sharing the box with another bot* finds them):

   ```sh
   pm2 stop <old-bot>                             # its name in pm2 list; never all
   pm2 list                                       # the old bot stopped, anything else as it was
   pm2 save                                       # so a reboot leaves it stopped
   ```

   Under systemd, `sudo systemctl disable --now <old-bot>`, which also keeps it
   stopped across a reboot. Then check that nothing sweeps: the newest sweep in
   its file stays where it was, from before you stopped it. The time is UTC.

   ```sh
   sqlite3 "file:$OLD/postings.db?mode=ro" "SELECT datetime(MAX(started), 'unixepoch') FROM sweeps;"
   ```

   Run it again 20 minutes later: the same time.
2. **Install DIAYN** as *Installing* says, up to and including the `.env`, and
   make `$B`, but do not run `setup` yet (step 8 says why). Then make the data
   directory, and check that it is empty:

   ```sh
   mkdir -p "$D" && chmod 700 "$D"
   ls -A "$D"                                     # nothing
   ```

3. **Copy the ledger with `.backup`**, never `cp`, and compare the two:

   ```sh
   (umask 077 && sqlite3 "file:$OLD/postings.db?mode=ro" ".backup '$D/postings.db'")
   sqlite3 "file:$OLD/postings.db?mode=ro" \
     "SELECT COUNT(*), MAX(rowid) FROM seen; SELECT COUNT(*), MAX(rowid) FROM postings;"
   sqlite3 "file:$D/postings.db?mode=ro" \
     "SELECT COUNT(*), MAX(rowid) FROM seen; SELECT COUNT(*), MAX(rowid) FROM postings;"
   ```

   The last two print the same four numbers. `postings`'s rowids are the bot's
   autocomplete values, and `.backup` keeps them. `umask 077` makes the copy
   readable by you alone, as DIAYN makes its own databases.
4. **Bring its boards**, if it had them: `boards.json`, what `discover` found,
   and `yc_cache.json`, its Y Combinator cache.

   ```sh
   ls "$OLD"                                      # boards.json and yc_cache.json, if it has them
   cp "$OLD/boards.json" "$D/"                    # if it has one
   cp "$OLD/yc_cache.json" "$D/"                  # if it has one
   ```

   DIAYN then polls the boards in `boards.json`, plus the seed boards built into
   `internship_poller.py` (`SEED_BOARDS`), less any company in its
   `BLOCKED_COMPANIES`; the ledger does not choose them. A board it polls and
   the old tracker did not hands its whole open board to the first sweep as
   new, and so does a company the old tracker blocked and DIAYN does not:
   compare the two before the first start.
5. **Bring the ledger up to the contract:**

   ```sh
   cd ~/DIAYN
   .venv/bin/python diayn.py upgrade-db
   ```

   It prints `integrity_check: ok`, then each table's rows and highest rowid
   before and after, which must match, and ends naming `$D/postings.db` as its
   `db_path`. It refuses, having changed nothing, a file that fails the check or
   is not schema version 2, and is safe on one that has the contract's tables
   already.
6. **Import the subscribers**, once:

   ```sh
   sqlite3 "file:$OLD/stats.db?mode=ro" "SELECT COUNT(*) FROM intern_pings;"
   .venv/bin/python diayn.py import-legacy --from "$OLD/stats.db"
   ```

   The first prints how many subscribers the old tracker has. `import-legacy`
   must print the same number, as `N subscribers in the old tracker; W
   imported, A already had a profile.`, with W and A adding up to N; otherwise
   it exits 1 and imports nothing. It opens `stats.db` read-only, and runs
   once: a second run is refused, so nobody who has since deleted their data
   comes back.
7. **Grant access before the first start**, to each server whose members should
   keep their alerts:

   ```sh
   .venv/bin/python diayn.py grant --server <id>
   ```

   An imported profile is only as good as its owner's access. Without a grant
   that covers them, nobody imported gets an alert, and 30 days after the bot
   first finds them without access, their profiles are deleted. The id is the
   server's **Copy Server ID** in Discord, with Developer Mode on; `grant`
   prints what it did, never the id.
8. **`setup` only now.** `setup` is for a new ledger: on a box with no
   `postings.db` it bootstraps one with a first sweep, and the old ledger could
   then come in only by a `.restore` over it. With the copied ledger in place it
   only checks: the token, the intent, the data directory and the files in it,
   tightening what others could read, and it leaves `postings.db` as it is. Run
   it now for its invite link, since DIAYN's own bot still has to join the
   server:

   ```sh
   .venv/bin/python diayn.py setup
   ```

9. **Start DIAYN** under its service manager, with the unit or the config from
   *Running it as a service* in place:

   ```sh
   sudo systemctl daemon-reload && sudo systemctl enable --now diayn
   ```

   or, under pm2, `pm2 start ~/diayn.config.cjs`, then `pm2 list` and
   `pm2 save`, as in *Sharing the box with another bot*. Then go through
   *Checking it runs*. In Discord, `/diayn access` shows the servers you
   granted, by name, with the counts.
10. **Bring the old bot back**, if it is to run on, only once DIAYN has passed
    *Checking it runs*, and the old bot runs a version with its tracker
    removed, or with both its sweep and its alerts turned off. An update that
    removes its tracker may also announce the move to DIAYN, and that must not
    go out before DIAYN runs. Under pm2, as the user whose pm2 runs it:

    ```sh
    pm2 restart <old-bot>                          # or however its own update starts it
    pm2 list                                       # the old bot online, anything else as it was
    pm2 save                                       # so a reboot brings it back
    ```

    Without that `pm2 save`, a reboot can bring it back stopped, as step 1
    saved it. Under systemd, `sudo systemctl enable --now <old-bot>` brings it
    back, and keeps it across a reboot.

With a separate `diayn` user, `diayn` cannot read the old bot's files, and
your own user cannot read `diayn`'s data directory. Run steps 3 and 4 from your
own user instead, with `D=/home/diayn/DIAYN/data` and `sudo` before every
command in them, inside the parentheses for the copy
(`(umask 077 && sudo sqlite3 …)`). Then give the copies to `diayn`:

```sh
sudo chown -R diayn: "$D"
```

For step 6, count the old subscribers the same way, and copy the old
`stats.db` to a file only `diayn` can read:

```sh
sudo sqlite3 "file:$OLD/stats.db?mode=ro" "SELECT COUNT(*) FROM intern_pings;"
(umask 077 && sudo sqlite3 "file:$OLD/stats.db?mode=ro" ".backup '/home/diayn/old-stats.db'")
sudo chown diayn: /home/diayn/old-stats.db
```

Then, as `diayn`, in `~/DIAYN`,
`.venv/bin/python diayn.py import-legacy --from ~/old-stats.db`, and once it
has printed its counts, `rm ~/old-stats.db`: the copy holds the old bot's
Discord ids.

## Moving DIAYN from another machine

For a DIAYN that already runs somewhere else, a laptop where it was tried out
for example, and is to run on the VPS from now on, with its profiles, grants
and ledger. If the VPS is new, go through *A fresh VPS* first. `$D` is the
data directory on whichever machine a command runs on. On the old one, set `D`
to its data directory first: `.venv/bin/python diayn.py config`, in its
checkout, prints it as `DIAYN_DATA`.

**Never run both copies at once.** One token, one running copy: two answer
every command twice, and the lock cannot see a copy on another machine. The old
copy stops in step 1, and stays stopped.

1. **Stop the old copy**, through its service (`pm2 stop diayn && pm2 save`, or
   `sudo systemctl disable --now diayn`), or with Ctrl-C in the terminal it
   runs in. Then prove it stopped: nothing holds its lock.

   ```sh
   flock -n "$D/postings.db.lock" true && echo free     # free; nothing printed means something still holds it
   ```

2. **Copy both databases with `.backup`**, on the old machine, into a directory
   only you can read. On a Mac, with Homebrew's `sqlite3` (*Installing* says
   why).

   ```sh
   mkdir -p ~/diayn-move && chmod 700 ~/diayn-move
   (umask 077 && sqlite3 "file:$D/postings.db?mode=ro" ".backup '$HOME/diayn-move/postings.db'")
   (umask 077 && sqlite3 "file:$D/users.db?mode=ro" ".backup '$HOME/diayn-move/users.db'")
   sqlite3 "file:$HOME/diayn-move/postings.db?mode=ro" \
     "PRAGMA integrity_check; SELECT COUNT(*) FROM seen; SELECT MAX(rowid) FROM postings;"
   ```

   The last prints `ok` and two numbers. Note them.
3. **Copy them to the new host** with `scp`, into a directory only you can read
   there:

   ```sh
   ssh <user>@<server-ip> 'install -d -m 700 ~/diayn-move'
   scp -p ~/diayn-move/* <user>@<server-ip>:diayn-move/
   ```

   `scp` carries the two finished copies, which nothing has open and which have
   no `-wal` beside them. It is never the way to copy a live database.
4. **Install DIAYN on the new host**, as *Installing* says, up to but not
   including `setup`: the checkout at a release tag, the venv and the tests,
   and a `.env` written by hand, `cp example.env .env && chmod 600 .env`, then
   the same token and settings as the old copy's, typed in, never copied.
   `config` on the old machine shows which are set, and a path in them, such as
   `DIAYN_DATA`, is this host's own now. Make `$B`. Then restore both databases
   into an empty data directory:

   ```sh
   mkdir -p "$D" && chmod 700 "$D"
   ls -A "$D"                                     # nothing
   flock -n "$D/postings.db.lock" \
     sqlite3 "$D/postings.db" ".restore '$HOME/diayn-move/postings.db'"
   flock -n "$D/postings.db.lock" \
     sqlite3 "$D/users.db" ".restore '$HOME/diayn-move/users.db'"
   sqlite3 "file:$D/postings.db?mode=ro" \
     "PRAGMA integrity_check; SELECT COUNT(*) FROM seen; SELECT MAX(rowid) FROM postings;"
   ```

   The last prints `ok` and the two numbers from step 2. Only now `setup`. With
   both databases in place it only checks: the token, the intent, the data
   directory and the files in it, tightening any that others could read, as
   the two restored files may be. It leaves `postings.db` as it is, where on an
   empty data directory it would have bootstrapped a new ledger.

   ```sh
   .venv/bin/python diayn.py setup
   ```

   The bot is in its servers already: the invite link it prints is for a new
   server only.
5. **Start DIAYN** under its service manager, as *Running it as a service*
   says, and go through *Checking it runs*.

Once it has run on the new host for a day, `rm -r ~/diayn-move` on both
machines: the copies of `users.db` hold everyone's profile. Leave the old copy
stopped for good. Under pm2, `pm2 delete diayn && pm2 save` takes it out of
the list a reboot brings back; a systemd unit that step 1 disabled stays off.

## Checking it runs

1. `pm2 list` or `systemctl status diayn` shows it online, with a restart count
   that is not climbing.
2. `pgrep -f 'diayn[.]py run' | wc -l` prints 1. It counts PIDs, which
   pgrep prints the same way on Linux and macOS, whereas its `-a` flag does not
   travel: on Linux it lists command lines, on macOS it adds ancestors to the
   match. The `[.]` keeps the pattern from matching a shell that runs this very
   line. To see the command line as well: `pgrep -lf` on macOS, and
   `pgrep -f --list-full` on Linux. pgrep also counts a wrapper whose own
   command line holds the same words, such as an `sh -c` or a `sudo -u` that
   started `run` (not `nohup`, which becomes the command it runs), so a 2 is
   not always two copies. The lock is the real guard:
   `flock -n "$D/postings.db.lock" true && echo free || echo held` prints
   `held` while DIAYN runs, and `doctor`'s `sweeper` line (step 4) says the
   same.
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

## After a reboot

1. `systemctl status diayn` shows it `active (running)`, or `pm2 list` shows it
   `online`. On a shared box, the other bot is `online` too, in `pm2 list` as
   the user whose pm2 runs it, unless a takeover still keeps it stopped
   (*Taking over from an older tracker*, step 10). One that came back stopped
   was saved stopped: as that user, `pm2 restart <other-bot> && pm2 save`.
2. The restart count is not climbing: `systemctl show diayn -p NRestarts`, or
   the ↺ column of `pm2 list`, the same a minute apart. Under pm2, a count that
   climbs with exit 3 or 78 in the log is the caveat in *Choosing pm2 or
   systemd*: `pm2 stop diayn`, fix what the log line names, then start it
   again from its config file, with the other bot in `pm2 list` as it should
   be, since the save takes the whole list:

   ```sh
   pm2 delete diayn && pm2 start ~/diayn.config.cjs && pm2 save
   ```

   Not `pm2 start diayn` or `pm2 restart diayn`: either keeps whatever options
   `pm2 resurrect` gave it, the ignored `stop_exit_codes` among them, where a
   start from the file gives it the file's own.
3. `.venv/bin/python diayn.py doctor`, in `~/DIAYN`, ends
   `doctor: nothing to fix`.
4. A `sweep: …` line in the log within 15 minutes of the start:
   `journalctl -u diayn -n 50 --no-pager`, or
   `pm2 logs diayn --lines 50 --nostream`.

## Backups

Once a day, a consistent copy of both databases, kept 14 days. Save this as
`~/diayn-backup.sh`:

```sh
#!/bin/sh
set -eu
umask 077
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
- `umask 077`, so every copy is readable by you alone. cron's own umask would
  make each copy of `users.db` mode 644, readable by everyone on the box but
  for `$B`'s own mode.
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
