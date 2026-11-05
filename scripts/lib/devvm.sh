# devvm.sh - SSH to the Windows dev VM a build script builds on
# (build-drivers.sh, build-probes.sh).  Sourced; needs REPO.
#
#   devvm_connect <dev VM name | [user@]host[:port]>
#                  HOST and SSH_OPTS for gssh and gscp: a VM the plugin
#                  provisioned is reached as utmbot reaches it (resolve_ssh in
#                  utm.sh), anything else as given; keepalives carry multi-hour
#                  first builds
#   devvm_profile  the VM user's %USERPROFILE%

# shellcheck source=utm.sh
. "$REPO/scripts/lib/utm.sh"

devvm_connect() {
    resolve_ssh "$1" || exit 1
    SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=60 -o ServerAliveCountMax=10
              "${SSH_TARGET_OPTS[@]+"${SSH_TARGET_OPTS[@]}"}")
    HOST="$SSH_HOST"
}

gssh() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }
gscp() { scp -q "${SSH_OPTS[@]}" "$@"; }

devvm_profile() {
    local profile
    profile="$(gssh 'echo %USERPROFILE%' | tr -d '\r')"
    [ -n "$profile" ] || { echo "could not read %USERPROFILE% on $HOST" >&2; exit 1; }
    printf '%s\n' "$profile"
}
