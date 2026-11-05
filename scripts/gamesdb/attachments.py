"""Users' attachment files, handled as the untrusted input they are.

A file from a report may be crafted to exploit whatever reads it, and its
name may carry the uploader's name.  So:

private    every copy is owner-only (600) and carries macOS's quarantine
           flag, so nothing runs it by accident; names are by id or index,
           keeping only a plain extension.
sandboxed  decoders (videoframes.swift for videos, plutil for binary
           plists) run under attachment.sb: no network, no other programs,
           no reads or writes in the home folder beyond the input's and the
           output's directories.
unzip      archives are extracted without links or .. paths, within entry
           and size limits; executables and scripts are listed, never
           written.
"""

import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from gamesdb import tunnel
from gamesdb.tunnel import die

HERE = Path(__file__).resolve().parent
SANDBOX = HERE / "attachment.sb"
UNZIP_MAX_ENTRIES, UNZIP_MAX_BYTES = 2000, 1 << 30
EXEC_EXT = {".exe", ".dll", ".sys", ".com", ".scr", ".msi", ".msp", ".bat", ".cmd", ".ps1",
            ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".lnk", ".url", ".reg",
            ".jar", ".app", ".pkg", ".dmg", ".command", ".sh", ".py", ".pl", ".rb", ".so",
            ".dylib", ".appx", ".msix", ".cpl", ".ocx", ".inf", ".iso", ".img", ".apk"}
EXEC_MAGIC = (b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"#!")


# ---- private files ---------------------------------------------------------------

def private_dir(d):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def private(path):
    """Owner-only, and quarantined the way browsers mark downloads: Gatekeeper
    then refuses to run it, on top of it not being executable."""
    os.chmod(path, 0o600)
    subprocess.run(["xattr", "-w", "com.apple.quarantine", f"0081;{int(time.time()):x};gamesdb;",
                    str(path)], capture_output=True)


def write_private(path, data):
    path = Path(path)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)
    private(path)
    return path


def plain_suffix(name):
    """The file name's extension if it is a plain one (.log, .mp4), else ''."""
    suffix = Path(name or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ""


# ---- sandboxed decoders ----------------------------------------------------------

def sandboxed(binary, args, inp, out):
    """Run binary under attachment.sb, reading inp and writing only into the
    directory out; its output passes through, its exit code is returned."""
    inp, out = Path(inp).resolve(), private_dir(Path(out).resolve())
    cmd = ["sandbox-exec", "-f", str(SANDBOX),
           "-D", f"SELF={binary}", "-D", f"SELFDIR={Path(binary).resolve().parent}",
           "-D", f"IN={inp.parent if inp.is_file() else inp}", "-D", f"OUT={out}",
           "-D", f"HOME={Path.home()}", str(binary), *map(str, args)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


def videoframes_binary():
    """videoframes.swift compiled once (and again when it changes)."""
    src = HERE / "videoframes.swift"
    out = tunnel.state_dir() / "bin" / "videoframes"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(["swiftc", "-O", "-o", str(out), str(src)], capture_output=True, text=True)
        if p.returncode != 0:
            die(f"swiftc videoframes.swift failed: {p.stderr[-600:]}")
    return out


def frames(video, out, count=12, at=(), every=None, start=None, end=None):
    """Still frames and a change metric from a video (videoframes.swift)."""
    video, out = Path(video).resolve(), Path(out).resolve()
    args = [video, out, count] + [x for t in at for x in ("--at", t)]
    if every:
        args += ["--every", every, "--from", start or 0] + (["--to", end] if end is not None else [])
    return sandboxed(videoframes_binary(), args, video, out)


def plist(src, out):
    """A binary plist as XML, out/<name>.xml."""
    src, out = Path(src).resolve(), Path(out).resolve()
    return sandboxed("/usr/bin/plutil", ["-convert", "xml1", "-o", out / (src.stem + ".xml"), src], src, out)


# ---- archives ----------------------------------------------------------------------

def unzip(zpath, dest):
    """Extract an archive into dest, entries renamed entry-NNNN.ext; prints
    one line per entry (name, size, original name)."""
    dest = private_dir(Path(dest).resolve())
    total = 0
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist()
        if len(infos) > UNZIP_MAX_ENTRIES:
            die(f"refusing: {len(infos)} entries (at most {UNZIP_MAX_ENTRIES})")
        for i, info in enumerate(infos):
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                print(f"skipped link {info.filename!r}", file=sys.stderr)
                continue
            total += info.file_size
            if total > UNZIP_MAX_BYTES:
                die("refusing: more than 1 GiB uncompressed")
            # stored by index: entry names can carry user names too
            name = f"entry-{i:04d}{plain_suffix(info.filename)}"
            with z.open(info) as src:
                head = src.read(4)
            if plain_suffix(info.filename) in EXEC_EXT or head.startswith(EXEC_MAGIC):
                print(f"NOT-EXTRACTED-EXECUTABLE {name}\t{info.file_size}\t{info.filename}")
                continue
            with z.open(info) as src:
                write_private(dest / name, src.read())
            print(f"{name}\t{info.file_size}\t{info.filename}")
    return 0
