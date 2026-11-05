#!/usr/bin/env bash
#
# Create a UTM virtual machine to test the VirtIO GPU stack from Linux:
# Ubuntu 26.04 arm64 with GNOME, Venus Vulkan, and Windows D3D programs
# through Wine under FEX-Emu with Neptune's D3D11/12 DLLs (or DXVK, or
# wined3d) - the setup of the Linux test VM the 3DMark results so far came
# from, rebuilt from public sources.
#
# Stages (each can be resumed with --from, or run alone with --only):
#
#   media     Ubuntu's arm64 cloud image (checked against Canonical's
#             SHA256SUMS) and a cloud-init seed ISO: the user, the host's SSH
#             key, passwordless sudo, ubuntu-desktop-minimal, GDM autologin
#   vm        create the VM in UTM from the image: VirtIO disk grown to the
#             disk size, the seed as a USB drive, shared network,
#             virtio-gpu-gl (Venus), TSO; register it with utmbot
#   install   boot it and wait for cloud-init to finish (desktop install and
#             one restart, 15-30 minutes)
#   session   GNOME without idle blank, lock or suspend; perf counters
#   fex       FEX-Emu and its x86_64 Ubuntu 24.04 rootfs
#   wine      Wine 10.9 staging-tkg (x86_64)
#   tools     cross compilers and build dependencies
#   dxvk      DXVK 2.6.1
#   mesa      Neptune's PE DLLs from osy/virtio-win-mesa (or --mesa-bundle)
#   nptunix   Neptune's x86_64 Wine unixlib
#   prefix    the Wine prefix, the 3DMark definitions, Unigine Heaven and
#             GravityMark (x64, in the prefix), and 3DMark (Fire Strike, Time
#             Spy) from --3dmark DIR or ~/.utmapp-bot/cache/3dmark, activated
#             with $UTMBOT_3DMARK_KEY or ~/.utmapp-bot/3dmark.key (no key: skipped)
#   verify    check all of the above

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=provision/common.sh
. "$REPO/scripts/provision/common.sh"

ROLE=linux
STAGES=(media vm install session fex wine tools dxvk mesa nptunix prefix verify)
NAME_PREFIX="Ubuntu Test"
NAME_FIRST=1
DEFAULT_USER=ubuntu
DEFAULT_PASSWORD=ubuntu
VM_MEMORY=16384
VM_DISK=204800
UBUNTU=26.04
IMAGE=ubuntu-$UBUNTU-server-cloudimg-arm64.img
IMAGE_BASE=https://cloud-images.ubuntu.com/releases/$UBUNTU/release

usage() {
    cat <<'EOF'
usage: provision-linux-test.sh [options]
       provision-linux-test.sh --name NAME --from STAGE
       provision-linux-test.sh --name NAME --only STAGE

options:
  --name NAME          VM name in UTM (default: "Ubuntu Test <n>", the first free)
  --user USER          Linux account (default: ubuntu)
  --password PASS      its password, for the console (default: ubuntu)
  --ssh-key FILE       public key the host logs in with (default: the first of
                       ~/.ssh/id_ed25519.pub, id_ecdsa.pub, id_rsa.pub)
  --mesa-bundle FILE   a git bundle with a neptune branch to build instead of
                       the public one (e.g. unpushed commits from a dev VM)
  --3dmark DIR         3DMark for the prefix stage: data_x64.msi (+
                       Futuremark_SystemInfo.msi), optionally dlc/ (tests).
                       Default: ~/.utmapp-bot/cache/3dmark
  --disk GIB           size of the system disk in GiB (default: 200); the
                       qcow2 grows only as it fills
  --emulated-net       one emulated NIC with an SSH port forward instead of the
                       shared network: what a UTM without the vmnet
                       entitlement gets anyway
  --from STAGE / --only STAGE   resume, or run one stage, of the VM --name

stages: media vm install session fex wine tools dxvk mesa nptunix prefix verify
EOF
}

# *** stages ***

stage_media() {
    local sums sha
    sums="$(curl -fsSL "$IMAGE_BASE/SHA256SUMS")" || die "cannot read $IMAGE_BASE/SHA256SUMS"
    sha="$(printf '%s\n' "$sums" | awk -v f="*$IMAGE" '$2 == f { print $1 }')"
    [ -n "$sha" ] || die "$IMAGE is not in $IMAGE_BASE/SHA256SUMS"
    download "$IMAGE_BASE/$IMAGE" "$CACHE/$IMAGE" "$sha"
    local seed="$STATE/seed" hash
    rm -rf "$seed"; mkdir -p "$seed"
    hash="$(openssl passwd -6 "$PASSWORD")"
    sed -e "s|@USER@|$WIN_USER|g" -e "s|@HOSTNAME@|$(slug "$NAME")|g" \
        -e "s|@PASSWORD_HASH@|$hash|g" -e "s|@PUBKEY@|$(cat "$PUBKEY")|g" \
        "$PROV/linux/user-data.in" > "$seed/user-data"
    printf 'instance-id: %s\nlocal-hostname: %s\n' "$(uuidgen)" "$(slug "$NAME")" > "$seed/meta-data"
    # DHCP by MAC, not by systemd-networkd's DUID: the host finds the VM's
    # address by its MAC in the vmnet leases
    printf 'version: 2\nethernets:\n  primary:\n    match:\n      macaddress: "%s"\n    dhcp4: true\n    dhcp-identifier: mac\n' \
        "$(printf '%s' "$MAC" | tr '[:upper:]' '[:lower:]')" > "$seed/network-config"
    rm -f "$STATE/seed.iso"
    # cloud-init's NoCloud source: a volume labelled CIDATA
    hdiutil makehybrid -quiet -iso -joliet -default-volume-name CIDATA -o "$STATE/seed.iso" "$seed"
}

stage_vm() {
    [ "$(utm exists "$NAME")" != true ] || die "UTM already has a VM named '$NAME'; delete it in UTM to recreate it, or resume with --from install"
    log "Creating the VM (importing the image converts it; a few seconds)"
    local tso
    tso="$(utm create-linux "$NAME" "$MAC" "$VM_MEMORY" "$VM_DISK" "$CACHE/$IMAGE" "$STATE/seed.iso" ${SSH_PORT:+"$SSH_PORT"})" ||
        die "creating the VM failed (above); a UTM that cannot resize an existing drive (\`guest size\`) or set TSO fails here"
    S_TSO="$tso"
    sed -i '' "s/^S_TSO=.*/S_TSO=$tso/" "$STATE/state.env"
    log "VM '$NAME' created (TSO $tso)"
}

stage_install() {
    [ "$(utm status "$NAME")" = started ] || { utm start "$NAME"; sleep 30; }
    wait_ssh 30
    log "Waiting for cloud-init (the desktop install; the VM then powers off)"
    local i
    for ((i = 0; i < 240; i++)); do
        [ "$(utm status "$NAME")" = stopped ] && break
        sleep 15
    done
    [ "$(utm status "$NAME")" = stopped ] || die "the VM did not power off after cloud-init within an hour; look at its console (utmbot shot)"
    boot_guest
    gssh 'cloud-init status --long; systemctl is-active gdm' | tr -d '\r' | tee "$STATE/step-install.log"
    grep -q 'status: done' "$STATE/step-install.log" || die "cloud-init did not finish; see $STATE/step-install.log and /var/log/cloud-init-output.log in the VM"
    record_host_keys
}

# Copy setup.sh and the helpers, then run one step.
linux_step() {
    local step="$1"; shift
    gssh 'mkdir -p ~/bin ~/.config/utmbot' >/dev/null
    gscp "$PROV/linux/setup.sh" "$PROV/linux/bin/"* "$REPO/bench/defs/"*.3dmdef "$WIN_USER@$IP:bin/"
    # on PATH for non-login shells too (utmbot test runs bash -c over SSH)
    gssh 'chmod +x ~/bin/setup.sh ~/bin/npt-wine ~/bin/npt-3dmark ~/bin/build-nptunix-x86 &&
          sudo ln -sf ~/bin/npt-wine ~/bin/npt-3dmark ~/bin/build-nptunix-x86 /usr/local/bin/' >/dev/null
    if [ "${S_TSO:-}" = true ]; then gssh 'touch ~/.config/utmbot/hw-tso'; else gssh 'rm -f ~/.config/utmbot/hw-tso'; fi
    log "Guest step: $step"
    gssh "~/bin/setup.sh $step $*" 2>&1 | tr -d '\r' | tee "$STATE/step-$step.log"
    return "${PIPESTATUS[0]}"
}

stage_session() { linux_step session; }
stage_fex() { linux_step fex; }
stage_wine() { linux_step wine; }
stage_tools() { linux_step tools; }
stage_dxvk() { linux_step dxvk; }
stage_nptunix() { linux_step nptunix; }

stage_mesa() {
    if [ -n "$MESA_BUNDLE" ]; then
        gscp "$MESA_BUNDLE" "$WIN_USER@$IP:neptune.bundle"
        linux_step mesa neptune.bundle
    else
        linux_step mesa
    fi
}

# Wine installers need the session's display (GNOME's Xwayland), which an SSH
# command does not have: the same variables npt-wine and `utmbot guest` set
WINE_GUI_ENV='export XDG_RUNTIME_DIR=/run/user/$(id -u) DISPLAY=:0 WAYLAND_DISPLAY=wayland-0
        export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
        export XAUTHORITY=$(ls -t "$XDG_RUNTIME_DIR"/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
        [ -n "$XAUTHORITY" ] || { echo "no Xwayland auth in $XDG_RUNTIME_DIR: is the GNOME session up (autologin)?"; exit 1; }'

stage_prefix() {
    linux_step prefix
    # Benchmarks in the Wine prefix: the x64 Windows builds, since games run
    # there through Wine (and Neptune's D3D DLLs) under FEX.
    local f
    for f in Unigine_Heaven-4.0.exe GravityMark_1.89.msi; do fetch "$f"; done
    log "Installing Unigine Heaven and GravityMark into the prefix"
    gssh 'rm -rf ~/bench-setup; mkdir -p ~/bench-setup'
    gscp "$DL/Unigine_Heaven-4.0.exe" "$DL/GravityMark_1.89.msi" "$WIN_USER@$IP:bench-setup/"
    gssh "set -e; $WINE_GUI_ENV"'
        P=~/wine-prefixes/multi-backend/pfx; W=~/wine-10.9-staging-tkg-amd64/bin
        export WINEPREFIX=$P; L=~/bench-setup.log; : > $L
        H="$P/drive_c/Program Files (x86)/Unigine/Heaven Benchmark 4.0/bin/Heaven.exe"
        G="$P/drive_c/Program Files/GravityMark/bin/GravityMark.exe"
        [ -f "$H" ] || FEXBash -c "$W/wine Z:$HOME/bench-setup/Unigine_Heaven-4.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" >>$L 2>&1 || true
        [ -f "$G" ] || FEXBash -c "$W/wine msiexec /i Z:$HOME/bench-setup/GravityMark_1.89.msi /qn" >>$L 2>&1 || true
        FEXBash -c "$W/wineserver -w"
        rm -rf ~/bench-setup
        test -f "$H" || { echo "Unigine Heaven did not install (see ~/bench-setup.log)"; tail -5 $L; exit 1; }
        test -f "$G" || { echo "GravityMark did not install (see ~/bench-setup.log)"; tail -5 $L; exit 1; }
        echo "BENCHMARKS OK: Heaven, GravityMark"'

    mark_setup
    [ -n "$MARK_DIR" ] || return 0
    if [ ! -f "$MARK_DIR/data_x64.msi" ]; then
        # 3DMark Basic's zip holds only the bundle (3dmark-setup.exe); the
        # prefix installs the MSIs inside it, taken from a Windows install
        log "$MARK_DIR has no data_x64.msi (Wine needs the MSIs, from a Windows install's Package Cache): skipping 3DMark"
        rm -f "$MARK_KEYFILE"; return 0
    fi
    log "Installing 3DMark into the prefix"
    gssh 'rm -rf ~/3dmark-setup; mkdir -p ~/3dmark-setup/none'
    gscp -r "$MARK_DIR/"* "$WIN_USER@$IP:3dmark-setup/"
    gscp "$MARK_KEYFILE" "$WIN_USER@$IP:3dmark-setup/key"
    rm -f "$MARK_KEYFILE"
    # the same MSI properties as the Windows content stage (see setup.ps1)
    gssh "set -e; trap 'rm -rf ~/3dmark-setup' EXIT; $WINE_GUI_ENV"'
        P=~/wine-prefixes/multi-backend/pfx; W=~/wine-10.9-staging-tkg-amd64/bin
        export WINEPREFIX=$P
        for m in data_x64.msi Futuremark_SystemInfo.msi; do
            [ -f ~/3dmark-setup/$m ] || continue
            FEXBash -c "$W/wine msiexec /i Z:$HOME/3dmark-setup/$m /qn INSTALLLOCATION=Z:$HOME/3dmark-setup/none DLC_DESTINATION=C:\\\\ProgramData\\\\UL\\\\3DMark\\\\chops" >/dev/null 2>&1
        done
        if [ -d ~/3dmark-setup/dlc ]; then mkdir -p "$P/drive_c/ProgramData/UL/3DMark/chops/dlc"; cp -r ~/3dmark-setup/dlc/* "$P/drive_c/ProgramData/UL/3DMark/chops/dlc/"; fi
        # 3DMark Basic: the tests are packed .dlc files; 3DMarkCmd installs a folder of them
        if ls ~/3dmark-setup/*.dlc >/dev/null 2>&1; then
            FEXBash -c "$W/wine \"C:/Program Files/UL/3DMark/3DMarkCmd.exe\" --install Z:$HOME/3dmark-setup" >/dev/null 2>&1 || echo "3DMarkCmd --install failed"
        fi
        k=$(cat ~/3dmark-setup/key); rm -f ~/3dmark-setup/key
        FEXBash -c "$W/wine \"C:/Program Files/UL/3DMark/3DMarkCmd.exe\" --register=$k" >/dev/null 2>&1 || echo "3DMark registration failed"
        FEXBash -c "$W/wineserver -w"
        test -f "$P/drive_c/Program Files/UL/3DMark/3DMarkCmd.exe" || { echo "3DMark did not install (run its MSI with /l*v to see why)"; exit 1; }
        for t in fire-strike-test time-spy-test; do [ -d "$P/drive_c/ProgramData/UL/3DMark/chops/dlc/$t" ] || { echo "3DMark has no $t"; exit 1; }; done
        echo "3DMARK OK: activated $(grep -c KeyCode "$P/user.reg" || true)"'
}

stage_verify() {
    linux_step verify || die "verify failed; see $STATE/step-verify.log"
    log "Done: '$NAME' is ready at $WIN_USER@$IP${SSH_PORT:+ port $SSH_PORT} (utmbot --vm '$NAME' …)"
}

provision_main "$@"
