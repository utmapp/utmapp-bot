---
name: provision-windows-dev-vm
description: Create a new UTM Windows 11 ARM64 VM for Windows kernel driver development and debugging — unattended install, guest tools, autologon, OpenSSH both ways, SSHFS-Win Z: share of the host, Visual Studio 2022 + WDK, Python/CMake/Git, a static address on the kdnet network — ready for /utmapp-bot:build-viogpu-driver and to kernel-debug test VMs. Use when asked to provision, create or set up a Windows dev/build VM, or to make an existing dev VM a kdnet debugger.
argument-hint: <path-to-windows-11-arm64.iso>
---

# Provision a Windows development VM

Arguments: `$ARGUMENTS`

1. `iso` — a Windows 11 **ARM64** installation ISO on this Mac.

If it is missing, ask for it. Everything else has a default.

## Before running

- The terminal may script UTM (Automation). The script checks first and
  stops with "macOS needs the user's permission" when someone has to click
  Allow; ask the user to.
- **A current UTM** (`docs/utm-host.md`, "Which UTM"). The script checks
  it before any stage and stops on an older one, naming what is missing.
- **The UTM that provisions decides the network for good**: with the vmnet
  entitlement the VM gets the shared network and a static address on the
  kdnet network, so it can kernel-debug test VMs. When the dev build is
  ad-hoc, provision with the release app (`UTM_APP=/Applications/UTM.app`,
  dev build quit; the script stops with that advice otherwise), or with a
  dev build signed with the VM entitlement profiles. Only with
  `--emulated-net`, or no entitled UTM on the Mac, does the VM get one
  emulated NIC with an SSH port forward and no kdnet: a build VM, not a
  debugger. Check with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/utmbot app`
  (`${CLAUDE_PLUGIN_ROOT}/docs/utm-host.md`).
- **Remote Login** must be on (System Settings > General > Sharing), with
  "Allow full disk access for remote users", or the guest's `Z:` share of
  `~/Documents` cannot be mounted or read.
- About 30 GB free on the Mac (the VM disk grows to roughly that size).

## Run

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/provision-windows-dev.sh" '<iso>'
```

Run it in the background and follow `~/.utmapp-bot/provision/<vm>/provision.log`; a full
run takes one to three hours, most of it the Windows install and Visual
Studio. Everything the scripts keep (VM settings, logs, the download cache)
is in `~/.utmapp-bot/`; set `UTMAPP_BOT_DATA` to use another directory
(without spaces: ssh cannot take a known_hosts path with spaces). Options:

- `--name NAME` — VM name in UTM (default `Windows Development <n>`, the first
  free number from 2). The script refuses a name that already exists.
- `--user USER` / `--password PASS` — Windows account (default `UTM` / `utm`;
  deliberately insecure, the VM is only reachable from this Mac).
- `--ssh-key FILE` — public key the host logs in with (default: first of
  `~/.ssh/id_ed25519.pub`, `id_ecdsa.pub`, `id_rsa.pub`).
- `--name NAME --from STAGE` — resume a failed run at `STAGE` (no ISO needed
  after the `media` stage). The script prints the exact command when a stage
  fails.
- `--name NAME --only STAGE` — run one stage on an existing VM. Dev VMs made
  before the kdnet network existed become kdnet debuggers with
  `--only kdnet` (it shuts the VM down once to move its second NIC).

Shared pieces live in `${CLAUDE_PLUGIN_ROOT}/scripts/provision/common.sh`, `${CLAUDE_PLUGIN_ROOT}/scripts/provision/guest/setup.ps1`
and `${CLAUDE_PLUGIN_ROOT}/scripts/provision/media/`, which `/utmapp-bot:provision-windows-test-vm`
uses too; change them with both skills in mind.

## What it does

| Stage      | Where | What |
|------------|-------|------|
| `media`    | host  | APFS clone of the ISO with `efisys_noprompt.bin` patched into its El Torito boot image (no "Press any key to boot from CD"), and a configuration ISO with `Autounattend.xml`, the setup scripts and the host's public key |
| `vm`       | host  | AppleScript creates the VM (aarch64, HVF, UEFI, 16 GiB, default cores, 128 GiB NVMe, shared NIC + host-only NIC for kdnet, virtio-ramfb-gl, TPM (which selects the Secure Boot capable firmware; the variable store has no keys, so Secure Boot is off), local-time RTC, sound) and exports it. Then it imports the VM back with both ISOs inserted and pins the host-only NIC to the kdnet network |
| `install`  | guest | Unattended Windows 11 Pro install; specialize pass turns off UAC, SmartScreen, Smart App Control, automatic device encryption and the firewall and sets the Defender policies; first logon installs guest tools, makes autologon permanent and installs OpenSSH Server with the host key in `administrators_authorized_keys`. Host waits for SSH |
| `system`   | SSH   | Re-applies the above, long paths, Explorer settings, Copilot/AI/Edge policies off, no sleep, keeps the host-only NIC's default route from competing with the shared NIC; then turns Microsoft Defender off for good through one Safe Mode boot (its WdFilter driver blocks writes to its service keys whenever it runs, even in the specialize pass) |
| `kdnet`    | SSH   | Renames the second NIC `kdnet` and gives it a static address, the first free one from `192.168.240.200` (kept in `state.env` as `KDNET_IP`), no gateway. An older VM's second NIC is first moved onto the kdnet network (shutdown, AppleScript, boot) |
| `tools`    | SSH   | 7-Zip, Git, CMake, Python 3.15 (per user, on PATH), WinFsp, SSHFS-Win |
| `sshfs`    | both  | Guest RSA key (SSHFS-Win's `\\sshfs.k\` uses `id_rsa`) appended to the host's `~/.ssh/authorized_keys`; persistent `Z:` = `\\sshfs.k\<you>@<host>\Documents` |
| `vs`       | SSH   | Visual Studio 2022 Community with exactly the components the build needs (`${CLAUDE_PLUGIN_ROOT}/scripts/provision/guest/vs2022.vsconfig`: C++ desktop, ARM64/ARM64EC/x64/x86 tools, Spectre libraries, Windows 11 SDK 26100, WDK extension), then `pip install meson mako packaging pyyaml` (pyyaml has no Python 3.15 win-arm64 wheel and is compiled) |
| `wdk`      | SSH   | WDK 10.0.26100.6584 |
| `finalize` | host  | Shuts down, replaces the ISO drives with one empty removable drive, boots, adds the VM to `~/.ssh/known_hosts` |
| `verify`   | SSH   | Checks every tool `build-mesa` and `build-drivers.cmd` look for, the MSVC/Spectre libraries, SDK/WDK, `Z:`, `kd.exe` and the kdnet address, UAC/firewall/Defender/BitLocker/autologon/sshd |

### The kdnet network

Every dev and test VM's second NIC is on one host-only vmnet network pinned
to `192.168.240.0/24` (UTM's "vlan guest address", set through AppleScript):
the Mac is `.1`, DHCP hands out `.2`–`.199` (a test VM's kdnet needs DHCP:
it cannot use a static address), and dev VMs, the debuggers, use `.200` and
up. Without the pin vmnet picks a different subnet from boot to boot. Do not
give the NIC a UTM "Host Network" (`HostNetUuid`): that network has no DHCP,
so a dev VM made by hand on one is not a kdnet debugger for test VMs.

Installers are pinned with SHA-256 in `${CLAUDE_PLUGIN_ROOT}/scripts/provision/downloads.txt` and
cached in `~/.utmapp-bot/cache/downloads/`; nothing outside this plugin is needed
except those downloads (and the Visual Studio payload the bootstrapper fetches).

The UTM guest tools are the exception: UTM attaches them itself. The `vm`
stage calls UTM's `install guest tools` scripting command, the same thing
as its Install Windows Guest Tools menu. UTM decides where the tools come
from and fetches a newer release when there is one. The plugin holds no URL
or path for them. A VM gets the release that was latest when its `vm` stage
ran.

## Output

On success the log ends with `Done: '<name>' is ready at UTM@<address> (kdnet
debugger address <kdnet-ip>)`. Report the VM name, its SSH destination, its
kdnet address and the verify summary
(`~/.utmapp-bot/provision/<vm>/step-verify.log`). Then offer to run
`/utmapp-bot:build-viogpu-driver '<name>' arm64x 'Z:\<mesa>' 'Z:\<kmd>'` as the
end-to-end check (a first build compiles LLVM and takes hours).

## When it fails

- **No SSH after the install timeout**: Windows Setup stopped at a prompt or
  failed. Ask the user to look at the VM's window in UTM. Setup logs are in
  `C:\Windows\Panther\` (`setupact.log`, `setuperr.log`), provisioning logs in
  `C:\utmapp-bot\specialize.log` and `C:\utmapp-bot\firstlogon.log`.
- **A guest step failed**: read `~/.utmapp-bot/provision/<vm>/step-<step>.log`, fix the
  cause, resume with `--from <step>`. Steps skip what is already installed.
- **`system` fails with "Microsoft Defender is still on"**: the Safe Mode pass
  did not run (`C:\utmapp-bot\defender-off.log` is missing) or a service key
  was not written (the log shows which). If the VM is stuck in Safe Mode, run
  `bcdedit /deletevalue {current} safeboot` in it and reboot. Then rerun
  `--from system`.
- **`verify` fails on `Z:`**: check Remote Login and full disk access for
  remote users on the Mac, then reboot the VM (the mapping is restored at
  logon) and rerun `--from verify`.
- **SHA-256 mismatch**: a download changed upstream. Do not bypass the check;
  tell the user, and update `downloads.txt` only if they confirm the new file.
- **"… is too old for this plugin (…)"**: update UTM (or select a current
  one with `UTM_APP`) and rerun.
- **`vm`: "UTM could not attach the guest tools: …"**: UTM's own error
  follows, usually no network or no empty removable drive. "did not finish
  attaching the guest tools in time" means UTM is still downloading them:
  look at UTM, then resume with `--from vm`.

