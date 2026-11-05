---
name: install-xcode-vm
description: Install an older Xcode from a XIP into a provisioned macOS VM, with optional iOS simulator runtimes (iOS 15 and 16 by default), and snapshot it — for running UTM on iOS versions the host's current Xcode no longer has simulators for. Runtimes download unattended: iOS 16 and later through that Xcode (xcodebuild -downloadPlatform -buildVersion), iOS 15 and older from Apple's public simulator index. Use when asked to set up an Xcode VM, install Xcode in a VM, or get iOS 15/16 simulators.
argument-hint: "[VM] [--xip FILE] [--ios VER ...] [--ios-dmg FILE ...]"
---

# Install Xcode in a macOS VM

Arguments: `$ARGUMENTS`

The host's Xcode has only the latest iOS simulators (Xcode 27 dropped iOS 15
and 16). Its builds still target UTM's minimum iOS, so the older simulators
are what the VM provides: run the host's build there
(`/utmapp-bot:test-utm-ui`). The VM's own Xcode builds only code that still
compiles with its older SDK.

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB lock acquire --purpose "Xcode VM" --wait 3600
$UB mac xcode "macOS 15" --xip ~/Downloads/Xcode_26.6_Apple_silicon.xip --ios 15.5 --ios 16.4
$UB lock release
```

## Before running

- **The VM.** A VM from `/utmapp-bot:provision-macos-vm` on a macOS the Xcode
  supports (Xcode 26 needs macOS 15.6 or later: the `macOS 15` or `macOS 26`
  VM), provisioned with room for it (`--disk-gb 200 --memory 12288`). It
  checks the free space before copying.
- **The XIP.** From the user (`~/Downloads/Xcode_<version>_Apple_silicon.xip`
  by default); never download Xcode yourself (it needs their Apple ID).
- **Runtimes.** `--ios 15.5 --ios 16.4` by default when the user asks for
  "iOS 15 and 16"; use the versions they name. Nothing is downloaded by
  hand:
  1. iOS 16.0 and later: `xcodebuild -downloadPlatform iOS -buildVersion
     <version>` in the VM (Xcode 16.1 and later);
  2. iOS 15 and older, which Xcode does not offer: Apple's public simulator
     index (`devimages-cdn.apple.com`), downloaded once into
     `~/.utmapp-bot/cache/simruntimes` and installed from there.
  `--ios-dmg FILE` installs a runtime DMG already on this Mac (offline).

## What it does

1. Starts the VM and waits for SSH.
2. Copies the XIP in, expands it with `xip --expand` (which checks Apple's
   signature), moves it to `/Applications/Xcode-<version>.app`.
3. `xcode-select -s`, `xcodebuild -license accept`, `-runFirstLaunch`, and
   the iOS platform of that Xcode (`xcodebuild -downloadPlatform iOS`, about
   8 GB) and the Metal toolchain (`-downloadComponent MetalToolchain`):
   without them Xcode builds nothing for a simulator.
4. Installs each runtime: an old runtime DMG holds a package whose payload is
   the `.simruntime` bundle; it goes into the user's
   `~/Library/Developer/CoreSimulator/Profiles/Runtimes` (the package's own
   target is CoreSimulator's protected system folder). A runtime disk image
   goes in with `simctl runtime add`.
5. Shuts the VM down and snapshots it as `xcode-<version>` (labels: the
   Xcode version and runtimes), on top of `macos-baseline`.

It resumes: an installed Xcode is not copied again, and an installed
runtime is skipped.

## Output

The Xcode version, `xcrun simctl list runtimes` in the VM, the snapshot, and
any runtime that needs the user's download.
