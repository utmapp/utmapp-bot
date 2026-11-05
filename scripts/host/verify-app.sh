#!/bin/bash
# verify-app.sh - prove what a UTM.app actually contains before testing it.
#
# usage: verify-app.sh [<UTM.app>] [--marker STRING ...] [--absent STRING ...]
#
# Default app: $UTM_APP, else ~/.utmapp-bot/utm-app (the last build-utm.sh),
# else /Applications/UTM.app, as utmbot picks it.  Checks:
#   - the graphics frameworks' build times, and for a build from a checkout,
#     that each is identical to the sysroot's (a dependency rebuilt after
#     the last build-utm.sh is not in the app);
#   - whether QEMULauncher has com.apple.vm.networking (without it VMs need
#     emulated networking; utmbot switches them for each boot);
#   - --marker: each STRING occurs in some embedded binary (put a unique
#     string in the code you changed; its absence means the change is not in
#     this app, whatever the build said);
#   - QEMURenderServer has both arm64 (Venus, DXMT) and x86_64 (D3DMetal
#     under Rosetta) slices;
#   - D3DMetal.framework is embedded (without it there is no D3DMetal backend);
#   - no embedded binary imports a library by an absolute non-system path;
#   - the code signature is valid.
set -uo pipefail

DATA="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}"
APP=""
MARKERS=()
ABSENT=()
while [ $# -gt 0 ]; do
    case "$1" in
        --marker) MARKERS+=("$2"); shift 2 ;;
        --absent) ABSENT+=("$2"); shift 2 ;;
        *) APP="$1"; shift ;;
    esac
done
# shellcheck source=../lib/utm.sh
. "$(dirname "$0")/../lib/utm.sh"
[ -n "$APP" ] || APP="$(utm_app)"
[ -d "$APP/Contents" ] || { echo "no app at $APP" >&2; exit 2; }
fail=0
bad() { echo "FAIL: $*"; fail=1; }
machouuid() { dwarfdump --uuid "$1" 2>/dev/null | awk '{ print $2, $3 }' | sort | tr '\n' ' '; }

echo "=== $APP ($(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null))"
FW="$APP/Contents/Frameworks"
for f in "$FW"/*.framework; do
    n="$(basename "$f" .framework)"
    b="$f/Versions/A/$n"; [ -f "$b" ] || b="$f/$n"
    [ -f "$b" ] && printf '  %-34s %s\n' "$n" "$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$b")"
done | grep -iE "virgl|qemu-aarch64|d3dmetal|dxmt|spice|epoxy|angle|GLES|MoltenVK" || true

# the app embeds copies of sysroot frameworks: compare contents (mtimes are copy times)
SYSROOT=""
utm_src=""
if [ "$APP" = "$(cat "$DATA/utm-app" 2>/dev/null)" ]; then
    utm_src="$(cat "$DATA/utm-checkout" 2>/dev/null || true)"
    # the sysroot build-utm.sh built from (arm64 or universal)
    SYSROOT="$(cat "$DATA/utm-sysroot" 2>/dev/null || true)"
    [ -d "$SYSROOT/Frameworks" ] || SYSROOT=""
fi
for cand in "$utm_src" "${UTM_CHECKOUT:-}"; do
    [ -z "$SYSROOT" ] || break
    [ -n "$cand" ] && [ -d "$cand/sysroot-macOS-arm64/Frameworks" ] && { SYSROOT="$cand/sysroot-macOS-arm64"; break; }
    [ -n "$cand" ] && [ -d "$cand/sysroot-macOS-arm64_x86_64/Frameworks" ] && { SYSROOT="$cand/sysroot-macOS-arm64_x86_64"; break; }
done
if [ -n "$SYSROOT" ]; then
    stale=""
    for f in "$SYSROOT"/Frameworks/*.framework; do
        n="$(basename "$f" .framework)"
        src="$f/Versions/A/$n"; [ -f "$src" ] || src="$f/$n"
        dst="$FW/$n.framework/Versions/A/$n"; [ -f "$dst" ] || dst="$FW/$n.framework/$n"
        [ -f "$src" ] && [ -f "$dst" ] || continue
        # the app build re-signs its copy: compare the Mach-O UUIDs, which signing keeps
        [ "$(machouuid "$src")" = "$(machouuid "$dst")" ] || stale="$stale $n"
    done
    [ -z "$stale" ] && echo "  frameworks: identical to $SYSROOT" || bad "frameworks differ from $SYSROOT (rebuild UTM after rebuild-dep):$stale"
fi

if app_has_vmnet "$APP"; then
    echo "  QEMULauncher: com.apple.vm.networking"
else
    echo "  note: QEMULauncher lacks com.apple.vm.networking (ad-hoc build): no shared/host-only"
    echo "        networks, so utmbot uses emulated networking and kdnet is unavailable; for vmnet"
    echo "        set up CodeSigning.xcconfig with the VM entitlement profiles and build --clean"
fi

# the first embedded binary that contains the string, relative to the app
binary_with() {
    local b
    while IFS= read -r -d '' b; do
        if LC_ALL=C grep -qF -- "$1" "$b" 2>/dev/null; then printf '%s\n' "${b#"$APP/"}"; return; fi
    done < <(find "$APP/Contents" -type f \( -perm -u+x -o -name '*.dylib' \) -print0)
}

for m in "${MARKERS[@]+"${MARKERS[@]}"}"; do
    hit="$(binary_with "$m")"
    if [ -n "$hit" ]; then echo "  marker '$m': $hit"; else bad "marker '$m' is in no embedded binary (stale build?)"; fi
done

# --absent: a change that was taken out again is really gone
for m in "${ABSENT[@]+"${ABSENT[@]}"}"; do
    hit="$(binary_with "$m")"
    if [ -z "$hit" ]; then echo "  absent '$m': in no embedded binary"; else bad "'$m' is still in $hit"; fi
done

RS="$(find "$APP/Contents" -path '*QEMURenderServer.app/Contents/MacOS/*' -type f | head -1)"
if [ -n "$RS" ]; then
    archs="$(lipo -archs "$RS" 2>/dev/null)"
    echo "  QEMURenderServer archs: $archs"
    [[ "$archs" == *arm64* ]] || bad "QEMURenderServer has no arm64 slice"
    [[ "$archs" == *x86_64* ]] || echo "  WARNING: no x86_64 slice: no D3DMetal backend"
else
    bad "no QEMURenderServer in the app"
fi
[ -d "$FW/D3DMetal.framework" ] && echo "  D3DMetal.framework: $(defaults read "$FW/D3DMetal.framework/Versions/A/Resources/Info" CFBundleShortVersionString 2>/dev/null || echo present)" \
    || echo "  WARNING: no D3DMetal.framework: DirectX uses DXMT only and D3D12 is unavailable"
for n in d3dmetal-native dxmt-native; do
    b="$FW/$n.framework/Versions/A/$n"
    [ -f "$b" ] || continue
    # per slice: a universal build may carry an empty stub for one architecture
    for a in $(lipo -archs "$b" 2>/dev/null); do
        sym="$(nm -gU -arch "$a" "$b" 2>/dev/null | grep -o '_[a-z_]*native_abi_version' | head -1)"
        echo "  $n ($a) exports ${sym:-no native_abi_version (not a usable backend slice)}"
    done
done

abs="$(find "$FW" -type f -perm -u+x -print0 | xargs -0 otool -L 2>/dev/null | awk '/^\t\// && !/^\t\/(System|usr\/lib)\// { print $1 }' | sort -u)"
[ -z "$abs" ] || bad "embedded binaries import absolute paths (fixup.sh not run?): $abs"

codesign --verify --deep --strict "$APP" 2>/dev/null && echo "  signature: valid" || echo "  WARNING: signature does not verify (ok for local Debug builds that are not re-sealed)"
[ $fail -eq 0 ] && echo "VERIFY-APP PASS" || echo "VERIFY-APP FAIL"
exit $fail
