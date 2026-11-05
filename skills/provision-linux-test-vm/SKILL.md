---
name: provision-linux-test-vm
description: Create a new UTM Ubuntu 26.04 arm64 VM to test the VirtIO GPU stack from Linux — GNOME with autologin, Venus Vulkan, and Windows D3D programs through Wine (10.9 staging-tkg) under FEX-Emu with Neptune's D3D11/12 DLLs built from osy/virtio-win-mesa (or DXVK, or wined3d), hardware TSO, 3DMark in the Wine prefix — from Ubuntu's cloud image, unattended. Use when asked to provision, create or set up a Linux/Ubuntu test VM, a Venus VM, or a VM to run Neptune under Wine/FEX.
argument-hint: "[--name NAME] [--mesa-bundle FILE] [--3dmark DIR]"
---

# Provision a Linux test VM

Arguments: `$ARGUMENTS`

The VM is built from public sources only: Venus for Vulkan programs, and
for Windows programs x86_64 Wine under FEX with three D3D backends.

```
arm64 Ubuntu 26.04 (GNOME on Wayland + Xwayland, autologin)
 ├─ Venus Vulkan (Ubuntu's Mesa)                       vkcube, CTS, native Vulkan
 └─ FEX-Emu + x86_64 Ubuntu 24.04 rootfs (x86_64 Venus ICD inside)
     └─ Wine 10.9 staging-tkg (Kron4ek), prefix ~/wine-prefixes/multi-backend/pfx
         ├─ neptune: ~/mesa builddir-win64 d3d11/dxgi/d3d12 + nptunix (x86_64 unixlib)
         ├─ dxvk:    ~/dxvk 2.6.1 (3.x needs depth clip, which Venus on Apple lacks)
         └─ wine:    wined3d, Vulkan renderer
```

## Before running

- A current UTM (it grows the cloud image's drive and sets TSO;
  `docs/utm-host.md`, "Which UTM"). The script checks it before any stage
  and stops on an older one, naming what is missing. Quit any other UTM
  first: with two open, scripting reaches the wrong one.
- About 60 GB free, and internet access from the Mac and the VM.
- No vmnet entitlement needed: with an ad-hoc dev build the VM gets an
  emulated NIC with an SSH port forward instead of the shared network.
- Optional: a git bundle of unpushed Neptune commits (`--mesa-bundle`, e.g.
  `git bundle create neptune.bundle neptune` on the dev machine).
- **Benchmarks in the Wine prefix:** Unigine Heaven and GravityMark (x64,
  from `downloads.txt`) always. 3DMark (Fire Strike, Time Spy) when the Mac
  has its key and installer, as for the Windows test VM: the key in
  `~/.utmapp-bot/3dmark.key` (or `$UTMBOT_3DMARK_KEY`), and the installer in
  `~/.utmapp-bot/cache/3dmark/` (or `--3dmark DIR`), in either form the
  Windows test VM skill describes. The prefix installs the MSI and then the
  packed `.dlc` tests with `3DMarkCmd --install`; a folder with only
  `3dmark-setup.exe` is skipped. Without a key or an installer, 3DMark
  is skipped.

## Run

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/provision-linux-test.sh" [--name NAME] [--mesa-bundle FILE] [--3dmark DIR]
```

Run it in the background. Follow `~/.utmapp-bot/provision/<vm>/provision.log`.
A fresh run took 12 minutes on an M2 Max with a fast connection (the
desktop install about 6, everything else about 6). Resume a failed run with `--name NAME --from STAGE`, and
redo one step with `--only STAGE` (e.g. `--only mesa` after new commits).

| Stage | Where | What |
|---|---|---|
| `media` | host | Ubuntu's arm64 cloud image, checked against Canonical's `SHA256SUMS`, and a cloud-init seed ISO (volume `CIDATA`): the user (default `ubuntu`/`ubuntu`) with the host's SSH key and passwordless sudo, `ubuntu-desktop-minimal`, GDM autologin, DHCP by MAC |
| `vm` | host | `utm.applescript create-linux`: aarch64 virt, HVF with **TSO**, UEFI, 16 GiB, the image as a VirtIO disk grown to 200 GiB, the seed as a USB drive, shared virtio-net, `virtio-gpu-gl-pci` (Venus), intel-hda. Records `S_OS=linux` so `utmbot --vm NAME` works with no `vm register` |
| `install` | guest | Boot, wait for cloud-init (the desktop install and one restart) |
| `session` | SSH | GNOME without idle blank, lock or suspend; `perf_event_paranoid=1` |
| `fex` | SSH | `ppa:fex-emu/fex` (`fex-emu-armv8.4`, binfmt), the Ubuntu 24.04 x86_64 rootfs extracted into `~/.fex-emu/RootFS` |
| `wine` | SSH | Wine 10.9 staging-tkg x86_64 into `~`, hash-checked |
| `tools` | SSH | mingw-w64, x86_64 Linux cross gcc, meson/ninja, build dependencies |
| `dxvk` | SSH | DXVK 2.6.1 build; its `build-win64.txt` cross file is also Neptune's |
| `mesa` | SSH | `osy/virtio-win-mesa` branch `neptune` (or the bundle's) into `~/mesa`, `meson -Dneptune=true` for win64 |
| `nptunix` | SSH | `~/bin/build-nptunix-x86`: the unixlib as x86_64 ELF, linked against the rootfs's libraries |
| `prefix` | SSH | The Wine prefix, the 3DMark definitions in `C:\`, Unigine Heaven and GravityMark, and 3DMark (MSI, Fire Strike and Time Spy, key) when the key and installer are there |
| `verify` | SSH | Venus device, autologin, FEX, Wine, DXVK, Neptune DLLs, x86_64 unixlib, prefix |

## Using it

```sh
export UTMBOT_VM='<vm>'
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB vm up --backend dxmt           # disposable boot, as for Windows
$UB verify                          # Venus, not llvmpipe
$UB test 'npt-wine ~/probe.exe'     # Neptune; --backend dxvk|wine for the others
$UB test 'npt-3dmark fs' --timeout 900   # prints "FPS <n> VALID"
```

- `npt-wine [--backend neptune|dxvk|wine] <exe> [args]` installs that
  backend's DLLs into the prefix, then runs the program. With Neptune it
  also rebuilds the x86_64 `nptunix.so` and pairs it with the fresh
  `nptunix.dll` in `~/npt-stage`. Wine loads the builtin DLL found on
  `WINEDLLPATH`, not the copy in system32.
- After a Neptune change: `--only mesa` (or `ninja -C ~/mesa/builddir-win64`
  in the VM). `npt-wine` picks up the new DLLs at the next run.
- For `/utmapp-bot:validate-stack`, use
  `--linux-vm '<vm>' --linux-cmd 'npt-3dmark fs'`.
- FEX's TSO emulation is off (`FEX_TSOENABLED=0` …) only when the VM has
  hardware TSO, which `~/.config/utmbot/hw-tso` records. That is about 13%
  on Fire Strike GT1. Results from a VM without TSO are not comparable.
- Programs build for x86_64 Windows (`x86_64-w64-mingw32-gcc`). The first
  Wine run after a boot is about 30 s slower.

## What stays manual

- Steam (`steam-launcher`, run as `FEXBash steam`) and its games need an
  interactive login. Games copied from another machine are not part of the
  recipe.
- Venus in the guest is Ubuntu's Mesa. Two guest-side Mesa fixes once had
  to be patched in by hand (a `seq_cst` Venus ring store, a `u_surface`
  clear). If Venus misbehaves under load, check whether the guest's Mesa
  has them before suspecting the host.

## When it fails

- **"… is too old for this plugin (…)"**: update UTM (or select a current
  one with `UTM_APP`). If the right one is selected, another UTM was open:
  quit it and resume.
- **`prefix`: a benchmark "did not install"**: `~/bench-setup.log` in the VM.
  The Wine installers need the GNOME session's Xwayland; the stage finds its
  auth file under `/run/user/<uid>/`, so the autologon session must be up.
- **`install` waits for SSH forever**: `utmbot --vm '<vm>' shot` shows the
  console. With vmnet and no address: the lookup reads the DHCP leases and
  the vmnet bridges' ARP table. With an SSH port forward: is anything else
  listening on that port (`lsof -i :<SSH_PORT>`)?
- **`install`: cloud-init did not finish**: `/var/log/cloud-init-output.log`
  in the VM (usually a package mirror problem); fix it, then `--from install`.
- **`mesa` fails**: `~/mesa/builddir-win64/meson-logs/meson-log.txt`. A new
  Neptune dependency means an extra package in the `tools` step.
