---
name: db-setup
description: Set up this Mac, once, as a games-db triage machine — the SSH tunnel to the server's internal API kept up for as long as the user is logged in (reconnecting with a cooldown through network loss and server maintenance), validated backups every 12 hours kept 2/7/4/12 (12-hourly, daily, weekly, monthly), and a dispatcher that polls the work feed every 15 minutes and runs the db skills in unattended Claude Code sessions (one UTM user at a time, usage-limit and outage recovery, clarifications resumed in the session that asked). Re-running it updates the installed scripts, settings and agents. Use when asked to "set up db", "install the triage agents", "update the agents", or when a db command says the Mac is not set up.
argument-hint: "--server _triage@<host> | --check | --uninstall [options]"
---

# Set up the games-db agents on this Mac

Arguments: `$ARGUMENTS`

Everything is done by one idempotent script:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gamesdb/setup.py" $ARGUMENTS
```

`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gamesdb/setup.py" --help` lists the
options. What it installs and how the db skills use it:
`${CLAUDE_PLUGIN_ROOT}/docs/gamesdb.md` ("Reaching the internal API",
"Interactive and auto mode").

## Before the first run

- **The server.** The first run needs `--server _triage@<host>`. The host
  comes from the user. Never guess it, search for it, or write it into the
  plugin; the script keeps it in `~/.utmapp-bot/gamesdb.env`. Later runs
  reuse it.
- **The site.** The first run also takes `--site https://<the site>`, the
  public address the games database serves, for links to its pages in
  comments and summaries. Like the server, it comes from the user; no site
  is built into the plugin (the games database can run anywhere).
- **Access.** `_triage` is an account on the server that may forward the
  internal API's socket and do nothing else. The server's administrator
  adds this Mac's public key with `sudo ./deploy/macos/add-operator.sh
  "<key>"` from the utm-games-db checkout. If the run stops with
  "Permission denied", it prints the exact command and key to hand them.
- **Awake and unlocked.** The agents drive VMs unattended, also with the
  lid closed. That needs system sleep disabled (`sudo pmset -a
  disablesleep 1`), the screen lock off (System Settings > Lock Screen >
  "Require password after screen saver begins or display is turned off":
  Never) and no screen saver ("Start Screen Saver when inactive": Never). A locked screen stops the VM window from being composited and
  lets macOS App-Nap UTM, so frame capture and timing go wrong. The run
  checks all three and warns.
- **The UTM sessions drive** (`--utm-app`, default `/Applications/UTM.app`)
  must be current (`docs/utm-host.md`, "Which UTM"). The run warns on an
  older one, and `--check` reports it.
- **Claude Code** must be installed and logged in (`claude`). The
  dispatcher runs `claude -p` sessions under the user's account.
- **The Steam VM** (this Mac's `test_vm`, `utmbot machine`; `steam-baseline`) is set up separately,
  by `/utmapp-bot:provision-windows-test-vm` and `/utmapp-bot:db-steam-load`.
  The agents work without it, but triage and gift work then report a
  blocker.

## Run

Installing the agents makes things that keep running after this session, so
Claude Code asks for permission to run the script. In auto permission mode
its safety check may refuse it outright ("Unauthorized Persistence"). Then
do not work around it: give the user the command to run in a terminal (or
with `!` in this session), exactly as below.

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gamesdb/setup.py" --server _triage@<host> [--utm-app /Applications/UTM.app] [--model <model>] [--paused]
```

**Several Macs** can share one server, each with its own Steam account.
Each needs a unique name (`--holder NAME`; default its LocalHostName,
kept on later runs): the server's triage leases use it so no two Macs
triage the same report (`docs/gamesdb.md`, "Several Macs").

It writes the settings, records the server's host key on first contact
(the fingerprint is printed), and stops the old on-demand tunnel and any
hand-started tunnel agent. It copies `scripts/gamesdb` to
`~/.utmapp-bot/agents/lib/` (the plugin directory is replaced on updates),
then loads three LaunchAgents and checks each step:

| Agent | Does |
|---|---|
| `app.getutm.utmapp-bot.tunnel` | `ssh -N -L 127.0.0.1:8081:<socket> _triage@host`, kept up; status in `gamesdb tunnel status` |
| `app.getutm.utmapp-bot.backup` | hourly `backup run`: a validated backup when the newest is 12 h old; keeps 2 / 7 daily / 4 weekly / 12 monthly |
| `app.getutm.utmapp-bot.dispatch` | the work dispatcher; `gamesdb dispatch status` |

Then it runs the first backup and shows what the dispatcher would start
(a dry run).

macOS asks its permission questions (UTM automation, Screen Recording) the
first time a dispatched session needs them. That session reports a
blocker, and the user gets a push notification over Remote Control to click
**Allow** on the Mac. Remote Control is off for dispatched sessions
otherwise. The dispatcher also warns, by push notification, 3 days and 1
day before the Claude Code login expires.

`--paused` installs the dispatcher without letting it start sessions;
`gamesdb dispatch resume` starts it later. `--no-dispatch` installs only the
tunnel and the backups.

## Updating

Run the same command again (no `--server` needed): it re-copies changed
scripts, rewrites changed agent definitions, restarts the agents whose code
changed, and leaves running Claude sessions alone. Do this after updating
the plugin.

## Check and remove

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gamesdb/setup.py" --check       # agents, API, tunnel, last backup, sleep settings
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gamesdb/setup.py" --uninstall   # agents gone; settings, state and backups kept
```

## Report

What was installed or changed, the tunnel's state, the first backup (name
and size, or why not), what the dispatcher would start, and the permission
check's result. Also report every WARNING line, above all sleep: a Mac that
sleeps drops the tunnel until it wakes, and a triage running at the time.
Remind the user that the backups hold the site's `SECRET_KEY` and its TLS
and DKIM keys (`~/.utmapp-bot/gamesdb/backups`, mode 700), so this Mac's
disk should be encrypted (FileVault).
