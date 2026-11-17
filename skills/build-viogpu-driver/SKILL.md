---
name: build-viogpu-driver
description: Build the VirtIO GPU Windows driver (viogpu3d) — the Neptune Direct3D and Venus Vulkan user-mode drivers from utmapp/mesa and the kernel driver from utmapp/viogpu-wddm — with utmapp/build-viogpu3d on the Windows dev VM over SSH, from checkouts on this Mac (uncommitted changes included), and pull the test-signed driver packages back. Use when asked to build the drivers, the KMD, the UMD, Neptune, Venus, viogpudo or a driver package.
argument-hint: "[dev VM | user@host[:port]] [build.ps1 arguments, e.g. -Arch arm64x,x86 -Target kmd]"
---

# Build the viogpu3d driver package

Arguments: `$ARGUMENTS`

The driver is three repositories, all the org's:

| Repository | Mac checkout (default) | Provides |
|---|---|---|
| [utmapp/build-viogpu3d](https://github.com/utmapp/build-viogpu3d) | `~/Documents/build-viogpu3d` | `build.ps1`: builds, packages, signs; its README is the reference |
| [utmapp/viogpu-wddm](https://github.com/utmapp/viogpu-wddm) | `~/Documents/viogpu-wddm` | `viogpu3d.sys` (the KMD), `vgpusrv.exe`, `viogpuap.exe`; `viogpudo.sys` |
| [utmapp/mesa](https://github.com/utmapp/mesa) | `~/Documents/mesa` | `neptune_umd*.dll` (Direct3D), `vulkan_virtio*.dll` (Venus) |

Clone any that is missing next to the others (`git clone
git@github.com:utmapp/<name>.git ~/Documents/<name>`). A fix goes into a
branch or worktree of these checkouts; a worktree is passed with `--kmd`,
`--umd` or `--build`.

## Run

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB dev up                 # the dev VM ($UB machine get dev_vm), under the UTM lease
bash "${CLAUDE_PLUGIN_ROOT}/scripts/build-drivers.sh" "$($UB machine get dev_vm)" -Arch arm64x,x86
$UB dev down
```

Everything after the VM is passed to `build.ps1` as it is (lists with
commas, no spaces). The usual ones:

| To build | Arguments |
|---|---|
| the package the test VMs use (ARM64, with x64 apps under emulation and 32-bit apps) | `-Arch arm64x,x86` |
| the quick native-only ARM64 package | `-Arch arm64` (also the default: the VM is ARM64) |
| just the KMD, repackaged with the last UMD build | `-Arch arm64x,x86 -Target kmd` |
| just Neptune, or just Venus, and repackage | `-Arch arm64x,x86 -Target neptune,kmd` |
| a release's set, plus the display-only driver | `-Target all,viogpudo -Arch all` |
| the checked KMD | `-Configuration Debug` |
| from scratch | add `-Clean` |

Options of the wrapper: `--build DIR`, `--kmd DIR`, `--umd DIR` (the Mac
checkouts), `--out DIR` (default `~/.utmapp-bot/drivers`),
`--guest-workdir DIR` (default `%USERPROFILE%\viogpu3d`; keep it, or the
next build starts over).

Run it in the background and wait for it. A first build takes a few minutes per
Mesa architecture; later builds are incremental.

## What it does

`${CLAUDE_PLUGIN_ROOT}/scripts/build-drivers.sh`:

1. Reads each Mac checkout: its commit, and when it has uncommitted changes
   (untracked files that are not ignored included) a work-in-progress commit
   of its working tree, made without touching the checkout's index or refs.
   Whatever GitHub's copy of the repository lacks goes into a git bundle.
2. Uploads the bundles and `${CLAUDE_PLUGIN_ROOT}/scripts/guest/build-viogpu3d.ps1`
   to `<workdir>\host` and runs that script, which keeps three clones on the
   VM's own disk (`<workdir>\build-viogpu3d`, `viogpu-wddm`, `mesa`), fetches
   GitHub and the bundle, and checks out exactly the Mac's state. Uncommitted
   changes stay uncommitted there too, so the stamp ends in `+` (see below).
   Nothing builds on the `Z:` share: the KMD builds in place, and builds on
   SSHFS-Win can hang the share.
3. Runs `build.ps1` there. It reads the driver version from viogpu-wddm,
   stamps it into the UMD (Windows only loads a UMD whose file version equals
   the INF `DriverVer`), builds, assembles each package in an empty directory,
   checks it against its INF (every file listed, the UMD versions), catalogs
   it and test-signs it with the KMD repository's `VirtIOTestCert`.
4. Copies `out\<Os>` back over `<out>/<Os>/`, and the UMD symbols to
   `<out>/symbols/`.

## Output

```
~/.utmapp-bot/drivers/
  Win10/                      (Win11/, Win10-Debug/ for -Os / -Configuration)
    viogpu3d-arm64x/          a package: .inf .cat .sys, the helpers, the UMDs, PDBs of the KMD
    viogpu3d-arm64x.json      its manifest: version, revisions {kmd, umd, build}, files, signer
    viogpu3d-x64/ …  viogpudo-arm64/ …
  symbols/<arch>/             PDBs of the user-mode drivers
  build.log                   the whole run
```

The run ends with one `PACKAGE <dir>  version … kmd … umd … build …` line
per package it built; report those. `out\<Os>` keeps packages from earlier
runs too, so trust those lines, not the directory listing, for what this run
made. To try a package: `$UB install ~/.utmapp-bot/drivers/Win10/viogpu3d-arm64x`
in a fresh disposable boot of a test VM (`/utmapp-bot:run-gpu-test-vm`).

## Which build is installed?

Every build stamps the commits it was made from (ten hex digits, or nine
and `+` for a tree with uncommitted changes). Verified in a guest:

| Where | Shows the commit? |
|---|---|
| Product version of `viogpu3d.sys` and every `neptune_umd*.dll` (Explorer → Properties → Details, or PowerShell) | yes: `100.6.101.58000 (git-68b837cda8)`; the KMD's is the viogpu-wddm commit, the UMD's the Mesa one |
| The installed INF, `C:\Windows\INF\oem<N>.inf`, header | yes: `; Built from:` with both commits |
| The package manifest on this Mac (`viogpu3d-<pkg>.json`, `revisions`) | yes, plus the build-viogpu3d commit; `$UB install` prints it |
| `vulkan_virtio*.dll` | not in Explorer (no version resource); the DLL carries `(git-…)` as the Vulkan driverInfo string that `vulkaninfo` prints (the test VMs have no `vulkaninfo`) |
| Device Manager, dxdiag, `pnputil /enum-drivers`, WMI | no: only `100.6.101.58000`, which every build shares |
| `vgpusrv.exe`, `viogpuap.exe`, `viogpudo.sys` | no |

The driver runs from the DriverStore (`viogpu3d.inf_<arch>_<hash>`); there is
no copy in `System32\drivers`. For a user, this one line in PowerShell
prints both commits of the driver Windows has loaded:

```powershell
$d = Split-Path ((Get-CimInstance Win32_VideoController | ? InstalledDisplayDrivers -like '*neptune_umd*').InstalledDisplayDrivers -split ',')[1]; Get-Item "$d\viogpu3d.sys","$d\neptune_umd.dll" | % { '{0}  {1}' -f $_.Name, $_.VersionInfo.ProductVersion }
```

```
viogpu3d.sys  100.6.101.58000 (git-d25bb08c4a)
neptune_umd.dll  100.6.101.58000 (git-68b837cda8)
```

## When it fails

Find the first real error in `build.log`, not the last line: `error:`,
`FAILED:`, `error C`, `fatal error`, `ERROR:`.

- Something missing on the VM (an MSVC toolset for an architecture, ARM64EC
  libraries, the WDK, Python): report exactly what and stop. Do not install
  software.
- An environment problem you can fix on the VM: fix it and rerun.
- A bug in `build.ps1` or its libraries: fix it in the build-viogpu3d
  checkout and rebuild with it; it is published like any other change to an
  org repository (`/utmapp-bot:land-stack-change` section 7).
- `… is neither on GitHub nor in the bundle`: the Mac checkout's remote for
  `utmapp/<name>` knows a branch GitHub no longer has. `git fetch --prune`
  that remote and run again.
