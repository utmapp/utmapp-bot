#!/usr/bin/env bash
#
# Create a UTM virtual machine for Windows kernel driver development, modelled
# on the "Windows Development" VM: install Windows 11 unattended from an ISO,
# then set it up over SSH with Visual Studio 2022, the WDK and everything
# build-viogpu-driver needs.  It is also the kernel debugger for test VMs
# (provision-windows-test.sh): its second NIC has a static address on the
# kdnet network.
#
# Stages (each can be resumed with --from, or run alone with --only):
#
#   media     no-prompt boot ISO + configuration ISO (Autounattend.xml, setup
#             scripts, this host's SSH public key)
#   vm        create the VM in UTM, second NIC on the kdnet network; UTM
#             attaches its guest tools (Install Windows Guest Tools)
#   install   boot it, install Windows unattended and wait for SSH
#   system    UAC, firewall, SmartScreen, Explorer, policies; Defender off
#             through one Safe Mode boot
#   kdnet     static kdnet address on the second NIC (also moves an older VM's
#             second NIC onto the kdnet network first)
#   tools     7-Zip, Git, CMake, Python, WinFsp, SSHFS-Win
#   sshfs     guest key <-> host authorized_keys, Z: = host ~/Documents
#   vs        Visual Studio 2022 Community (scripts/provision/guest/vs2022.vsconfig),
#             then meson, mako, packaging, pyyaml (pyyaml may need the compiler)
#   wdk       Windows Driver Kit 10.0.26100 (with the debuggers)
#   finalize  eject the ISOs, restart, add the VM to ~/.ssh/known_hosts
#   verify    check every build prerequisite

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=provision/common.sh
. "$REPO/scripts/provision/common.sh"

ROLE=dev
STAGES=(media vm install system kdnet tools sshfs vs wdk finalize verify)
NAME_PREFIX="Windows Development"
NAME_FIRST=2
VM_MEMORY=16384
VM_DISK=131072
NIC2=virtio-net-pci

usage() {
    cat <<'EOF'
usage: provision-windows-dev.sh [options] <windows-11-arm64.iso>
       provision-windows-dev.sh --name NAME --from STAGE
       provision-windows-dev.sh --name NAME --only STAGE

  <windows-11-arm64.iso>  Windows 11 ARM64 installation ISO (only needed for
                          the media stage)

options:
  --name NAME       VM name in UTM (default: "Windows Development <n>", the
                    first one that is free)
  --user USER       Windows account (default: UTM)
  --password PASS   its password (default: utm)
  --ssh-key FILE    public key the host logs in with (default: the first of
                    ~/.ssh/id_ed25519.pub, id_ecdsa.pub, id_rsa.pub)
  --disk GIB        size of the system disk in GiB (default: 128); the
                    qcow2 grows only as it fills, snapshots included
  --emulated-net    one emulated NIC with an SSH port forward instead of the
                    shared (and kdnet) network: what a UTM without the vmnet
                    entitlement gets anyway; no kernel debugging
  --from STAGE      resume an earlier run of the VM given by --name at STAGE
  --only STAGE      run just STAGE on the VM given by --name (e.g. --only kdnet
                    to make an existing dev VM a kdnet debugger)

stages: media vm install system kdnet tools sshfs vs wdk finalize verify
EOF
}

# *** stages ***

stage_kdnet() {
    if ! has_kdnet; then
        log "No kdnet network: this VM was made for a UTM without vmnet entitlement, so it cannot kernel-debug other VMs"
        return 0
    fi
    if ! kdnet_network_pinned; then
        # A VM from before the kdnet network existed.
        shutdown_guest
        pin_kdnet_network
        boot_guest
    fi
    local mac2; mac2="$(utm nic "$NAME" 1 | cut -f1)"
    [ -n "$mac2" ] || die "cannot read the second NIC's MAC address"
    guest_step kdnet -Mac "$mac2" -Address "$KDNET_IP"
}

stage_tools() {
    upload 7z2605-arm64.exe Git-2.56.0.4-arm64.exe cmake-4.4.5-windows-arm64.msi python-3.15.2-arm64.exe \
        winfsp-2.2.26286.msi sshfs-win-3.7.21011-x64.msi
    guest_step tools
}

stage_sshfs() {
    guest_step sshfs -HostUser "$(id -un)"
    local key; key="$(sed -n 's/^GUEST_PUBKEY //p' "$STATE/step-sshfs.log" | tail -1)"
    [ -n "$key" ] || die "the guest did not report its public key"
    mkdir -p "$HOME/.ssh"
    touch "$HOME/.ssh/authorized_keys"
    if ! grep -qxF "$key" "$HOME/.ssh/authorized_keys"; then
        log "Adding the guest's key to ~/.ssh/authorized_keys"
        printf '%s\n' "$key" >> "$HOME/.ssh/authorized_keys"
    fi
    if ! nc -z -G 3 127.0.0.1 22 >/dev/null 2>&1; then
        echo "WARNING: Remote Login (SSH) is off on this Mac; Z: needs it (System Settings > General > Sharing)" >&2
    fi
}

stage_vs() {
    upload vs_community.exe
    log "Installing Visual Studio 2022 (30-90 minutes)"
    guest_step vs
    guest_step python
}

stage_wdk() {
    upload wdksetup.exe
    guest_step wdk
}

stage_finalize() {
    finalize_vm true
    # Give the autologon session time to reconnect Z:.
    sleep 30
}

stage_verify() {
    if guest_step verify -Role dev ${KDNET_IP:+-KdnetAddress "$KDNET_IP"}; then
        log "Done: '$NAME' is ready at $WIN_USER@$IP${SSH_PORT:+ port $SSH_PORT} (${KDNET_IP:+kdnet debugger address $KDNET_IP}${SSH_PORT:+no kdnet: emulated network})"
    else
        die "verification failed; see $STATE/step-verify.log"
    fi
}

provision_main "$@"
