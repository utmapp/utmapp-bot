# utm.sh - host-side facts about UTM and its VMs, shared by the provisioning
# and build scripts (sourced: functions utm_app, app_has_vmnet,
# app_scripting_missing, guest_ip, free_port, unsigned_shmem_group,
# slug, crlf, vm_ssh_opts, resolve_ssh) and the utmbot harness (run as
# `bash utm.sh <verb> ...`):
#
#   app             the UTM.app to drive: $UTM_APP, else the last build of
#                   build-utm.sh (~/.utmapp-bot/utm-app), else
#                   /Applications/UTM.app
#   vmnet [<app>]   "yes" when the app can use vmnet networks (shared,
#                   host-only, bridged): its QEMULauncher has
#                   com.apple.vm.networking.  The release app has it; a
#                   build without VM entitlement profiles (ad-hoc signed)
#                   does not, and its VMs need emulated networking with a
#                   port forward for SSH.
#   ip <mac>        the address the host's DHCP server leased to that MAC
#                   (shared and host-only vmnet networks both lease from it)
#   bundle <name>   the .utm bundle UTM has registered under that name
#   free-port <n>   the first TCP port from <n> nothing listens on
#   shmem-group [<app>]  for an unsigned app, the app group virglrenderer
#                   must get as APP_SANDBOX_GROUP_ID (empty otherwise; see
#                   unsigned_shmem_group)

utm_app() {
    local data="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}" app="${UTM_APP:-}"
    if [ -z "$app" ] && [ -f "$data/utm-app" ]; then
        app="$(cat "$data/utm-app")"
        [ -d "$app" ] || app=""
    fi
    printf '%s\n' "${app:-/Applications/UTM.app}"
}

app_has_vmnet() {
    local app="${1:-$(utm_app)}" launcher
    launcher="$(ls -d "$app"/Contents/XPCServices/*/Contents/MacOS/QEMULauncher.app 2>/dev/null | head -1)"
    [ -n "$launcher" ] && codesign -d --entitlements - "$launcher" 2>/dev/null | grep -q com.apple.vm.networking
}

# The scripting terms the plugin uses that the app's dictionary lacks, by
# name, one per line; nothing on a current UTM, which has them all.  Looked
# up by their codes.
app_scripting_missing() {
    local app="${1:-$(utm_app)}" sdef term
    sdef="$(sdef "$app" 2>/dev/null)" || sdef=""
    [ -n "$sdef" ] || { echo "scripting dictionary"; return 0; }
    for term in "install guest tools:UTMvigts" "environment:EnVi" "capture screen:UTMiCpSc" "file:file" \
                "debug log:DbLg" "tpm:TpMd" "tso:TsOr" "rtc local time:RtLt" "sound devices:SnDs"; do
        grep -q "code=\"${term#*:}\"" <<<"$sdef" || printf '%s\n' "${term%%:*}"
    done
}

# The address of a MAC.  Candidates are the ARP entries on the vmnet bridges
# (systemd-networkd leases by DUID, so its lease has no MAC) and the lease
# that expires last (a VM recreated with the same MAC leaves older leases
# behind).  ARP entries outlive an address change by ~20 minutes, so with
# several candidates the one that answers wins.  ARP and dhcpd_leases write
# MAC octets without leading zeros; dhcpd_leases prefixes the hardware type
# ("1,").
guest_ip() {
    local mac lease cands ip
    mac="$(printf '%s' "${1:-$MAC}" | tr '[:upper:]' '[:lower:]' | sed -E 's/(^|:)0([0-9a-f])/\1\2/g')"
    lease="$(awk -v mac="1,$mac" '
        /^\{/ { ip = ""; hw = ""; ex = 0 }
        /ip_address=/ { sub(/.*ip_address=/, ""); ip = $0 }
        /hw_address=/ { sub(/.*hw_address=/, ""); hw = $0 }
        /lease=0x/ { sub(/.*lease=0x/, ""); ex = 0; n = length($0)
                     for (i = 1; i <= n; i++) ex = ex * 16 + index("0123456789abcdef", tolower(substr($0, i, 1))) - 1 }
        /^\}/ { if (hw == mac && ex >= best) { best = ex; found = ip } }
        END { if (found != "") print found }' /var/db/dhcpd_leases 2>/dev/null)"
    cands="$( { [ -z "$lease" ] || echo "$lease"
                arp -an 2>/dev/null | awk -v mac="$mac" '$4 == mac && $6 ~ /^bridge/ { gsub(/[()]/, "", $2); print $2 }'
              } | awk '!seen[$0]++')"
    [ -n "$cands" ] || return 0
    if [ "$(printf '%s\n' "$cands" | wc -l)" -gt 1 ]; then
        for ip in $cands; do
            if ping -c 1 -t 1 "$ip" >/dev/null 2>&1; then echo "$ip"; return; fi
        done
    fi
    printf '%s\n' "$cands" | head -1
}

# An unsigned UTM marks its app group "invalid." and so gives virglrenderer
# no APP_SANDBOX_GROUP_ID: the sandbox then denies its shared memory and the
# guest has no 3D (D3D and Venus).  Its entitlements claim the group without
# a team prefix, which works.  The scripts pass it in the start environment
# (same value, so harmless where UTM passes it).
unsigned_shmem_group() {
    local g
    g="$(plutil -extract AppGroupIdentifier raw "${1:-$(utm_app)}/Contents/Info.plist" 2>/dev/null)"
    case "$g" in invalid.*) printf '%s\n' "${g#invalid.}" ;; esac
}

slug() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//; s/-$//'; }

# Files for a Windows guest with CRLF line ends: cmd.exe misparses labels in
# LF-only batch files.
crlf() { perl -pi -e 's/\r?\n/\r\n/' "$@"; }

# vm_ssh_opts <state.env>: SSH_TARGET_OPTS for ssh/scp to a provisioned VM,
# as utmbot reaches it: its key, its known_hosts with the host keys under its
# name (HostKeyAlias: the address can change) and its port (the forward on
# 127.0.0.1 of a VM without vmnet, else 22).  Also sets VM_USER, VM_MAC and
# VM_SSH_PORT from the state.
vm_ssh_opts() {
    local st="$1" name key
    eval "$(. "$st"; printf 'name=%q VM_USER=%q key=%q VM_MAC=%q VM_SSH_PORT=%q\n' "$NAME" "${S_USER:-UTM}" "${S_PUBKEY:-}" "${MAC:-}" "${SSH_PORT:-}")"
    SSH_TARGET_OPTS=(-o "UserKnownHostsFile=$(dirname "$st")/known_hosts" -o "HostKeyAlias=$(slug "$name")"
                     -o StrictHostKeyChecking=accept-new -o "Port=${VM_SSH_PORT:-22}")
    [ -z "$key" ] || [ ! -f "${key%.pub}" ] || SSH_TARGET_OPTS+=(-i "${key%.pub}")
}

# resolve_ssh <VM name | [user@]host[:port]>: set SSH_HOST (user@host) and
# SSH_TARGET_OPTS for ssh/scp.  A VM the plugin knows is reached as
# vm_ssh_opts says, at its forwarded port on 127.0.0.1 or its DHCP address.
resolve_ssh() {
    local arg="$1" data="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}" st
    st="$data/provision/$(slug "$arg")/state.env"
    SSH_TARGET_OPTS=()
    if [ -f "$st" ]; then
        vm_ssh_opts "$st"
        if [ -n "$VM_SSH_PORT" ]; then SSH_HOST="$VM_USER@127.0.0.1"; else SSH_HOST="$VM_USER@$(guest_ip "$VM_MAC")"; fi
        [ "${SSH_HOST#*@}" != "" ] || { echo "no address for '$arg' (is it running?)" >&2; return 1; }
    else
        case "$arg" in
            *:*) SSH_TARGET_OPTS=(-o "Port=${arg##*:}"); SSH_HOST="${arg%:*}" ;;
            *) SSH_HOST="$arg" ;;
        esac
    fi
}

free_port() {
    local p="$1"
    while nc -z 127.0.0.1 "$p" 2>/dev/null; do p=$((p + 1)); done
    echo "$p"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        app) utm_app ;;
        vmnet) if app_has_vmnet "${2:-}"; then echo yes; else echo no; fi ;;
        ip) guest_ip "$2" ;;
        free-port) free_port "$2" ;;
        scripting-missing) app_scripting_missing "${2:-}" ;;
        shmem-group) unsigned_shmem_group "${2:-}" ;;
        *) echo "usage: $0 app | vmnet [<app>] | ip <mac> | free-port <n> | scripting-missing [<app>] | shmem-group [<app>]" >&2; exit 2 ;;
    esac
fi
