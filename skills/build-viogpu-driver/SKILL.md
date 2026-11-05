---
name: build-viogpu-driver
description: Build the VirtIO GPU Windows driver — the Neptune user-mode driver (Mesa) and the viogpu3d kernel-mode driver — on a Windows build VM over SSH, and pull the verified, test-signed driver package back to this host. Use when asked to build the drivers, the KMD, the UMD, or a driver package.
argument-hint: <dev VM name | [user@]host[:port]> <arm64x|x64|all> <mesa-path> <kmd-path>
---

# Build the viogpu3d driver package

Arguments: `$ARGUMENTS`

1. The Windows build VM: the name of a VM made by
   `/utmapp-bot:provision-windows-dev-vm` (its user, key, and address or
   forwarded port come from its provisioning state), or an SSH destination
   `[user@]host[:port]` with key authentication.
2. `arch` — `arm64x`, `x64` or `all`. There is no 32-bit driver package: the
   WDK no longer builds x86 kernel-mode drivers. 32-bit applications are
   covered by the x86 UMD that `all` adds to the 64-bit packages.
3. `mesa-path` — the Mesa source tree (with the Neptune driver), as a path **on the VM**, e.g. `Z:\virtio-win-mesa`.
4. `kmd-path` — the `kvm-guest-drivers-windows` tree, as a path **on the VM**, e.g. `Z:\kvm-guest-drivers-windows`.

If any argument is missing, ask for it. The VM is assumed to have every build
tool installed and configured; this skill does not set the VM up.

## Run

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB dev up                 # the dev VM ($UB machine get dev_vm), under the UTM lease
bash "${CLAUDE_PLUGIN_ROOT}/scripts/build-drivers.sh" "$($UB machine get dev_vm)" <arch> '<mesa-path>' '<kmd-path>'
$UB dev down
```

A worktree for another revision works: create it under `~/Documents` (the
dev VM's `Z:`) and pass its `Z:\…` path. The script reads the revisions on
this Mac when the guest cannot (a worktree's `.git` points at a Mac path).

Quote the Windows paths so the shell keeps the backslashes. Run it in the
background and wait for it to finish: a first build compiles LLVM and can take
hours; later builds are incremental and take minutes. Options:

- `--guest-workdir DIR` — build directory on the VM (default
  `%USERPROFILE%\utmapp-bot`). It holds `build-mesa\` with its LLVM and Mesa
  build trees and `kmd\`, a mirror of the KMD source. Reuse the same directory
  every time; a new one means a full rebuild.
- `--out DIR` — where packages land on this host (default `~/.utmapp-bot/drivers`).

## What it does

`${CLAUDE_PLUGIN_ROOT}/scripts/build-drivers.sh` first clones the latest
[build-mesa](https://github.com/osy/build-mesa) into `~/.utmapp-bot/build-mesa/`
(or updates it to the latest commit), then uploads it and
`${CLAUDE_PLUGIN_ROOT}/scripts/guest/` to the work directory and runs `${CLAUDE_PLUGIN_ROOT}/scripts/guest/build-drivers.cmd` there, which:

1. Mirrors the KMD source to `kmd\` (building on an SSHFS-Win share can hang
   the share, so the KMD is never built in place).
2. Reads the driver version (`STAMPINF_VERSION`) from the KMD project. The UMD
   is stamped with it, because Windows only loads a UMD whose file version
   equals the INF `DriverVer`.
3. Builds the UMD with `build-mesa` (`build_arm64x.cmd` for `arm64x`,
   `build.cmd` otherwise).
4. Builds the Win10 KMD package with `build\build.bat`, pointing it at the UMD
   through `MESA_PREFIX` / `MESA_ARM64X_PREFIX` / `MESA_X86_PREFIX`.
   `viogpu.sln` maps viogpu3d's Win11 configuration onto Win10, so the Win10
   package serves both.
5. Test-signs with the KMD repo's `build\signAll.bat` (`VirtIOTestCert`).
6. Verifies each package with `${CLAUDE_PLUGIN_ROOT}/scripts/guest/verify-package.ps1` and fails the
   build unless the files, versions, INF references, signatures and catalog
   are all consistent.

| `arch`  | UMD builds          | Packages                                               |
|---------|---------------------|--------------------------------------------------------|
| arm64x  | arm64 + arm64ec     | `ARM64` (ARM64X UMD)                                   |
| x64     | x64                 | `amd64`                                                |
| all     | x86, x64, arm64x    | `amd64`, `ARM64`; both also carry the x86 UMD for WOW64 |

## Output

On success:

- `~/.utmapp-bot/drivers/<TargetArch>/` — one test-signed package per target
  (`viogpu3d.inf`/`.sys`/`.cat`, the Neptune UMD DLLs, `vulkan_virtio.dll`,
  `virtio_icd.json`, plus the repo's other viogpu components). This is the input
  for installing the driver on a test VM.
- `~/.utmapp-bot/drivers/build-info.json` — driver version, packages built by this run,
  and the Mesa/KMD source revisions.
- `~/.utmapp-bot/drivers/build-<arch>.log` — the full build log.

Report the driver version and the package directories to the user. To try
the package: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/utmbot install ~/.utmapp-bot/drivers/<TargetArch>`
on a freshly started test VM (`/utmapp-bot:run-gpu-test-vm`).

## When it fails

Find the first real error in the log, not the last line: search for
`FAILED:`, `error `, `fatal error` and `ERROR:`.

- If the cause is something missing on the VM (a compiler component, SDK or
  WDK piece, a tool), report exactly what is missing and stop. Do not install
  software.
- If it is an environment problem you can fix on the VM, fix it and rerun.
- `ERROR: package ... failed verification` lists what is inconsistent in the
  package; do not hand out a package that failed verification.
