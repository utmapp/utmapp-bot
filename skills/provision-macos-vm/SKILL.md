---
name: provision-macos-vm
description: Create a UTM macOS VM (Apple Virtualization backend) for testing UTM itself on a given macOS version — install from an IPSW (or have UTM download the latest one), get through Setup Assistant to a desktop with a known admin account, then Remote Login, automatic login, no display sleep, no screen saver or screen lock, passwordless sudo and the host's SSH key, shut down and snapshot as macos-baseline. macOS 27 guests are set up unattended through UTM's provisioning option; older ones (12 to 26) are driven through the VM's screen. Use when asked to provision, create or set up a macOS VM, "a VM for every macOS version", or a macOS VM to test UTM's UI, an Xcode VM, or an older macOS.
argument-hint: "<major version | IPSW path | latest> [--name NAME] [--disk-gb N] [--memory MiB]"
---

# Provision a macOS VM

Arguments: `$ARGUMENTS`

One command does all of it, and resumes at the stage that did not finish:

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB lock acquire --purpose "provision macOS <major>" --wait 3600
$UB mac provision "macOS <major>" --ipsw <IPSW>      # or --latest
$UB lock release
```

## Before running

- **A current UTM.** On the Apple backend, screen capture, keyboard and
  mouse input and the latest-IPSW download go through UTM's scripting. An
  older UTM fails at the first `mac screen` with "too old for this plugin";
  update UTM (or select a current build with `UTM_APP`). Only one UTM runs at
  a time: quit any other first.
- **The IPSW.** Pick it by major version from the IPSWs the user keeps
  (`~/Downloads/UniversalMac_<version>_<build>_Restore.ipsw` by default; ask
  where they are if not there). `--latest` leaves it to UTM: it takes the
  latest IPSW this Mac supports from Apple and downloads it during install
  (about 20 GB, kept in UTM's caches). Use `--latest` for the newest macOS,
  which has no IPSW in the folder.
- **Disk and memory.** Defaults: 4 cores, 8 GB, 96 GB disk (sparse). A VM
  that will get Xcode (`/utmapp-bot:install-xcode-vm`) needs `--disk-gb 200
  --memory 12288`.
- **Two at a time.** macOS allows two running macOS VMs per Mac; an install
  counts as one. Two `mac provision` runs can go in parallel.
- **Name.** `macOS <major>` unless the user wants another. The account is
  `utm`, password `utmutm` (`--user`, `--password`; macOS wants four
  characters or more).

## What it does

| Stage | How |
|---|---|
| created | `make new virtual machine` with the IPSW (its hardware model), or for the latest one |
| installed | UTM's `install`, polled with `query install` (30 to 60 min; downloads first for `--latest`) |
| setup | macOS 27 guest on a macOS 27 host: `start … provisioning` (account, automatic login, Remote Login), then wait for the desktop. Older: Setup Assistant, pane by pane, from the screen |
| baseline | Remote Login (in Terminal, the one step that needs the screen, unless it is on already), then over SSH: the host's key, `pmset` display/system sleep off, screen saver 0, `sysadminctl -screenLock off`, automatic login (`/etc/kcpassword`), passwordless sudo; registers the VM for `utmbot --vm` |
| snapshot | shut down, disk snapshot `macos-baseline` |

The Setup Assistant driver (`scripts/utmbot/macvm.py`, `PANES`) reads each
screen's text (Vision OCR on `capture screen`), finds the pane by its
title, and clicks or types its way on: language, country, accessibility,
data & privacy, Migration Assistant (Not Now), Apple ID (Set Up Later,
Skip), terms (Agree twice), the account form, Location Services (Don't
Use), analytics, Screen Time, Siri, appearance. Versions differ in which
panes they show and in what order; the table is matched pane by pane, so
order does not matter.

## When it stops

- **"Setup Assistant is stuck on this screen"**: a pane the table does not
  know. Read the screen (`$UB mac screen NAME` prints its text with
  coordinates; the PNG path is in the error). Add one line to `PANES` that
  recognizes it by its title and says what to click, rerun `mac provision`.
  Never click through a pane you have not read (an erase or a "Migrate"
  button is a click away).
- **Remote Login does not come up**: look at the screen. Terminal shows the
  output of `launchctl`. A password prompt that is still there means the
  password typed did not match the account.
- **Install fails**: the IPSW does not match this Mac (too old for the
  host's macOS, or not an Apple silicon IPSW). `query install` returns
  Virtualization's error.

## Driving the VM by hand

Any prompt in the guest (a permission dialog, an alert) can be answered
the same way the provisioning does:

```sh
$UB mac screen "macOS 14"                 # text on screen, x,y of each box centre
$UB mac click "macOS 14" "Allow"          # click a text; --at X Y for a position
$UB mac key "macOS 14" cmd+space return   # keys and chords
$UB mac type "macOS 14" "some text"
$UB mac wait "macOS 14" "Done" --timeout 120
```

Coordinates are in the pixels of the capture (the window's size decides
the guest's resolution: always capture first).

## Output

The VM's name, macOS version (`$UB --vm NAME guest sw_vers`), its address
and user, the snapshot, and anything the run had to work around.
