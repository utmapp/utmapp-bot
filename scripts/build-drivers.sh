#!/usr/bin/env bash
#
# Build the Neptune user-mode driver (Mesa) and the viogpu3d kernel-mode driver
# on a Windows build VM over SSH, then pull the verified, test-signed driver
# package(s) back to this host.
#
# The VM must already have the toolchain installed (Visual Studio with the C++
# workload and ARM64/ARM64EC/Spectre libraries, WDK, Python, CMake, Git) and
# accept SSH key authentication.  The Mesa and KMD sources are given as paths on
# the VM (typically a share of this host); they are only read.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# State lives outside the plugin, whose directory is replaced on every update.
DATA="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}"
BUILD_MESA_URL=https://github.com/osy/build-mesa.git
BUILD_MESA="$DATA/build-mesa"

usage() {
    cat <<'EOF'
usage: build-drivers.sh [options] <dev VM name | [user@]host[:port]> <arch> <mesa-path> <kmd-path>

  dev VM name     a VM made by provision-windows-dev.sh, reached as utmbot
                  reaches it; or [user@]host[:port], any Windows build VM
  arch            arm64x | x64 | all
  mesa-path       Mesa source tree, as a path on the VM (e.g. Z:\virtio-win-mesa)
  kmd-path        kvm-guest-drivers-windows tree, as a path on the VM

options:
  --out DIR            where packages are written on this host
                       (default: ~/.utmapp-bot/drivers)
  --guest-workdir DIR  build directory on the VM; keep it between runs so the
                       builds stay incremental (default: %USERPROFILE%\utmapp-bot)
EOF
}

OUT="$DATA/drivers"
WORK=""
POSITIONAL=()
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --guest-workdir) WORK="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
if [ ${#POSITIONAL[@]} -ne 4 ]; then
    usage >&2
    exit 2
fi
HOST="${POSITIONAL[0]}"
ARCH="${POSITIONAL[1]}"
MESA="${POSITIONAL[2]}"
KMD="${POSITIONAL[3]}"

case "$ARCH" in
    arm64x|x64|all) ;;
    *) echo "unknown arch '$ARCH', expected arm64x, x64 or all" >&2; exit 2 ;;
esac

# *** get the latest build-mesa ***

if [ -d "$BUILD_MESA/.git" ]; then
    echo "=== Updating build-mesa"
    git -C "$BUILD_MESA" fetch -q --depth 1 origin HEAD
    git -C "$BUILD_MESA" reset -q --hard FETCH_HEAD
else
    echo "=== Cloning build-mesa"
    rm -rf "$BUILD_MESA"
    mkdir -p "$DATA"
    git clone -q --depth 1 "$BUILD_MESA_URL" "$BUILD_MESA"
fi
[ -f "$BUILD_MESA/build.cmd" ] || { echo "$BUILD_MESA has no build.cmd" >&2; exit 1; }
echo "    build-mesa $(git -C "$BUILD_MESA" rev-parse --short HEAD)"

# shellcheck source=lib/devvm.sh
. "$REPO/scripts/lib/devvm.sh"
devvm_connect "$HOST"

[ -n "$WORK" ] || WORK="$(devvm_profile)\\utmapp-bot"
# scp wants forward slashes for Windows paths.
WORK_SCP="${WORK//\\//}"

# *** upload build-mesa and the guest scripts ***

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/build-mesa" "$STAGE/scripts"
git -C "$BUILD_MESA" ls-files -z |
    tar -C "$BUILD_MESA" --null -T - -cf - | tar -C "$STAGE/build-mesa" -xf -
cp "$REPO"/scripts/guest/* "$STAGE/scripts/"
find "$STAGE" \( -name '*.cmd' -o -name '*.bat' -o -name '*.ps1' \) -print0 |
    while IFS= read -r -d '' f; do crlf "$f"; done

echo "=== Uploading build scripts to $HOST:$WORK"
gssh "if not exist \"$WORK\" mkdir \"$WORK\""
gscp -r "$STAGE/build-mesa" "$STAGE/scripts" "$HOST:$WORK_SCP/"

# *** build on the VM ***

mkdir -p "$OUT"
LOG="$OUT/build-$ARCH.log"
echo "=== Building $ARCH on $HOST (log: $LOG)"
set +e
gssh "call \"$WORK\\scripts\\build-drivers.cmd\" $ARCH \"$MESA\" \"$KMD\" \"$WORK\"" 2>&1 | perl -pe 'BEGIN { $| = 1 } s/\r//g' | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e
if [ "$rc" -ne 0 ]; then
    echo "=== Build failed (exit $rc); see $LOG" >&2
    exit "$rc"
fi

# *** pull the packages ***

version="$(sed -n 's/^DRIVER_VERSION //p' "$LOG" | tail -1)"
mesa_rev="$(sed -n 's/^MESA_REVISION //p' "$LOG" | tail -1)"
kmd_rev="$(sed -n 's/^KMD_REVISION //p' "$LOG" | tail -1)"
# a git worktree's .git is a file naming a path on this Mac, which the guest
# cannot follow: read the revision here (Z: is the host's ~/Documents)
host_rev() {
    local p="$1"
    case "$p" in [Zz]:\*|[Zz]:/*) p="$HOME/Documents/$(printf '%s' "${p:3}" | tr '\\' '/')" ;; *) return 0 ;; esac
    git -C "$p" rev-parse HEAD 2>/dev/null || true
}
[ -n "$mesa_rev" ] || mesa_rev="$(host_rev "$MESA")"
[ -n "$kmd_rev" ] || kmd_rev="$(host_rev "$KMD")"
packages=()
while read -r target dir; do
    [ -n "$target" ] || continue
    echo "=== Pulling $target package"
    rm -rf "${OUT:?}/$target"
    gscp -r "$HOST:${dir//\\//}" "$OUT/"
    packages+=("$target")
done < <(sed -n 's/^PACKAGE //p' "$LOG")
if [ ${#packages[@]} -eq 0 ]; then
    echo "=== No packages reported; see $LOG" >&2
    exit 1
fi

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
{
    echo "{"
    echo "  \"driver_version\": $(json_str "$version"),"
    echo "  \"arch\": $(json_str "$ARCH"),"
    printf '  "packages": ['
    sep=""
    for p in "${packages[@]}"; do printf '%s%s' "$sep" "$(json_str "$p")"; sep=", "; done
    echo "],"
    echo "  \"mesa\": { \"path\": $(json_str "$MESA"), \"revision\": $(json_str "$mesa_rev") },"
    echo "  \"kmd\": { \"path\": $(json_str "$KMD"), \"revision\": $(json_str "$kmd_rev") },"
    echo "  \"built_at\": $(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    echo "}"
} > "$OUT/build-info.json"

echo "=== Done: driver version $version"
for p in "${packages[@]}"; do echo "    $OUT/$p"; done
echo "    $OUT/build-info.json"
