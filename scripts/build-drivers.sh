#!/usr/bin/env bash
#
# Build the viogpu3d driver with utmapp/build-viogpu3d on the Windows dev VM,
# from checkouts on this Mac, and pull the packages back.
#
# The VM keeps its own clones of the three repositories on its own disk
# (never the SSHFS share: the kernel driver builds in place, and builds on the
# share can hang it).  Each run moves them to exactly what the Mac's checkouts
# hold: the same commit, and the same uncommitted changes, which reach the VM
# as a work-in-progress commit in a git bundle.  build.ps1 then stamps the
# binaries with the commit, and a "+" for uncommitted changes, as it would on
# any other machine.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# State lives outside the plugin, whose directory is replaced on every update.
DATA="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}"

usage() {
    cat <<'EOF'
usage: build-drivers.sh [options] <dev VM name | [user@]host[:port]> [build.ps1 arguments ...]

  dev VM name     a VM made by provision-windows-dev.sh, reached as utmbot
                  reaches it; or [user@]host[:port], any Windows build VM
  build.ps1 arguments
                  passed through, e.g. -Arch arm64x,x86  -Target kmd
                  -Os Win11  -Configuration Debug  -Clean  -Target all,viogpudo
                  (default: build.ps1's own, everything for the VM's arch: arm64)

options (checkouts on this Mac; each defaults to ~/Documents/<name>):
  --build DIR          utmapp/build-viogpu3d
  --kmd DIR            utmapp/viogpu-wddm
  --umd DIR            utmapp/mesa
  --out DIR            where packages are written on this host
                       (default: ~/.utmapp-bot/drivers)
  --guest-workdir DIR  where the VM keeps its clones and build trees; keep it
                       between runs so builds stay incremental
                       (default: %USERPROFILE%\viogpu3d)
EOF
}

BUILD_SRC="$HOME/Documents/build-viogpu3d"
KMD_SRC="$HOME/Documents/viogpu-wddm"
UMD_SRC="$HOME/Documents/mesa"
OUT="$DATA/drivers"
WORK=""
HOST=""
BUILD_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --build) BUILD_SRC="$2"; shift 2 ;;
        --kmd) KMD_SRC="$2"; shift 2 ;;
        --umd) UMD_SRC="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --guest-workdir) WORK="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *)
            if [ -z "$HOST" ]; then HOST="$1"; else BUILD_ARGS+=("$1"); fi
            shift ;;
    esac
done
[ -n "$HOST" ] || { usage >&2; exit 2; }
for a in "${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}"; do
    case "$a" in *[[:space:]\"\'\;\&\|]*) echo "build.ps1 argument '$a': use commas for lists, no spaces or quotes" >&2; exit 2 ;; esac
done

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# *** what to build: commit, and uncommitted changes, of each checkout ***

# Prints "commit[:wip]" for checkout $2 (repository utmapp/$1) and writes
# $STAGE/$1.bundle with whatever GitHub's copy may lack.
snapshot() {
    local name="$1" dir="$2" head tree wip="" idx remote target
    git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || { echo "$dir is not a git checkout (utmapp/$name)" >&2; return 1; }
    head="$(git -C "$dir" rev-parse HEAD)"
    # The working tree as a tree object, built in a scratch index so the
    # checkout's own index and refs are left alone.  Untracked files that are
    # not ignored count: they would be in a build on this Mac too.
    idx="$STAGE/$name.index"
    cp "$(git -C "$dir" rev-parse --path-format=absolute --git-path index)" "$idx" 2>/dev/null || true
    (cd "$dir" && GIT_INDEX_FILE="$idx" git add -A . >/dev/null)
    tree="$(cd "$dir" && GIT_INDEX_FILE="$idx" git write-tree)"
    if [ "$tree" != "$(git -C "$dir" rev-parse "HEAD^{tree}")" ]; then
        wip="$(git -C "$dir" commit-tree "$tree" -p "$head" -m "utmapp-bot build: uncommitted changes of $dir")"
    fi
    target="${wip:-$head}"
    # Bundle what is not on the org's repository already, as this checkout
    # last saw it (the VM fetches that from GitHub first).
    remote="$(git -C "$dir" remote -v | awk -v n="$name" '$2 ~ ("github\\.com[:/]utmapp/" n "(\\.git)?$") { print $1; exit }')"
    git -C "$dir" update-ref refs/utmbot/build "$target"
    local not=()
    [ -n "$remote" ] && not=(--not "--remotes=$remote")
    [ -n "$remote" ] || echo "    $name: no remote for utmapp/$name in $dir; sending its whole history" >&2
    if ! git -C "$dir" bundle create -q "$STAGE/$name.bundle" refs/utmbot/build "${not[@]+"${not[@]}"}" 2>"$STAGE/$name.bundle.err"; then
        # nothing to send: the commit is already on GitHub
        grep -qi "empty bundle" "$STAGE/$name.bundle.err" || { cat "$STAGE/$name.bundle.err" >&2; git -C "$dir" update-ref -d refs/utmbot/build; return 1; }
        rm -f "$STAGE/$name.bundle"
    fi
    git -C "$dir" update-ref -d refs/utmbot/build
    printf '%s%s\n' "$head" "${wip:+:$wip}"
}

echo "=== Reading the checkouts"
REVS=""
for pair in "build-viogpu3d=$BUILD_SRC" "viogpu-wddm=$KMD_SRC" "mesa=$UMD_SRC"; do
    name="${pair%%=*}"; dir="${pair#*=}"
    spec="$(snapshot "$name" "$dir")"
    note=""
    case "$spec" in *:*) note=" + uncommitted changes" ;; esac
    [ -f "$STAGE/$name.bundle" ] && note="$note (bundle $(du -h "$STAGE/$name.bundle" | cut -f1 | tr -d ' '))"
    echo "    $name  $dir  ${spec:0:10}$note"
    REVS="${REVS:+$REVS,}$name=$spec"
done
rm -f "$STAGE"/*.index "$STAGE"/*.err
printf '%s\r\n' "${BUILD_ARGS[*]+"${BUILD_ARGS[*]}"}" > "$STAGE/build-args.txt"
cp "$REPO/scripts/guest/build-viogpu3d.ps1" "$STAGE/"
crlf() { perl -pi -e 's/\r?\n/\r\n/' "$1"; }
crlf "$STAGE/build-viogpu3d.ps1"

# shellcheck source=lib/devvm.sh
. "$REPO/scripts/lib/devvm.sh"
devvm_connect "$HOST"
[ -n "$WORK" ] || WORK="$(devvm_profile)\\viogpu3d"
WORK_SCP="${WORK//\\//}"

echo "=== Uploading to $HOST:$WORK\\host"
gssh "if exist \"$WORK\\host\" rmdir /s /q \"$WORK\\host\"" >/dev/null
gssh "mkdir \"$WORK\\host\"" >/dev/null
gscp "$STAGE"/* "$HOST:$WORK_SCP/host/"

# *** build on the VM ***

mkdir -p "$OUT"
LOG="$OUT/build.log"
echo "=== Building on $HOST (log: $LOG)"
set +e
gssh "powershell -NoProfile -ExecutionPolicy Bypass -File \"$WORK\\host\\build-viogpu3d.ps1\" -Work \"$WORK\" -Revisions $REVS -BuildArgsFile \"$WORK\\host\\build-args.txt\"" 2>&1 |
    perl -pe 'BEGIN { $| = 1 } s/\r//g; s/\e\[[0-9;]*m//g' | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e
if [ "$rc" -ne 0 ]; then
    echo "=== Build failed (exit $rc); see $LOG" >&2
    exit "$rc"
fi

# *** pull the packages ***

outdir="$(sed -n 's/^BUILD-OUT //p' "$LOG" | tail -1)"
symdir="$(sed -n 's/^BUILD-SYMBOLS //p' "$LOG" | tail -1)"
[ -n "$outdir" ] || { echo "=== build.ps1 reported no output directory; see $LOG" >&2; exit 1; }
osdir="${outdir##*\\}"
# The packages this run made, from build.ps1's summary ("    viogpu3d-arm64x: C:\...").
built=()
while read -r p; do built+=("$p"); done < <(sed -n -E 's/^ +((viogpu3d|viogpudo)-[a-z0-9]+): [A-Za-z]:\\.*/\1/p' "$LOG" | sort -u)

echo "=== Pulling $outdir"
# into a scratch directory first, so a failed copy leaves the last packages
rm -rf "${OUT:?}/.pull"
mkdir -p "$OUT/.pull"
gscp -r "$HOST:${outdir//\\//}" "$OUT/.pull/"
rm -rf "${OUT:?}/$osdir"
mv "$OUT/.pull/$osdir" "$OUT/$osdir"
rm -rf "${OUT:?}/.pull"
if [ -n "$symdir" ] && gssh "if exist \"$symdir\" (exit 0) else (exit 1)" >/dev/null 2>&1; then
    rm -rf "${OUT:?}/symbols"
    gscp -r "$HOST:${symdir//\\//}" "$OUT/"
fi

echo "=== Done"
if [ ${#built[@]} -eq 0 ]; then
    echo "    no package this run (user-mode files only?); see $LOG"
fi
for p in "${built[@]+"${built[@]}"}"; do
    m="$OUT/$osdir/$p.json"
    printf 'PACKAGE %s' "$OUT/$osdir/$p"
    [ -f "$m" ] && python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); r=m.get("revisions",{}); print("  version %s  %s" % (m.get("version"), "  ".join("%s %s" % kv for kv in r.items())))' "$m" || echo
done
[ -d "$OUT/symbols" ] && echo "SYMBOLS $OUT/symbols"
