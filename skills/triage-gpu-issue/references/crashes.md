# Crashes

## Host

`$UB crashes` lists `.ips` reports of QEMULauncher (QEMU itself),
QEMURenderServer (render server and workers), UTM and QEMUHelper since this
VM started, plus worker deaths from the render-server log.

| Fingerprint | Meaning |
|---|---|
| `worker N DIED on signal 9` | Routine: workers are killed when their guest context goes away. Not a crash. |
| `DIED on signal 6/11/4/10` + a QEMURenderServer `.ips` | A real crash. Read the faulting thread's frames. `libmetalirconverter`/`libdxilconv` frames: the shader converter choked on one shader (find which by hash). `abort` from Metal validation: a descriptor Metal refused. |
| QEMULauncher `.ips` with `hvf_vcpu_exec` / `__assert_rtn` | QEMU's HVF accelerator hit something it cannot emulate. Often a guest device access in a state the device model does not expect (e.g. a command completed or dropped when the queue was full). Correlate with what the guest driver did just before. |
| `.ips` with `SIGKILL` and `dyld` "path missing"/code-signature reasons; `proxy: … Broken pipe` | A binary was overwritten in place while running (`cp` over a live Mach-O). Install by writing a new file and renaming it over the old one. |
| Worker in state `U` at 0% CPU for minutes after a host binary swap | Rosetta re-translating a changed x86_64 binary. The first launch after a swap is slow; do one throwaway boot. |
| x86_64 helper that was re-signed hangs in dyld when loaded from a temp/scratch directory | Stage re-signed x86_64 binaries under the home directory, not `/tmp` or `/private/tmp`. |
| A crash right at device creation after a host-library update | Check for ABI skew between the guest driver and the host (`wire format mismatch` in the guest log), and between virglrenderer and the backend libraries (each must export the expected `*_native_abi_version`; see `verify-app.sh`). |

## Guest user mode (Windows)

- Get the exception first. Use the Application event log (`snapshot` shows
  ID 1000/1002), or run under a debugger: `$UB cdb <image>` for a running
  process, `$UB dump <image> --full` for an offline dump.
- `0xc0000005` in the UMD after a device is destroyed or the DLL unloaded: a
  driver thread or reaper outlived the DLL. Check every detached thread and
  every callback for teardown ordering.
- `Unhandled illegal instruction` (Linux guest, FEX) in a guest driver DLL:
  an atomic read-modify-write (or a `seq_cst` store, which compiles to
  `xchg`) on write-combining shared memory. Only plain loads and stores are
  safe there. Symbolize with `x86_64-w64-mingw32-addr2line -e <dll>
  <ImageBase+RVA>`.
- A Wine "timeout" is often a crash: `abort()` waits for `winedbg --auto`.
  Set `SEM_NOGPFAULTERRORBOX` or disable the crash dialog so crashes are
  reported as crashes.

## Guest kernel (Windows)

With kd attached a bugcheck **halts** in kd instead of rebooting:
`$UB kd crash` runs `.bugcheck`, `!analyze -v` and `kv`. Without kd, read
`C:\Windows\Minidump\*.dmp` on the dev VM with `kd -z <dmp> -c "!analyze -v; q"`.

| Bugcheck | Usual cause in a display miniport |
|---|---|
| `0xD1` / `0xA` | Pageable code or data touched at DISPATCH_LEVEL or under a spinlock. Check that every function on a DPC, interrupt or spinlock path is in a non-paged section. |
| `0x3B` / `0x50` | A use-after-free or a stale pointer on allocation close or destroy paths, often during reinstall or teardown. |
| `0x1A` | Memory corruption: PTE or pool. |
| `0x116` | A TDR the driver could not recover from. With kd attached it always bugchecks; test TDR recovery without kd. |
| `0x119` | A completed fence went backwards: preemption or resubmission replayed old packets. |
