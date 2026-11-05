# Host safety: GPU wedges and kernel panics

A guest workload can take the whole Mac down. A GPU that faults repeatedly
or hangs on a command buffer can stall the kernel's GPU work queue. The
panic text reads like `Excessive time stalled (>60s) in
IOGPUWorkQueue::addCommandToTail_slow`; a WindowServer userspace watchdog
timeout is another form. Once it starts, nothing in user space stops it:
killing the process cannot reap a thread stuck in the kernel. Prevention is
the only defence.

## Rules

1. **The kill-watcher is armed for every run** (`vm up` does it). Check it
   with `$UB killwatch status`. It can die under memory pressure; `status`
   says `DEAD`, and then you re-arm it. When it fires it SIGKILLs QEMU, the
   render server and the workers, writes `killwatch.fired`, and every wait
   returns `VM-DEAD`.
2. **Know what it can see** (log-sinks.md). Without the start-environment
   UTM, it sees no render-server lines; its trigger is the `gpuEvent`
   report, after the GPU already restarted. Keep runs short and attended.
3. **No fault-reproduction sweeps.** Never loop a faulting workload "to get
   statistics". Run a fault workload only with a candidate fix aboard, once
   per question, and tear down at the verdict.
4. **No long unattended runs.** Every script needs a hard deadline. Report
   progress while it runs.
5. **After a fire:** look at `$UB crashes` and `$UB hostlog`. Check for
   processes in state `U` (`$UB procs`). Do not start another VM until every
   QEMU and render-server process is gone. If they will not die, the GPU is
   still wedged: tell the user, and expect that only a reboot of the Mac
   clears it.
6. **Logs that survive a panic.** The run directory under
   `~/.utmapp-bot/runs/` survives a host panic, and the harness keeps it out
   of Spotlight. macOS moves the panic report and `gpuEvent` reports to
   `DiagnosticReports/Retired/` within minutes; `$UB crashes` reads both.

## Capture and inspection hazards

- **Metal GPU capture in many processes at once** can panic the host. Scope
  capture to one worker.
- **`vmmap` on a render server** stalls GPU submission long enough for the
  guest to reset the GPU (a TDR you caused).
- **`sample`** stops the target for about a second; the guest sees a hitch.
  **LLDB** stops it for as long as it is attached. Do not leave a debugger
  stopped on a worker while the guest has GPU work outstanding.
- **`MTL_SHADER_VALIDATION`** has wedged the GPU under D3DMetal.

## Host wedges that are not panics

- **Every new VM fails to create GPU objects or hangs at start; a host
  reboot "fixes" it.** Metal's shared-event pool or GPU memory is exhausted
  by a leftover process: a stuck render server, a test program, a game under
  CrossOver. Look for and kill leftovers before blaming the driver.
- **The guest "becomes unreachable" mid-run.** Check host disk space. Crash
  cores (`/cores`), logs and swap can fill the disk.
- **A shell's background-nice setting.** It applies to processes started
  with `&` from zsh and is inherited by everything they start. Start UTM
  with `open`, never from a backgrounded shell command.
- **Throughput sinks over a session while every check passes.** The host
  degrades within a session; a VM restart only partly recovers it. Compare
  only interleaved runs (`/utmapp-bot:perf-regression`).
