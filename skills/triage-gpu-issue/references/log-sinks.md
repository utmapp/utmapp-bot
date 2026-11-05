# Where host output goes under UTM

Measured on current UTM and on an older release (a 5.0.8 build from before
2026-11, which reporters may still run) with Windows Neptune guests; check
again after UTM changes its logging.

| Source | Sink | Notes |
|---|---|---|
| QEMU stdout/stderr, `-trace` events, SPICE server debug | `<VM>.utm/Data/debug.log` (Debug Log on) | Starts with `Launching: qemu-system-… <args>` and an `Environment variables:` block. `$UB hostlog`/`killwatch` read only from the last `Launching:` line on. The harness copies it into the run directory at `vm down`. |
| Render server and workers (`npt:`, `proxy:`, `vkr:`, virglrenderer, d3dmetal-native, DXMT, Metal errors they print) | `debug.log` on current UTM; nowhere readable on older releases | They inherit QEMU's stderr. On current UTM with Debug Log on, their lines are in debug.log. On the older release none of them appeared there. Why is not isolated (Debug vs Release configuration, the newer graphics dependencies, or UTM's plumbing): look at debug.log of the app you test before relying on it. `VIRGL_LOG_FILE` moves them to a file. The sandboxed render server can write only in UTM's group container (`~/Library/Group Containers/<team>.com.utmapp.UTM`), and macOS does not let the terminal list or read that folder unless the terminal has Full Disk Access. So `vm up` does not set it. |
| GPU faults and GPU restarts | `/Library/Logs/DiagnosticReports/gpuEvent-<process>-<date>.ips` | Written by macOS for every GPU fault/restart, whatever the logs say. Moves to `Retired/` within minutes. The process name is cut to 16 characters (`QEMURenderServer`). The body has `restart_reason_desc`, `bif0_fault` (address, read/write) and `guilty_dm`. `$UB crashes` summarizes them. |
| Process crashes | `~/Library/Logs/DiagnosticReports/{QEMULauncher,QEMURenderServer,UTM,QEMUHelper}-*.ips` (+ `Retired/`) | A JSON header line, then a JSON body: `exception`, `termination`, `faultingThread`, `threads[].frames`, `usedImages`. QEMU itself runs as **QEMULauncher**. |
| Resource reports | `*.diag` (e.g. "disk writes") | Noise for GPU triage. |
| UTM app, QEMUHelper XPC | unified log: `log show --predicate 'process IN {"UTM","QEMUHelper","QEMULauncher"}' --last 10m` | The app's own messages (scripting errors, launch failures). |
| Guest kernel (Windows) | kd over kdnet (`$UB kd …`) → `kd.log`; `C:\Windows\Minidump`, `C:\Windows\LiveKernelReports` | KMD `DbgPrint` streams to kd.log live. |
| Guest UMD (Windows) | the app's stderr (`$UB test` captures it); `OutputDebugString` (`$UB ods`) | Run the app through `test`/`launch`, not by double-click, to keep its output. |
| Guest (Linux) | the program's output file in `~/utmbot/`; `journalctl -k` for virtio_gpu/drm | Never pipe through `tee`: it buffers and loses output on kill. |

## What the kill-watcher can actually see

`killwatch` follows debug.log and new `gpuEvent` reports.

- **When render-server lines are not in debug.log** (older releases, or Debug
  Log off) it cannot see render-server errors at all. Its only real trigger is a
  `gpuEvent` report, which arrives *after* the GPU has already restarted.
- **When they are** (current builds, Debug Log on) it also sees them. Two of its patterns
  are confirmed present in current sources: `retiring as device-lost` (a GPU
  wait that never completed, from virglrenderer) and worker deaths. The
  Metal command-buffer error strings in its pattern are Metal's own error
  text. d3dmetal-native currently has no code that logs command-buffer
  errors, so whether those lines appear has not been verified. Watch for
  them, but do not count on them.

So on any UTM: keep runs short and attended, and look at `crashes` after
every run.
