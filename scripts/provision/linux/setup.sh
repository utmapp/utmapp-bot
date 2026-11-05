#!/bin/bash
# setup.sh - runs on a Linux test VM over SSH, one step at a time, for
# provision-linux-test.sh.  Runs as the VM's user, who has passwordless sudo
# (cloud-init).  Every step is idempotent.
#
# usage: setup.sh <step> [args]
#
#   session   GNOME: no idle blank, lock or suspend; perf counters for profiling
#   fex       FEX-Emu (ppa:fex-emu/fex) and its Ubuntu 24.04 x86_64 rootfs
#   wine      Wine 10.9 staging-tkg x86_64 (Kron4ek build) in ~/wine-10.9-staging-tkg-amd64
#   tools     cross compilers and build dependencies
#   dxvk      DXVK 2.6.1 (x86_64 PE); its cross file builds Neptune too
#   mesa [BUNDLE]
#             Neptune's Windows DLLs (x86_64 PE) from ~/mesa, a clone of
#             osy/virtio-win-mesa (branch neptune); with BUNDLE (a git bundle
#             in ~) its neptune branch is checked out instead
#   nptunix   the x86_64 Wine unixlib that Neptune's PE DLLs call into
#   prefix    the Wine prefix ~/wine-prefixes/multi-backend/pfx, and the
#             3DMark definitions in it
#   verify    prints VERIFY OK or VERIFY FAILED
#
# The bin/ files (npt-wine, npt-3dmark, build-nptunix-x86) are copied to
# ~/bin by the host before every step.
set -euo pipefail

STEP="${1:?step}"
shift || true

TKG=wine-10.9-staging-tkg-amd64
TKG_URL=https://github.com/Kron4ek/Wine-Builds/releases/download/10.9/$TKG.tar.xz
TKG_SHA=ba924d6010984eafe53659e25050243fd04d170e58f69ac3b416d8a27d0eee44
MONO=wine-mono-10.1.0-x86.msi   # the wine-mono Wine 10.9 asks for
MONO_URL=https://dl.winehq.org/wine/wine-mono/10.1.0/$MONO
MONO_SHA=c88c2431890bc32cacec8d7ea70e53a5ae4b95c8c55ca6e75ef8db0e4ccf1070
DXVK_TAG=v2.6.1           # 3.0 needs VK_EXT_depth_clip_enable, which Venus on Apple lacks
MESA_URL=https://github.com/osy/virtio-win-mesa.git
# FEX keeps its rootfs in ~/.local/share/fex-emu (~/.fex-emu before)
rootfs() { ls -d "$HOME"/.local/share/fex-emu/RootFS/Ubuntu_24_04 "$HOME"/.fex-emu/RootFS/Ubuntu_24_04 2>/dev/null | head -1 || true; }
PFX=$HOME/wine-prefixes/multi-backend/pfx

apt_install() { sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -y -q "$@" >/dev/null; }
session_env() { export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus; }

case "$STEP" in
session)
    session_env
    gsettings set org.gnome.desktop.session idle-delay 0
    gsettings set org.gnome.desktop.screensaver lock-enabled false
    gsettings set org.gnome.desktop.screensaver idle-activation-enabled false
    gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
    echo 'kernel.perf_event_paranoid = 1' | sudo tee /etc/sysctl.d/99-perf.conf >/dev/null
    sudo sysctl -q -w kernel.perf_event_paranoid=1
    echo "SESSION OK"
    ;;
fex)
    if ! command -v FEXBash >/dev/null; then
        sudo add-apt-repository -y ppa:fex-emu/fex >/dev/null
        sudo apt-get update -q >/dev/null
        # armv8.4: the Apple M-series flavour
        apt_install fex-emu-armv8.4 fex-emu-binfmt32 fex-emu-binfmt64
    fi
    if [ -z "$(rootfs)" ] || [ ! -d "$(rootfs)/usr" ]; then
        # the x86_64 Ubuntu 24.04 rootfs, extracted (writable), made the
        # default; its x86_64 Mesa provides the Venus ICD that DXVK and
        # wined3d use.  --force-ui=tty: with a desktop session the fetcher
        # asks its questions in zenity dialogs on the VM's screen, -y or not
        # a squashfs without its extracted tree is a partial run: the fetcher
        # would ask (overwrite or validate?) whatever -y says
        rm -f "$HOME"/.local/share/fex-emu/RootFS/Ubuntu_24_04.sqsh "$HOME"/.fex-emu/RootFS/Ubuntu_24_04.sqsh
        FEXRootFSFetcher -y -x --force-ui=tty --distro-name=ubuntu --distro-version=24.04 </dev/null
    fi
    # the fetcher says it set the default but (FEX 2610) writes no config
    cfg="${XDG_CONFIG_HOME:-$HOME/.config}/fex-emu"
    [ -d "$HOME/.fex-emu" ] && cfg="$HOME/.fex-emu"
    mkdir -p "$cfg"
    [ -s "$cfg/Config.json" ] || printf '{"Config":{"RootFS":"Ubuntu_24_04"},"ThunksDB":{}}\n' > "$cfg/Config.json"
    FEXServer -k 2>/dev/null || true
    sleep 1
    FEXBash -c 'uname -m' | grep -qx x86_64
    echo "FEX OK: $(FEXBash -c 'uname -m')"
    ;;
wine)
    if [ ! -x ~/$TKG/bin/wine ]; then
        curl -fsSL -o /tmp/$TKG.tar.xz "$TKG_URL"
        echo "$TKG_SHA  /tmp/$TKG.tar.xz" | sha256sum -c --quiet
        tar -C ~ -xJf /tmp/$TKG.tar.xz
        rm -f /tmp/$TKG.tar.xz
    fi
    echo "WINE OK: $(FEXBash -c "~/$TKG/bin/wine --version" 2>/dev/null)"
    ;;
tools)
    apt_install git curl xz-utils mingw-w64 gcc-x86-64-linux-gnu binutils-x86-64-linux-gnu \
        meson ninja-build pkgconf build-essential python3-mako python3-yaml python3-packaging \
        bison flex glslang-tools spirv-tools cmake directx-headers-dev libvulkan-dev \
        libdrm-dev libx11-dev libx11-xcb-dev libxcb-dri3-dev libxcb-present-dev libxcb-dri2-0-dev \
        libxcb-shm0-dev libxshmfence-dev x11-utils unzip vulkan-tools mesa-utils
    echo "TOOLS OK"
    ;;
dxvk)
    [ -d ~/dxvk/.git ] || git clone -q https://github.com/doitsujin/dxvk.git ~/dxvk
    cd ~/dxvk
    git checkout -q "$DXVK_TAG"
    git submodule update -q --init
    [ -f builddir-win64/build.ninja ] ||
        meson setup builddir-win64 --cross-file=build-win64.txt -Dbuildtype=release -Db_ndebug=true >/dev/null
    ninja -C builddir-win64 >/dev/null
    echo "DXVK OK: $(git describe --tags)"
    ;;
mesa)
    bundle="${1:-}"
    [ -d ~/mesa/.git ] || git clone -q -b neptune "$MESA_URL" ~/mesa
    cd ~/mesa
    if [ -n "$bundle" ]; then
        git fetch -q ~/"$bundle" +neptune:refs/remotes/bundle/neptune
        git checkout -q -B "sync/$(date +%F)" bundle/neptune
    fi
    if [ ! -f builddir-win64/build.ninja ]; then
        meson setup builddir-win64 --cross-file="$HOME/dxvk/build-win64.txt" \
            -Dneptune=true -Dgallium-drivers= -Dvulkan-drivers= -Dplatforms= -Dglx=disabled \
            -Dbuild.pkg_config_path= -Db_lto=false \
            -Dc_args=-fno-omit-frame-pointer -Dcpp_args=-fno-omit-frame-pointer >/tmp/mesa-setup.log 2>&1 ||
            { tail -15 /tmp/mesa-setup.log; rm -rf builddir-win64; exit 1; }
    fi
    ninja -C builddir-win64 >/tmp/mesa-build.log 2>&1 || { grep -m5 -B2 -A8 "error" /tmp/mesa-build.log | head -40; exit 1; }
    ls builddir-win64/src/virtio/neptune/{d3d11,dxgi,d3d12}.dll builddir-win64/src/virtio/neptune/nptunix/nptunix.dll >/dev/null
    echo "MESA OK: $(git log --oneline -1)"
    ;;
nptunix)
    mkdir -p ~/npt-stage
    ~/bin/build-nptunix-x86 ~/npt-stage/nptunix.so >/dev/null
    cp -f ~/mesa/builddir-win64/src/virtio/neptune/nptunix/nptunix.dll ~/npt-stage/
    echo "NPTUNIX OK: $(file -b ~/npt-stage/nptunix.so | cut -d, -f1-2)"
    ;;
prefix)
    if [ ! -f "$PFX/system.reg" ]; then
        mkdir -p "$(dirname "$PFX")"
        WINEPREFIX="$PFX" WINEARCH=win64 FEXBash -c "~/$TKG/bin/wine wineboot --init" >/dev/null 2>&1
        WINEPREFIX="$PFX" FEXBash -c "~/$TKG/bin/wineserver -w"
    fi
    # .NET for installers that need it: 3DMark's MSI requires .NET 4.5
    # (launch condition NETFRAMEWORK45) and runs .NET custom actions
    if [ ! -d "$PFX/drive_c/windows/mono" ]; then
        curl -fsSL -o /tmp/$MONO "$MONO_URL"
        echo "$MONO_SHA  /tmp/$MONO" | sha256sum -c --quiet
        WINEPREFIX="$PFX" FEXBash -c "~/$TKG/bin/wine msiexec /i Z:/tmp/$MONO /qn" >/dev/null 2>&1
        rm -f /tmp/$MONO
    fi
    cp -f ~/bin/fs_gt1.3dmdef ~/bin/ts_gt1.3dmdef "$PFX/drive_c/"
    echo "PREFIX OK: 3DMark $([ -f "$PFX/drive_c/Program Files/UL/3DMark/3DMarkCmd.exe" ] && echo installed || echo 'not installed')"
    ;;
verify)
    failed=0
    check() { if eval "$2" >/dev/null 2>&1; then echo "ok    $1"; else echo "FAIL  $1"; failed=$((failed + 1)); fi; }
    check "Venus Vulkan device" "vulkaninfo --summary 2>/dev/null | grep -q DRIVER_ID_MESA_VENUS"
    check "autologin" "grep -q '^AutomaticLogin=$(id -un)' /etc/gdm3/custom.conf"
    check "FEX x86_64" "FEXBash -c 'uname -m' | grep -qx x86_64"
    check "Wine $TKG" "FEXBash -c '~/$TKG/bin/wine --version'"
    check "DXVK d3d11.dll" "test -f ~/dxvk/builddir-win64/src/d3d11/d3d11.dll"
    check "Neptune d3d11.dll" "test -f ~/mesa/builddir-win64/src/virtio/neptune/d3d11.dll"
    check "FEX rootfs" "test -d \"$(rootfs)/usr\""
    check "nptunix.so x86_64" "file ~/npt-stage/nptunix.so | grep -q x86-64"
    check "Wine prefix" "test -f $PFX/system.reg"
    [ $failed -eq 0 ] && echo "VERIFY OK" || { echo "VERIFY FAILED ($failed)"; exit 1; }
    ;;
*)
    echo "unknown step $STEP" >&2
    exit 2
    ;;
esac
