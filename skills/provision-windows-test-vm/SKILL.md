---
name: provision-windows-test-vm
description: Create a new UTM Windows 11 ARM64 VM to test Windows kernel drivers on — same unattended install, guest tools, autologon, OpenSSH (host to VM), UAC/Defender/firewall off as the dev VMs, but no build tools or host share, no viogpudo display driver, Secure Boot off, test signing on and kdnet kernel debugging over an e1000 NIC to a dev VM — then check that kd.exe on the dev VM can break in. Use when asked to provision, create or set up a Windows test/target/debuggee VM.
argument-hint: <path-to-windows-11-arm64.iso>
---

# Provision a Windows test VM

Arguments: `$ARGUMENTS`

1. `iso` — a Windows 11 **ARM64** installation ISO on this Mac.

If it is missing, ask for it. Everything else has a default.

## Before running

- **A current UTM**, checked before any stage, as for the dev skill.
- **The UTM that provisions decides the network for good**
  (`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/utmbot app`). With the vmnet
  entitlement (the release app, or a dev build signed with the VM
  entitlement profiles) the VM gets the shared network plus the kdnet NIC,
  and needs a debugger dev VM (below). Such a VM still runs on an ad-hoc dev
  build later (`vm up` emulates its NICs per boot). So when the dev build is
  ad-hoc, provision with the release app (`UTM_APP=/Applications/UTM.app`,
  dev build quit) or an entitled dev build; the script stops with that
  advice otherwise. Only with
  `--emulated-net`, or no entitled UTM on the Mac at all, does the VM get one
  emulated NIC with an SSH port forward: test signing, no kernel debugging,
  no debugger needed (`debug` only turns test signing on, `kdtest` is
  skipped). Tell the user that trade-off before choosing it
  (`${CLAUDE_PLUGIN_ROOT}/docs/utm-host.md`).
- With vmnet, a dev VM that is a kdnet debugger: one made by
  `/utmapp-bot:provision-windows-dev-vm`, with a kdnet address (`KDNET_IP` in
  `~/.utmapp-bot/provision/<vm>/state.env`). The test VM is configured to debug to the
  first such VM, or `--debugger NAME`. A dev VM made before the kdnet network
  existed becomes one with
  `bash ${CLAUDE_PLUGIN_ROOT}/scripts/provision-windows-dev.sh --name '<dev vm>' --only kdnet`. Without
  any, the script stops before doing anything.
- The terminal may script UTM (Automation), and about 20 GB free.

## Run

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/provision-windows-test.sh" '<iso>'
```

Run it in the background and follow `~/.utmapp-bot/provision/<vm>/provision.log`; a run
takes 30–70 minutes, most of it the Windows install. Options:

- `--name NAME` — VM name (default `Windows Test <n>`, the first free number
  from 1).
- `--debugger NAME` — the dev VM that debugs this one.
- `--port N` — kdnet port (default: the first from 50000 no other test VM in
  `~/.utmapp-bot/provision/` uses, so several test VMs can share a debugger).
- `--disk GIB` — the system disk (default 64; 256 for the Steam VM). The
  qcow2 grows only as it fills.
- `--3dmark DIR` — the 3DMark installer for the `content` stage (see
  below). Default: `~/.utmapp-bot/cache/3dmark`.
- `--user`, `--password`, `--ssh-key`, `--name NAME --from STAGE`,
  `--name NAME --only STAGE` — as for the dev skill.

## What it does

The `media`, `install` and `system` stages, and most of `vm`, are the dev
skill's (`${CLAUDE_PLUGIN_ROOT}/scripts/provision/common.sh`). The guest steps are in
`${CLAUDE_PLUGIN_ROOT}/scripts/provision/guest/setup.ps1`.

| Stage      | Where | What |
|------------|-------|------|
| `media`    | host  | No-prompt boot ISO, configuration ISO (Autounattend, setup scripts, host key) |
| `vm`       | host  | As for dev VMs (UTM attaches its own guest tools to the third, empty drive), but 8 GiB, 64 GiB NVMe and the second NIC an **e1000** on the kdnet network (`192.168.240.0/24`, DHCP `.2`–`.199`) |
| `install`  | guest | Unattended install, guest tools (`utm-guest-tools*.exe /S`: every driver, including the VirtIO GPU ones for now, and the agents), autologon, OpenSSH Server with the host key |
| `system`   | SSH   | UAC, firewall, SmartScreen, policies, power; Defender off through one Safe Mode boot |
| `gpu`      | SSH   | Removes the guest tools' VirtIO GPU drivers, viogpudo and viogpu3d, so the GPU is free for the driver under test: re-enables the Basic Display fallback, sets a loaded driver's service not to start and restarts (removing a loaded viogpudo hangs Windows), then `pnputil /delete-driver /uninstall` and `sc delete` for the services (a deferred delete gets one more restart). Stops Windows Update from offering drivers. Its own code; the ISO's interactive `Uninstall-VirtioGpu.cmd` explains the same steps |
| `finalize` | host  | Shuts down, ejects the ISOs, makes sure Secure Boot is off (below), boots, adds the VM to `~/.ssh/known_hosts` |
| `debug`    | SSH   | `testsigning on`; `dbgsettings net hostip:<debugger kdnet ip> port:<port> key:1.1.1.1 /noumex` (user-mode breakpoints, such as the one the guest tools' `spice-webdavd.exe` hits at every boot, would otherwise stop the VM until a debugger attaches); `busparams` = the e1000's PCI location (Windows has no ARM64 e1000 driver, but kdnet's `kd_02_8086.dll` drives it; without busparams kdnet could take the virtio NIC, which `kd_02_1af4.dll` also drives); `debug on`; restart |
| `verify`   | both  | Secure Boot off, booted with TESTSIGNING and DEBUG, the dbgsettings, the kdnet adapter (KDNIC) Up (it is only Up when kdnet started on the e1000), no VirtIO GPU driver package or service and the GPU unbound, Basic Display fallback on, guest tools, VirtIO network, no build tools, UAC/firewall/Defender/BitLocker/autologon/sshd; notes the DHCP lease kdnet took for the e1000 |
| `kdtest`   | both  | Starts the debugger VM if it is off (and shuts it down again afterwards), runs `kd.exe -k net:port=<port>,key=1.1.1.1 -b` there, restarts the test VM, and passes when kd breaks in and `vertarget` answers; then lets the target run and closes kd |
| `content`  | test  | Benchmarks for `/utmapp-bot:perf-regression` and `/utmapp-bot:validate-stack`, from the pinned downloads in `downloads.txt`: VC++ 2015–2022 runtimes (arm64, x64, x86), GravityMark 1.89 (native arm64), Unigine Heaven 4.0; display and sleep timeouts off, Game DVR off. With `--3dmark DIR`, 3DMark from UL's full installer or from its MSI payloads (`data_x64.msi`, `Futuremark_SystemInfo.msi`), tests from `DIR/dlc`, activated with `$UTMBOT_3DMARK_KEY` or `~/.utmapp-bot/3dmark.key`. Prints `CONTENT OK` with what is installed and whether 3DMark is activated. Rerun alone with `--only content` on any test VM |

### Secure Boot

Test signing and kernel debugging need Secure Boot off. UTM creates the
variable store without keys, so the VM is in UEFI Setup Mode and Secure Boot
is off from the start; Windows installs fine like that (the Autounattend
bypasses the Secure Boot check). `finalize` still checks the store with
`${CLAUDE_PLUGIN_ROOT}/scripts/provision/efivars.py` and, if it has a Platform Key (UTM's "Preload
Secure Boot Keys", or a future UTM default), deletes it while the VM is off,
keeping the original as `~/.utmapp-bot/provision/<vm>/efi_vars.fd.with-pk`.

Deleting the PK was chosen over the alternatives because the UEFI
specification defines its effect (no PK → Setup Mode → `SecureBoot` = 0) for
any firmware build. The EDK2 menu would need keystroke automation that breaks
with any menu change, an EFI app needs a toolchain and a boot path, and
dropping the TPM to get UTM's non-secure firmware relies on how UTM happens to
pick firmware today. `efivars.py` reads the store, a qcow2 image, directly. It
deletes the variable the way EDK2 does, by clearing one bit of the record's
State byte, and `pk-on` sets it again. `verify` confirms the result from
inside Windows (`Confirm-SecureBootUEFI` is False).

### Content

Only what can be installed legitimately and without an account is
automated. Steam and Epic install silently, but signing in and downloading
games stays manual (`SteamSetup.exe /S`, then sign in from the VM's window).
The FFXIV Dawntrail benchmark has no stable download URL and runs only from
its window. Games copied from another machine are not part of any image.
**3DMark** (Fire Strike and Time Spy) needs two things on the Mac, and
the stage skips 3DMark, saying why, when either is missing:
- **The key:** one line in `~/.utmapp-bot/3dmark.key`, or
  `$UTMBOT_3DMARK_KEY`. Put it there by hand once; nothing asks for it.
- **The installer:** `~/.utmapp-bot/cache/3dmark/` (or `--3dmark DIR`).
  Either of:
  - **3DMark Basic's download, unzipped there** (or anywhere, passed with
    `--3dmark DIR`): `3dmark-setup.exe`, `data_x64.msi` and every test as a
    packed `3dmark-v2-…-<test>-v….dlc` (13 GB in all). The stage uploads only
    the MSI and the Fire Strike and Time Spy `.dlc` files (about 3 GB),
    installs the tests with `3DMarkCmd --install`, and activates with the
    key. Download it by hand from
    [TechPowerUp](https://www.techpowerup.com/download/futuremark-3dmark-timespy-raytracing/)
    or [TechSpot](https://www.techspot.com/downloads/5775-3dmark.html). Both
    refuse scripted downloads, and UL's own direct download refuses
    Professional keys.
  - **UL's MSIs from an existing install:** `data_x64.msi` and
    `Futuremark_SystemInfo.msi` from `C:\ProgramData\Package Cache\…`,
    plus `dlc/fire-strike-test` and `dlc/time-spy-test` from
    `C:\ProgramData\UL\3DMark\chops\dlc`. This form also works for the
    Linux test VM's Wine prefix.

GravityMark and Unigine Heaven need nothing: they come from
`downloads.txt`.

3DMark's command line needs a **Professional** key. Activation is per
Windows user, so the stage registers as the autologon user, which is also
the SSH user. A 3DMark taken out of a Package Cache is only Burn's engine,
without its payload, so the stage installs the MSIs directly. Alone, the
3DMark MSI fails (1603) in its test side-load action. The stage passes the
folders Burn would pass (`INSTALLLOCATION`, `DLC_DESTINATION`).

## Output

On success the `kdtest` stage prints `Done: '<name>' is ready at
UTM@<address>; kernel debugging works from '<dev vm>'` and the kd command
line, and the log ends with the `content` stage's `CONTENT OK: …` line
(benchmarks installed, 3DMark activated, its tests). The VM is left running,
from a writable boot: `utmbot --vm '<name>' vm down` before using it with
`vm up`. Report the VM name,
its SSH destination, the debugger VM, its kdnet address and port, and the
verify and kdtest results (`~/.utmapp-bot/provision/<vm>/step-verify.log`,
`~/.utmapp-bot/provision/<vm>/kdtest.log`). To debug, run on the dev VM:

```
"C:\Program Files (x86)\Windows Kits\10\Debuggers\arm64\kd.exe" -k net:port=<port>,key=1.1.1.1
```

(or WinDbg with the same port and key), then restart the test VM.

## When it fails

- **Install, Defender, SHA-256, guest tools**: as for the dev skill.
- **`media` fails attaching an ISO ("Resource busy")**: an earlier run left
  the image attached without a mount (`hdiutil info`). `hdiutil detach
  /dev/diskN`; when that hangs, only a logout or reboot clears it. Copying
  the ISO to a new path works around it.
- **Resume**: the failure prints the command to resume with, including the
  ISO, `--disk` and `--3dmark`.
- **`gpu` fails**: read `~/.utmapp-bot/provision/<vm>/step-gpu.log`. A service still
  `Pending` after the restarts means the SCM kept a record; restart the VM
  once more and rerun `--from gpu`. If the VM hung while removing a driver, a
  loaded viogpudo was torn down; force-stop it in UTM, start it and rerun
  `--from gpu` (the step only deletes packages once nothing is loaded).
- **`debug` fails with "Secure Boot is on"**: `finalize` did not turn it off.
  Check `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/provision/efivars.py status '<bundle>/Data/efi_vars.fd'`
  while the VM is stopped. If `pk-off` reports a compressed cluster, the store
  is still UTM's template: boot the VM once so the firmware rewrites it, then
  rerun `--from finalize`.
- **`verify`: KDNIC not Up**: kdnet did not start on the
  e1000. Compare `bcdedit /dbgsettings` busparams with the e1000's location
  (`Get-PnpDevice` `PCI\VEN_8086&DEV_100E*`), and check that the VM's second
  NIC is `e1000` on `192.168.240.0/24`
  (`osascript ${CLAUDE_PLUGIN_ROOT}/scripts/provision/utm.applescript nic '<vm>' 1`).
- **`kdtest` fails**: read `~/.utmapp-bot/provision/<vm>/kdtest.log`. Check that the
  debugger VM has its kdnet address (`ipconfig` on it shows
  `192.168.240.2xx`; if not, run the dev script with `--only kdnet`) and that
  no other kd.exe holds the port. The stage restarts the test VM if it is
  left in a break. Rerun with `--only kdtest`.
