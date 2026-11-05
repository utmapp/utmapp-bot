---
name: build-utm
description: Build a development UTM.app — UTM itself from a checkout or worktree (a branch, a pull request, a fix), or with host-side changes: rebuild one dependency (virglrenderer, QEMU, d3dmetal-native, DXMT, SPICE, …) from a checkout or worktree for both slices of the universal app (or arm64 only from the dependency tree), repackage it into the framework the app embeds, rebuild UTM, and prove the change is in the app before testing it. Use when a host-side change (render server, backend shim, QEMU device model, UTM itself) has to be tested in UTM, or when asked to "build UTM", "rebuild virglrenderer into UTM" or "check what's in this UTM.app".
argument-hint: "<UTM checkout> [project ...]"
---

# Build the host side into UTM

Arguments: `$ARGUMENTS`

1. `UTM checkout` — a clone of `https://github.com/utmapp/UTM`.
2. `project` — dependency trees to rebuild (names under
   `build-macOS-arm64/`, or unique prefixes: `virglrenderer`, `qemu`, `dxmt`,
   `d3dmetal-native`, `spice-gtk`, …). None means only UTM itself.

## Before running

- **Xcode** that can build the checkout. Check the README of the UTM branch;
  a newer UTM may need a newer Xcode than this Mac has. If it cannot build
  here, see "Without Xcode" below.
- **A sysroot.** Download a sysroot artifact from the GitHub Actions run
  of the commit the checkout is based on, and extract it at the checkout
  root. With `gh`:

  ```sh
  base=$(git -C <checkout> merge-base HEAD origin/main)
  run=$(gh run list -R utmapp/UTM -w build.yml -c "$base" -L 1 --json databaseId -q '.[0].databaseId')
  gh run download "$run" -R utmapp/UTM -n Sysroot-macos-universal -D /tmp/sysroot
  tar -xzf /tmp/sysroot/sysroot.tgz -C <checkout>     # sysroot-macOS-arm64_x86_64/
  ```

  The universal one from CI already has D3DMetal in it. Which artifacts
  there are:
  - `Sysroot-macos-universal` → `sysroot-macOS-arm64_x86_64`. Needed for
    D3DMetal: its render server runs x86_64 under Rosetta. Build with
    `--universal`.
  - `Sysroot-macOS-arm64` → `sysroot-macOS-arm64`. arm64 only: DXMT and
    Venus work, D3DMetal does not, and UTM's default backend still picks
    D3DMetal, so test with `--backend dxmt` (`utmbot vm up` refuses the
    other combination).
- **Code signing: with or without the VM entitlements.** Check whether the
  checkout has a `CodeSigning.xcconfig`.
  - **Without one** (most machines) the build is ad-hoc signed and works,
    except that it lacks `com.apple.vm.networking`: no shared, host-only or
    bridged (vmnet) networks. The harness copes: `utmbot vm up` runs VMs
    with emulated networking and an SSH port forward for each boot, and
    provisioning makes VMs that way. What it cannot give is a network
    between VMs, so there is no kdnet kernel debugging. If the user's task
    needs vmnet, say so instead of working around it
    (`${CLAUDE_PLUGIN_ROOT}/docs/utm-host.md`). Unsigned UTM builds
    currently have no guest 3D unless `APP_SANDBOX_GROUP_ID` reaches
    virglrenderer; `utmbot vm up` passes it (see docs/utm-host.md).
  - **With the VM entitlement profiles** (a paid account Apple granted the
    entitlement): install the development certificate and the profiles for
    `<prefix>.UTM`, `<prefix>.QEMUHelper` and `<prefix>.QEMULauncher` into
    `~/Library/Developer/Xcode/UserData/Provisioning Profiles/`, and write
    `CodeSigning.xcconfig` from `CodeSigning.xcconfig.sample` (team,
    `DEVELOPER_ACCOUNT_VM_ACCESS = YES`, the three profile UUIDs). No valid
    identity in `security find-identity -v -p codesigning` means the Apple
    WWDR G3 intermediate certificate is missing. After any signing change do
    one `--clean` build: an incremental build keeps a stale ad-hoc
    `QEMULauncher.app` inside QEMUHelper. Signing needs the login keychain
    unlocked (`errSecInternalComponent` otherwise).
  - `--unsigned` builds the ad-hoc flavor (not recorded: drive it with `UTM_APP`) even where a
    `CodeSigning.xcconfig` exists, into its own derived data, to test what
    other developers get.
- **To change a dependency** you also need its build tree. Those exist only
  after one full `./scripts/build_dependencies.sh -p macos -a arm64` (hours;
  set `NCPU=4` if it dies of memory pressure). Never run that again
  afterwards: it deletes the build trees, and any uncommitted edit in them.
  Keep edits to dependency trees as patches; the trees are not git
  checkouts you can trust.
- **D3DMetal** is not in any sysroot (CI adds it to the universal one when
  it has the distribution URL). Install it with UTM's
  `scripts/install_d3dmetal.sh <sysroot> <GPTK dmg>`. Without the DMG, copy
  the already-processed `D3DMetal.framework` out of a release
  `/Applications/UTM.app/Contents/Frameworks/`. It only works with an x86_64
  render server, i.e. a universal sysroot.

## Run

**Which path.** A change that has to run on D3DMetal (its render workers run
x86_64 under Rosetta), or any change to a dependency the universal app
embeds, takes the universal path. `rebuild-dep.py` is for arm64-only
testing on DXMT or Venus.

```sh
H=${CLAUDE_PLUGIN_ROOT}/scripts/host

# universal (both slices; D3DMetal and DXMT): build from a checkout or worktree
git -C ~/Documents/virglrenderer worktree add --detach <wt> <rev>   # your change goes in <wt>
python3 $H/rebuild-universal.py --utm <checkout> --src <wt> virglrenderer
bash    $H/build-utm.sh   <checkout> --universal
bash    $H/verify-app.sh  "$(cat ~/.utmapp-bot/utm-app)" --marker '<unique string in your change>'
# …test…, then put the app back:
python3 $H/rebuild-universal.py --utm <checkout> --restore
bash    $H/build-utm.sh   <checkout> --universal
bash    $H/verify-app.sh  "$(cat ~/.utmapp-bot/utm-app)" --absent '<the same string>'

# arm64 only (DXMT, Venus): the dependency tree's own copy
python3 $H/rebuild-dep.py --utm <checkout> --list           # trees and their build dirs
python3 $H/rebuild-dep.py --utm <checkout> virglrenderer    # build + install + fixup
bash    $H/build-utm.sh   <checkout> [--clean] [--unsigned] # Debug; records the app
```

- `rebuild-universal.py` builds `--src` for arm64 and for x86_64 in
  `~/.utmapp-bot/host-builds/`, with the dependency tree's meson options.
  The x86_64 slice links against the universal sysroot's fat libraries and
  D3DMetal. It merges each library and executable with `lipo` into the
  universal sysroot, backs up the originals for `--restore`, and runs
  `fixup.sh`. Wrap-pinned subprojects come from a sibling checkout at the
  pinned revision (`~/Documents/directx-native`), never a download. Measured
  for virglrenderer: about 30 s for both slices, plus 15 s for the app.
- **The dependency tree's copy is not the app's revision.** The tree under
  `build-macOS-arm64/` is at whatever its last full dependency build had,
  usually older than the universal sysroot from CI. Build your change on
  the revision the app carries: check it with the marker of a recent commit,
  or the sysroot's date against the checkout's log.

Then test with `/utmapp-bot:run-gpu-test-vm`. `utmbot` drives the app that
`build-utm.sh` recorded in `~/.utmapp-bot/utm-app` unless `UTM_APP` is set.
`UTM_APP` overrides that record, and dispatched sessions have it set to the
release app, so name the dev app explicitly there:
`UTM_APP="$(cat ~/.utmapp-bot/utm-app)"`. Pass it as the first argument of
`verify-app.sh` too.

The first boot after UTM launches often writes only the debug log's header.
`hostlog` says so; boot again before concluding that a line is missing.
**Quit any other UTM first**: the release and dev apps share a bundle id.
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/utmbot app` confirms which app runs,
its vmnet entitlement and its scripting features.

## What it does

- `rebuild-dep.py` runs ninja and `meson install` (or `make install`) in
  the tree's existing build directory. It then gives the libraries the tree
  installed to UTM's own `scripts/fixup.sh`, which wraps each dylib as
  `Frameworks/<name>.framework` and rewrites install names and imports.
  Executables in `libexec/` get their imports rewritten in place, e.g.
  `virgl_render_server`, which becomes QEMURenderServer.app.
- `build-utm.sh` runs an incremental Debug build (Debug keeps
  `get-task-allow`, so LLDB can attach to QEMULauncher and the render-server
  workers). It judges success by the `** BUILD SUCCEEDED **` line: a failed
  build leaves the last good app on disk.
- `verify-app.sh` checks:
  - framework build times (spot a stale one);
  - your `--marker` strings in the embedded binaries;
  - QEMURenderServer slices: arm64 for Venus/DXMT, x86_64 for D3DMetal under
    Rosetta;
  - whether D3DMetal is embedded;
  - each backend slice's `*_native_abi_version` export (virglrenderer
    refuses a backend whose ABI version differs);
  - that every embedded framework is the sysroot's (same Mach-O UUID): a
    dependency rebuilt after the last `build-utm.sh` is not in the app yet;
  - whether QEMULauncher has `com.apple.vm.networking` (a note, not a
    failure: see code signing above);
  - no absolute-path imports;
  - the signature.

## Traps

- **Testing a stale framework and concluding "no effect"** is the most
  common way to lose a day. Always `verify-app.sh --marker`.
- **Universal pieces.** The render server and the backend shims carry an
  x86_64 slice (D3DMetal runs under Rosetta). Build them with
  `rebuild-universal.py`. After a d3dmetal-native or virglrenderer change,
  check that **both** slices carry it (`lipo -archs` plus the marker), and
  confirm it at runtime from a worker that `utmbot procs` shows as x86_64.
  An arm64-only rebuild leaves the D3DMetal path on old code.
- **Which sysroot gets embedded.** `rebuild-dep.py` installs into
  `sysroot-macOS-arm64` (the platform of the build trees), and an
  arm64-only `build-utm.sh` embeds it. A `--universal` build embeds
  `sysroot-macOS-arm64_x86_64` instead, without the rebuilt dependency;
  `build-utm.sh` warns when that happens. Test an arm64 rebuild on DXMT or
  Venus with an arm64-only build.
- **ABI changes between virglrenderer and the shims** must ship together:
  the shared headers carry an ABI version that both sides check. Rebuild
  every consumer, and restart the VM.
- **Never `cp` over a binary that is running** (a QEMU or render server in
  use). macOS kills the process, or the next launch, with a code-signing
  SIGKILL. Write a new file and rename it into place, and only while no VM
  runs.
- **Host libraries load when the VM starts.** Rebuilding while a VM runs
  changes nothing until `vm down` + `vm up`.
- **Swift packages** (CocoaSpice, QEMUKit) come from `Package.resolved`, not
  sibling checkouts. For a quick test, edit the checkout under
  `DerivedData/…/SourcePackages/checkouts/<pkg>` (it is read-only:
  `chmod u+w`), and revert after.

## Without Xcode (test a host library in a release UTM)

Copy the app and replace the framework in the copy:

```sh
ditto /Applications/UTM.app ~/utm-test/UTM.app && xattr -cr ~/utm-test/UTM.app
```

1. Replace `Contents/Frameworks/<name>.framework/Versions/A/<name>`.
2. Re-sign that framework, then the app, with
   `codesign -f -s "<your signing identity>" --timestamp=none`. For the
   app, also pass
   `--preserve-metadata=entitlements,requirements,flags,runtime`.
3. Run with `UTM_APP=~/utm-test/UTM.app`.

Keep the copy **outside Documents, Downloads and Desktop**. In those
folders UTM waits forever on a privacy prompt nobody sees, and the VM stays
"starting". A re-signed x86_64 library can hang dyld when loaded from
`/tmp`, so keep re-signed binaries under the home directory.

## When it fails

- `rebuild-dep.py`: "no configured build directory" means the full
  dependency build never ran for that tree.
- `build-utm.sh` errors about a missing resource or sysroot: the sysroot
  artifact does not match the checkout's branch. Download a matching one.
- `build-utm.sh` FAILED with only "The following build commands failed":
  signing (no valid identity, a locked keychain — `errSecInternalComponent`,
  which the script points out — a profile that does not match the bundle id
  or lacks an entitlement) or a package plugin; the log has the details.
- A VM fails to start with "cannot create vmnet interface" although the app
  should have the entitlement: the launcher copy inside QEMUHelper is stale;
  build `--clean`. On an ad-hoc app this is expected for a VM started
  outside the harness; `utmbot vm up` handles it.
- After a successful build `utmbot` still drives the old app: another UTM is
  running, or `UTM_APP` points elsewhere.
