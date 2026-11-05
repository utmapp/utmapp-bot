---
name: run-gpu-test-vm
description: Run, test and observe the VirtIO GPU (Neptune) driver stack in a UTM test VM — boot it disposable with a chosen DirectX backend and host environment, install a driver package, verify D3D reaches the VirtIO adapter, run programs in the guest's interactive console session, take screenshots and frame-pacing samples, read host logs and crash reports, and tear down cleanly. Use whenever a change has to be tried in a VM, a test or app has to run in the guest, or someone asks to "boot the test VM", "install the driver", "run X in the VM", "take a screenshot" or "check the host log".
argument-hint: "[what to run or check]"
---

# Run the test VM

Request: `$ARGUMENTS`

Everything goes through one harness:

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB help
```

It picks the VM from `--vm NAME`, `$UTMBOT_VM`, or the only test VM the plugin
knows. VMs made by the `provision-*` skills are known already. Other VMs have
to be registered first, e.g. a Linux guest:
`$UB vm register 'Ubuntu' --user ubuntu --os linux`.

Which UTM it drives (`$UTM_APP`, else the last `/utmapp-bot:build-utm`
build, else `/Applications/UTM.app`), what a UTM without the vmnet
entitlement can and cannot do, and the macOS prompts that make calls hang:
`${CLAUDE_PLUGIN_ROOT}/docs/utm-host.md`. Start with `$UB app` when unsure
which app runs or a UTM call fails.

## Ground rules

These are the mistakes that cost the most time before. The harness enforces
the first four.

- **Every boot is disposable.** `vm up` starts the VM with `saving false` and
  refuses to continue unless QEMU really runs with `-snapshot`. A killed VM
  then cannot corrupt the disk, and every test starts from the same image.
- **Kill and relaunch, never reboot.** Iterate with `vm down` + `vm up`.
  Never reinstall a driver into a boot where a driver already ran (`install`
  refuses): it wedges the display and is not a clean test. The one
  sanctioned guest restart is `vm up --nodebug` (below).
- **One UTM at a time.** The release app and a dev build share a bundle id,
  so AppleScript reaches whichever one runs. `vm up` refuses when another
  UTM is running: quit it, do not work around it.
- **The kill-watcher is armed on every `vm up`.** It stops the VM on a GPU
  fault or GPU hang. A GPU that keeps faulting can panic the Mac, and a panic
  cannot be stopped once it starts. Never run a long or unattended workload
  without it: `$UB killwatch status` must say `armed`.
- **Wait on markers, not sleeps.** `test`, `bench`, `install` and `wait` poll
  for a completion marker, print progress, and stop early with `VM-DEAD`.
  Report progress to the user during long runs. "Is it stuck?" is the most
  common complaint about these sessions.
- **Pull results before `vm down`.** The disposable boot discards everything
  in the guest. Harness commands already copy their outputs to the run
  directory.
- **Test only VMs the plugin made or the user named.** Never start, change or
  delete other VMs, and never kill a UTM you did not start.

## The loop

```sh
$UB vm up [--backend d3dmetal|dxmt|default] [--env NAME=value ...] [--tag what]
$UB install ~/.utmapp-bot/drivers/ARM64     # a package from /utmapp-bot:build-viogpu-driver
$UB display-fix                              # after install: restart DWM, reset the mode
$UB verify                                   # must print VERIFY PASS
$UB test <exe, probe name or guest path> [args] [--genv NAME=value] [--timeout S]
$UB launch <exe> [args] ; $UB launch-out     # long-running / GUI programs
$UB shot ; $UB frames 10                     # see it
$UB hostlog ; $UB crashes                    # anything wrong on the host?
$UB vm down
```

`vm up` prints `UP <vm> ssh=… run_dir=… backend=… net=as-configured|emulated`. `net=emulated` means the app has no vmnet
entitlement: the VM runs on QEMU's user network for this boot, SSH goes
through a forwarded port, and guests cannot reach each other (no kdnet).
`vm down` restores the network modes. Each VM lifetime gets a run directory,
`~/.utmapp-bot/runs/<vm>/<time>-<tag>/` (also linked as `latest`). The debug
log, render-server logs, screenshots, frame samples, test outputs, dumps and
benchmark results all land there, so quote that path in reports.

### Backends and host environment

- `--backend d3dmetal|dxmt|default|disabled` sets UTM's global
  `QEMUDirectXDriver` default for this boot, and `vm down` restores it. If the
  harness died mid-run, check `defaults read com.utmapp.UTM QEMUDirectXDriver`
  (0 = default, 2 = DXMT, 3 = D3DMetal).
- `--env NAME=value` passes host environment variables to QEMU, the render
  server and its workers, through UTM's AppleScript `start … environment`.
  The Debug Log header lists the environment QEMU started with.
- The render server's own log (`npt:`, `proxy:`, DXMT) reaches debug.log
  (older releases left it out, which matters when reading a reporter's
  log; references in triage-gpu-issue's log-sinks.md). Do not set
  `VIRGL_LOG_FILE`: the only place the sandboxed render server can write it
  is UTM's group container, which the terminal cannot read.
- `vm up` turns the VM's **Debug Log** on by default (it changes the
  configuration, so only while the VM is stopped). Use `--debug-log off` for
  benchmark runs that must match a user's setup.
- Metal API validation is a separate global switch. It costs ~20% and
  changes timing:
  `defaults write com.utmapp.UTM GraphicsValidation -bool YES` (off: `NO`).
- Prove the backend instead of assuming it: `$UB backend`. A D3DMetal worker
  runs x86_64 under Rosetta; DXMT and Venus workers run arm64. Check it
  every time a result depends on the backend.

### Running things in the guest

- **Windows:** D3D works only in the interactive console session. sshd's
  window station has no displays, so a D3D app started over SSH fails to find
  the adapter, or lands silently on WARP. `test`/`launch` run the command
  through a scheduled task in the console session. `guest CMD` is for
  everything that does not touch the GPU (files, registry, `pnputil`, `sc`,
  `tasklist`).
- `test` waits for the exit marker and prints the output and exit code.
  `launch` returns at once; read it later with `launch-out`.
- Guest-side driver knobs go to the program with `--genv NAME=value`. Setting
  them over SSH or with `setx` does not reach a process started by the task
  scheduler.
- A probe built for x64 runs emulated on Arm and cannot load a native arm64
  driver DLL directly. That gives errors like `ERROR_BAD_EXE_FORMAT` (`193`)
  or `0xC000007B`, which are not driver bugs; the same goes for .NET/AnyCPU
  programs and PowerShell `Add-Type`. Test the native path with the ARM64
  probes from `scripts/build-probes.sh`.
- **Linux:** `test`/`launch` start the program detached (`setsid`) in the
  GUI session, with `DISPLAY`/`XAUTHORITY` for Xwayland, with output straight
  to a file in `~/utmbot/`, so it survives SSH and its output is not lost
  when it is killed. On a VM from `/utmapp-bot:provision-linux-test-vm`, run
  Windows programs with `npt-wine [--backend neptune|dxvk|wine] <exe>` and
  3DMark with `npt-3dmark fs|ts`. `verify` checks that
  Vulkan reports the Venus device, not llvmpipe. Graphics drivers for Windows
  programs run x86_64 under FEX, so build anything for that path as x86_64.
  The first Wine run after a boot is ~30 s slower.

### Seeing the VM

- `shot` takes one screenshot of the guest display. It reads the guest
  framebuffer through UTM's `capture screen`: no window, no Screen Recording
  permission, nothing on top of the VM in the picture. One still proves
  little.
- `frames SECS` samples the window with ScreenCaptureKit and reports
  distinct (pixel-changing) fps, holds over 100 ms, the black fraction and
  mean luma. Use it to call anything "frozen", "black" or "stuttering". The
  terminal needs Screen Recording permission.
- **A black window after idle is usually the guest display powered off, not
  a driver bug.** `vm up` disables the monitor timeout, and one UTM input
  wakes the display:
  `$UB input click 400 300`.
- Keep UTM in front (`$UB front`). macOS App Naps a background UTM, and its
  vCPUs then run ~4x slower. `check-host` reports nap, a locked screen, a
  sleeping display, low disk, swap and background CPU hogs.
- Guest input that games ignore, such as `SendInput` into some engines,
  can be sent through UTM instead: `$UB input key|combo|text|click …`. A key
  press shorter than a frame can be dropped, so `input key` holds keys
  (`--hold`, default 0.15 s; 0.3 s for games running at low frame rates).

### Anti-tamper titles

Some games raise exceptions on purpose when a kernel debugger is enabled. The
test VMs are provisioned with `/noumex`, so user-mode exceptions do not stop
the VM, but such a loader can still refuse to run. `vm up --nodebug` runs
`bcdedit /debug off` and restarts the guest once. The harness gates the
restart on the boot id, because sshd keeps answering during shutdown. It is
refused unless the boot is disposable, so the change lasts exactly one VM
lifetime.

## Output

Report the `UP`/`TEST`/`VERIFY`/`INSTALLED`/`FRAMES` lines and the run
directory. If the run directory has a screenshot or frame sample that shows
the result, show it to the user.

## When it fails

- **`vm up` TIMEOUT waiting for SSH**: the VM is left running. Look with
  `$UB shot` and `$UB snapshot` before `vm down`: a stuck boot, a guest
  waiting at a prompt, or no DHCP lease.
- **"macOS needs the user's permission" / `AppleEvent timed out`**: a prompt
  nobody can see: Automation permission (the first time this terminal
  scripts UTM), or a folder-access prompt for an app copy in
  Documents, Downloads or Desktop, on a locked screen. Ask the user to unlock
  the Mac and answer it; see `docs/utm-host.md`.
- **"cannot create vmnet interface"** or **"does not have the entitlement for
  Shared Network…"**: a vmnet VM started outside the harness on a UTM without
  the entitlement. Start it with `vm up`, which switches it to emulated
  networking for the boot.
- **`install`: "did not install … on any device" / "up-to-date"**: an equal or
  newer driver is bound already; start from a fresh `vm up`. Push the whole
  package, never single files: pnputil skips or rejects packages whose
  catalog no longer matches.
- **`verify` FAIL with S_OK nowhere**: see `/utmapp-bot:triage-gpu-issue`
  (references/install-and-load.md).
- **`killwatch` FIRED / `VM-DEAD`**: do not just restart. Run `$UB crashes`
  and `$UB hostlog`, then follow `/utmapp-bot:triage-gpu-issue`
  (references/host-safety.md).
- **The VM never stops / U-state processes after `vm down`**: a GPU hang is
  still in flight. Do not start another VM until they are gone, and tell the
  user.
