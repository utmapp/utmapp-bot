---
name: perf-regression
description: Measure GPU performance in a UTM VM the valid way and decide whether a change made it faster, slower or no different — 3DMark single-test runs with validity checks, interleaved A/B between builds, drivers, backends or UTM apps, frame pacing from the VM window, host GPU/CPU sampling, and profiling pointers. Use when asked to benchmark, "check for a regression", compare fps, explain why something is slow, or investigate stutter or frame-rate decay.
argument-hint: "<what to compare or measure>"
---

# Performance and regressions

Request: `$ARGUMENTS`

`UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"`

## Rules: why most perf numbers in this stack were wrong

1. **Measure before fixing.** Name the metric. Show that it separates good
   from bad, and know its noise (several runs), before changing code. Change
   one thing, then measure again.
2. **Only interleaved, same-session comparisons count.** The host drifts
   within a session: the same build has lost most of its fps over hours,
   while every health check passed. A VM restart only partly recovers it.
   Never compare against a number from another session, another day or a
   note. `$UB ab` runs A B B A … pairs in one session.
3. **Discard the first run** after a boot, a build or a host-library swap.
   That run pays for shader compilation and Rosetta translation of changed
   x86_64 code.
4. **Warm shader cache.** A cold cache makes the first graphics test miss
   3DMark's 60 s start deadline (`NO_RESPONSE`/`PROCESS_EXITED` on GT1 only).
   That looks exactly like a driver hang, with a clean host log.
5. **One benchmark family per boot.** Time Spy first, if both are run. A
   second fullscreen family in the same boot is not comparable.
6. **Host conditions:**
   - UTM in front (App Nap makes vCPUs ~4x slower);
   - display awake and screen unlocked (a locked Mac cannot be un-napped,
     so its numbers are not quotable);
   - no background CPU hogs;
   - same window scale and resolution.

   `bench` checks these and marks a run `NOT QUOTABLE` otherwise.
7. **Validity comes from the benchmark, not the score.** A run that lost
   focus or fell back to windowed mode still prints a number. `bench` reads
   3DMark's status codes:
   - `1000`: windowed/lost exclusive mode, often foreground-lock poisoning;
   - `10000`: focus or resolution flake.
8. **Same configuration as the claim.** Use the same backend (prove it with
   `$UB backend`), the same D3DMetal version, Debug Log off unless that is
   what is measured, and GraphicsValidation off (it costs ~20%).
9. **A decay over time, or a run far slower than its siblings, is a hang in
   slow motion.** Treat it as a bug, not noise (`presentloop` measures
   decay).
10. **Every claimed improvement is a measured one.** If a change does
    nothing measurable, drop it. Do not tune for one title: the fix has to
    hold for every legal workload.

## Run

**3DMark** must be installed and activated in the test VM's base image: the
`content` stage of `/utmapp-bot:provision-windows-test-vm` does it
(`--only content --3dmark <dir>` on an existing VM; the Linux VM's `prefix`
stage likewise). `3DMarkCmd` needs a **Professional** key, from
`$UTMBOT_3DMARK_KEY` or `~/.utmapp-bot/3dmark.key` (mode 600); never put it
in a log, a host command line or the repository. Without one `bench`
reports `NOT_ACTIVATED`. Single-test definitions are in
`${CLAUDE_PLUGIN_ROOT}/bench/defs/`.
`fs_gt1`/`ts_gt1` run only Fire Strike or Time Spy graphics test 1, the
fastest repro loop.

```sh
$UB vm up --backend d3dmetal --debug-log off && $UB install <pkg> && $UB display-fix
$UB bench fs_gt1 -n 3 --warmup [--frames-at 12]   # VALID/INVALID, fps, run dirs
```

**A/B** (each run is a fresh disposable boot, with an install if the arm
names a driver, a discarded warm-up, and a measured run):

```sh
$UB ab fs_gt1 --pairs 3 \
    --a backend=d3dmetal,driver=$HOME/.utmapp-bot/drivers-old/ARM64 \
    --b backend=d3dmetal,driver=$HOME/.utmapp-bot/drivers/ARM64
# arms: driver=DIR (required), backend=dxmt|d3dmetal (else UTM's current default),
#       app=/path/UTM.app, env=K=V;K=V, genv=K=V, debuglog=on
```

It counts only runs that are VALID and were taken on a fit host (not `NOT
QUOTABLE`), and says how many it dropped. It prints each arm's mean and
stdev, and `B-A` against a noise band of 2× the larger stdev. `WITHIN NOISE`
means no measurable difference. Debug Log is off unless an arm says
`debuglog=on`.

**Frame pacing** (stutter, held frames, fps the user sees):
`$UB frames 20` counts pixel-distinct frames in the VM window. The app's
present count overstates it whenever frames are dropped or repeated on the
way to the window. `presentloop` (from `/utmapp-bot:write-gpu-probe`)
reports per-window fps and the worst frame time. `--rtv-per-frame`
reproduces the "creates a view every frame" pattern, and `DECAY` means
released objects are not being freed.

**Where the time goes** (full stack, one in-game window per pass; profiling
boots are never measurement boots):

1. **Bound first.** `$UB perfcap 20` during the test: host GPU `dev`/`ren`
   near 100% means GPU-bound, and no CPU-side change will raise the frame
   rate. Then find what fills the GPU (step 4), not what the CPU does.
2. **Window.** Start the workload in the background, then `$UB ingame`. It
   returns once the guest display has been animating for 6 samples in a
   row. Loading screens differ by ~0.1 between grabs, a game by 10–100, and a
   fade gives one or two large values. A fixed delay misses: Fire Strike
   loads for 20–30 s, and the time varies from run to run. Prove the window:
   `$UB shot` at start and end (3DMark's overlay shows the test time).
3. **Guest (ETW).** `$UB etw start`, wait 10 s, `$UB etw stop NAME`, then
   `$UB etw analyze <etl> <process> --dev '<dev vm>'`. It records context
   switches and ready-thread events with stacks, plus DxgKrnl/DXGI. The
   analysis gives:
   - per-thread on-CPU, ready (no vCPU) and blocked time;
   - off-CPU time grouped by wait stack, overall and per busiest thread;
   - who woke whom;
   - the GPU queue: DMA busy, packet latency, presents/s and the interval
     distribution.

   The dump runs on the dev VM, which has WPT and the symbol cache (the
   first run downloads ~2 GB of Microsoft symbols). UMD frames are bare
   offsets because the package is built `release`. Name them from the
   source: the present path waits in `tritonPresentFlushAndGate`.
   **Sampled CPU profiling does not work in the guest:** WPR's CPU and GPU
   profiles and xperf `PROFILE` fail with "The request is not supported"
   (0x80070032). On this host Hypervisor.framework emulates the PMU
   registers itself (`Arm::Emulator::read_pmccntr_el0` in QEMU's host
   profile), so UTM's QEMU PMU emulation never sees them, and Windows lists
   only the `Timer` source (`xperf -pmcsources`). Wait analysis replaces
   on-CPU hot spots. Say so in a report.
   For an x64 app (emulated in the guest), the stack walk often stops
   before the app's frames, so a large share of wait time is reported as
   `(no caller stack …)`. The per-thread on-CPU, ready and blocked times
   still hold. One app thread near 100% on-CPU means the guest CPU sets the
   frame rate.
4. **Host.** `$UB hostprof 10 --ctx <guest process>` is a system-wide Time
   Profiler for QEMU and that guest process's render worker, with x86_64
   (Rosetta) frames named. `$UB hostprof 5 --metal` is a Metal System Trace
   that gives GPU time per process and channel (the game vs. UTM's display
   path). Use one xctrace session at a time: kperf allows a single sampling
   session, and two attached Time Profilers fail with "could not lock
   kperf". Processing a loaded system's trace takes minutes after the
   recording ends. Frames shown as `?!0x7ff8…` in a render worker are not in
   the x86_64 shared cache (`/System/Volumes/Preboot/Cryptexes/Rosetta/System/Library/dyld/*.map`):
   they belong to Rosetta's runtime, and no symbols exist for them. Attribute
   them by their named caller and callee (for example the frame between
   `IOGPUCommandQueueSubmitCommandBuffers` and `IOConnectCallMethod` is
   command-buffer submission).
5. **Timing logs.** Guest UMD: run the workload with `--genv
   NPT_DEBUG=present_timing` (and `--genv NPT_PRESENT_GATE_TRACE=<ms>` for
   slow presents) plus `$UB ods 300`, then `$UB ods get`. Every 120 presents
   it writes `PT n=… avg_us sig= arm= wait= cb= total= wait_max=`. `wait` is
   the pacing wait for frame N−depth; the log names the depth. Host render
   server: `vm up --env NPT_DEBUG=profile --env NPT_PROFILE_PERIOD_MS=2000`,
   then `$UB hostlog --grep NPT-PROF` (ring dispatch, relax and idle time,
   per method). **The host profiler is not free:** under Rosetta its
   per-command `clock_gettime(CLOCK_MONOTONIC)` was 21% of the ring thread's
   samples. Never take a Time Profiler capture with it on.

What profiles of this stack have shown, as a guide to reading yours:

- Fire Strike-class D3D11 workloads on D3DMetal were GPU-bound: the host GPU
  near 100%, almost all of it the game's translated work, UTM's display
  path a few percent. Guest and host CPU costs that looked large (pacing
  waits, contended locks in Hypervisor.framework) did not set the frame
  rate: halving the vCPUs in an interleaved A/B left fps unchanged. Check
  for such an ablation before chasing a CPU-side cost.
- A workload can be bound on both sides at once (one guest thread near a
  full core and the host GPU near full). Then speeding up either side alone
  gains little.
- An ablation that removes a bound measures the bound, not the mechanism.
  Do not batch or reorder asynchronous commands to "fix" throughput. Find
  the serializing wait first.

## Output

For every comparison report:
- arm definitions;
- valid run counts;
- per-run fps;
- mean ± stdev;
- delta and noise band;
- host warnings;
- backend + D3DMetal version;
- run directories.

Call a result a regression only outside the noise band, and only from
interleaved runs.

## When it fails

- **`NEVER_STARTED`**: the console task did not start 3DMark. Usually the
  session is wedged after a fullscreen run; start a fresh boot.
- **`INVALID` 1000 on every run**: fullscreen is being denied. Check that
  `install` ran `fglock` and that `fgforce` was pushed. A console window
  stealing focus causes it too.
- **`NO_RESULT`**: the export was caught mid-write, or the run was
  cancelled. `bench` already falls back to the newest result in Documents.
- **Big variance**: check `check-host` during the run, Spotlight indexing,
  the other VMs, and thermal state. Add pairs rather than trusting a single
  outlier.
