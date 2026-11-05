#!/usr/bin/env python3
"""Rebuild one UTM dependency in place and repackage it as the app's framework.

usage: rebuild-dep.py --utm <UTM checkout> [--platform macOS-arm64] <project> [...]
       rebuild-dep.py --utm <UTM checkout> --list

<project> is a source tree name (or unique prefix) under the checkout's
build-<platform>/ directory, e.g. virglrenderer, qemu, dxmt, d3dmetal-native,
spice-gtk.  For each:

  1. build it incrementally in its existing build directory (ninja for meson
     trees, make for autotools trees) and install into sysroot-<platform>/;
  2. hand every library it installed into sysroot/lib, and every executable it
     installed into sysroot/libexec, to UTM's own scripts/fixup.sh, which turns
     a dylib into the Frameworks/<name>.framework the app embeds and rewrites
     an executable's imports.

Never run build_dependencies.sh for this: it takes hours and deletes the
build trees (and with them any uncommitted edit made there; keep such edits as
patches).  The trees only exist after one full dependency build: the sysroot
artifacts from CI do not include them.

UTM.app EMBEDS copies of these frameworks (and copies libexec/
virgl_render_server into QEMURenderServer.app) at Xcode build time: run
build-utm.sh afterwards, then verify-app.sh.  Testing a stale framework and
concluding "no effect" is the most common way to lose a day here.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from deps import build_dir, find_tree, fixup, run, trees


def installed(bd):
    """Files a meson build installs (absolute paths)."""
    intro = bd / "meson-info" / "intro-installed.json"
    if not intro.exists():
        return None
    return [Path(p) for p in json.loads(intro.read_text()).values()]


def rebuild(utm, platform, tree, fixup_only=False):
    sysroot = utm / f"sysroot-{platform}"
    bd = build_dir(tree)
    if not bd:
        sys.exit(f"{tree}: no configured build directory (needs one full dependency build)")
    print(f"=== {tree.name} ({bd})")
    t0 = time.time() - 1
    if not fixup_only:
        if (bd / "build.ninja").exists():
            run(["ninja", "-C", bd])
            run(["meson", "install", "-C", bd, "--no-rebuild", "--only-changed"])
        else:
            run(["make", "-C", bd, "install"])
    files = installed(bd)
    if files is None:
        # autotools: whatever it just installed
        files = [p for d in ("lib", "libexec") for p in (sysroot / d).rglob("*")
                 if p.is_file() and p.stat().st_mtime >= t0]
    libdir, libexec = (sysroot / "lib").resolve(), (sysroot / "libexec").resolve()
    dylibs, exes = [], []
    for f in files:
        if not f.exists() or f.is_symlink():
            continue
        parent = f.resolve().parent
        if f.suffix == ".dylib" and parent == libdir:
            dylibs.append(f)
        elif parent == libexec and os.access(f, os.X_OK):
            exes.append(f)
    plat = "macos" if platform.lower().startswith("macos") else platform.split("-")[0].lower()
    fixup(utm, sysroot, dylibs, exes, plat)
    if not dylibs and not exes:
        print("  nothing installed into lib/ or libexec/ (headers only, or not a library)")
    print(f"  NOTE: now run build-utm.sh so UTM.app embeds the new {tree.name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--utm", required=True, type=Path, help="UTM checkout")
    ap.add_argument("--platform", default="macOS-arm64")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fixup-only", action="store_true")
    ap.add_argument("projects", nargs="*")
    a = ap.parse_args()
    root = a.utm / f"build-{a.platform}"
    if not root.is_dir():
        sys.exit(f"no {root}: the dependency build trees exist only after one full "
                 f"`scripts/build_dependencies.sh -p macos -a arm64` run")
    if a.list or not a.projects:
        for t in trees(root):
            bd = build_dir(t)
            print(f"{t.name:40s} {bd.relative_to(root) if bd else '(not configured)'}")
        return
    for p in a.projects:
        rebuild(a.utm, a.platform, find_tree(root, p), a.fixup_only)


if __name__ == "__main__":
    main()
