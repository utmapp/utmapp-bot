---
name: test-utm-ui
description: Test a UTM UI change on several macOS and iOS versions, as UTM's contribution guidelines ask — build UTM with the change, run it in macOS VMs (12 through the newest) and in iOS simulators (the host's newest iOS, and older iOS in a macOS VM with an older Xcode), and screenshot each; answer any prompt in a VM through its screen; optionally attach LLDB to UTM in a VM. Use when asked to test a UI change across versions, take screenshots of UTM on macOS N or iOS N, check a layout on older systems, or debug UTM on an older macOS.
argument-hint: "[UTM checkout] [--macos 12,13,...] [--ios 15.5,16.4,27.0]"
---

# Test a UTM UI change across versions

Request: `$ARGUMENTS`

Debug UTM on this Mac (or the host's simulator) as usual. Use VMs only for
what the host cannot run: another macOS version, or an iOS version the host's
Xcode has no simulator for.

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB lock acquire --purpose "UTM UI test" --wait 3600
```

## 1. Build what you test

- **macOS, for the VMs:** ad-hoc signed (a dev build's VM entitlement
  profiles tie it to this Mac, and it will not launch in a VM):
  `scripts/host/build-utm.sh <checkout> --unsigned` → app in
  `~/.utmapp-bot/unsigned/DerivedData/Build/Products/Debug/UTM.app`. UTM
  supports macOS 12 and later.
- **iOS simulator, on the host:** the checkout needs
  `sysroot-ios_simulator-arm64` (UTM's `Sysroot-ios_simulator-arm64` CI
  artifact: `gh run download <run> -R utmapp/UTM -n Sysroot-ios_simulator-arm64`,
  extracted into the checkout). Then:
  `xcodebuild -project UTM.xcodeproj -scheme iOS -destination 'generic/platform=iOS Simulator' -configuration Debug -derivedDataPath <dd> ARCHS=arm64 ONLY_ACTIVE_ARCH=NO CODE_SIGN_IDENTITY=- CODE_SIGNING_REQUIRED=NO build`
- **iOS simulator, for an older iOS:** the same host build. This Mac's Xcode
  still targets UTM's minimum iOS (15); it only lacks the old simulators,
  which run in the Xcode VM (`/utmapp-bot:install-xcode-vm`). `ui sim --vm`
  copies a host app into the VM. UTM's main branch needs the newest SDK, so
  building it with the VM's older Xcode usually fails; `$UB ui build-ios VM
  --checkout <checkout>` is for code that still builds there.

Keep a copy of each app (`ditto`), so a later build does not replace what
you are testing.

## 2. Screenshot it

```sh
$UB ui mac "macOS 12" --app <macOS UTM.app>       # each macOS VM
$UB ui sim --app <sim UTM.app> --runtime "iOS 27.0"                  # host simulator
$UB ui sim --vm "macOS 15" --app <app path in VM> --runtime "iOS 15.5"  # older iOS
```

- `ui mac` copies the app into the VM, starts it, closes the first-launch
  What's New sheet, waits for the welcome view (`--wait TEXT` for another
  screen) and saves the VM's display. `ui sim` installs and launches it on an
  iPad simulator (the split view shows the welcome pane; `--family iPhone`
  or `--device NAME` for others), marks What's New seen, and saves the
  simulator's screen. Screenshots land in `~/.utmapp-bot/ui/<vm|host>/`.
- At most two macOS VMs run at once: shut each down after its screenshot
  (`$UB mac down VM`).
- A runtime's first boot in a VM takes minutes (it builds its shared cache):
  a blank app or a hung `simctl` then is load, not the app. A simulator that
  stays wedged: `xcrun simctl shutdown all` and kill
  `com.apple.CoreSimulator.CoreSimulatorService` in the VM, then retry.
- **An app that quits on launch** on an older iOS (the screenshot shows the
  home screen) crashed: read `~/Library/Logs/DiagnosticReports/UTM-*.ips` in
  the VM (or on this Mac for a host simulator). New SwiftUI used without a
  working availability check is the usual cause on iOS 15 and 16.
- **Navigate before the shot** with the screen tools of
  `/utmapp-bot:provision-macos-vm` ("Driving the VM by hand"): `mac click VM
  TEXT`, `mac key VM cmd+n`, `mac type`, `mac wait`, then `mac screen VM --out
  FILE`. Any prompt in a VM is answered the same way: read it first, never
  click through one you have not read.
- **Look at every screenshot** before reporting: the change must be there
  and nothing else may be broken (clipped text, overlapping controls, wrong
  colours in dark mode). Compare with a screenshot of the base build when a
  layout looks off.

## 3. Debug UTM in a VM (when only that macOS shows the bug)

This Mac's LLDB attaches to UTM in the VM through this Mac's `debugserver`,
copied into the guest (macOS 13 and later). A Debug build has
`get-task-allow`:

```sh
$UB ui lldb "macOS 14" -o "thread list" -o "bt all"     # runs, detaches, prints
$UB ui lldb "macOS 14"                                  # prints the lldb line to connect yourself
```

Symbols come from the build on this Mac (the same app). On macOS 12, which
this Mac's debugserver does not run on, debug only if the user wants it
there: `$UB mac clt "macOS 12"` installs the Command Line Tools in the guest
(softwareupdate, no dialog; not part of provisioning), and `ui lldb` then
runs the guest's own lldb.

## Output

A table: version (macOS N in a VM, iOS N simulator on the host or in which
VM), build, screenshot path, and what it shows. Attach or show the
screenshots the user asked for.
