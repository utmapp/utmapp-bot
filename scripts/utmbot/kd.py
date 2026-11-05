"""Kernel debugging a test VM over kdnet, with kd.exe running on its debugger dev VM.

kd runs under `ssh -tt` on a local pseudo-terminal owned by a detached relay
process (`utmbot _kdrelay`).  The relay copies commands from a FIFO into the
terminal and kd's output, without terminal escape codes, into kd.log.  Kernel
DbgPrint/KdPrint output arrives there asynchronously, with no break needed,
which is how driver load and unload are watched live.

kd only executes commands while the target is HALTED (a `kd>` prompt).
`break` sends Ctrl+C through the terminal, which the dev VM's console turns
into kd's break-in; it halts a running or hung target at once.  (kd -b,
"break on connect", is ignored by a target that already connected once in
this boot.)  A halted target freezes every vCPU: SSH, the display and the
guest clock all stop until `go`.
"""

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from . import remote
from .common import VM, Session, die, ensure_dir, log, pid_alive

KD = r"C:\Program Files (x86)\Windows Kits\10\Debuggers\arm64\kd.exe"
KEY = "1.1.1.1"
PROMPT = re.compile(r"^\d*:?\d*:?\w*>\s*$|kd>\s*$", re.M)


def _paths(vm):
    d = ensure_dir(vm.session_dir() / "kd")
    return d, d / "kd.in", d / "kd.log", d / "pids"


def debugger_of(vm):
    if not vm.debugger or not vm.kd_port:
        die(f"'{vm.name}' has no kdnet debugger configured (provision it with "
            "provision-windows-test-vm; a VM provisioned for a UTM without the vmnet "
            "entitlement has none)")
    if Session(vm).get("net_prev"):
        die(f"'{vm.name}' runs with emulated networking this boot (this UTM has no vmnet "
            "entitlement), so kdnet cannot reach the debugger VM. Kernel debugging needs a "
            "UTM signed with com.apple.vm.networking: tell the user, and use the release app "
            "or a build with the VM entitlement profiles (build-utm)")
    return VM(vm.debugger)


def status(vm):
    d, fifo, klog, pids = _paths(vm)
    if not pids.exists():
        return "not attached"
    p = pids.read_text().split()
    alive = all(pid_alive(x) for x in p)
    tail = klog.read_text(errors="replace").rstrip().splitlines()[-1:] if klog.exists() else []
    halted = bool(tail and PROMPT.search(tail[0]))
    return ("attached" if alive else "DEAD") + (", target HALTED at a prompt" if halted else "")


def start(vm, initial_break=False, sympath=None):
    import sys
    dbg = debugger_of(vm)
    d, fifo, klog, pids = _paths(vm)
    stop(vm, quiet=True)
    if not remote.reachable(dbg):
        die(f"debugger VM '{dbg.name}' is not reachable over SSH; start it in UTM")
    remote.ssh(dbg, "taskkill /f /im kd.exe >nul 2>&1 & exit 0")
    if fifo.exists():
        fifo.unlink()
    os.mkfifo(fifo)
    # the driver's own PDBs first, then Microsoft's (nt!, dxgkrnl!), cached:
    # measured quick enough (kd crash 16 s with a warm cache)
    sym = sympath or r"C:\utmbot\symbols;srv*C:\utmbot\symcache*https://msdl.microsoft.com/download/symbols"
    kdcmd = (f'"{KD}" -k net:port={vm.kd_port},key={KEY} ' + ("-b " if initial_break else "")
             + f'-y "{sym}"')
    argv = ["ssh", "-tt"] + remote.ssh_opts(dbg) + [remote.dest(dbg), kdcmd]
    main = Path(__file__).resolve().parent / "__main__.py"
    p = subprocess.Popen([sys.executable, str(main), "--vm", vm.name, "_kdrelay"] + argv,
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids.write_text(f"{p.pid}")
    time.sleep(3)
    if p.poll() is not None:
        die(f"kd exited at once; see {klog}")
    log(f"kd attached on '{dbg.name}' (port {vm.kd_port}); log {klog}")
    if initial_break:
        log("kd -b: the target halts when it connects, i.e. only at its next boot")


_ESC = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?|\r")


def relay(vm, argv):
    """Own the terminal kd runs on: FIFO -> terminal, terminal -> kd.log."""
    import fcntl
    import pty
    import select
    import struct
    import termios
    d, fifo, klog, pids = _paths(vm)
    master, slave = pty.openpty()
    # wide, so the dev VM's console does not wrap kd's output at 80 columns
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))
    p = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    fin = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # RDWR: never sees EOF
    pending = b""
    with open(klog, "ab", buffering=0) as out:
        while p.poll() is None:
            r, _, _ = select.select([master, fin], [], [], 1.0)
            if fin in r:
                data = os.read(fin, 4096)
                os.write(master, data.replace(b"\n", b"\r"))
            if master in r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                buf = pending + data
                # keep a possibly cut escape sequence for the next read
                cut = buf.rfind(b"\x1b")
                if cut >= 0 and len(buf) - cut < 16 and not _ESC.match(buf, cut):
                    buf, pending = buf[:cut], buf[cut:]
                else:
                    pending = b""
                out.write(_ESC.sub(b"", buf))


def stop(vm, quiet=False):
    d, fifo, klog, pids = _paths(vm)
    if pids.exists():
        for x in pids.read_text().split():
            if pid_alive(x):
                os.killpg(int(x), signal.SIGTERM) if _is_group(x) else os.kill(int(x), signal.SIGTERM)
        pids.unlink()
    try:
        dbg = debugger_of(vm)
        if remote.reachable(dbg, timeout=4):
            # kd holds the KMD's PDB open on the dev VM: a KMD rebuild fails
            # with LNK1201 until it is gone
            remote.ssh(dbg, "taskkill /f /im kd.exe >nul 2>&1 & exit 0")
    except Exception:
        pass
    if fifo.exists():
        fifo.unlink()
    if not quiet:
        log("kd detached")


def _is_group(pid):
    try:
        return os.getpgid(int(pid)) == int(pid)
    except Exception:
        return False


def tail(vm, n=40):
    _, _, klog, _ = _paths(vm)
    if not klog.exists():
        die("no kd.log; `utmbot kd start` first")
    print("\n".join(klog.read_text(errors="replace").splitlines()[-n:]))


def send(vm, cmd, timeout=120):
    d, fifo, klog, pids = _paths(vm)
    if not pids.exists():
        die("kd is not attached; `utmbot kd start`")
    text = klog.read_text(errors="replace")
    last = text.rstrip().splitlines()[-1] if text.strip() else ""
    if not PROMPT.search(last):
        die("the target is running, not halted at a kd> prompt: kd cannot run commands now. "
            "`utmbot kd break` halts it (and freezes the VM until `kd go`).")
    start_size = klog.stat().st_size
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, (cmd + "\n").encode())
    os.close(fd)
    if cmd.strip() in ("g", "gn", "gh"):
        print("target resumed")
        return
    t0, stable_since, last_size = time.time(), None, start_size
    while time.time() - t0 < timeout:
        time.sleep(0.5)
        size = klog.stat().st_size
        if size != last_size:
            last_size, stable_since = size, time.time()
            continue
        new = klog.read_text(errors="replace")[start_size:]
        if stable_since and time.time() - stable_since > 1.0 and PROMPT.search(new.rstrip().splitlines()[-1] if new.strip() else ""):
            print(new.rstrip())
            return
    print(klog.read_text(errors="replace")[start_size:].rstrip())
    log(f"no prompt after {timeout}s (long-running command, or symbols downloading)")


def brk(vm):
    """Halt the target: Ctrl+C on kd's terminal (kd's own break-in)."""
    d, fifo, klog, pids = _paths(vm)
    if not pids.exists() or "attached" not in status(vm):
        start(vm)
        time.sleep(10)
    if "HALTED" in status(vm):
        log("target is already halted")
        return
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, b"\x03")
    os.close(fd)
    t0 = time.time()
    while time.time() - t0 < 60:
        text = klog.read_text(errors="replace").rstrip()
        if text and PROMPT.search(text.splitlines()[-1]):
            log("target halted (the whole VM is frozen until `utmbot kd go`)")
            return
        time.sleep(1)
    log("no break-in within 60s: kd is not connected to the target (kdnet not up, wrong "
        "port, another kd holding it); `utmbot kd tail`")


def verbose(vm, level=4):
    """Make the display miniport's DbgPrint reach kd.log, before an install:
    its DebugLevel (a DWORD under the service's Parameters; absent or 0 is
    silent, 4 is info) and the kernel's print filter (Kd_DEFAULT_Mask is 0 by
    default, and drops the driver's messages even with DebugLevel set)."""
    rc, out = remote.ssh(vm, r'reg add "HKLM\SYSTEM\CurrentControlSet\Services\VioGpu3D\Parameters" '
                             f'/v DebugLevel /t REG_DWORD /d {int(level)} /f', timeout=60)
    if rc != 0:
        die(f"could not set DebugLevel in the guest: {out.strip()[-300:]}")
    log(f"VioGpu3D DebugLevel = {int(level)} (read when the driver loads: install or reboot after this)")
    brk(vm)
    send(vm, "ed nt!Kd_DEFAULT_Mask 0xffffffff")
    send(vm, ".reload", timeout=300)
    send(vm, "g")
    print("KD VERBOSE: DebugLevel set, kernel print filter open, symbols reloaded, target running")


def crash(vm):
    """Collect bugcheck/halt state."""
    for c in (".lastevent", ".bugcheck", "!analyze -v", "kv", "lmv m viogpu3d"):
        print(f"### {c}")
        send(vm, c, timeout=300)
