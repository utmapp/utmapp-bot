#!/bin/bash
# build-probes.sh - build the probes in probes/ on a Windows dev VM.
#
# usage: build-probes.sh <dev VM name | [user@]host[:port]>
#
# Uploads probes/*.c to %USERPROFILE%\utmapp-bot\probes on the VM, builds each
# for ARM64 (native: the only build that exercises the driver natively) and x64
# (emulated: what x64 apps see), and pulls the executables to
# ~/.utmapp-bot/probes/{ARM64,x64}/, where the utmbot harness looks for them.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}"
OUT="$DATA/probes"

[ $# -eq 1 ] || { sed -n '2,9p' "$0" >&2; exit 2; }
HOST="$1"

# shellcheck source=lib/devvm.sh
. "$REPO/scripts/lib/devvm.sh"
devvm_connect "$HOST"

WORK="$(devvm_profile)\\utmapp-bot\\probes"
WORK_SCP="${WORK//\\//}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/probes"
cp "$REPO"/probes/*.c "$REPO"/probes/*.h "$STAGE/probes/"
cp "$REPO/scripts/guest/build-probes.cmd" "$STAGE/probes/"
crlf "$STAGE/probes/build-probes.cmd"

echo "=== Uploading probes to $HOST:$WORK"
gssh "if exist \"$WORK\" rmdir /s /q \"$WORK\"" || true
gssh "mkdir \"$WORK\""
gscp "$STAGE"/probes/* "$HOST:$WORK_SCP/"

echo "=== Building"
mkdir -p "$OUT"
set +e
gssh "call \"$WORK\\build-probes.cmd\" \"$WORK\"" 2>&1 | tr -d '\r' | tee "$OUT/build.log"
set -e
grep -q '^PROBES_OK' "$OUT/build.log" || { echo "=== Build failed; see $OUT/build.log" >&2; exit 1; }

for a in ARM64 x64; do
    rm -rf "${OUT:?}/$a"
    mkdir -p "$OUT/$a"
    gscp "$HOST:$WORK_SCP/$a/*.exe" "$OUT/$a/"
done
echo "=== Done: $(ls "$OUT/ARM64" | tr '\n' ' ')"
echo "    $OUT/ARM64  $OUT/x64"
