"""Shared by rebuild-dep.py and rebuild-universal.py: UTM's dependency build
trees (build-<platform>/<project>/ after one full build_dependencies.sh run)
and UTM's scripts/fixup.sh, which repackages what a rebuild installed."""

import subprocess
import sys


def run(cmd, **kw):
    print("+ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), check=True, **kw)


def out(cmd):
    return subprocess.run(list(map(str, cmd)), check=True, capture_output=True, text=True).stdout


def trees(build_root):
    return sorted(p for p in build_root.iterdir() if p.is_dir())


def find_tree(build_root, name):
    """The tree named `name`, else the one tree whose name starts with it."""
    hits = [t for t in trees(build_root) if t.name == name] or \
           [t for t in trees(build_root) if t.name.startswith(name)]
    if len(hits) != 1:
        sys.exit(f"'{name}' matches {[h.name for h in hits] or 'nothing'} under {build_root}")
    return hits[0]


def build_dir(tree):
    """The tree's configured build directory (meson or autotools), or None."""
    for sub in ("utm_build", "build", "_build"):
        if (tree / sub / "build.ninja").exists() or (tree / sub / "Makefile").exists():
            return tree / sub
    if (tree / "Makefile").exists():
        return tree
    return None


def fixup(utm, sysroot, dylibs, exes, platform="macos"):
    """fixup.sh: each dylib in sysroot/lib becomes the Frameworks/<name>.framework
    the app embeds; each executable in sysroot/libexec gets its imports rewritten."""
    script = utm / "scripts" / "fixup.sh"
    if dylibs:
        run(["sh", script, "-p", platform, "-s", sysroot] + list(dylibs))
    if exes:
        run(["sh", script, "-p", platform, "-s", sysroot, "-i"] + list(exes))
