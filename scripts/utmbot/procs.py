"""Find UTM's processes for a VM.

    UTM ── QEMUHelper (XPC) ── QEMULauncher <qemu framework> <args … -name VM …>
                           └── QEMULauncher <swtpm framework> …        (not QEMU)
    QEMULauncher (QEMU) ── QEMURenderServer --socket-fd N               (render server)
                              └── QEMURenderServer --worker-context-id N
                                     --worker-context-name NAME         (one per guest
                                     GPU context; NAME is the guest process, e.g. dwm.exe)
The GPU work happens in the workers.  A D3DMetal worker is x86_64 (Rosetta),
a DXMT or Venus worker arm64.
"""

import ctypes
import re

from .common import run

P_TRANSLATED = 0x00020000


def _translated(pid):
    """True when pid runs under Rosetta (sysctl kern.proc.pid p_flag)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 40:
            return None
        # struct kinfo_proc { struct extern_proc kp_proc; ... }; p_flag follows
        # the p_un union (16 bytes) and two pointers.
        p_flag = int.from_bytes(buf.raw[32:36], "little")
        return bool(p_flag & P_TRANSLATED)
    except Exception:
        return None


def ps():
    rc, out = run(["ps", "-axww", "-o", "pid=,ppid=,pcpu=,rss=,stat=,command="])
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(\S+)\s+(.*)", line)
        if m:
            rows.append({"pid": int(m[1]), "ppid": int(m[2]), "cpu": float(m[3]),
                         "rss_mb": int(m[4]) // 1024, "stat": m[5], "cmd": m[6]})
    return rows


def classify(rows=None):
    rows = rows if rows is not None else ps()
    out = {"utm": [], "helper": [], "qemu": [], "swtpm": [], "server": [], "worker": []}
    for r in rows:
        c = r["cmd"]
        # the launcher and render server live inside QEMUHelper.xpc: test them first
        if re.search(r"/Contents/MacOS/UTM(\s|$)", c):
            out["utm"].append(r)
        elif "QEMULauncher" in c:
            # by the framework the launcher runs: QEMU's own arguments name the
            # TPM socket "<uuid>.swtpm", so a plain "swtpm" test misfiles QEMU
            fw = re.search(r"/Frameworks/([^/]+)\.framework/", c)
            (out["swtpm"] if fw and fw[1].startswith("swtpm") else out["qemu"]).append(r)
        elif "QEMURenderServer" in c:
            m = re.search(r"--worker-context-id\s+(\d+)", c)
            if m:
                r["ctx_id"] = int(m[1])
                # the guest process that owns the GPU context (truncated by the guest)
                n = re.search(r"--worker-context-name\s+(\S+)", c)
                r["ctx_name"] = n[1] if n else "-"
                t = _translated(r["pid"])
                r["arch"] = {True: "x86_64", False: "arm64", None: "?"}[t]
                out["worker"].append(r)
            else:
                out["server"].append(r)
        elif "/QEMUHelper" in c:
            out["helper"].append(r)
    return out


def utm_app_of(row):
    m = re.match(r"(.*?/[^/]+\.app)/Contents/MacOS/UTM", row["cmd"])
    return m[1] if m else None


def vm_tree(vm_name, rows=None):
    """The QEMU process for vm_name and its render server and workers."""
    rows = rows if rows is not None else ps()
    cl = classify(rows)
    qemu = [q for q in cl["qemu"]
            if re.search(r"-name\s+(\"?)" + re.escape(vm_name) + r"\1(\s|$)", q["cmd"])]
    q = qemu[0] if qemu else None
    servers, workers = [], []
    if q:
        servers = [s for s in cl["server"] if s["ppid"] == q["pid"]]
        spids = {s["pid"] for s in servers}
        workers = [w for w in cl["worker"] if w["ppid"] in spids]
    return {"qemu": q, "servers": servers, "workers": workers, "all": cl}


def env_of(pid):
    """Environment of a process (same user), from `ps eww`."""
    rc, out = run(["ps", "eww", "-o", "command=", "-p", str(pid)])
    env = {}
    for tok in re.findall(r"(?:^|\s)([A-Z_][A-Z0-9_]*)=(\S*)", out):
        env.setdefault(tok[0], tok[1])
    return env


def napped(pid):
    """App Nap check: `ps -M` thread priorities; a napped process shows 4."""
    rc, out = run(["ps", "-M", "-p", str(pid)])
    pri = []
    for line in out.splitlines()[1:]:
        f = line.split()
        for tok in f:
            if re.fullmatch(r"\d+[A-Z]?", tok) and tok[:-1].isdigit() and tok[-1:].isalpha():
                pri.append(int(tok[:-1]))
                break
    return pri
