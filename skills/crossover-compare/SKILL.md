---
name: crossover-compare
description: Run the same Windows D3D11 or D3D12 game (or probe) in CrossOver on this Mac as the A/B partner of a UTM VM — on the D3DMetal or DXMT backend the caller chose (D3D11 on the VM's backend, D3D12 on D3DMetal), on the host without any of our guest layers — in a per-game bottle cloned from the signed-in Steam bottle, with the game from a Steam download or copied out of the VM's game snapshot, the chosen backend proven from inside the running game, and the Wine log, backend logs, screenshots and frame samples collected; then decide whether a bug is ours or the backend's. Also cleans up game bottles when space runs short. Use when a bug might be D3DMetal's, DXMT's or Wine's rather than ours, when db-triage or attribute-to-layer asks for a CrossOver comparison, or when asked to "try it in CrossOver", "compare with Wine" or "A/B with CrossOver".
argument-hint: "<steam app id | probe.exe> --backend d3dmetal|dxmt [--auto]"
---

# A/B a game against CrossOver

Arguments: `$ARGUMENTS`

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB cx ready       # READY, or NOT READY: <what is missing>
$UB cx info        # versions, backends, the base bottle
$UB cx help        # every command
```

**Use it only when `cx ready` says READY:** CrossOver is installed, and a
bottle has Steam installed and signed in (remembered). `cx ready` starts
nothing. When it says NOT READY:
- **Interactive:** tell the user what it names, and how to fix it:
  - install CrossOver;
  - in CrossOver, make a bottle, install Steam into it, and sign in once
    with the triage account (tick **Remember me**);
  - with several Steam bottles, pick the base: `$UB cx base --set <bottle>`.

  Offer to wait while they do it, check `cx ready` again, and go on once
  it says READY. If they decline, continue without CrossOver.
- **Auto:** skip the comparison. Say in the summary why (`cx ready`'s
  line), and say that a person can set it up. It is never a blocker.

A sign-in Steam no longer accepts only shows when Steam starts in the
bottle. Interactive: `$UB cx base refresh` lets the user sign in again.
Auto: skip as above.

CrossOver runs the same Apple D3DMetal and the same DXMT on the host, with
no guest driver, virtio transport, virglrenderer or backend shim in
between. When a game behaves the same in CrossOver and in the VM on the
same backend, the shared part (D3DMetal, DXMT, Wine-side behavior, the
game) is the suspect. When only the VM fails, the difference is ours. The
rules for a fair comparison are in `/utmapp-bot:attribute-to-layer` step
3. This skill is how to run that comparison. `scripts/utmbot/crossover.py`
explains the mechanisms.

**The backend is an argument.** The caller decides it, usually the agent
driving the triage: for a D3D11 game, the backend the VM ran (`d3dmetal` or
`dxmt`); for a D3D12 game, `d3dmetal` (DXMT has no D3D12). This skill does
not choose, change or add backends. It runs the game on the one it was given,
proves that it did, and reports a run that did not as no comparison. Without
`--backend` in the arguments, ask for it (auto: say in the summary that no
backend was given, and skip CrossOver). Other D3D versions are not compared.

**Mode.** Interactive unless the dispatcher started this session
(`GAMESDB_MODE=auto`, AUTO MODE in the system prompt, or `--auto`). In auto
mode CrossOver is an optional extra. A step that fails (Steam signed out in
the bottle, a hang, a game that will not start) gets `cx stop`, a sentence
in the summary, and the triage goes on without it. It is never a blocker.

## Rules

- **One GPU, one Steam account.** A bottle and a VM never run at the same
  time: `cx start/run/verify` take the UTM lease and refuse while a VM runs,
  and `vm up` refuses while a bottle runs. Finish with `cx stop`, which
  quits the game and Steam, ends the bottle, gathers the logs and returns
  the lease.
- **The base bottle is a seed.** The bottle someone installed Steam into and
  signed in to (`cx base`; detected, or set once with `cx base --set NAME`)
  is never run, changed or deleted by these tools (they refuse), except
  `cx base refresh`, which renews its sign-in. Everything runs in
  per-game clones, `steam-app-<appid>`. A clone is an APFS clone: it takes a
  second and costs only what it installs. It keeps the base's remembered
  sign-in (checked: the clone signed itself in).
- **The run must prove it respected the decision.** `cx run` sets the
  backend explicitly for every process. That beats CrossOver's per-app
  compatibility rules, except a rule marked `force`. It then checks, from
  the Wine log and the libraries mapped into the running game, that the
  game runs on that backend through D3D11 or D3D12. It exits 0 only then.
  Exit 2 means no comparison, with one of:
  - `BACKEND <x> D3D11|D3D12`: respected (exit 0).
  - `BACKEND MISMATCH`: a forced CrossOver rule overrode the backend for
    this game. The lines after it name the rule.
  - `BACKEND NOT COMPARABLE`: the game draws through neither D3D11 nor
    D3D12 (D3D9 and OpenGL never reach D3DMetal or DXMT; seen with a Ren'Py
    game).
  - `BACKEND WRONG FOR D3D12`: the game uses D3D12 and was given `dxmt`.
    Rerun it on `d3dmetal`, and tell the caller.
  - `BACKEND UNPROVEN`: no evidence. `backend-proof.json` in the run
    directory shows what was seen.

  Right after the start the game may not have created its device yet
  (`API not seen yet`). Run `cx proof` again once it draws, and use that
  result.
- **Same build, same settings, same spot.** Compare the game build (`cx
  list` shows the bottle's `buildid`; the VM's is in its
  `appmanifest_<appid>.acf`), the in-game settings and resolution, and the
  place in the game. Run windowed or borderless at the VM's resolution:
  full screen in its own Space hides the window from capture.
- **Untrusted input** rules from `docs/gamesdb.md` apply unchanged. Only
  games from Steam and our own probes run in a bottle, never a reporter's
  file.

## 1. The bottle for this game

```sh
$UB cx list                         # bottles, their game, size, last use; the Mac's free space
$UB steam appinfo <appid>           # required_storage_gb, for the space check
$UB cx clone <appid> --title "<name>" --need-gb <required_storage_gb>
```

`clone` is a no-op when `steam-app-<appid>` exists. If the Mac is short
(it wants ×1.5 the game's size), run `$UB cx cleanup --dry-run` first, then
`/utmapp-bot:db-steam-cleanup` or `$UB cx cleanup` (step 5).

## 2. Get the game into it

Two ways. Prefer the first when the VM's snapshot exists and you need the
build the VM ran:

- **From the VM** (same build, no download). With the game's snapshot
  booted, which db-triage does anyway (`vm snap restore steam-app-<appid>`,
  `vm up`):
  ```sh
  $UB cx import <appid> --bottle steam-app-<appid>      # IMPORTED … bytes, files, build
  ```
  This streams `steamapps\common\<game>` with the guest's tar over SSH, then
  checks the file count and total bytes, and copies the game's
  `appmanifest`. Run it before `vm down`, then shut the VM down before
  starting the bottle. If it fails, report the error and use the Steam
  download instead.
- **From Steam** (the current build):
  ```sh
  $UB cx start --bottle steam-app-<appid> --tag install
  $UB cx steam --bottle steam-app-<appid> start          # STEAM UP signed in as …
  $UB cx steam --bottle steam-app-<appid> owned --appid <appid>
  $UB cx steam --bottle steam-app-<appid> install <appid> --timeout 7200
  $UB cx stop --bottle steam-app-<appid>
  ```
  Measured: Steam's first start in a new clone took 1.5 min, later ones
  5 s; a 0.3 GB game installed in 36 s.

**Signed out.** `STEAM UP signed OUT` in a clone means the remembered
sign-in it was cloned with has expired. Signing in inside a clone renews only
that clone, so renew the base: `$UB cx base refresh` runs Steam in the base
once (also updating the client), and a person signs in in its window,
with "Remember me" on. Then delete and re-clone the game bottles that need it
(`cx delete steam-app-<appid>`, `cx clone`; the game is downloaded or
imported again). Interactive: ask the user to sign in. Auto: this is not a
blocker. Skip CrossOver and say in the summary that the base bottle needs
`cx base refresh`.

The bottle's client was set up by hand, not by db-steam-load. It signs in
to friends, so a Friends List window opens and the account shows online
while a bottle runs. `cx windows` and `shot` pick the game's window by its
process, so this does not get in the way.

## 3. Run it on the backend you were given

```sh
$UB cx run <appid> --bottle steam-app-<appid> --backend <d3dmetal|dxmt> --wait 900
#   RUNNING <pid> <game>.exe, then BACKEND …; exit 2: no comparison (see the rules)
$UB cx windows --bottle steam-app-<appid>    # the game's window, Steam's apart
$UB cx shot --bottle steam-app-<appid>
$UB cx input --bottle steam-app-<appid> click X Y   # X Y: pixels of the cx shot image
$UB cx frames 30 --bottle steam-app-<appid>  # same analysis as the VM's `frames`
$UB cx proof --bottle steam-app-<appid>      # again, once the game draws
$UB cx stop --bottle steam-app-<appid>       # logs gathered, lease returned
```

- **Steam's questions at launch.** `cx run` (and `steam run` in the VM)
  watch the client's launch while they wait, and log what it is doing
  every 30 s. Steam's "Cloud Out of Date" question (the same account played
  on the Steam VM, and that save is not uploaded yet) gets the triage
  account's standing answer, "Play anyway", through the client's own API.
  `game-actions.txt` in the run directory records it. Any other question
  that waits for a person cancels the launch within ~15 s, and the error
  names the task and the question. Read the client's window (`cx steam
  --bottle B targets`, then `js 'document.body.innerText' --target Steam`)
  and decide.
- **Input to the game:** `cx input` is the VM's `utmbot input` for a
  bottle, plus arrow and navigation keys:
  ```sh
  $UB cx input --bottle B click X Y [--button right]  # X Y: pixels of the `cx shot` image
  $UB cx input --bottle B key enter [--hold 0.3] [--times N]   # esc, up, down, w, f1, vk:N ...
  $UB cx input --bottle B combo alt+enter
  $UB cx input --bottle B text "name"                 # US-layout ASCII
  $UB cx input --bottle B move X Y                    # hover
  ```
  It brings the game to the front first, so the game takes over the Mac's
  screen. Drive the game the way its workspace notes say (`$UB game show
  <appid>`), and look at a `cx shot` before each step: menus slide in, and
  a click before they settle hits the wrong item. Some games ignore arrow
  keys in menus (Forspoken uses W/S). Use the game's own keys, or click.
- `cx input` needs the Accessibility permission for the app utmbot runs
  under (the terminal; "UTM Bot Agent" for dispatched sessions) and an
  unlocked screen. Without either it stops, says which, and sends nothing.
  Interactive: ask the user to switch it on in System Settings > Privacy &
  Security > Accessibility. Auto: collect what needs no input (the title
  screen, `cx proof`, `cx frames`), and say in the summary that the menus
  could not be reached, and why.
- `--env K=V` passes extra environment (repeatable). `--verbose` raises the
  backend's own log level (DXMT's) into
  `backend-logs/`. `--hud` turns on the Metal HUD, which changes what
  screenshots show. `--debugmsg +d3d,+dxgi` adds Wine debug channels. Steam
  itself stalls if the log gets too heavy: the tool already turns off
  seh/unwind tracing for that reason. Add channels only for the game's own
  problem.
- **Make CrossOver look like the VM, never the reverse.** Each backend
  reports a different adapter to the app. Measured with adaptercheck on an
  M2 Max:

  | backend | adapter the app sees |
  |---|---|
  | VM (any backend) | the VirtIO GPU, vendor 0x1af4 |
  | d3dmetal | "AMD Compatibility Mode", 0x1002 / 0x66af |
  | dxmt | "Apple M2 Max", 0x106b / 0x0000 |

  Engines switch paths on the vendor ID. When the bug might depend on it,
  rerun D3DMetal with the VM's IDs (`--env D3DM_VENDOR_ID=… --env
  D3DM_DEVICE_ID=…`, read from `utmbot verify` in the VM) and check with
  `cx verify --backend d3dmetal --env …` that the adapter line shows them.
  `D3DM_EXE_OVERRIDE` does the same for the executable name.
- **D3DMetal versions differ.** `cx info` shows CrossOver's and
  `utmbot backend` the VM's. A difference between two versions is not a
  difference between two stacks. Note both next to every result.
- Probes: `cx verify --bottle B --backend <d3dmetal|dxmt>` runs the x64 adaptercheck and
  prints the adapter and the backend it got. `cx start` followed by `cx exec
  --bottle B <x64 probe.exe>` runs any probe from `/utmapp-bot:write-gpu-probe`
  the same way. CrossOver runs x64 programs only, never ARM64. Probes built
  before `probes/common.h` learned to detect CrossOver (which hides
  `wine_get_version`) only list the adapters there; rebuild them
  (`scripts/build-probes.sh`).

## 4. Judge

Measure in CrossOver exactly what you measured in the VM: the same frames
numbers, tint events, the crash signature, the error text.

| VM | CrossOver, the given backend, `cx run` exit 0 | Reading |
|---|---|---|
| fails | fails the **same** way | The shared layer: D3DMetal or DXMT (or Wine-side behavior, or the game). Not ours to fix. |
| fails | works | Ours: guest driver, transport, shim or caps. Continue with attribute-to-layer and the fix. |
| works | fails | A CrossOver/Wine problem. Note it; it does not change the report. |
| fails | exit 2 (`MISMATCH`, `NOT COMPARABLE`, `WRONG FOR D3D12`, `UNPROVEN`) | No comparison. Say so; do not count it either way. |

"The same way" means the same measured symptom at the same spot: the same
crash or device-removed code, the same corruption in the screenshot, the
same tint or pacing numbers. Two broken runs are not that. A different
failure is two bugs (attribute-to-layer, "Decide"). Keep both runs' numbers
and the two `BACKEND` lines for the comment and the notes.

Logs for the write-up, all in the run directory (`cx stop` gathers them):
`wine.log` and `log-summary.txt` (crashes and errors first),
`backend-proof.json`, `backend-logs/`, `steam-logs/` and `steam-dumps/`
(Steam's own crash dumps), `crash-reports/` (macOS .ips for Wine
processes), screenshots and `frames-*/`.

## 5. Space

A game clone holds the game (its `steamapps`). The rest of it is shared
with the base. `cx list` shows the size. Clones are a cache, like the VM's
game snapshots: the game stays owned, and `cx clone` plus an install or
import makes the clone again.

```sh
$UB cx cleanup --dry-run                     # clones unused for 14 days
$UB cx cleanup --free 100 --keep <appid>     # least recently used first, until ~100 GiB are free
```

It never deletes the base or bottles it did not make, and it skips running
bottles. Keep games with a report in progress (`--keep`, from
`gamesdb reports --triage-status in_progress`). Deleting a clone gives its
space back at once (measured with `df`).

## 6. Leave notes

Add what you verified to the game's workspace (`$UB game path <appid>`,
`NOTES.md`): which D3D version it uses, the backend it ran on in CrossOver
and the `BACKEND` line, the build, quirks (launchers, windows, input), and
one line per comparison under "Triage history". What you learned about
CrossOver itself (a bottle, Steam in a bottle, a backend's behaviour) goes
in `$UB notes`, under CrossOver, and in your summary if this skill should
say it.

## Report

The bottle and how the game got there (import or download, build), each run
(backend asked for, the `BACKEND` line, measured result), the reading from
the table, the run directories, and the Mac's free space if you cleaned up.
