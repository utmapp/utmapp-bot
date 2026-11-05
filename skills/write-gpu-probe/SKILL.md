---
name: write-gpu-probe
description: Write or extend a GPU probe: a small deterministic C test program for the VirtIO GPU stack (D3D11/D3D12 on Windows guests or under Wine, Vulkan/Venus on Linux guests). A probe renders offscreen, reads back into a sentinel-filled buffer, and checks one access path (clear, upload, copy, VB/CB draw, dynamic map, UAV, sampling or present) against exact expected values. It ends with a single PASS/FAIL line. Use this skill to turn a GPU bug into a minimal repro, to get fail-before/pass-after proof for a fix, or to add a new check or access path to an existing probe (readback11.c, readback12.c, vkreadback.c, presentloop.c, adaptercheck.c). Typical requests: "write a test program", "make a repro", "add a check to the probe", "build the probes". Do not use it only to run existing probes or benchmarks, to build drivers, or to write unit tests inside the Mesa tree.
argument-hint: "<symptom to reproduce>"
---

# Write a GPU probe

Symptom: `$ARGUMENTS`

A 30-second deterministic probe beats any amount of reasoning about a game
that fails after 20 minutes. It also proves a fix: the probe fails before
the change and passes after it.

## Start from the templates

`${CLAUDE_PLUGIN_ROOT}/probes/`:

| Probe | What it shows |
|---|---|
| `adaptercheck.c` | Lists DXGI adapters; creates D3D11 and D3D12 on the VirtIO adapter at each feature level. `ADAPTERCHECK PASS` = D3D reaches our driver. |
| `readback11.c` | D3D11, one check per access path: clear, CPU upload, draw (VB + CB + PS), dynamic `Map(WRITE_DISCARD)` renaming, compute UAV write. |
| `readback12.c` | D3D12: clear, upload-heap copy; with barriers and a fence. |
| `presentloop.c` | Windowed or fullscreen present loop: fps per 5 s window, worst frame gap, `--rtv-per-frame`; verdict `STEADY` / `DECAY` / `HITCH`. |
| `vkreadback.c` | Vulkan (Linux, Venus): clear, buffer-to-image copy. |
| `fglock.c`, `fgforce.c` | Foreground helpers the harness uses for fullscreen workloads. |

Copy the closest one, keep its skeleton, and replace one path's body with
the smallest sequence that shows the bug.

## Principles

- **Pick the VirtIO adapter explicitly** (`probe_find_adapter` in
  `common.h`: vendor `0x1af4`, `D3D_DRIVER_TYPE_UNKNOWN`). A default
  hardware device can silently be WARP.
- **Read back, do not look.** Render into a texture and copy it into a
  CPU-readable resource that you **filled with a sentinel first**. Compare
  every texel. The sentinel separates "the GPU wrote the wrong value" from
  "the write or copy never happened". One bug was mis-attributed to compute
  for hours because the readback copy, not the dispatch, was dropped.
- **One access path per check.** Clear, upload, copy, vertex/constant
  buffer, dynamic map, UAV, sampling and present map to different host
  mechanisms and fail independently. Find the first one that fails.
- **Exact expected values.** Pick colors that are exact in the format, e.g.
  0.6 or 0.2 for UNORM8, not 0.5, which rounds either way. Print expected
  and actual in hex.
- **Small, fixed sizes, no timing dependence,** unless timing is the bug
  (`presentloop`).
- **Report a single verdict line** (`<NAME> PASS|FAIL`), exit 0 on pass,
  and keep the details above it.
- **Check device removal at the end** (`GetDeviceRemovedReason`). A probe
  that passes on a removed device proves nothing.
- **Native vs emulated.** Build ARM64 for the driver's native path, and x64
  for what x64 apps see (they run emulated and use the driver's ARM64EC
  half). An x64 probe cannot load a native arm64 DLL directly; that error is
  the caller's architecture, not the driver.
- **Do not commit diagnostics into the driver to make the probe work.**
  The probe stands alone and goes into the regression set.

## Build and run

`UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"` (see `/utmapp-bot:run-gpu-test-vm`).

**Windows, MSVC on the dev VM** (both architectures, into
`~/.utmapp-bot/probes/{ARM64,x64}/`, where the harness finds them):

```sh
bash ${CLAUDE_PLUGIN_ROOT}/scripts/build-probes.sh '<dev VM name>'   # or [user@]host[:port]
```

It builds every `.c` in `probes/`. To add your probe, put it there (or in a
copy of the directory) and rerun.

**Without a dev VM**, mingw-w64 builds the x64 versions on the Mac
(`brew install mingw-w64`):

```sh
x86_64-w64-mingw32-gcc -std=c11 -O1 -I${CLAUDE_PLUGIN_ROOT}/probes -o readback11.exe readback11.c -ld3d11 -ldxgi -ldxguid -luser32
```

**Run** in the console session, never over SSH:

```sh
$UB test ~/.utmapp-bot/probes/ARM64/readback11.exe
```

On a Linux test VM (`/utmapp-bot:provision-linux-test-vm`), the D3D probes
run as x86_64 Windows programs under Wine. Build them in the VM with its
mingw cross compiler, and run them through `npt-wine`, which uses Neptune by
default (`--backend dxvk|wine` for the others):

```sh
$UB put ${CLAUDE_PLUGIN_ROOT}/probes/readback11.c ~/probes/   # and common.h
$UB guest 'cd ~/probes && x86_64-w64-mingw32-gcc -std=c11 -O1 -I. -o readback11.exe readback11.c -ld3d11 -ldxgi -ldxguid -luser32'
$UB test 'npt-wine ~/probes/readback11.exe'
```

Under Wine there is no KMD, and Neptune's DXGI reports one adapter named
after the host GPU, with vendor 0. `probe_find_adapter` then takes the first
hardware adapter that is not Microsoft's, and says so in its output.

**Linux guest:**

```sh
$UB put ${CLAUDE_PLUGIN_ROOT}/probes/vkreadback.c '~/utmbot/'
$UB test 'cc -O2 -o ~/utmbot/vkreadback ~/utmbot/vkreadback.c -lvulkan && ~/utmbot/vkreadback'
```

For a Windows exe under Wine/FEX in the Linux guest, build it as x86_64.

## Use it as proof

Run the probe on the build before the fix (FAIL) and after it (PASS), in the
same session, and put both verdicts in the commit message or PR. Keep it
next to the regression tests of the layer that was fixed.
