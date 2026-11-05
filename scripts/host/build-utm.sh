#!/bin/bash
# build-utm.sh - build a Debug UTM.app from a checkout and record it for utmbot.
#
# usage: build-utm.sh <UTM checkout> [--universal] [--clean] [--unsigned] [--derived-data DIR]
#
# Incremental xcodebuild of the macOS scheme (Debug: get-task-allow lets LLDB
# attach to QEMULauncher and the render-server workers).  The app embeds the
# frameworks in sysroot-macOS-*/Frameworks at build time, so rebuild after
# every rebuild-dep.py.  On success the app's path is written to
# ~/.utmapp-bot/utm-app, which utmbot drives from then on (UTM_APP overrides).
#
# Signing follows the checkout's CodeSigning.xcconfig; without one the build
# is ad-hoc signed and has no vmnet entitlement.  --unsigned forces that
# (ignoring CodeSigning.xcconfig, in its own derived data, not recorded) to
# test what a developer without VM entitlement profiles gets.
set -euo pipefail

DATA="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}"
[ $# -ge 1 ] || { sed -n '2,15p' "$0" >&2; exit 2; }
UTM="$(cd "$1" && pwd)"; shift
DD=""
UNSIGNED=""
ARCHS=arm64
CLEAN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --derived-data) DD="$2"; shift 2 ;;
        # arm64 + x86_64: the x86_64 render server is what runs D3DMetal
        --universal) ARCHS="arm64 x86_64"; shift ;;
        # after a signing change: incremental builds keep a stale ad-hoc
        # QEMULauncher.app inside QEMUHelper.xpc
        --clean) CLEAN=clean; shift ;;
        --unsigned) UNSIGNED=1; shift ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done

[ -d "$UTM/UTM.xcodeproj" ] || { echo "$UTM is not a UTM checkout" >&2; exit 1; }
SYSROOT="$UTM/sysroot-macOS-${ARCHS// /_}"
[ -d "$SYSROOT/Frameworks" ] || { echo "no $SYSROOT/Frameworks: download the Sysroot-macos-universal (--universal) or Sysroot-macOS-arm64 artifact of a recent GitHub Actions run into the checkout, or build dependencies once" >&2; exit 1; }
if [ "$ARCHS" = arm64 ] && [ -d "$UTM/sysroot-macOS-arm64_x86_64/Frameworks" ]; then
    echo "note: a universal sysroot exists; pass --universal to build with D3DMetal" >&2
fi
# rebuild-dep.py installs into sysroot-macOS-arm64 (the only one with build
# trees); a universal build embeds the other sysroot and would leave it out
if [ "$ARCHS" != arm64 ] && [ -d "$UTM/sysroot-macOS-arm64/Frameworks" ] &&
   [ -n "$(find "$UTM/sysroot-macOS-arm64/Frameworks" -maxdepth 3 -newer "$SYSROOT/Frameworks" -type f -print -quit 2>/dev/null)" ]; then
    echo "WARNING: sysroot-macOS-arm64 has frameworks newer than $SYSROOT (a rebuild-dep.py?)." >&2
    echo "         This --universal build embeds $SYSROOT, without them. Build arm64-only to test" >&2
    echo "         the rebuilt dependency (DXMT/Venus), and confirm with verify-app.sh --marker" >&2
fi
if [ "$ARCHS" = arm64 ]; then
    echo "WARNING: arm64-only build: no D3DMetal backend (it needs the x86_64 render server);" >&2
    echo "         test with --backend dxmt, or build --universal from Sysroot-macos-universal" >&2
elif [ ! -d "$SYSROOT/Frameworks/D3DMetal.framework" ]; then
    echo "WARNING: no D3DMetal.framework in the sysroot: this build has no D3DMetal backend" >&2
    echo "         (DirectX falls back to DXMT and D3D12 apps fail). Install it with" >&2
    echo "         $UTM/scripts/install_d3dmetal.sh $SYSROOT <Game Porting Toolkit DMG>" >&2
fi

mkdir -p "$DATA"
LOG="$DATA/utm-build.log"
# plugin/macro validation needs an interactive "Trust & Enable"; UTM's own
# build_utm.sh skips it the same way
args=(-project "$UTM/UTM.xcodeproj" -scheme macOS -configuration Debug -destination 'generic/platform=macOS'
      "ARCHS=$ARCHS" ONLY_ACTIVE_ARCH=NO -skipPackagePluginValidation -skipMacroValidation)
if [ -n "$UNSIGNED" ]; then
    # empty values fall back to the project's ad-hoc defaults; the same
    # bundle id as the release, like a checkout without CodeSigning.xcconfig
    args+=(CODE_SIGN_IDENTITY_MAC=- DEVELOPMENT_TEAM= DEVELOPER_ACCOUNT_VM_ACCESS=NO
           DEVELOPER_ACCOUNT_PAID=NO PROVISIONING_PROFILE_SPECIFIER_MAC=
           PROVISIONING_PROFILE_SPECIFIER_HELPER= PROVISIONING_PROFILE_SPECIFIER_LAUNCHER=
           PRODUCT_BUNDLE_PREFIX=com.utmapp)
    DD="${DD:-$DATA/unsigned/DerivedData}"
fi
[ -n "$DD" ] && args+=(-derivedDataPath "$DD")
echo "=== xcodebuild (log: $LOG)"
set +e
xcodebuild "${args[@]}" $CLEAN build > "$LOG" 2>&1
rc=$?
set -e
# a failed build leaves the last good app on disk: judge by the result line
grep -E "error:|^\*\* BUILD" "$LOG" | sort -u | tail -20
# failures without an error: line (plugin validation, signing) are only in this summary
sed -n '/^The following build commands failed:/,/failures)$/p' "$LOG" | head -20
if grep -q errSecInternalComponent "$LOG"; then
    echo "=== codesign could not use the signing key (errSecInternalComponent): the login" >&2
    echo "    keychain is locked (a locked screen, or an SSH session). Unlock the Mac, or run" >&2
    echo "    security unlock-keychain ~/Library/Keychains/login.keychain-db, and build again" >&2
fi
grep -q '^\*\* BUILD SUCCEEDED' "$LOG" && [ $rc -eq 0 ] || { echo "=== BUILD FAILED" >&2; exit 1; }

settings="$(xcodebuild "${args[@]}" -showBuildSettings 2>/dev/null)"
dir="$(printf '%s\n' "$settings" | awk -F' = ' '$1 ~ /^ *BUILT_PRODUCTS_DIR$/ { print $2; exit }')"
APP="$dir/UTM.app"
[ -d "$APP" ] || { echo "built app not found at $APP" >&2; exit 1; }
if [ -n "$UNSIGNED" ]; then
    # a test flavor: leave the recorded app, which every later run drives, alone
    echo "=== BUILD OK: $APP (not recorded; drive it with UTM_APP='$APP')"
else
    printf '%s\n' "$APP" > "$DATA/utm-app"
    printf '%s\n' "$UTM" > "$DATA/utm-checkout"
    printf '%s\n' "$SYSROOT" > "$DATA/utm-sysroot"
    echo "=== BUILD OK: $APP (recorded in $DATA/utm-app)"
fi
echo "    quit any running UTM before testing: release and dev builds share a bundle id"
. "$(dirname "$0")/../lib/utm.sh"
if ! app_has_vmnet "$APP"; then
    echo "    no vmnet entitlement (ad-hoc signed): utmbot runs VMs with emulated networking"
    echo "    and SSH port forwards, and there is no kdnet kernel debugging."
fi
