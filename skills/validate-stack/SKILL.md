---
name: validate-stack
description: Run the full validation matrix for a change to any layer of the VirtIO GPU stack (guest driver, virglrenderer, d3dmetal-native, DXMT, QEMU, UTM) — Windows and optionally Linux guests, D3DMetal and DXMT backends, Time Spy and Fire Strike single tests — checking both that every cell renders (not black, frames changing) and that scores stay within a per-machine baseline band. Use before declaring a change safe, after updating any layer, or when asked to "run the matrix", "validate everything" or "check for regressions across backends".
argument-hint: "[driver package dir]"
---

# Validate the whole stack

Package: `$ARGUMENTS`

## Before running

1. **Know what is under test.** Run `git status` and `git log -1` in every
   tree that went into this build. Then prove the app contains the change:
   `${CLAUDE_PLUGIN_ROOT}/scripts/host/verify-app.sh --marker <string from your change>`.
   A stale framework or an uncommitted edit in another tree invalidates the
   whole matrix. For a guest driver change, prove the package carries it:
   a string unique to the change (a new message, an error text) in
   `viogpu3d.sys` or the UMD DLLs (`strings -a <pkg>/viogpu3d.sys | grep
   …`), absent from the build before it, and the commits in the package's
   `build-info.json`.
   Baselines record what produced them (VM, app and version, driver, date,
   under `_provenance` in `~/.utmapp-bot/baselines.json`). A band against
   another VM's or app's numbers says little: save a baseline on the same
   VM and app first (`utmbot matrix --windows-vm … --released --save-baseline`).
2. Host changes: rebuild with `/utmapp-bot:build-utm`. Guest driver
   changes: build with `/utmapp-bot:build-viogpu-driver`, and pass the
   package dir.
3. The Windows test VM has 3DMark installed and activated (the `content`
   stage of `/utmapp-bot:provision-windows-test-vm`; see
   `/utmapp-bot:perf-regression`). A Linux cell
   needs a registered Linux VM and a command that runs the workload and
   prints a line `FPS <number>`.
4. Nothing else uses the GPU: no other VM, and no CrossOver game.

## Run

```sh
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/utmbot matrix \
    --windows-vm '<test vm>' --driver ~/.utmapp-bot/drivers/ARM64 \
    [--linux-vm '<linux vm>' --linux-cmd '<workload printing FPS n>'] \
    [--band 0.07] [--save-baseline]
```

`--released` in place of `--driver` runs every cell on the released driver
from UTM's guest tools drive.

Cells:
- `windows/d3dmetal/ts_gt1` (D3D12, so D3DMetal only)
- `windows/d3dmetal/fs_gt1`
- `windows/dxmt/fs_gt1`
- `linux/d3dmetal` and `linux/dxmt`, when given

Each cell:
1. a fresh disposable boot on that backend;
2. driver install and display reset;
3. a discarded warm-up run;
4. a measured run, with a frame sample taken while the workload runs.

It takes about 10–15 minutes per Windows cell. Run it in the background and
report progress per cell.

A cell **passes** when all of these hold:
- 3DMark says VALID;
- the frame sample is not black (<20% black samples) and its frames change;
- fps is no lower than the baseline minus the band.

The first run on a machine has no baseline; `--save-baseline` records it in
`~/.utmapp-bot/baselines.json`. Baselines are per machine, macOS, display
and D3DMetal version: never compare against another machine's numbers.

## Output

The harness prints a `MATRIX` table: PASS/FAIL per cell, fps, the baseline
delta, and notes such as black %, frozen, or the INVALID code. A JSON copy
goes to `~/.utmapp-bot/runs/matrix-*.json`. Report the table, and show the
saved frame PNGs of any failing cell.

## When a cell fails

- **INVALID / NEVER_STARTED / NO_RESULT**: see `/utmapp-bot:perf-regression`
  "When it fails"; rerun that cell alone before concluding.
- **Black or frozen**: `/utmapp-bot:triage-gpu-issue`
  (black-screen-and-pacing.md). Confirm the backend actually used
  (`utmbot backend`): the capture is only meaningful for that backend.
- **Below the band**: rerun the cell. Then do an interleaved A/B against the
  previous build (`utmbot ab`, which counts only VALID runs on a fit host)
  before calling it a regression; the host
  drifts, and a lone low cell is often drift. Dips that the old build
  reproduces in the same session are host state, not the change.
- **Kill-watcher fired**: stop the matrix and follow host-safety.md. Do not
  rerun the fault in a loop.
