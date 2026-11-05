#!/usr/bin/env python3
"""Rebuild one meson dependency from a source checkout for BOTH slices of a
universal UTM.app (arm64 for Venus/DXMT, x86_64 for D3DMetal under Rosetta),
and put it into the universal sysroot the app embeds.

usage: rebuild-universal.py --utm <UTM checkout> --src <source dir> <project>
       rebuild-universal.py --utm <UTM checkout> --restore [<backup dir>]

<project> names the dependency tree under build-macOS-arm64/ whose meson
options and cross file are reused (virglrenderer, d3dmetal-native, dxmt).
<source dir> is what gets built: a checkout or a `git worktree` with the
change, at the revision the app should carry.  The dependency tree's own copy
is not used: it is at whatever revision the last full dependency build had,
which is usually not the one in the app (the universal sysroot comes from CI).

For each slice it configures a build directory outside the source
(~/.utmapp-bot/host-builds/<project>-<arch>/), builds, and installs into a
staging prefix.  The x86_64 slice links against the universal sysroot's fat
libraries and frameworks (D3DMetal is x86_64 only).  Then every library and
executable it installed is merged with `lipo` into
sysroot-macOS-arm64_x86_64/{lib,libexec}, the originals backed up first, and
UTM's scripts/fixup.sh repackages them as the frameworks the app embeds.

Subprojects pinned by a wrap file (directx-native) come from a sibling
checkout at the pinned revision (~/Documents/<name>), never a download.

Afterwards: build-utm.sh <checkout> --universal, then verify-app.sh --marker.
`--restore` puts back the files the last run replaced.
"""

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from deps import build_dir, find_tree, fixup, out, run

DATA = Path(os.environ.get("UTMAPP_BOT_DATA", "~/.utmapp-bot")).expanduser()
WORK = DATA / "host-builds"


def meson_build_dir(utm, project):
    """The arm64 dependency tree's meson build directory."""
    tree = find_tree(utm / "build-macOS-arm64", project)
    bd = build_dir(tree)
    if not bd or not (bd / "meson-private" / "cmd_line.txt").exists():
        sys.exit(f"{tree}: not a configured meson build (this tool only handles meson trees)")
    return bd


def options(bd):
    """The -D options and cross file the dependency build was configured with."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(bd / "meson-private" / "cmd_line.txt")
    opts = {k: v for k, v in cp["options"].items() if k != "prefix"} if cp.has_section("options") else {}
    cross = cp["properties"].get("cross_file", "") if cp.has_section("properties") else ""
    files = re.findall(r"'([^']+)'", cross)
    if not files:
        sys.exit(f"{bd}: no cross file recorded")
    return opts, Path(files[0])


def x86_cross(arm_cross, arm_sysroot, uni_sysroot, pcdir, linkdir, dest):
    """The arm64 cross file, for x86_64: same compiler and SDK; link against the
    universal sysroot's fat libraries and frameworks."""
    t = arm_cross.read_text()
    t = t.replace("'-arch','arm64'", "'-arch','x86_64'").replace("arm64-apple-macos", "x86_64-apple-macos")
    t = t.replace(f"-L{arm_sysroot}/lib", f"-L{linkdir}','-L{uni_sysroot}/lib")
    t = t.replace(f"-F{arm_sysroot}/Frameworks", f"-F{uni_sysroot}/Frameworks")
    t = re.sub(r"cpu_family = '[^']*'", "cpu_family = 'x86_64'", t)
    t = re.sub(r"(?m)^cpu = '[^']*'", "cpu = 'x86_64'", t)
    t = t.replace("[properties]", f"[properties]\npkg_config_libdir = ['{pcdir}']", 1)
    dest.write_text(t)
    return dest


def x86_linkdir(uni_sysroot, dest):
    """Link names for the universal sysroot's libraries.  CI's sysroot ships
    only the versioned files (libepoxy.0.dylib), not the libepoxy.dylib
    symlinks that `-lepoxy` looks for."""
    dest.mkdir(parents=True, exist_ok=True)
    for lib in (uni_sysroot / "lib").glob("lib*.dylib"):
        m = re.fullmatch(r"(lib[^.]+)(?:\.[\d.]+)?\.dylib", lib.name)
        if not m:
            continue
        link = dest / f"{m[1]}.dylib"
        if not (uni_sysroot / "lib" / link.name).exists() and not link.exists():
            link.symlink_to(lib)
    return dest


def x86_pkgconfig(arm_sysroot, uni_sysroot, dest):
    """The arm64 sysroot's .pc files pointing at the universal sysroot."""
    dest.mkdir(parents=True, exist_ok=True)
    for pc in (arm_sysroot / "lib" / "pkgconfig").glob("*.pc"):
        (dest / pc.name).write_text(pc.read_text().replace(str(arm_sysroot), str(uni_sysroot)))
    return dest


def link_subprojects(src):
    """Wrap-pinned subprojects from sibling checkouts at the pinned revision."""
    sub = src / "subprojects"
    for wrap in sub.glob("*.wrap") if sub.is_dir() else []:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(wrap)
        if not cp.has_section("wrap-git"):
            continue
        name = cp["wrap-git"].get("directory", wrap.stem).strip()
        rev = cp["wrap-git"].get("revision", "").strip()
        target = sub / name
        if target.exists():
            continue
        sib = Path.home() / "Documents" / name
        if not (sib / ".git").exists():
            continue  # optional subprojects (perfetto, …) are not needed by the UTM build
        have = out(["git", "-C", sib, "rev-parse", "HEAD"]).strip()
        if rev and not have.startswith(rev[:12]) and not rev.startswith(have[:12]):
            sys.exit(f"{sib} is at {have[:12]}, but {wrap.name} pins {rev[:12]}: check that "
                     f"revision out there (or make a worktree of it) first")
        target.symlink_to(sib)
        print(f"  subproject {name}: {sib} (at the pinned {rev[:12]})")


def build(src, project, arch, cross, opts):
    bdir, prefix = WORK / f"{project}-{arch}", WORK / f"{project}-{arch}-prefix"
    if prefix.exists():
        shutil.rmtree(prefix)
    if not (bdir / "build.ninja").exists():
        args = ["meson", "setup", bdir, src, "--cross-file", cross, "--prefix", prefix,
                "--wrap-mode=nodownload"] + [f"-D{k}={v}" for k, v in opts.items()]
        run(args)
    else:
        run(["meson", "configure", bdir, "--prefix", prefix])
    t0 = time.time()
    run(["ninja", "-C", bdir])
    run(["meson", "install", "-C", bdir, "--no-rebuild"], stdout=subprocess.DEVNULL)
    print(f"  {arch}: built in {time.time() - t0:.0f} s")
    return prefix


def products(prefix):
    """(subdir, name) of each real library and executable a build installed."""
    found = []
    for sub, keep in (("lib", lambda p: p.suffix == ".dylib"), ("libexec", lambda p: os.access(p, os.X_OK))):
        d = prefix / sub
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            if p.is_file() and not p.is_symlink() and keep(p):
                found.append((sub, p.name))
    return found


def merge(utm, project, arm_prefix, x86_prefix):
    uni = utm / "sysroot-macOS-arm64_x86_64"
    backup = WORK / f"backup-{project}-{time.strftime('%Y%m%d-%H%M%S')}"
    dylibs, exes, record = [], [], []
    for sub, name in products(arm_prefix):
        a, x = arm_prefix / sub / name, x86_prefix / sub / name
        if not x.exists():
            sys.exit(f"the x86_64 build did not install {sub}/{name}")
        dest = uni / sub / name
        if dest.exists():
            (backup / sub).mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup / sub / name)
        tmp = dest.with_name(dest.name + ".new")
        run(["lipo", "-create", a, x, "-output", tmp])
        tmp.replace(dest)  # a new file, renamed into place: never cp over a binary in use
        record.append(f"{sub}/{name}")
        (dylibs if sub == "lib" else exes).append(dest)
    # the frameworks fixup.sh rewrites are backed up too
    for d in dylibs:
        fw = uni / "Frameworks" / f"{re.sub(r'^lib', '', d.name.rsplit('.', 1)[0])}.framework"
        if fw.exists():
            shutil.copytree(fw, backup / "Frameworks" / fw.name, symlinks=True, dirs_exist_ok=True)
            record.append(f"Frameworks/{fw.name}")
    fixup(utm, uni, dylibs, exes)
    (backup / "files.json").parent.mkdir(parents=True, exist_ok=True)
    (backup / "files.json").write_text(json.dumps({"sysroot": str(uni), "files": record}, indent=2))
    (WORK / "last-backup").write_text(str(backup) + "\n")
    for f in dylibs + exes:
        print(f"  {f.relative_to(uni)}: {out(['lipo', '-archs', f]).strip()}")
    print(f"  originals backed up to {backup} (--restore puts them back)")


def restore(backup):
    meta = json.loads((backup / "files.json").read_text())
    uni = Path(meta["sysroot"])
    for rel in meta["files"]:
        src, dest = backup / rel, uni / rel
        if src.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest, symlinks=True)
        elif src.exists():
            tmp = dest.with_name(dest.name + ".new")
            shutil.copy2(src, tmp)
            tmp.replace(dest)
        print(f"  restored {rel}")
    print("  now build-utm.sh --universal again")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--utm", required=True, type=Path)
    ap.add_argument("--src", type=Path)
    ap.add_argument("--restore", nargs="?", const="", default=None)
    ap.add_argument("project", nargs="?")
    a = ap.parse_args()
    utm = a.utm.resolve()
    if a.restore is not None:
        b = Path(a.restore) if a.restore else Path((WORK / "last-backup").read_text().strip())
        return restore(b)
    if not a.src or not a.project:
        ap.error("--src and <project> are required")
    src = a.src.resolve()
    arm, uni = utm / "sysroot-macOS-arm64", utm / "sysroot-macOS-arm64_x86_64"
    if not (uni / "Frameworks").is_dir():
        sys.exit(f"no {uni}: download the Sysroot-macos-universal artifact first (build-utm)")
    opts, arm_cross = options(meson_build_dir(utm, a.project))
    WORK.mkdir(parents=True, exist_ok=True)
    link_subprojects(src)
    cross86 = x86_cross(arm_cross, arm, uni, x86_pkgconfig(arm, uni, WORK / "pkgconfig-x86_64"),
                        x86_linkdir(uni, WORK / "lib-x86_64"), WORK / "meson-darwin-x86_64.cross")
    print(f"=== {a.project} from {src} ({out(['git', '-C', src, 'rev-parse', '--short', 'HEAD']).strip()})")
    arm_prefix = build(src, a.project, "arm64", arm_cross, opts)
    x86_prefix = build(src, a.project, "x86_64", cross86, opts)
    merge(utm, a.project, arm_prefix, x86_prefix)
    print(f"  NOTE: now `build-utm.sh {utm} --universal`, then `verify-app.sh --marker …`")


if __name__ == "__main__":
    main()
