# Device removed, GPU faults, TDR

## Codes

| Code | Meaning |
|---|---|
| `0x887a0005` DXGI_ERROR_DEVICE_REMOVED | The runtime or driver gave up on the device. `GetDeviceRemovedReason` says which. |
| `0x887a0007` DEVICE_RESET | A GPU reset (TDR) took the device. |
| `0x887a0006` DEVICE_HUNG | The device was judged hung. |
| `0x887a0001` INVALID_CALL | Often a latched runtime error from an earlier call, not the call that returned it. |
| `0x887a0020` DRIVER_INTERNAL_ERROR | The runtime rejected the driver, e.g. inconsistent format caps at device creation. Not `_UNSUPPORTED` (`0x887a0004`). |
| Bugcheck `0x116` VIDEO_TDR_FAILURE | The GPU did not recover from a reset. With kd attached, dxgkrnl bugchecks instead of recovering. |
| LiveKernelEvent `141`/`117` | A TDR that recovered. Reports are in `C:\Windows\LiveKernelReports`. |
| Bugcheck `0x119` (arg1=1) | The driver reported a fence value going backwards. |
| `gpuEvent-*.ips`, `restart_reason_desc: BIF0 page fault` | The host GPU faulted on an address. `bif0_fault.address` and read/write are recorded. |
| `restart_reason_desc: firmware-detected lockup`, a `CDM Kill timeout` | The host GPU hung on a workload. |
| Host lines `PageFault`, `SubmissionsIgnored`, `InvalidResource`, `OutOfMemory` (Metal command-buffer errors) | The Metal-level classes. After a page fault Metal may ignore all further submissions from that process: a black screen with no further errors. |

## Decision tree

1. **Host evidence:** `$UB crashes` (gpuEvent reports, worker deaths) and
   `$UB hostlog`.
   - A render-server `.ips` with `abort()` in Metal validation means a
     descriptor Metal refused (e.g. pixel format 0). Fix the translation.
   - A `gpuEvent` page fault means a GPU access to freed, non-resident or
     out-of-range memory: go to 3.
2. **No host error at all?** The guest side removed the device. Check:
   - **The UMD's return codes.** `Map` may only fail with
     `WASSTILLDRAWING` (for DO_NOT_WAIT) or `DEVICE_REMOVED`; any other error
     makes the runtime remove the device on purpose.
   - **Latched runtime errors.** A runtime-side validation failure surfaces
     later and elsewhere: an earlier call returned an error the app ignored.
   - **A TDR.** Check the System event log and LiveKernelReports. Something
     host-side stalled GPU submission past the guest's timeout. Your own
     tooling counts: `vmmap` on a render server, or LLDB stopped on a worker.
3. **Census before experiments.** Collect every fault from many runs:
   `gpuEvent` addresses and read/write, which queue, and how long after
   launch. Group them into families before changing anything; one symptom is
   often several bugs, and an A/B on a mixed population proves nothing. Then
   suspect, in order:
   - **resource lifetime**: freed while a command buffer still uses it;
     command buffers not retaining their resources;
   - **residency**: a resource not made resident before use. Heaps and
     aliased or "impostor" resources are easy to miss;
   - **page size**: the x86_64 render server under Rosetta and the GPU
     disagree on page granularity for host-visible memory;
   - **queue ordering**: work on two queues without the fence the app
     relied on.
4. **One run per question, kill-watcher armed.** A page-faulting workload
   repeated many times has panicked the host (host-safety.md). Run a fault
   workload only with a candidate fix aboard, and stop at the verdict.

## Dead ends, do not redo

- **Refusing a create to avoid a crash.** Failing a shader or resource
  creation on the host killed the worker or removed the device in the guest.
  Create something valid and degrade instead.
- **A/B on unclassified faults.** The "fix" moved one family and hid behind
  another.
- **Watchdog thresholds counting idle time.** A GPU "brake" that counted
  idle time fired falsely. Measure busy time.
- **"Lost sync reply returns zero"** and **"cross-queue hole"**: both
  eliminated with evidence. Do not re-open them without new evidence.
- **Single-queue serialization as the fix for an ordering fault.** It did
  not fix the fault, and it costs throughput for every app. Fix the missing
  ordering edge.
