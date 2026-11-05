---
name: kernel-debug
description: Kernel-debug a Windows test VM over kdnet with kd.exe running on its debugger dev VM — attach, watch the display miniport's DbgPrint output live while it loads, break in to a hung guest, collect bugcheck analysis, resume, and find user-mode DLL load failures with loader snaps. Use when a driver will not load, the guest hangs or bugchecks, a frozen VM may be halted in the debugger, or someone asks to "attach kd", "break in", "get the bugcheck" or "watch the KMD log".
argument-hint: "[start|break|crash|send <cmd>|stop]"
---

# Kernel debugging over kdnet

Request: `$ARGUMENTS`

`UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"`

## Before running

- **kdnet needs a network between two VMs, which only vmnet gives.** The UTM
  running both VMs must have the vmnet entitlement (the release app, or a
  dev build signed with the VM entitlement profiles), and the test VM must
  have been provisioned with one. On an ad-hoc dev build `utmbot vm up` runs
  VMs with emulated networking and `kd` refuses with that reason. Tell the
  user what is needed rather than working around it
  (`${CLAUDE_PLUGIN_ROOT}/docs/utm-host.md`). Host-side changes that need a
  dev UTM and kernel debugging at once need the profiles.
- The test VM was made by `/utmapp-bot:provision-windows-test-vm`. Its
  state records the debugger dev VM and the kdnet port; `kdtest` proved the
  link.
- The debugger dev VM is running, and the host reaches it over SSH.
- kd runs **on the dev VM**. It debugs the test VM over the host-only kdnet
  network (`192.168.240.0/24`, key `1.1.1.1`).

## Run

```sh
$UB kd start            # attach; kernel DbgPrint now streams to kd.log
$UB kd verbose          # the KMD's messages too: DebugLevel 4 + nt!Kd_DEFAULT_Mask; before install
$UB kd tail 60          # read it (works while the target runs)
$UB kd status           # attached? target halted at a prompt?
$UB kd break            # halt the target (Ctrl+C to kd); freezes the VM
$UB kd send "!process 0 0 dwm.exe"   # only while halted
$UB kd crash            # .lastevent, .bugcheck, !analyze -v, kv, lmv viogpu3d
$UB kd go               # resume
$UB kd stop             # detach (also before rebuilding the KMD: kd locks its PDB)
```

`kd.log` lives in `~/.utmapp-bot/runs/<vm>/kd/`. Start kd **before** the
thing you want to watch, e.g. before `$UB install`, so the miniport's
`DriverEntry`/`StartDevice` messages are captured. The KMD is silent
unless two things are set, and `kd verbose` sets both:
- its `DebugLevel` (`HKLM\SYSTEM\CurrentControlSet\Services\VioGpu3D\Parameters`,
  a DWORD; absent or 0 is silent, 4 is info), read when the driver loads;
- the kernel's print filter: `nt!Kd_DEFAULT_Mask` is 0 by default and drops
  the driver's messages even with `DebugLevel` set.

`send` prints only the output of its own command. To find what something
else printed (an install's DbgPrint), note `wc -l` of kd.log before it and
read from there.

## What it does

- kd runs under `ssh -tt` on a local terminal that a detached relay process
  owns. The relay copies commands from a FIFO into the terminal and kd's
  output, without terminal escape codes, into kd.log. The terminal is 400
  columns wide, so long lines (`!analyze -v`, stacks) are not wrapped.
- `send` refuses unless the last line of kd.log is a `kd>` prompt: commands
  only run while the target is halted. `break` sends Ctrl+C through the
  terminal, which is kd's own break-in: it halts a running or hung target
  within seconds. (`kd start --break` passes `-b`, "break on connect", which
  a target honors only when it first connects, i.e. at boot.)
- A halted target **freezes every vCPU**: SSH, the display and the guest
  clock all stop until `go`. Do not leave it halted.
- Symbols: `--sympath` takes a symbol path on the dev VM. The default is
  `C:\utmbot\symbols` (put the driver's PDBs there; the released driver
  ships none), then Microsoft's symbol server cached in `C:\utmbot\symcache`
  for `nt!` and `dxgkrnl!`. After a fresh break, `.reload` before `kv`
  (`kd verbose` does).

## Patterns

- **"The VM froze."** Check `$UB kd status` before anything else. A halted
  target looks exactly like a hang: window frozen, SSH dead, debug.log
  stopped at the same second. Typical causes:
  - a Code Integrity break (`… not signed correctly`, harmless with test
    signing; `kd go`);
  - an assertion (`c0000420`) or `int3` (`80000003`);
  - a bugcheck.

  The test VMs are provisioned with `/noumex`, so user-mode exceptions do not
  break in. A game whose anti-tamper still misbehaves under a debugger runs
  with `$UB vm up --nodebug`.
- **Bugchecks halt instead of rebooting** while kd is attached. Collect with
  `kd crash` before anything else. A manual `.crash` from kd *reboots* a
  kdnet target instead of holding it, so catch natural bugchecks. TDR
  recovery cannot be tested with kd attached: dxgkrnl bugchecks with `0x116`.
- **Driver load, live.** `kd start`, then `$UB install`. The KMD's messages
  show where `StartDevice` fails. If the KMD prints nothing, `kd verbose`
  was not run before the install.
- **A user-mode DLL that fails to load.** Enable loader snaps for that exe
  (IFEO `GlobalFlag=2`, see the triage skill's install-and-load.md). The
  loader then prints the exact missing DLL and status to kd.
- **Hang analysis while halted:**
  - `!process 0 7 <image>` gives every thread's kernel stack;
  - `!locks` for a deadlock (`Possible deadlock` in kd.log);
  - `!running -it` for what the CPUs are doing;
  - `lmv m viogpu3d` confirms the loaded driver build.

## Without kd

When kd cannot be used (no vmnet, no debugger VM, kdnet broken) and the
user still needs evidence, collect what does not need it, and say what it
cannot show:

- `$UB snapshot`: guest event log, driver state, minidumps and
  LiveKernelReports, host logs, crash reports;
- a bugcheck that rebooted the guest leaves `C:\Windows\Minidump` or
  `MEMORY.DMP`: pull it with `$UB get` before `vm down` and analyze it with
  `kd -z` on any Windows machine with the debugging tools;
- a user-mode hang: `$UB dump <image>` and `$UB cdb <image>`.

A live kernel stack of a hung guest needs kd.

## When it fails

- **`kd start`: debugger VM not reachable**: start it in UTM; it must be
  the VM named in the test VM's provision state.
- **`break` never halts**: kd is not connected (kd.log has no "Connected to
  target" line). Check:
  - the test VM is booted with debugging on (`bcdedit /enum` shows
    `debug Yes`);
  - its kdnet adapter (KDNIC) is up;
  - no other kd holds the port (`kd stop` kills every kd.exe on the dev VM).

  `provision-windows-test-vm --only kdtest` re-checks the link.
- **Nothing streams but the target runs**: a previous kd still owns the
  port, or the target was booted with `--nodebug` (kernel debugging off for
  that boot).
