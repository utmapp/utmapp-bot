# Hangs and freezes

## First: what exactly stopped?

| Observation | Meaning |
|---|---|
| `kd.log` ends at a `kd>` prompt; the window and debug.log stopped at the same second | The guest is **halted by the kernel debugger**, not hung. Reasons: a breakpoint, an assertion (`code c0000420`), an int3 (`80000003`), a Code Integrity "not signed correctly" break, or a bugcheck. `$UB kd crash`, then `kd go` or analyze. |
| SSH works, the app is unresponsive, `frames` shows 0 distinct fps in the app window | App-level hang: a GPU wait that never completes, a deadlock, or a lost wakeup. |
| SSH dead, VM window frozen, QEMU CPU high | Guest kernel hang, or an interrupt storm/busy loop. kd break-in, then stacks. |
| SSH dead, QEMU CPU ~0, vCPUs idle | Guest waiting on a completion that never came. A virtio queue event was lost, or the host never signalled. |
| Window stopped but the guest is fine over SSH and the taskbar clock moves after an input | **Display/scanout path**, not a hang: see black-screen-and-pacing.md. |
| `frames` shows no change but the clock moves | Static content. Not a hang. |
| Render worker `DIED on signal 6/11` (+ `.ips`) and the guest waits forever | The host side crashed. Fix the crash; the guest's wait is a victim. |
| `retiring as device-lost` in the render-server log, or a `gpuEvent` with `firmware-detected lockup` | **The GPU itself hung** on a submitted workload. Treat it as a GPU fault (device-removed-and-faults.md), and see host-safety.md. |
| Throughput falls over minutes, or one run takes far longer than the rest | A hang in slow motion: the same class as a hang. Do not dismiss it as noise. |

## Decision tree

1. `$UB snapshot`, then `$UB kd status` / `kd tail`. If halted, it is a kd
   stop: read why (`kd crash`) before anything else.
2. **Host side alive?** Check `$UB vm status`: worker CPU and state (`U` =
   stuck in the kernel; a GPU-hang risk). Is there a render-server log at all
   (`$UB hostlog`)? If a worker is busy at 100% for the whole hang, it is a host
   backlog or a long operation, not a lost signal.
   - With `NPT_DEBUG=profile` (host, `--env`), the per-method dispatch time
     shows *which* operation the host is stuck in or backlogged on.
     *Example: thousands of pipeline compiles running serially on one ring
     thread with a cold shader cache looked like a hang at a splash screen;
     the profile showed the compile backlog.*
3. **Which side is waiting?** Take guest stacks:
   - `$UB cdb <image>` for the user-mode app: all threads.
   - `$UB dump <image> --full` for a dump to analyze offline.
   - kd `!process 0 7 <image>` for the kernel view.

   A thread in the UMD waiting on a fence or ring sequence number points at
   the host: find what it waits for. A thread in the runtime waiting on a
   present points at the present/display path.
4. **GPU or CPU?** If the host log shows the GPU never completed a submitted
   command buffer, it is a GPU hang. Bisect *which* submission. Synchronize
   after every call in a debug build: the last call logged without syncing
   is only where the CPU was. Then narrow down to the shader or resource
   (skip/replace by hash). Only then reason about the cause.
5. **Lost wakeup?** If the GPU finished but the guest never noticed, compare
   when the host signalled with when the guest woke. The guest present-gate
   and event-waiter trace knobs exist for this (see knobs.md). The questions
   they answer are "did the event never come?" and "did it come, but the
   value read stale?"
6. **QEMU/HVF** (QEMU process aborts, `.ips` with `hvf_vcpu_exec`/assert):
   the guest did something HVF cannot emulate, often a device command
   completed or dropped in the wrong state. See crashes.md.

## Dead ends, do not redo

- **"The last logged call hung the GPU."** Without per-call sync the log
  position is just where the CPU was. Readbacks after a GPU hang are
  garbage, and the GPU's "guilty data master" field has pointed at compute
  when a vertex shader was the culprit.
- **Shortening a wait, or retrying, until the hang goes away.** That hides
  the ordering bug, and it corrupts the measurement: a short poll makes
  "event never came" indistinguishable from "came late".
- **Calling a kd halt a hang**, or worse, rebooting. Over kdnet a halted
  target cannot be resumed by a reboot of kd; resume with `kd go`.
- **Fixing the guest wait when the host worker crashed.** Look for the
  `.ips` first.
- **Stubbing a pixel shader to `ret` to bisect.** It can crash the shader
  converter itself. Stub the reads or break the loops instead.
- **Concluding "self-wait" or "cross-queue hole" from a single trace.** Both
  were confidently claimed and later retracted. Confirm a deadlock graph
  with stacks from both sides before writing it down.
