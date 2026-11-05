---
name: db-steam-install
description: Install one Steam game (by name or Steam app id) on the games-db test VM (`utmbot machine get test_vm`) and save it as its own snapshot (steam-app-<appid>) — restore steam-baseline, boot writable, check the triage account owns it or claim the free license for a free game or demo, install through the client's own API with progress, shut down cleanly and snapshot. Use when asked to "install <game>" for triage, or by db-triage when a game has no snapshot yet.
argument-hint: "<game name | steam app id> [--reinstall] [--auto]"
---

# Install a Steam game into its own snapshot

Arguments: `$ARGUMENTS`

```sh
export UTMBOT_VM="$("${CLAUDE_PLUGIN_ROOT}/bin/utmbot" machine get test_vm)"   # fails: see docs, "The Steam VM"
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
GDB="${CLAUDE_PLUGIN_ROOT}/bin/gamesdb"
```

Snapshots and boots: `${CLAUDE_PLUGIN_ROOT}/docs/gamesdb.md` ("The Steam
VM"). Needs `steam-baseline` (`/utmapp-bot:db-steam-load`).

**Mode.** Interactive unless the dispatcher started this session
(`GAMESDB_MODE=auto`, AUTO MODE in the system prompt, or `--auto`). Where
this skill says *ask*, auto mode decides itself when that is safe or
reversible, and otherwise leaves the item as it is and lists it in the
summary. `docs/gamesdb.md`, "Interactive and auto mode", has the rules,
including the one exception: a blocker only a person can clear.

It uses UTM across several boots, so it holds the UTM lease from start to
end. Interactive: `$UB lock status` first, and while auto-triage holds UTM,
wait as `docs/gamesdb.md` ("UTM is one at a time") says.

```sh
$UB lock acquire --purpose "db-steam-install <appid>" --wait 21600   # first, before any vm command
$UB lock release                                   # last, on every path out
```

Called from db-triage, which already holds the lease in the same session,
`acquire` just keeps it, and the release is db-triage's to do.

## 1. Which app

- A number is the app id.
- A name: `$UB steam resolve <name>`. Take an exact title match of type
  `game`. If several candidates fit, or none does exactly, ask, listing
  each candidate's name and app id (auto: install nothing, list them in the
  summary, and stop).
- Then `$UB steam appinfo <appid>`. Note `type` (`game`, `demo`, …),
  `is_free`, `windows` (stop if false: the VM runs Windows) and
  `required_storage_gb`.

If `steam-app-<appid>` exists (`$UB vm snap list`) and `--reinstall` was not
given, the game is installed already: report the snapshot and stop.

## 2. Boot the baseline writable

```sh
$UB vm snap restore steam-baseline
$UB vm up --writable --tag install-<appid>
$UB steam start --wait 300            # "STEAM UP signed in as …"; signed OUT: db-steam-load, "Signed out during other work"
```

Check space: the guest's free space (`$UB guest "fsutil volume diskfree c:"`)
must exceed the required storage with room to spare (×1.5), and so must the
host's (`df -h ~`). If the host is short, run `/utmapp-bot:db-steam-cleanup`
first.

## 3. Ownership

```sh
$UB steam owned --appid <appid>       # owned, free, installed (the console's app_status)
```

- Owned: go on.
- Not owned, but free (`is_free`) or a demo (`type: demo`): claim it with
  `$UB steam license <appid>` (`LICENSED`; it stays on the account).
- Not owned and not free: stop. The game has to be gifted to the triage
  account first (`/utmapp-bot:db-steam-accept`). Shut down with `vm
  shutdown` and restore `steam-baseline` (nothing worth keeping changed).

## 4. Install

```sh
$UB steam install <appid> --timeout 7200
```

It queues the install with the client's console (`app_install`: default
library, no dialogs), logs progress as it changes, and returns
`INSTALLED <appid> <name> <bytes>` once `app_status` reports it fully
installed. Mention progress in your output during long
downloads. A stall is reported as `STALLED` after 15 minutes without
progress: look at `$UB shot` and the client (a license prompt, an EULA, a
full disk), fix it, then rerun the same command; it resumes.

Do not launch the game here. First-run installers (DirectX, VC++
redistributables) run on its first start in each triage boot. A
disposable boot discards them, which keeps the snapshot what Steam
installed.

## 5. Save

```sh
$UB steam stop
$UB vm shutdown
$UB vm snap create steam-app-<appid> --set kind=game --set appid=<appid> --set "title=<name>" [--overwrite]
$UB game init <appid> --title "<name>" [--fullgame <appid of the full game, for a demo>]
```

Record in the workspace's `game.json`/`NOTES.md` what you saw: the
download size, the time it took, the install directory, EULAs, and
first-run installers. Anything that holds for other games too (a Steam
client behaviour, a stall and what cleared it) goes in `$UB notes`.

## 6. Free games become triage-available

If you claimed a free license in step 3, the game can be triaged by
anyone's report now. Record it: `$GDB owned-add steam <appid> --free`. If a
donation request pointed at this demo, close it:
`$GDB triage-request <request id> done` (a 409, "already resolved", means
another triage machine closed it first: fine).

## Report

App id, title, size installed, snapshot name, how long the download took,
and whether a free license was claimed and recorded.
