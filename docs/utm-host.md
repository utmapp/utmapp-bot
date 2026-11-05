# The UTM app and the Mac: what every skill assumes

Read this when a UTM call fails or hangs, when choosing which UTM to run, or
when a task touches networking or kernel debugging. `utmbot app` reports the
app the harness drives, its version, whether it has the vmnet entitlement,
and which scripting features it has.

## Which UTM

The harness and the provisioning scripts drive one app, chosen in this order:

1. `$UTM_APP`;
2. the last build of `/utmapp-bot:build-utm` (`~/.utmapp-bot/utm-app`);
3. `/Applications/UTM.app`.

Only one UTM may run at a time. The release app and a dev build share the
bundle id `com.utmapp.UTM`, so with both running AppleScript reaches
whichever one macOS picks. The harness refuses to start a VM while another
UTM runs; quit it rather than working around this. Launch UTM with `open`,
never through an AppleScript `tell`: a UTM launched that way was seen to
never start a VM.

Guest-driver work can use the release app. Host-side changes (virglrenderer,
the backend shims, QEMU, UTM itself) need a dev build.

**The plugin needs a current UTM.** It uses scripting that older releases
lack (the table below). The harness, the provisioning scripts and `db-setup`
check the app's dictionary and stop (db-setup warns), naming what is
missing: update UTM. `guest size` of an existing drive has no term of its
own to look for; an older UTM fails at it in the Linux `vm` stage.

| Scripting feature | Used for |
|---|---|
| `start … environment` | `vm up --env`, host debug knobs |
| `capture screen` | `shot`, `ingame` (the guest framebuffer: no window, no Screen Recording) |
| `install guest tools` | the released driver (`install --released`), provisioning |
| `debug log`, `tpm`, `tso`, `sound devices`, the RTC in the configuration | the Debug Log toggle, provisioning |
| `guest size` of an existing drive | Linux provisioning (grow the cloud image) |
| `file` of a virtual machine | the VM bundle's path |

## Signing and the vmnet entitlement

Shared, host-only and bridged networks use Apple's vmnet, which needs the
restricted `com.apple.vm.networking` entitlement, and with it a provisioning
profile from a paid developer account with Apple's VM entitlements. The
release app has it on every Mac. A build from a checkout without
`CodeSigning.xcconfig` (ad-hoc signed) does not; `build-utm.sh --unsigned`
makes such a build on purpose. `verify-app.sh` and `utmbot app` tell which
one you have.

Without the entitlement:

- `vm up` switches the VM's NICs to emulated (QEMU's user network) for that
  boot, forwards a 127.0.0.1 port to the guest's SSH, and puts the modes back
  at `vm down` (`UP … net=emulated`). Everything that only needs SSH and the
  internet works: install, verify, test, bench, frames, profiling. The guest
  reaches the host at 10.0.2.2.
- A VM keeps the network it was made with. Make Windows VMs with the
  release app (`UTM_APP=/Applications/UTM.app`, the dev build quit): they get
  the shared and kdnet NICs, and the ad-hoc build still runs them (emulated
  per boot). The provisioning scripts stop and say so when the selected UTM
  lacks the entitlement and the release app has it. `--emulated-net` (or a
  Mac with no entitled UTM at all, or the Linux VM, which needs dev-build
  scripting) gives the VM one emulated NIC with a fixed forward (`SSH_PORT`
  in its `state.env`) and no kdnet NIC, for good.
- **Guests cannot reach each other**, so kdnet kernel debugging (a test VM
  and its debugger dev VM) does not work, and neither does anything else
  that needs a VM-to-VM or host-only network. Kernel debugging a guest-only
  change works on the release app; kernel debugging with a host-side change
  needs a dev build signed with the VM entitlement profiles.

When a task needs vmnet (kernel debugging, a bug in shared or host-only
networking, bridged networking, a test of the network setup itself) and the
app lacks the entitlement, do not work around it: tell the user that it
needs a UTM signed with the VM entitlement (the release app, if the change
under test is guest-only, or a dev build with their profiles), and why.
`vm up --require-vmnet` stops instead of switching.

Two things unsigned builds need from the harness:

- New VMs get Shared networking, which an unsigned build cannot start
  ("cannot create vmnet interface"). The provisioning scripts switch a VM
  they create with such a build to Emulated VLAN with an SSH port forward.
- **No 3D at all** otherwise: an unsigned UTM gives virglrenderer no app
  group, the sandbox denies its shared memory, and the guest's GPU
  resources fail ("RES_INFO blob not created", D3D device creation E_FAIL,
  no render workers). `vm up` passes `APP_SANDBOX_GROUP_ID` through the
  start environment (the group the entitlements claim, so harmless where
  UTM passes it itself). A guest D3D failure on an unsigned build that a
  signed build does not have is this until shown otherwise.

## macOS permissions and prompts

Every one of these shows up as a hang or a timeout, not an error:

- **Automation** (the terminal controlling UTM). macOS asks the first time
  a terminal app scripts UTM (seen to carry over to rebuilt ad-hoc builds of
  the same bundle id; a reset or a new terminal app asks again). The harness
  checks first and stops with "macOS needs the user's permission"; ask the
  user to click Allow. On a locked screen nobody can, and every AppleScript
  call would wait minutes (`AppleEvent timed out`).
- **Keychain.** Signing a dev build needs the login keychain unlocked, and
  it can stay locked after the screen is unlocked (after sleep, or in an
  SSH session). codesign then fails with `errSecInternalComponent`; the
  user can run `! security unlock-keychain ~/Library/Keychains/login.keychain-db`
  (it asks for their password). An `--unsigned` build needs no keychain.
- **Screen Recording** for `frames`, which samples the VM window.
- **Folder access.** A UTM copy inside Documents, Downloads or Desktop
  waits on a privacy prompt and the VM stays "starting". Keep app copies
  elsewhere in the home directory.
- **Remote Login** (System Settings > General > Sharing) for the dev VM's
  `Z:` share of the Mac.

## Host conditions that change results

A background UTM is App Napped (its vCPUs run several times slower), a
locked screen prevents un-napping it, and a sleeping display stops the
window from drawing. `utmbot check-host` reports these; `bench` marks runs
taken under them as not quotable.
