# common.sh - shared by provision-windows-dev.sh, provision-windows-test.sh
# and provision-linux-test.sh.
#
# Sourced, not run.  The including script sets:
#
#   ROLE          dev, test (Windows) or linux
#   STAGES        its stage names, in order; each is a function stage_<name>
#   NAME_PREFIX   default VM name prefix ("Windows Development")
#   NAME_FIRST    first number tried for the default name
#   VM_MEMORY     MiB
#   VM_DISK       MiB (--disk GIB overrides it)
#   NIC2          hardware of the second NIC (on the kdnet network)
#   DEFAULT_USER, DEFAULT_PASSWORD  the guest account unless --user/--password
#                 (default UTM / utm)
#   usage()       its help text
#
# then calls `provision_main "$@"`.  Shared stages (Windows): media, vm,
# install, system; shared helpers for everything else.
#
# The selected UTM must be current: an older one lacks scripting the plugin
# uses (app_scripting_missing in lib/utm.sh) and is refused, by setup_state
# before a run that creates the VM and by the first UTM call of any other stage.
#
# The kdnet network: every VM's second NIC is on one host-only vmnet network
# pinned to $KDNET_NET.0/24 (vmnet-host start/end/mask through UTM's "vlan
# guest address" and "vlan dhcp end address").  The host is .1, DHCP hands out
# .2-.199 (kdnet on a test VM needs DHCP: it cannot use a static address),
# and dev VMs, the debuggers, take static addresses from .200 up.  Without the
# pin vmnet picks a different subnet from boot to boot.
#
# A UTM without the vmnet entitlement (an ad-hoc signed build) cannot start a
# VM with a shared or host-only NIC.  For it the VM gets one emulated NIC with
# 127.0.0.1:$SSH_PORT forwarded to the guest's SSH, and no kdnet network:
# VMs on emulated networks cannot reach each other, so there is no kernel
# debugging.  The choice is made once, by the UTM of the first run, and kept
# in state.env.

set -euo pipefail

PROV="$REPO/scripts/provision"
# State lives outside the plugin, whose directory is replaced on every update.
DATA="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}"
CACHE="$DATA/cache"
DL="$CACHE/downloads"
KDNET_NET=192.168.240
KDNET_SUBNET="$KDNET_NET.0/24"
KDNET_DHCP_END="$KDNET_NET.199"
KDNET_KEY=1.1.1.1

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "=== $(date +%H:%M:%S) $*"; }
# utm_app, app_has_vmnet, app_scripting_missing, guest_ip, free_port, slug, crlf, ...
. "$REPO/scripts/lib/utm.sh"
UTM_APP="$(utm_app)"
export UTM_APP
SHMEM_GROUP="$(unsigned_shmem_group "$UTM_APP")"
# The first call has utmbot make sure exactly this UTM runs (launched with
# `open`, never by an AppleScript tell) and that this terminal may script it.
UTM_READY=""
utm() {
    if [ -z "$UTM_READY" ]; then
        python3 "$REPO/scripts/utmbot" app >/dev/null || die "UTM is not ready (above)"
        UTM_READY=1
    fi
    # an unsigned UTM needs the app group for guest 3D (see utm.sh)
    if [ "$1" = start ] && [ -n "$SHMEM_GROUP" ]; then
        set -- "$@" "APP_SANDBOX_GROUP_ID=$SHMEM_GROUP"
    fi
    osascript "$PROV/utm.applescript" "$@"
}

# *** arguments and run state ***

ISO=""
NAME=""
WIN_USER=""
PASSWORD=""
PUBKEY=""
FROM=""
ONLY=""
DEBUGGER=""
KD_PORT_ARG=""
MARK_DIR=""
MESA_BUNDLE=""
EMULATED_NET=""

# for_roles "<roles>" <option>: the option exists only for those roles.
for_roles() {
    case " $1 " in *" $ROLE "*) ;; *) echo "unknown option: $2" >&2; usage >&2; exit 2 ;; esac
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --name) NAME="$2"; shift 2 ;;
            --user) WIN_USER="$2"; shift 2 ;;
            --password) PASSWORD="$2"; shift 2 ;;
            --ssh-key) PUBKEY="$2"; shift 2 ;;
            --from) FROM="$2"; shift 2 ;;
            --only) ONLY="$2"; shift 2 ;;
            --debugger) for_roles test "$1"; DEBUGGER="$2"; shift 2 ;;
            --port) for_roles test "$1"; KD_PORT_ARG="$2"; shift 2 ;;
            --3dmark) for_roles "test linux" "$1"; MARK_DIR="$2"; shift 2 ;;
            --mesa-bundle) for_roles linux "$1"; MESA_BUNDLE="$2"; shift 2 ;;
            --emulated-net) EMULATED_NET=1; shift ;;
            --disk) [[ "$2" =~ ^[0-9]+$ ]] && [ "$2" -ge 32 ] || die "--disk takes a size in GiB (32 or more)"
                    VM_DISK=$(($2 * 1024)); shift 2 ;;
            -h|--help) usage; exit 0 ;;
            -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
            *) [ -z "$ISO" ] && [ "$ROLE" != linux ] || { usage >&2; exit 2; }; ISO="$1"; shift ;;
        esac
    done
}

# Values every state.env of a role holds for key $1 (used to hand out unique
# kdnet addresses and ports).
taken() {
    local f
    for f in "$DATA"/provision/*/state.env; do
        [ -f "$f" ] && [ "$f" != "$STATE/state.env" ] || continue
        sed -n "s/^$1=//p" "$f"
    done
}

setup_state() {
    if [ -n "$FROM" ] && [ -n "$ONLY" ]; then die "--from and --only exclude each other"; fi
    # A run that creates the VM needs a current UTM: say so now, not after the
    # slow media stage.  Other stages (many only use SSH) are checked on their
    # first UTM call: utm() runs `utmbot app`, which refuses an older UTM the
    # same way.
    local missing
    if [ -z "$ONLY$FROM" ] || [ "$FROM" = media ] || [ "$FROM$ONLY" = vm ]; then
        missing="$(app_scripting_missing "$UTM_APP" | paste -sd, - | sed 's/,/, /g')"
        [ -z "$missing" ] || die "$UTM_APP is too old for this plugin (its scripting lacks $missing): update UTM, or set UTM_APP to a current one"
    fi
    if [ -n "$FROM$ONLY" ]; then
        [ -n "$NAME" ] || die "--from and --only need --name"
        printf '%s\n' "${STAGES[@]}" | grep -qx "$FROM$ONLY" || die "unknown stage '$FROM$ONLY'"
    else
        FROM=media
        [ -n "$ISO" ] || [ "$ROLE" = linux ] || { usage >&2; exit 2; }
        if [ -z "$NAME" ]; then
            local n=$NAME_FIRST
            while [ "$(utm exists "$NAME_PREFIX $n")" = true ]; do n=$((n + 1)); done
            NAME="$NAME_PREFIX $n"
        elif [ "$(utm exists "$NAME")" = true ]; then
            die "UTM already has a VM named '$NAME'; pick another --name, or resume it with --from"
        fi
    fi

    STATE="$DATA/provision/$(slug "$NAME")"
    mkdir -p "$STATE" "$DL"
    exec > >(tee -a "$STATE/provision.log") 2>&1

    # Settings are fixed by the first run; resumed runs read them back.
    if [ "$FROM" != media ] && [ ! -f "$STATE/state.env" ]; then
        die "no earlier run for '$NAME' in $STATE"
    fi
    if [ -f "$STATE/state.env" ]; then
        # shellcheck source=/dev/null
        . "$STATE/state.env"
    fi
    [ "${S_ROLE:-$ROLE}" = "$ROLE" ] || die "'$NAME' is a $S_ROLE VM, not a $ROLE VM"
    : "${WIN_USER:=${S_USER:-${DEFAULT_USER:-UTM}}}"
    : "${PASSWORD:=${S_PASSWORD:-${DEFAULT_PASSWORD:-utm}}}"
    : "${MAC:=$(printf '0E:%02X:%02X:%02X:%02X:%02X' $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)))}"
    if [ -z "$PUBKEY" ]; then
        PUBKEY="${S_PUBKEY:-}"
        local k
        for k in id_ed25519 id_ecdsa id_rsa; do
            [ -n "$PUBKEY" ] || [ ! -f "$HOME/.ssh/$k.pub" ] || PUBKEY="$HOME/.ssh/$k.pub"
        done
    fi
    [ -n "$PUBKEY" ] && [ -f "$PUBKEY" ] || die "no SSH public key; create one with ssh-keygen or pass --ssh-key"
    [ -f "${PUBKEY%.pub}" ] || die "no private key next to $PUBKEY"

    # Without vmnet: an SSH port forward instead of the shared network, no kdnet.
    # A VM keeps the network it is made with, and one made with vmnet NICs
    # still runs on a UTM without the entitlement (utmbot emulates them per
    # boot), so prefer making Windows VMs with the release app when it has it.
    if [ -z "${SSH_PORT:-}" ] && [ "$FROM" = media ] && [ -z "$EMULATED_NET" ] && [ "$ROLE" != linux ] &&
       ! app_has_vmnet "$UTM_APP" && [ "$UTM_APP" != /Applications/UTM.app ] && app_has_vmnet /Applications/UTM.app; then
        die "$UTM_APP has no vmnet entitlement, so this VM would get emulated networking and never kdnet. Make it with the release app instead (UTM_APP=/Applications/UTM.app, after quitting this one); your build can still run it. Or pass --emulated-net to accept a VM without kdnet"
    fi
    if [ -z "${SSH_PORT:-}" ] && [ "$FROM" = media ] && { [ -n "$EMULATED_NET" ] || ! app_has_vmnet "$UTM_APP"; }; then
        SSH_PORT="$(free_port 22200)"
        while taken SSH_PORT | grep -qx "$SSH_PORT"; do SSH_PORT="$(free_port $((SSH_PORT + 1)))"; done
        log "$UTM_APP has no vmnet entitlement: emulated network, SSH through 127.0.0.1:$SSH_PORT, no kdnet"
    fi
    if [ -z "${SSH_PORT:-}" ] && ! app_has_vmnet "$UTM_APP"; then
        die "'$NAME' uses vmnet networking, which $UTM_APP cannot start (no com.apple.vm.networking entitlement); resume with a UTM that has it, e.g. UTM_APP=/Applications/UTM.app"
    fi

    # kdnet: dev VMs get a static address, test VMs a debugger and a port.
    local extra="" i
    if [ -n "${SSH_PORT:-}" ]; then
        extra="SSH_PORT=$SSH_PORT"
        [ "$ROLE" != linux ] || extra="$extra
S_OS=linux
S_TSO=${S_TSO:-}"
    elif [ "$ROLE" = linux ]; then
        # no kdnet; S_TSO is written by the vm stage
        extra="S_OS=linux
S_TSO=${S_TSO:-}"
    elif [ "$ROLE" = dev ]; then
        if [ -z "${KDNET_IP:-}" ]; then
            for ((i = 200; i < 250; i++)); do
                taken KDNET_IP | grep -qx "$KDNET_NET.$i" || { KDNET_IP="$KDNET_NET.$i"; break; }
            done
            [ -n "${KDNET_IP:-}" ] || die "no free kdnet address in $KDNET_NET.200-249"
        fi
        extra="KDNET_IP=$KDNET_IP"
    else
        DEBUGGER="${DEBUGGER:-${S_DEBUGGER:-}}"
        if [ -z "$DEBUGGER" ]; then
            # The first dev VM that has a kdnet address.
            local f
            for f in "$DATA"/provision/*/state.env; do
                [ -f "$f" ] && grep -q '^KDNET_IP=' "$f" || continue
                DEBUGGER="$(. "$f"; printf '%s' "$NAME")"
                break
            done
        fi
        [ -n "$DEBUGGER" ] || die "no dev VM with a kdnet address to debug from; provision one with provision-windows-dev.sh, migrate one with --only kdnet, or pass --debugger NAME"
        DEBUGGER_STATE="$DATA/provision/$(slug "$DEBUGGER")/state.env"
        [ -f "$DEBUGGER_STATE" ] || die "'$DEBUGGER' was not provisioned by provision-windows-dev.sh"
        DEBUGGER_IP="$(sed -n 's/^KDNET_IP=//p' "$DEBUGGER_STATE")"
        [ -n "$DEBUGGER_IP" ] || die "'$DEBUGGER' has no kdnet address yet; run: $REPO/scripts/provision-windows-dev.sh --name '$DEBUGGER' --only kdnet"
        KD_PORT="${KD_PORT_ARG:-${KD_PORT:-}}"
        if [ -z "$KD_PORT" ]; then
            for ((i = 50000; i < 50100; i++)); do
                taken KD_PORT | grep -qx "$i" || { KD_PORT=$i; break; }
            done
        fi
        extra="S_DEBUGGER=$(printf '%q' "$DEBUGGER")
KD_PORT=$KD_PORT"
    fi

    cat > "$STATE/state.env" <<EOF
NAME=$(printf '%q' "$NAME")
S_ROLE=$ROLE
S_USER=$(printf '%q' "$WIN_USER")
S_PASSWORD=$(printf '%q' "$PASSWORD")
S_PUBKEY=$(printf '%q' "$PUBKEY")
MAC=$MAC
$extra
EOF

    # as utmbot reaches the VM (host keys under its name, see vm_ssh_opts)
    vm_ssh_opts "$STATE/state.env"
    SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=60 -o ServerAliveCountMax=10 "${SSH_TARGET_OPTS[@]}")
    log "VM '$NAME' ($ROLE), account $WIN_USER, state in $STATE"
}

# *** helpers ***

sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

# download <url> <file> <sha256> [<hint>]: <url> to <file>, unless <file> is
# there with that hash; a download with another hash fails (with <hint>).
download() {
    local url="$1" file="$2" sha="$3"
    [ -f "$file" ] && [ "$(sha256 "$file")" = "$sha" ] && return 0
    log "Downloading $(basename "$file")"
    curl -fL --retry 3 -o "$file.part" "$url" || die "download of $url failed"
    [ "$(sha256 "$file.part")" = "$sha" ] || die "$(basename "$file"): SHA-256 mismatch${4:+; $4}"
    mv "$file.part" "$file"
}

# Fetch a file listed in downloads.txt into the cache.
fetch() {
    local file="$1" sha url
    read -r sha url < <(awk -v f="$file" '$1 == f { print $2, $3 }' "$PROV/downloads.txt") || true
    [ -n "$url" ] || die "$file is not in downloads.txt"
    download "$url" "$DL/$file" "$sha" "update downloads.txt if the new file is expected"
}

# 3DMark, with Fire Strike and Time Spy.  The installer is the folder given
# with --3dmark, else ~/.utmapp-bot/cache/3dmark: either 3DMark Basic's
# download unzipped (data_x64.msi, 3dmark-setup.exe, a packed .dlc per test)
# or UL's MSIs from a Package Cache plus dlc/ with the unpacked tests; the key is $UTMBOT_3DMARK_KEY or ~/.utmapp-bot/3dmark.key.  Without a
# key, or without an installer, 3DMark is skipped (the other benchmarks are
# installed regardless).  Sets MARK_DIR (empty: skip) and MARK_KEYFILE (a
# temporary file with the key; the caller uploads and removes it).
mark_setup() {
    MARK_KEYFILE=""
    if [ -n "${UTMBOT_3DMARK_KEY:-}" ]; then
        MARK_KEYFILE="$(mktemp)"; printf '%s' "$UTMBOT_3DMARK_KEY" > "$MARK_KEYFILE"
    elif [ -s "$DATA/3dmark.key" ]; then
        MARK_KEYFILE="$(mktemp)"; tr -d ' \r\n' < "$DATA/3dmark.key" > "$MARK_KEYFILE"
    else
        log "No 3DMark key (\$UTMBOT_3DMARK_KEY or $DATA/3dmark.key): skipping 3DMark"
        MARK_DIR=""; return 0
    fi
    if [ -z "$MARK_DIR" ] && { [ -f "$DATA/cache/3dmark/data_x64.msi" ] || [ -f "$DATA/cache/3dmark/3dmark-setup.exe" ]; }; then
        MARK_DIR="$DATA/cache/3dmark"
    fi
    if [ -z "$MARK_DIR" ]; then
        log "No 3DMark installer (--3dmark DIR, or $DATA/cache/3dmark: unzip 3DMark Basic's zip there): skipping 3DMark"
        rm -f "$MARK_KEYFILE"; MARK_KEYFILE=""; return 0
    fi
    local t f
    if ls "$MARK_DIR"/*.dlc >/dev/null 2>&1; then
        # 3DMark Basic's download (installer + every test as a packed .dlc,
        # 13 GB): stage only the installer and the two tests the harness runs
        local stage="$CACHE/3dmark-stage"
        rm -rf "$stage"; mkdir -p "$stage"
        for f in data_x64.msi Futuremark_SystemInfo.msi 3dmark-setup.exe; do
            [ -f "$MARK_DIR/$f" ] && ln -s "$MARK_DIR/$f" "$stage/$f"
        done
        for t in fire-strike-test time-spy-test; do
            f="$(ls "$MARK_DIR"/*-"$t"-v*.dlc 2>/dev/null | head -1)"
            if [ -n "$f" ]; then ln -s "$f" "$stage/$(basename "$f")"
            else log "WARNING: $MARK_DIR has no $t .dlc: 3DMark installs without it"; fi
        done
        # the MSI is enough on Windows; drop the 250 MB bundle when it is there
        [ -e "$stage/data_x64.msi" ] && rm -f "$stage/3dmark-setup.exe"
        MARK_DIR="$stage"
    else
        for t in fire-strike-test time-spy-test; do
            [ -d "$MARK_DIR/dlc/$t" ] || log "WARNING: $MARK_DIR/dlc has no $t: 3DMark installs, but that test needs downloading in the guest"
        done
    fi
    log "3DMark from $MARK_DIR, activated with the key"
}


le32() { dd if="$1" bs=1 skip="$2" count=4 2>/dev/null | od -An -tu4 | tr -d ' '; }

# Copy the install ISO with its El Torito boot image replaced by
# efisys_noprompt.bin: the stock image waits for "Press any key to boot from
# CD or DVD" and falls through to the empty disk when nobody does.  The copy
# is an APFS clone, so only the 1.7 MB boot image takes space.
make_boot_iso() {
    local src="$1" dst="$2" mnt="$3"
    local prompt="$mnt/efi/microsoft/boot/efisys.bin" noprompt="$mnt/efi/microsoft/boot/efisys_noprompt.bin"
    [ -f "$noprompt" ] || die "$src has no efi/microsoft/boot/efisys_noprompt.bin"
    local size; size=$(stat -f%z "$prompt")
    [ "$size" -eq "$(stat -f%z "$noprompt")" ] || die "efisys.bin and efisys_noprompt.bin differ in size"
    [ "$(dd if="$src" bs=2048 skip=17 count=1 2>/dev/null | head -c 30 | tail -c 23)" = "EL TORITO SPECIFICATION" ] ||
        die "$src has no El Torito boot record"
    local catalog; catalog=$(le32 "$src" $((17 * 2048 + 71)))
    rm -f "$dst.tmp"
    cp -c "$src" "$dst.tmp" 2>/dev/null || cp "$src" "$dst.tmp"
    # an ISO patched before (e.g. this cache's own output) is used as it is
    local patched=0 already=0 off rba image
    for ((off = 32; off < 2048; off += 32)); do
        [ "$(dd if="$src" bs=1 skip=$((catalog * 2048 + off)) count=1 2>/dev/null | od -An -tx1 | tr -d ' ')" = 88 ] || continue
        rba=$(le32 "$src" $((catalog * 2048 + off + 8)))
        image="$(dd if="$src" bs=2048 skip="$rba" count=$(((size + 2047) / 2048)) 2>/dev/null | head -c "$size" | shasum | cut -d' ' -f1)"
        if [ "$image" = "$(shasum < "$prompt" | cut -d' ' -f1)" ]; then
            dd if="$noprompt" of="$dst.tmp" bs=2048 seek="$rba" conv=notrunc 2>/dev/null
            patched=$((patched + 1))
        elif [ "$image" = "$(shasum < "$noprompt" | cut -d' ' -f1)" ]; then
            already=$((already + 1))
        fi
    done
    [ $((patched + already)) -gt 0 ] || die "could not find efisys.bin in the boot catalog of $src"
    mv "$dst.tmp" "$dst"
}

# Where SSH reaches the guest: the forward on the host, or its DHCP address.
guest_addr() { if [ -n "${SSH_PORT:-}" ]; then echo 127.0.0.1; else guest_ip; fi; }
has_kdnet() { [ -z "${SSH_PORT:-}" ]; }
# The VM's .utm bundle.
vm_path() { utm file "$NAME"; }

gssh() { ssh "${SSH_OPTS[@]}" "$WIN_USER@$IP" "$@"; }
gscp() { scp -q "${SSH_OPTS[@]}" "$@"; }

# Wait until the guest accepts SSH with our key.
wait_ssh() {
    local minutes="$1" start=$SECONDS last=-1 status stopped=0
    while :; do
        IP="$(guest_addr)"
        if [ -n "$IP" ] && gssh 'echo ok' 2>/dev/null | grep -q ok; then
            log "SSH is up at $WIN_USER@$IP${SSH_PORT:+ port $SSH_PORT}"
            return 0
        fi
        status="$(utm status "$NAME" 2>/dev/null || true)"
        # UTM restarts QEMU when the guest reboots; only a VM that stays
        # stopped has really stopped.
        if [ "$status" = stopped ]; then stopped=$((stopped + 1)); else stopped=0; fi
        [ "$stopped" -lt 3 ] || die "the VM stopped; look at its console in UTM"
        if [ $(((SECONDS - start) / 300)) -ne "$last" ]; then
            last=$(((SECONDS - start) / 300))
            log "Waiting for SSH ($((last * 5)) min; VM $status${IP:+, $IP})"
        fi
        if [ $((SECONDS - start)) -gt $((minutes * 60)) ]; then
            die "no SSH after $minutes minutes; look at the VM's console in UTM.  Windows Setup logs: C:\\Windows\\Panther, provisioning logs: C:\\utmapp-bot\\*.log"
        fi
        sleep 15
    done
}

# Restart the guest and wait for it to come back.
reboot_guest() {
    log "Restarting the VM"
    gssh 'shutdown /r /t 0' || true
    sleep 60
    wait_ssh 20
}

# Shut the guest down and wait until UTM reports the VM stopped.
shutdown_guest() {
    log "Shutting down the VM"
    gssh 'shutdown /s /t 0' || true
    local i
    for ((i = 0; i < 60; i++)); do
        [ "$(utm status "$NAME")" = stopped ] && return 0
        sleep 5
    done
    utm stop "$NAME"
}

boot_guest() {
    utm start "$NAME"
    sleep 30
    wait_ssh 20
}

# Run one step of setup.ps1 on the guest.
guest_step() {
    local step="$1"; shift
    local stage; stage="$(mktemp -d)"
    mkdir "$stage/setup"
    cp "$PROV"/guest/* "$stage/setup/"
    crlf "$stage/setup/"*.ps1
    gssh 'if not exist C:\utmapp-bot\downloads mkdir C:\utmapp-bot\downloads' >/dev/null
    gscp -r "$stage/setup" "$WIN_USER@$IP:C:/utmapp-bot/"
    rm -rf "$stage"
    log "Guest step: $step"
    gssh "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\utmapp-bot\\setup\\setup.ps1 -Step $step $*" | tr -d '\r' | sed '/^Preparing: /d' | tee "$STATE/step-$step.log"
    return "${PIPESTATUS[0]}"
}

# Copy installers from the cache to the guest.
upload() {
    for f in "$@"; do
        fetch "$f"
        gscp "$DL/$f" "$WIN_USER@$IP:C:/utmapp-bot/downloads/$f"
    done
}

# *** shared stages ***

stage_media() {
    [ -n "$ISO" ] || die "the media stage needs the Windows ISO"
    [ -f "$ISO" ] || die "$ISO not found"
    ISO="$(cd "$(dirname "$ISO")" && pwd)/$(basename "$ISO")"
    local mnt; mnt="$(mktemp -d)"
    MOUNT="$mnt"
    hdiutil attach -readonly -nobrowse -noverify -mountpoint "$mnt" "$ISO" >/dev/null 2>&1 || die "cannot mount $ISO"
    [ -f "$mnt/efi/boot/bootaa64.efi" ] || die "$ISO is not a Windows ARM64 ISO (no efi/boot/bootaa64.efi)"
    [ -f "$mnt/sources/install.wim" ] || [ -f "$mnt/sources/install.esd" ] || die "$ISO is not a Windows installation ISO"
    local lang
    lang="$(tr -d '\r' < "$mnt/sources/lang.ini" 2>/dev/null | awk '/^\[Available UI Languages\]/ { f = 1; next } f && /=/ { print $1; exit }')"
    lang="${lang:-en-us}"

    local boot; boot="$CACHE/$(basename "${ISO%.*}")-noprompt.iso"
    if [ ! -f "$boot" ] || [ "$ISO" -nt "$boot" ]; then
        log "Making the no-prompt boot ISO"
        make_boot_iso "$ISO" "$boot" "$mnt"
    fi
    hdiutil detach "$mnt" -quiet

    log "Making the configuration ISO (language $lang)"
    local cfg="$STATE/media"
    rm -rf "$cfg"
    mkdir -p "$cfg/utmapp-bot"
    cp "$PROV"/media/* "$cfg/utmapp-bot/"
    crlf "$cfg/utmapp-bot/"*.cmd "$cfg/utmapp-bot/"*.ps1
    cp "$PUBKEY" "$cfg/utmapp-bot/host_authorized_keys"
    json() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
    printf '{ "user": "%s", "password": "%s" }\r\n' "$(json "$WIN_USER")" "$(json "$PASSWORD")" > "$cfg/utmapp-bot/settings.json"
    rmdir "$mnt"
    MOUNT=""
    xml() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
    sed -e "s|@ARCH@|arm64|g" -e "s|@LANG@|$lang|g" \
        -e "s|@USER@|$(xml "$WIN_USER" | sed 's/[|&\\]/\\&/g')|g" \
        -e "s|@PASSWORD@|$(xml "$PASSWORD" | sed 's/[|&\\]/\\&/g')|g" \
        "$PROV/Autounattend.xml.in" | perl -pe 's/\r?\n/\r\n/' > "$cfg/Autounattend.xml"
    rm -f "$STATE/config.iso"
    hdiutil makehybrid -quiet -iso -joliet -default-volume-name UTMAPPBOT -o "$STATE/config.iso" "$cfg"

    ln -sf "$boot" "$STATE/boot.iso"
}

stage_vm() {
    local bundle="$STATE/vm/$NAME.utm" out
    [ -f "$STATE/config.iso" ] && [ -e "$STATE/boot.iso" ] || die "run the media stage first"
    if [ "$(utm exists "$NAME")" != true ]; then
        if [ ! -d "$bundle" ]; then
            log "Creating the VM"
            rm -rf "$STATE/vm"
            mkdir -p "$STATE/vm"
            if has_kdnet; then
                utm create "$NAME" "$MAC" "$bundle" "$NIC2" "$VM_MEMORY" "$VM_DISK" >/dev/null
            else
                utm create "$NAME" "$MAC" "$bundle" none "$VM_MEMORY" "$VM_DISK" "$SSH_PORT" >/dev/null
            fi
            # no serial port, as on the golden VM
            plutil -replace Serial -json '[]' "$bundle/config.plist"
        fi
        log "Importing the VM into UTM"
        utm import "$bundle" "$(readlink "$STATE/boot.iso")" "$STATE/config.iso" >/dev/null
        rm -rf "$STATE/vm"
    elif [ "$(cat "$STATE/last-stage" 2>/dev/null)" = media ]; then
        # this stage failed after the import (attaching the guest tools):
        # carry on from there
        log "'$NAME' is in UTM already (imported by the vm stage that failed)"
    else
        die "UTM already has a VM named '$NAME'; delete it in UTM to recreate it, or resume with --from install"
    fi
    # the guest tools in the third (empty) drive, by UTM's own Install Windows
    # Guest Tools: UTM knows where they come from; firstlogon.ps1 installs them
    log "Attaching the guest tools (UTM fetches the latest when needed)"
    if ! out="$(utm guest-tools "$NAME" 2>&1)"; then
        case "$out" in
            *-1712*) die "UTM did not finish attaching the guest tools in time (still downloading them? look at UTM): $out" ;;
            *) die "UTM could not attach the guest tools: $out" ;;
        esac
    fi
    if has_kdnet; then pin_kdnet_network; fi
}

# Put the second NIC on the kdnet network.  The VM must be stopped.
pin_kdnet_network() {
    log "Pinning the second NIC to the kdnet network $KDNET_SUBNET"
    utm kdnet-net "$NAME" "$KDNET_SUBNET" "$KDNET_DHCP_END" >/dev/null
}

kdnet_network_pinned() {
    [ "$(utm nic "$NAME" 1 | cut -f3)" = "$KDNET_SUBNET" ]
}

stage_install() {
    if [ "$(utm status "$NAME")" != started ]; then
        log "Starting the VM; Windows installs unattended (about 20-40 minutes)"
        utm start "$NAME"
    fi
    wait_ssh 120
}

stage_system() {
    guest_step system
    guest_step defender
    if grep -q '^DEFENDER REBOOT' "$STATE/step-defender.log"; then
        log "Restarting through Safe Mode to turn off Microsoft Defender"
        gssh 'shutdown /r /t 0' || true
        sleep 90
        wait_ssh 30
        guest_step defender
        grep -q '^DEFENDER OFF' "$STATE/step-defender.log" || die "Microsoft Defender is still on; see C:\\utmapp-bot\\defender-off.log"
    fi
}

# Shut down, replace the ISO drives with one empty removable drive, run
# "$@" while the VM is off, boot and record the host keys.
finalize_vm() {
    shutdown_guest
    utm eject "$NAME"
    "$@"
    boot_guest
    record_host_keys
}

# Record the guest's host keys: in the VM's known_hosts under its name (as
# SSH_OPTS and utmbot look them up), and for a vmnet VM, whose address is its
# own, in ~/.ssh/known_hosts for manual ssh.  A forwarded port on 127.0.0.1
# can belong to another VM tomorrow, so it never goes there.
record_host_keys() {
    local alias keys
    alias="$(slug "$NAME")"
    keys="$(ssh-keyscan -p "${SSH_PORT:-22}" -t ed25519,ecdsa,rsa "$IP" 2>/dev/null)"
    [ -n "$keys" ] || { echo "WARNING: ssh-keyscan of $IP returned nothing" >&2; return 0; }
    ssh-keygen -R "$alias" -f "$STATE/known_hosts" >/dev/null 2>&1 || true
    printf '%s\n' "$keys" | awk -v a="$alias" '{ $1 = a; print }' >> "$STATE/known_hosts"
    if has_kdnet; then
        log "Adding $IP to ~/.ssh/known_hosts"
        ssh-keygen -R "$IP" >/dev/null 2>&1 || true
        printf '%s\n' "$keys" >> "$HOME/.ssh/known_hosts"
    fi
}

# *** run ***

provision_main() {
    # the arguments, for the resume hint (--3dmark, --disk, the ISO carry over)
    RESUME_ARGS=()
    local skip=""
    for a in "$@"; do
        if [ -n "$skip" ]; then skip=""; continue; fi
        case "$a" in --from|--only|--name) skip=1 ;; *) RESUME_ARGS+=("$a") ;; esac
    done
    parse_args "$@"
    setup_state
    MOUNT=""
    STAGE=""
    trap on_exit EXIT
    # one user of UTM at a time across sessions (scripts/utmbot/lease.py);
    # the media stage alone does not touch UTM
    if [ "$ONLY" != media ]; then
        # a session that already holds the lease (a skill running this) keeps it
        # afterwards: release only a lease taken here
        local held
        held="$(cd "$REPO/scripts" && python3 -c 'from utmbot import lease; c = lease.read(); print("mine" if c and c.get("owner") == lease.me()["owner"] else "")' 2>/dev/null)"
        python3 "$REPO/scripts/utmbot" lock acquire --purpose "provision $NAME" >/dev/null ||
            die "UTM is in use by another session (python3 $REPO/scripts/utmbot lock status)"
        [ "$held" = mine ] || LEASED=1
    fi
    local s started=""
    for s in "${STAGES[@]}"; do
        if [ -n "$ONLY" ]; then
            [ "$s" = "$ONLY" ] || continue
        else
            [ "$s" = "$FROM" ] && started=1
            [ -n "$started" ] || continue
        fi
        STAGE="$s"
        log "Stage: $s"
        if [ "$s" != media ] && [ "$s" != vm ] && [ "$s" != install ] && [ -z "${IP:-}" ]; then
            [ "$(utm status "$NAME")" != stopped ] || utm start "$NAME"
            wait_ssh 20
        fi
        "stage_$s"
        [ -n "$ONLY" ] || echo "$s" > "$STATE/last-stage"
    done
    # a finished run claims this Mac's test or dev VM role if it is free
    # (`utmbot machine`; the skills ask for the role, not a VM name)
    if [ -z "$ONLY" ] && [ "$STAGE" = "${STAGES[${#STAGES[@]}-1]}" ] && { [ "$ROLE" = test ] || [ "$ROLE" = dev ]; }; then
        python3 "$REPO/scripts/utmbot" machine claim "${ROLE}_vm" "$NAME" || true
    fi
}

on_exit() {
    local rc=$?
    [ -z "$MOUNT" ] || hdiutil detach "$MOUNT" -quiet 2>/dev/null || true
    [ -z "${LEASED:-}" ] || python3 "$REPO/scripts/utmbot" lock release >/dev/null 2>&1 || true
    if [ $rc -ne 0 ] && [ -n "$STAGE" ]; then
        echo "=== Stage $STAGE failed; after fixing the cause, resume with:" >&2
        local more=""
        [ ${#RESUME_ARGS[@]} -eq 0 ] || more=" $(printf '%q ' "${RESUME_ARGS[@]}")"
        if [ -n "$ONLY" ]; then
            echo "    $0 --name '$NAME'${more% } --only $STAGE" >&2
        else
            echo "    $0 --name '$NAME'${more% } --from $STAGE" >&2
        fi
    fi
}
