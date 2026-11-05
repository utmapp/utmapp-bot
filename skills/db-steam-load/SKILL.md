---
name: db-steam-load
description: Set up Steam on the games-db test VM (`utmbot machine get test_vm`) and save it as the steam-baseline snapshot every game install starts from — no VirtIO GPU driver, Steam installed silently, the user signs in once in the VM window, then the client is configured without clicks (appear offline and do not sign in to friends, run at startup, no store or "what's new" pop-ups at start, open in the Library) and the VM is shut down cleanly and snapshotted. Also refreshes an existing baseline (client update, expired sign-in). Use when asked to "load Steam", "set up the Steam VM", "make the Steam baseline" or when Steam reports it is signed out.
argument-hint: "[--refresh]"
---

# Load Steam into the baseline snapshot

**Someone has to be there:** a person signs in to Steam in the VM's window.
Run it interactively, or from a dispatched session over Remote Control
(see "Signed out during other work" at the end: the user never needs to
start this skill for an expired sign-in).

Arguments: `$ARGUMENTS` (`--refresh`: start from the existing `steam-baseline`
instead of a clean Windows)

```sh
export UTMBOT_VM="$("${CLAUDE_PLUGIN_ROOT}/bin/utmbot" machine get test_vm)"   # fails: see docs, "The Steam VM"
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
```

The Steam VM, its snapshots and why boots here are writable:
`${CLAUDE_PLUGIN_ROOT}/docs/gamesdb.md` (section "The Steam VM"). Take the
UTM lease first and release it at the end
(`$UB lock acquire --purpose "db-steam-load" --wait 21600`; dispatched work
waits meanwhile). The VM
must exist (`/utmapp-bot:provision-windows-test-vm <iso> --name "Windows
Steam" --disk 256`), and only one UTM may run (`$UB app`).

## 1. Start from the right disk

```sh
$UB vm snap list
```

- **First run** (no `windows-clean`): the disk is the freshly provisioned
  VM. Save it first so a later reload never stacks on an old Steam:
  `$UB vm snap create windows-clean --set kind=base`.
- **Rebuild:** `$UB vm snap restore windows-clean`.
- **`--refresh`:** `$UB vm snap restore steam-baseline`.

If `restore` refuses because the disk is dirty, a writable boot was never
saved. Ask the user before passing `--force`.

## 2. Boot writable and check there is no GPU driver

```sh
$UB vm up --writable --tag steam-load
$UB guest "pnputil /enum-drivers | findstr /i viogpu"     # must print nothing
```

The baseline must not contain a VirtIO GPU driver. Every test boot installs
the driver under test itself, and a driver baked into the snapshot would
mix two builds. If the check prints anything, stop: the VM was not
provisioned as a test VM (its `gpu` stage removes them).

## 3. Install and start Steam

```sh
$UB steam setup            # silent install + DevTools file (skipped when installed)
$UB steam start --wait 900 # the first start self-updates (minutes); ends "STEAM UP signed OUT"
```

## 4. The user signs in

If `steam start` said `signed in as …`, skip this step, and report it as
it is: Steam was signed in when this run started it. Do not conclude that
the snapshot kept its session. The user may have signed in a moment ago
(in this VM window, or through another run of this skill). Only a `steam
start` that printed `signed OUT` before anyone signed in shows whether the
old session had expired.

Tell the user, in one message:

> Steam is waiting for you in the **$UTMBOT_VM** VM window in UTM. Sign
> in with the triage account and tick **Remember me** (password plus Steam
> Guard, or the QR code in the Steam mobile app). Reply when you are done,
> or I will notice on my own.

Then wait: `$UB steam wait-login --timeout 1800`. It polls the client and
returns `SIGNED IN <account>`. `$UB shot` shows the VM's screen, and the
client's own windows can be read through DevTools: `$UB steam targets`
lists them (the sign-in window is "Sign in to Steam"). The password never passes through
this session; do not offer to type it. If it times out, ask the user
what they see (`$UB shot` takes a screenshot).

## 5. Configure

```sh
$UB steam configure
```

It sets these through the client's own settings API, reads each back, and
prints `CONFIGURED` with the values:
- friends: persona **Invisible** (appear offline), and `bSignIntoFriends`
  off, so the client does not sign in to friends at all when it starts
  (after a restart the persona reads 0, Offline);
- `run_at_startup` (Steam writes `HKCU\…\Run\Steam = "…steam.exe" -silent`;
  the check reads it back), and `cef_remote_debugging_enabled` next to the
  DevTools file;
- `start_page` **library**;
- `enable_marketing_messages` off (special-offer pop-ups) and
  `show_store_content_on_home` off (store content on the Library home).

Anything it cannot set it names as `NOT SET`. Then use `$UB steam js` to
inspect the live `SteamClient` API (the module docstring in
`scripts/utmbot/steam.py` explains how) and set that value through it by
hand. Do not fall back to clicking. Record the method that works now in
`$UB notes`, and say in your summary that `configure` needs it, for a
maintainer to fix in the plugin.

## 6. Save

```sh
$UB steam stop                                  # clean exit: Steam writes its config
$UB vm shutdown                                 # clean guest shutdown, never vm down
$UB vm snap create steam-baseline --overwrite --set kind=baseline
$UB vm snap list
```

Without `--overwrite`, `create` refuses when `steam-baseline` exists. Use
`--overwrite` for a refresh or rebuild, since that is the point.

## 7. Prove it, and tell the site what the account owns

Boot it the way every later skill does, disposable, and check that Steam
comes up by itself, signed in and offline:

```sh
$UB vm up --tag steam-check
$UB steam start --wait 300     # autostart already ran it: "STEAM UP signed in as …"
$UB steam settings             # as configured; persona_state 0 (offline)
$UB steam owned --entries > /tmp/owned.json   # the account's games, free ones marked
$UB vm down
```

Record it on the site (adding entries is idempotent; it updates the
"Auto-Triage available" badges):
`"${CLAUDE_PLUGIN_ROOT}/bin/gamesdb" owned-add --json /tmp/owned.json`.
Then `$UB lock release`.

Existing `steam-app-*` snapshots still hold the old client and session.
They work as long as that session stays valid; mention them to the user if
the reason for the refresh was an expired sign-in.

## Signed out during other work

Any session that gets `STEAM UP signed OUT` (db-triage, db-steam-install,
db-steam-accept) handles it the same way. Only a person can sign in, so
prepare everything else first, so that signing in is all they have to do:

```sh
$UB vm down                               # or vm shutdown, after a writable boot of a game
$UB vm snap restore steam-baseline
$UB vm up --writable --tag steam-signin
$UB steam start --wait 300                # "STEAM UP signed OUT": the sign-in window is up
```

- **Interactive:** tell the user to sign in in the `$UTMBOT_VM` window
  (step 4's message), then continue here from step 4.
- **Auto:** this is a blocker. Keep the VM running and run
  `"${CLAUDE_PLUGIN_ROOT}/bin/gamesdb" session blocked --scope steam
  --keep-vm --reason "Steam is signed out: sign in to Steam in the
  $UTMBOT_VM window on the triage Mac (tick Remember me), then reply
  here"`, then end your turn. The dispatcher holds all Steam work until it
  is cleared. Over Remote Control, once the user replies, continue here
  from step 4 (the VM is already up writable from `steam-baseline`, at the
  sign-in window), then run `session unblocked`.

The refresh signs in `steam-baseline` only. Game snapshots made before it
keep the dead session: the session that needs one deletes it and installs
the game again from the new baseline.

## Report

The snapshot name, client version, account name (it is ours, not personal
data), the settings `configure` printed, and the check boot's result.
