---
name: triage-gpu-issue
description: Triage a graphics problem in a UTM VM using the VirtIO GPU stack (Neptune D3D or Venus Vulkan) — hangs and freezes, black or flickering or stuttering output, corrupted rendering, device removed / GPU faults / TDRs, crashes of the guest app, the render server or QEMU, driver install or load failures, and host GPU wedges. Starts with an evidence snapshot, routes to a symptom playbook with fingerprints, a decision tree, the real debug knobs and the known dead ends. Use when anything in the guest renders wrong, hangs, crashes or fails to start, or when asked to "investigate", "debug", "root-cause" or "triage" a GPU issue.
argument-hint: "<symptom, app, or issue>"
---

# Triage a GPU issue

Symptom: `$ARGUMENTS`

`UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"` — see
`/utmapp-bot:run-gpu-test-vm` for booting, running and capturing.

## Rules that decide whether triage converges

1. **Evidence first, theory second.** Do not attempt a fix without concrete
   evidence of the cause. Name a metric, show that it tells good from bad,
   know its noise, change one thing, measure again. Every claim in a report
   needs a log line, capture or number behind it.
2. **Look at every layer before theorising about one.** The most expensive
   misdiagnoses in this stack were caused somewhere the investigator was not
   looking:
   - an uncommitted edit in another tree;
   - a stale framework embedded in the app;
   - a different D3DMetal than the "good" run used;
   - a halted kernel debugger;
   - a napping or swapping host.

   `snapshot` puts all of that on one page.
3. **Reproduce small and deterministic.** A reliable 30 s repro is worth more
   than any amount of reasoning about a 20-minute one. Turn the symptom into
   a probe early (`/utmapp-bot:write-gpu-probe`).
4. **Never mask.** Do not shorten a timeout, add a retry, or force a refresh
   so the symptom goes away. If you cannot say what blocked or what
   corrupted, you have not fixed it. A fix must hold for every legal use of
   the API, not one title.
5. **A wedged VM is a bug to debug, not to clean up.** Collect `snapshot`,
   `crashes`, kd state and a frame sample before `vm down`.
6. **Host safety beats progress.** Never run fault-reproduction sweeps.
   Arm the kill-watcher, run one run per question, and tear down at the
   verdict (references/host-safety.md).

## First five minutes

```sh
$UB snapshot          # host state, processes + worker arch, app + embedded libs,
                      # backend, source trees, host log errors, crash reports,
                      # kd state, guest driver/event log/dumps -> snapshot-*.md
```

Then answer each of these before forming a theory:

- **Are the logs live?** They must come from *this* VM lifetime (the
  harness ignores a debug.log older than the current `vm up`). The render
  server's lines (`npt:`, `proxy:`) are in debug.log on current UTM with
  Debug Log on, and missing on older releases; see references/log-sinks.md for
  what reaches which file under UTM.
- **Is the guest halted by kd** rather than hung? A frozen VM with kd
  attached is usually a break (`$UB kd status`, `/utmapp-bot:kernel-debug`).
- **Real crash or routine teardown?** A worker that `DIED on signal 9` was
  killed on purpose. Signal 6/11 with an `.ips` is real. `gpuEvent-*.ips` is
  a GPU fault or restart.
- **Which backend and which D3DMetal** actually ran (`$UB backend`)? Record
  both next to every result.
- **Are all trees and binaries what you think?** Check `snapshot`'s source
  trees and embedded-library build times. Use
  `${CLAUDE_PLUGIN_ROOT}/scripts/host/verify-app.sh --marker <string>` to
  prove a change is in the app you ran.
- **Is the host healthy?** Check `$UB check-host`: App Nap, locked screen,
  sleeping display, swap, disk, Spotlight.
- **Is it ours?** If the bug could belong to D3DMetal, DXMT, Wine or the UTM
  display path, run `/utmapp-bot:attribute-to-layer` before going deep.

## Pick the playbook

| Symptom | Read |
|---|---|
| App, guest or whole VM stops making progress; "hang", "freeze", "stuck loading" | `references/hang.md` |
| Black screen/window, black flashes, flicker, held/repeated frames, stutter, frames out of order | `references/black-screen-and-pacing.md` |
| Wrong colors, missing/garbage geometry, missing effects/shadows, texture corruption | `references/corruption.md` |
| `DXGI_ERROR_DEVICE_REMOVED`/`RESET`, GPU page faults, TDR, bugcheck 0x116/0x117 | `references/device-removed-and-faults.md` |
| Guest app crash, render-server/QEMU crash, kernel bugcheck | `references/crashes.md` |
| Driver will not install/load, D3D create fails, app lands on WARP, version skew | `references/install-and-load.md` |
| Host panicked, GPU wedged, Mac unresponsive, kill-watcher fired | `references/host-safety.md` |
| Slower than before / slower than expected / fps decays | `/utmapp-bot:perf-regression` |

Debug knobs and how to check that they still exist: `references/knobs.md`.

## Close out

- Write the finding with its evidence: log lines, capture paths and numbers.
  State what was **not** verified.
- If the work spans sessions, write a handoff
  (`/utmapp-bot:investigation-handoff`).
- Land fixes with `/utmapp-bot:land-stack-change`, whose review step
  (MSDN or Vulkan spec, related paths, `/code-review high`, validate again)
  comes before anything is pushed: each commit carries the
  probe or measurement that shows its effect.
