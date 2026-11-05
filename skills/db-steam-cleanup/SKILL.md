---
name: db-steam-cleanup
description: Free disk space on the games-db Steam VM by deleting the per-game install snapshots (steam-app-<appid>) that have not been used recently — least recently used first, never the baseline snapshots, measured before and after. Use when the host or the Steam VM runs short of space, before a large install, or when asked to "clean up Steam snapshots" or "free space".
argument-hint: "[--older-than DAYS (default 14)] [--free GiB] [--dry-run] [--auto]"
---

# Delete game snapshots nobody used lately

Arguments: `$ARGUMENTS`

```sh
export UTMBOT_VM="$("${CLAUDE_PLUGIN_ROOT}/bin/utmbot" machine get test_vm)"   # fails: see docs, "The Steam VM"
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
```

Snapshots: `${CLAUDE_PLUGIN_ROOT}/docs/gamesdb.md` ("The Steam VM").
Deleting snapshots changes the VM's disks, so hold the UTM lease while you
do (`$UB lock acquire --purpose "db-steam-cleanup" --wait 21600`, and `$UB
lock release` at the end). A game snapshot is only a cache, and an aging
one: the game stays owned, a stale snapshot downloads the game's updates on
every boot anyway, and `/utmapp-bot:db-steam-install` makes the snapshot
again when a report needs it. The VM must be stopped.

**Mode.** Interactive unless the dispatcher started this session
(`GAMESDB_MODE=auto`, AUTO MODE in the system prompt, or `--auto`). Where
this skill says *ask*, auto mode decides itself when that is safe or
reversible, and otherwise leaves the item as it is and lists it in the
summary. `docs/gamesdb.md`, "Interactive and auto mode", has the rules,
including the one exception: a blocker only a person can clear.

In interactive mode run `$UB lock status` first: if auto-triage holds
UTM, tell the user which job and since when, and that their request waits
for it; then acquire with `--wait 600` steps, telling them each time.

## 1. Measure

```sh
$UB vm snap list --json          # snapshots with kind, appid, title, created, last_used; image_bytes
df -h ~                          # the host
```

## 2. Choose

Candidates: snapshots with `kind=game` (named `steam-app-*`). **Never**
`windows-clean`, `steam-baseline`, or any snapshot without that label.
Unlabelled ones were not made by these skills: leave them, and list them in
your summary.

- Default: every game snapshot whose `last_used` (else `created`) is older
  than `--older-than` days (14).
- With `--free N`: least recently used first, until about N GiB would be
  freed. Snapshot sizes are not reported, so estimate from the games' install
  sizes (`$UB steam appinfo <appid>` → `required_storage_gb`) and say it is
  an estimate.
- Keep a snapshot the current disk equals (`disk` in the listing), and
  keep any game with a report in progress
  (`$GDB reports --triage-status in_progress`, where
  `GDB="${CLAUDE_PLUGIN_ROOT}/bin/gamesdb"`).

With `--dry-run`, list what would go and stop.

## 3. Delete

```sh
$UB vm snap delete steam-app-<appid>      # one at a time
```

If the disk was `dirty` (a writable boot never saved), leave it. Deleting
snapshots does not touch it, but say so in the report.

## 4. Measure again

`$UB vm snap list --json` (`image_bytes`) and `df -h ~`. Deleting frees the
snapshots' clusters inside the qcow2 images, so they are reused before the
image grows again. The host gets space back only if the image shrank. Report
both numbers and do not claim space that did not come back.

## CrossOver game bottles

When it is the Mac that runs short, CrossOver's per-game bottles
(`steam-app-<appid>`, one per game run in CrossOver) are the other cache.
They hold their game's files on the host, the same as a snapshot. Measure
them with `$UB cx list` and delete the same way: `$UB cx cleanup
--older-than <days>` or `--free <GiB>`, with `--keep <appid>` for games
with a report in progress, `--dry-run` first. It never touches the base
Steam bottle or bottles it did not make (`/utmapp-bot:crossover-compare`,
step 5).

## Report

Each snapshot deleted (title, app id, last used), the image size and the
host's free space before and after.
