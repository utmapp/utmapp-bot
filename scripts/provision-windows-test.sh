#!/usr/bin/env bash
#
# Create a UTM virtual machine to test Windows kernel drivers on: the same
# unattended Windows 11 install, guest tools, autologon, OpenSSH (host -> VM
# only) and system settings as provision-windows-dev.sh, but no build tools
# and no host share.  The VirtIO GPU is left without a driver for the driver
# under test, test signing is on, and the kernel is debugged over kdnet from a
# dev VM: kdnet drives the second NIC, an e1000 that Windows has no ARM64
# driver for, towards the dev VM's static address on the kdnet network.
#
# Stages (each can be resumed with --from, or run alone with --only):
#
#   media     no-prompt boot ISO + configuration ISO (as for dev VMs)
#   vm        create the VM in UTM, second NIC an e1000 on the kdnet network;
#             UTM attaches its guest tools (Install Windows Guest Tools)
#   install   boot it, install Windows unattended and wait for SSH
#   system    UAC, firewall, SmartScreen, Explorer, policies; Defender off
#             through one Safe Mode boot
#   gpu       remove the guest tools' VirtIO GPU drivers (viogpudo, viogpu3d)
#   finalize  eject the ISOs; make sure Secure Boot is off (delete the
#             Platform Key if the variable store has one); restart; add the VM
#             to ~/.ssh/known_hosts
#   debug     test signing on, kdnet towards <debugger>:<port>, key 1.1.1.1
#   verify    check all of the above, and that kdnet got a DHCP lease
#   kdtest    break in with kd.exe on the debugger VM (started if needed)
#             and read the target's version over kdnet
#   content   benchmarks for perf-regression and validate-stack: VC++
#             runtimes, GravityMark (arm64), Unigine Heaven, power and Game
#             DVR settings; 3DMark (Fire Strike, Time Spy) from --3dmark DIR
#             or ~/.utmapp-bot/cache/3dmark, activated with the key in
#             $UTMBOT_3DMARK_KEY or ~/.utmapp-bot/3dmark.key (no key: skipped)

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=provision/common.sh
. "$REPO/scripts/provision/common.sh"

ROLE=test
STAGES=(media vm install system gpu finalize debug verify kdtest content)
NAME_PREFIX="Windows Test"
NAME_FIRST=1
VM_MEMORY=8192
VM_DISK=65536
NIC2=e1000

usage() {
    cat <<'EOF'
usage: provision-windows-test.sh [options] <windows-11-arm64.iso>
       provision-windows-test.sh --name NAME --from STAGE
       provision-windows-test.sh --name NAME --only STAGE

  <windows-11-arm64.iso>  Windows 11 ARM64 installation ISO (only needed for
                          the media stage)

options:
  --name NAME       VM name in UTM (default: "Windows Test <n>", the first
                    one that is free)
  --user USER       Windows account (default: UTM)
  --password PASS   its password (default: utm)
  --ssh-key FILE    public key the host logs in with (default: the first of
                    ~/.ssh/id_ed25519.pub, id_ecdsa.pub, id_rsa.pub)
  --debugger NAME   dev VM that debugs this one (default: the first dev VM
                    with a kdnet address)
  --port N          kdnet port (default: the first of 50000.. no other test
                    VM uses)
  --3dmark DIR      3DMark installer for the content stage: a folder with
                    UL's full 3dmark-setup.exe, or its payloads data_x64.msi
                    (+ Futuremark_SystemInfo.msi) as found in a Package
                    Cache; optionally a dlc/ subfolder with
                    downloaded tests (C:\ProgramData\UL\3DMark\chops\dlc
                    of another install).  Default: ~/.utmapp-bot/cache/3dmark.
                    3DMark is skipped without a key or an installer.
  --disk GIB        size of the system disk in GiB (default: 64); the
                    qcow2 grows only as it fills, snapshots included
  --emulated-net    one emulated NIC with an SSH port forward instead of the
                    shared (and kdnet) network: what a UTM without the vmnet
                    entitlement gets anyway; no kernel debugging
  --from STAGE      resume an earlier run of the VM given by --name at STAGE
  --only STAGE      run just STAGE on the VM given by --name (e.g. --only
                    kdtest)

stages: media vm install system gpu finalize debug verify kdtest content
EOF
}

EFIVARS="$PROV/efivars.py"

# *** stages ***

# Up to two restarts: one so the loaded driver is gone before its package is
# deleted, one if the service manager defers deleting the services.
stage_gpu() {
    local i
    for ((i = 0; i < 3; i++)); do
        guest_step gpu
        grep -q '^GPU OFF' "$STATE/step-gpu.log" && return 0
        grep -q '^GPU REBOOT' "$STATE/step-gpu.log" || die "the gpu step did not finish; see $STATE/step-gpu.log"
        reboot_guest
    done
    die "the VirtIO GPU drivers are still there after $i restarts; see $STATE/step-gpu.log"
}

# With the VM stopped: no Platform Key, no Secure Boot.  UTM creates the
# variable store without keys, so normally there is nothing to do; a store
# with keys (UTM's "Preload Secure Boot Keys", or a later UTM default) gets
# its PK deleted, which the UEFI specification defines as Setup Mode with
# Secure Boot off.  The original store is kept next to it.
secure_boot_off() {
    local bundle vars status
    bundle="$(vm_path)"
    vars="$bundle/Data/efi_vars.fd"
    [ -f "$vars" ] || die "no UEFI variable store at $vars"
    status="$(python3 -I "$EFIVARS" status "$vars")"
    log "UEFI variable store: $status"
    if [ "$status" = "PK present" ]; then
        cp -n "$vars" "$STATE/efi_vars.fd.with-pk" || true
        log "Deleting the Platform Key to turn Secure Boot off (original in $STATE/efi_vars.fd.with-pk)"
        python3 -I "$EFIVARS" pk-off "$vars"
    fi
}

stage_finalize() {
    finalize_vm secure_boot_off
}

stage_debug() {
    if ! has_kdnet; then
        log "Test signing only: no kdnet network (a UTM without vmnet entitlement), so no kernel debugging"
        guest_step debug
        reboot_guest
        return 0
    fi
    log "kdnet: debugger '$DEBUGGER' at $DEBUGGER_IP:$KD_PORT, key $KDNET_KEY"
    guest_step debug -HostIp "$DEBUGGER_IP" -Port "$KD_PORT" -Key "$KDNET_KEY"
    reboot_guest
    # Again, now that the kdnet adapter exists: keeps its default route off
    # the shared network.
    guest_step debug -HostIp "$DEBUGGER_IP" -Port "$KD_PORT" -Key "$KDNET_KEY"
}

stage_verify() {
    local mac2 lease
    if ! has_kdnet; then
        guest_step verify -Role test || die "verification failed; see $STATE/step-verify.log"
        return 0
    fi
    guest_step verify -Role test -HostIp "$DEBUGGER_IP" -Port "$KD_PORT" -Key "$KDNET_KEY" ||
        die "verification failed; see $STATE/step-verify.log"
    # kdnet runs its own DHCP client on the e1000.  A note only: a lease
    # outlives the boot that took it; the KDNIC check and kdtest are the proof.
    mac2="$(utm nic "$NAME" 1 | cut -f1)"
    lease="$(guest_ip "$mac2")"
    echo "  note  kdnet DHCP lease on the e1000 ($mac2) ${lease:-none}" | tee -a "$STATE/step-verify.log"
}

stage_kdtest() {
    if ! has_kdnet; then
        log "Done: '$NAME' is ready at $WIN_USER@$IP port $SSH_PORT (test signing on; no kernel debugging without a vmnet network)"
        return 0
    fi
    if [ "$(utm exists "$DEBUGGER")" != true ]; then
        log "No VM '$DEBUGGER' in UTM; skipping the kdnet test"
        log "Done: '$NAME' is ready at $WIN_USER@$IP (kdnet to $DEBUGGER_IP:$KD_PORT, key $KDNET_KEY, not tested)"
        return 0
    fi
    local d_host="" d_opts=() started="" i
    dssh() { ssh "${d_opts[@]}" "$d_host" "$@"; }

    if [ "$(utm status "$DEBUGGER")" = stopped ]; then
        log "Starting the debugger VM '$DEBUGGER'"
        utm start "$DEBUGGER"
        started=1
    fi
    # its address once it has booted; as utmbot reaches it (resolve_ssh)
    for ((i = 0; i < 60; i++)); do
        if resolve_ssh "$DEBUGGER" 2>/dev/null; then
            d_host="$SSH_HOST"
            d_opts=(-o BatchMode=yes -o ConnectTimeout=10 "${SSH_TARGET_OPTS[@]}")
            dssh 'echo ok' 2>/dev/null | grep -q ok && break
        fi
        d_host=""
        sleep 10
    done
    [ -n "$d_host" ] || die "no SSH to the debugger VM '$DEBUGGER'"
    dssh 'ipconfig' | tr -d '\r' | grep -q "$DEBUGGER_IP\$" ||
        die "'$DEBUGGER' does not have $DEBUGGER_IP; run: $REPO/scripts/provision-windows-dev.sh --name '$DEBUGGER' --only kdnet"

    # kd listens before the target restarts (kdnet connects at boot), breaks
    # in (-b), reads the target's version and lets it go on.  kd's stdin stays
    # open (sleep) so it does not see end of input and quit.
    local out="$STATE/kdtest.log" kd
    dssh 'taskkill /im kd.exe /f' >/dev/null 2>&1 || true
    log "kd.exe on '$DEBUGGER' ($d_host) listening on port $KD_PORT; restarting the test VM"
    kd="cd /d \"C:\\Program Files (x86)\\Windows Kits\\10\\Debuggers\\arm64\" && kd.exe -k net:port=$KD_PORT,key=$KDNET_KEY -b -c \".echo KDTEST-BEGIN;vertarget;.echo KDTEST-END;g\""
    (sleep 1200 | dssh "$kd" > "$out" 2>&1) &
    local kdpid=$!
    sleep 10
    gssh 'shutdown /r /t 0' || true

    local passed=""
    for ((i = 0; i < 60; i++)); do
        if tr -d '\r' < "$out" | grep -qx 'KDTEST-END'; then passed=1; break; fi
        sleep 10
    done
    # The target was let go ("g") before kd ends, so it keeps running.
    sleep 5
    dssh 'taskkill /im kd.exe /f' >/dev/null 2>&1 || true
    pkill -P "$kdpid" 2>/dev/null || true
    kill "$kdpid" 2>/dev/null || true
    wait "$kdpid" 2>/dev/null || true
    tr -d '\r' < "$out" | sed -n '/^KDTEST-BEGIN$/,/^KDTEST-END$/p'

    if ! (wait_ssh 15); then
        # A target left in a break with no debugger waits forever.
        log "The test VM did not come back; restarting it"
        utm stop "$NAME"
        boot_guest
    else
        wait_ssh 1
    fi
    if [ -n "$started" ]; then
        log "Shutting the debugger VM down again"
        dssh 'shutdown /s /t 0' >/dev/null 2>&1 || true
    fi
    [ -n "$passed" ] || die "kd.exe did not break in over kdnet; see $out"
    log "Done: '$NAME' is ready at $WIN_USER@$IP; kernel debugging works from '$DEBUGGER':"
    log "    kd.exe -k net:port=$KD_PORT,key=$KDNET_KEY   (on $d_host)"
}

# Benchmarks.  Everything here is freely downloadable except 3DMark, whose
# command line (3DMarkCmd, used by `utmbot bench`) needs a Professional key.
# The key never enters the repository, a log or a host command line: it is
# copied to the guest as a file the guest step reads and deletes.  3DMarkCmd
# only takes it as an argument (--register), so it is on the guest's process
# list while it registers.
stage_content() {
    upload vc_redist.arm64.exe vc_redist.x64.exe vc_redist.x86.exe directx_Jun2010_redist.exe GravityMark_1.89_arm64.msi Unigine_Heaven-4.0.exe
    local args=()
    mark_setup
    if [ -n "$MARK_DIR" ]; then
        [ -f "$MARK_DIR/3dmark-setup.exe" ] || [ -f "$MARK_DIR/data_x64.msi" ] || die "$MARK_DIR has neither 3dmark-setup.exe nor data_x64.msi"
        log "Uploading 3DMark from $MARK_DIR"
        gssh 'if exist C:\utmapp-bot\downloads\3dmark rmdir /s /q C:\utmapp-bot\downloads\3dmark' >/dev/null
        gscp -r "$MARK_DIR" "$WIN_USER@$IP:C:/utmapp-bot/downloads/3dmark"
        gscp "$MARK_KEYFILE" "$WIN_USER@$IP:C:/utmapp-bot/downloads/3dmark.key"
        rm -f "$MARK_KEYFILE"
        args=(-Mark)
    fi
    guest_step content "${args[@]+"${args[@]}"}" || die "the content step failed; see $STATE/step-content.log"
    grep -q '^CONTENT OK' "$STATE/step-content.log" || die "the content step did not finish; see $STATE/step-content.log"
}

provision_main "$@"
