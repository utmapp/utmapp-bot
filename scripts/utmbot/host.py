"""Host side: logs, crash reports, backend proof, kill-watcher, perf/memory sampling.

Where host output lands under UTM (see skills/triage-gpu-issue/references/log-sinks.md):
  * Data/debug.log in the VM bundle (Debug Log on): QEMU's stdout/stderr,
    -trace output, SPICE, the launch command line and environment, and the
    render server and its workers (npt:, proxy:, vkr:, DXMT), which inherit
    QEMU's stderr.  With Debug Log off all of it is lost.
  * gpuEvent-*.ips (/Library/Logs/DiagnosticReports, moved to Retired/ within
    minutes): written by macOS for every GPU fault/restart, whatever the logs say.
  * Crash reports: ~/Library/Logs/DiagnosticReports/{QEMULauncher,QEMURenderServer,UTM}*.ips
"""

import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import procs
from .common import Session, die, log, pid_alive, run

# Lines that mean "something is wrong" (case-insensitive).  Only patterns that
# exist in current sources or in Metal's own error text; see knob-audit.sh.
HOSTLOG_RE = re.compile(
    r"(DIED on signal|exited with status [1-9]|dispatch failed|fatal decoder state|"
    r"retiring as device-lost|failed to (load|open|create|start)|not found|"
    r"backend refused|ring_submit_cmd|wire format mismatch|"
    r"Caused GPU (Address|Timeout|Hang)|PageFault|SubmissionsIgnored|InvalidResource|"
    r"OutOfMemory|Insufficient Memory|command buffer.*(error|abort)|"
    r"MTLCommandBuffer.*Error|validation error|-\[MTL.*failed assertion|"
    r"hvf.*(error|fail|assert)|Assertion|assertion failed|abort\(|SIGILL|SIGSEGV|"
    r"npt:.*(fail|error|unsupported)|vkr:.*(fail|error)|proxy: failed)", re.I)
HOSTLOG_NOISE = re.compile(
    r"(^Launching: |^    [A-Z_][A-Z0-9_]*=|failed to load D3D11On12CreateDevice|Spice-DEBUG|GSpice|GLib-GIO|"
    r"dropping ID3D12DeviceRemovedExtendedData)", re.I)

# Lines after which the VM is stopped at once: GPU faults and GPU hangs.  A
# GPU that faults repeatedly or hangs can take the host down (kernel panic
# "Excessive time stalled ... IOGPUWorkQueue", WindowServer watchdog), and
# a panic cannot be stopped once it starts.
FATAL_RE = re.compile(
    r"(Caused GPU (Address|Timeout|Hang)|kIOGPUCommandBufferCallbackError"
    r"(PageFault|OutOfMemory|Timeout|Hang)|Insufficient Memory|command buffer was aborted|"
    r"retiring as device-lost)", re.I)

DIAG_DIRS = [Path.home() / "Library/Logs/DiagnosticReports",
             Path("/Library/Logs/DiagnosticReports"),
             Path("/Library/Logs/DiagnosticReports/Retired"),
             Path.home() / "Library/Logs/DiagnosticReports/Retired"]


# ---- logs -------------------------------------------------------------------

def log_sources(vm):
    """[(label, path)] of every host log of the current VM lifetime."""
    from .utm import current_log_text
    sess = Session(vm)
    srcs = []
    dl = vm.debug_log()
    if dl and dl.exists():
        srcs.append(("debug.log", dl))
    rd = sess.run_dir()
    for f in sorted(rd.glob("virgl-*.log")):
        srcs.append((f.name, f))
    return srcs


def hostlog(vm, n=60, all_lines=False, pattern=None):
    from .utm import current_log_text
    rx = re.compile(pattern, re.I) if pattern else HOSTLOG_RE
    found = 0
    srcs = log_sources(vm)
    if not srcs or (srcs[0][0] == "debug.log" and not current_log_text(vm)):
        print("note: no debug.log from this VM lifetime (Debug Log is off); QEMU and "
              "render-server errors are invisible: rely on `crashes` and gpuEvent reports")
    for lbl, p in srcs:
        text = current_log_text(vm) if lbl == "debug.log" else p.read_text(errors="replace")
        if lbl == "debug.log" and 0 < len(text.splitlines()) < 25:
            # seen 3 of 3 times on the first boot after UTM launched
            print(f"note: debug.log holds only UTM's header ({len(text.splitlines())} lines). The first "
                  "boot after UTM launches often logs nothing more: no matches here prove nothing. "
                  "`vm down` and `vm up` again (UTM still running) for a real log")
        hits = [l for l in text.splitlines() if rx.search(l) and
                (all_lines or not HOSTLOG_NOISE.search(l))]
        found += len(hits)
        if hits:
            print(f"--- {lbl}: {len(hits)} matching line(s), last {min(n, len(hits))}")
            for l in hits[-n:]:
                print(l[:400])
    print(f"HOSTLOG {found} line(s)")
    return found


# ---- crash reports ------------------------------------------------------------

def _ips(path):
    """An .ips file is a JSON header line followed by a JSON body."""
    try:
        head, _, body = Path(path).read_text(errors="replace").partition("\n")
        return json.loads(head), (json.loads(body) if body.strip() else {})
    except Exception:
        return {}, {}


def _summarize_ips(path):
    head, body = _ips(path)
    name = Path(path).name
    if name.startswith("gpuEvent"):
        a = body.get("analysis", {})
        return (f"GPU EVENT {body.get('process_name', '?')}: "
                f"{a.get('restart_reason_desc', '?')}"
                + (f" fault addr=0x{a['bif0_fault'].get('address', 0):x} "
                   f"{'read' if a['bif0_fault'].get('is_read') else 'write'}"
                   if isinstance(a.get("bif0_fault"), dict) else "")
                + (f" guilty_dm={a.get('guilty_dm')}" if "guilty_dm" in a else ""))
    exc = body.get("exception", {})
    term = body.get("termination", {})
    ft = body.get("faultingThread", 0)
    frames = []
    try:
        imgs = body.get("usedImages", [])
        for f in body["threads"][ft]["frames"][:6]:
            img = imgs[f.get("imageIndex", 0)].get("name", "?") if imgs else "?"
            frames.append(f"{img}!{f.get('symbol', hex(f.get('imageOffset', 0)))}")
    except Exception:
        pass
    return (f"CRASH {body.get('procName', head.get('app_name', '?'))} "
            f"{exc.get('type', '')} {exc.get('signal', '')} {exc.get('subtype', '')} "
            f"{term.get('indicator', '')}\n      " + "\n      ".join(frames))


def crash_reports(since=None):
    pats = ["QEMULauncher*.ips", "QEMURenderServer*.ips", "UTM*.ips", "QEMUHelper*.ips",
            "gpuEvent-*.ips"]
    out = []
    for d in DIAG_DIRS:
        for p in pats:
            for f in glob.glob(str(d / p)):
                m = os.path.getmtime(f)
                if since is None or m >= since:
                    out.append((m, f))
    return sorted(set(out))


def crashes(vm, n=10, all_time=False):
    sess = Session(vm)
    since = None if all_time else sess.get("started")
    reps = crash_reports(since)
    for m, f in reps[-n:]:
        print(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(m))} {f}")
        print("    " + _summarize_ips(f))
    from .utm import current_log_text
    deaths = []
    for lbl, p in log_sources(vm):
        # the render server logs its workers' deaths in the VM's debug.log
        text = current_log_text(vm) if lbl == "debug.log" else p.read_text(errors="replace")
        for l in text.splitlines():
            m = re.search(r"worker (\d+) (DIED on signal (\d+)|exited with status (\d+))", l)
            if m:
                deaths.append((lbl, l.strip(), m[3]))
    for lbl, l, sig in deaths:
        # SIGKILL is how a worker is torn down when its context goes away
        # render workers leave no .ips crash report (seen: none for 3 SIGSEGVs);
        # the log line is the evidence, a debugger the way to the stack
        tag = ("routine" if sig == "9" else
               "REAL: no crash report is written for render workers; reproduce it with LLDB "
               "attached to the worker (Debug dev build: run-gpu-test-vm, triage-gpu-issue)" if sig else "")
        print(f"{lbl}: {l} {tag}")
    if not reps and not deaths:
        print("no crash reports, GPU events or worker deaths" +
              ("" if all_time else " since this VM started"))
    print(f"CRASHES reports={len(reps)} worker_deaths={len(deaths)} "
          f"real={sum(1 for d in deaths if d[2] and d[2] != '9')}")


# ---- backend proof ------------------------------------------------------------

def backend(vm):
    """Which DirectX backend is live, proven from the processes and logs."""
    from .utm import current_log_text, header_env
    tree = procs.vm_tree(vm.name)
    env = header_env(current_log_text(vm))
    if not env and tree["qemu"]:
        env = procs.env_of(tree["qemu"]["pid"])
    print(f"requested  {Session(vm).get('backend_requested', '?')}")
    print(f"NPT_BACKEND={env.get('NPT_BACKEND', '(unset: DirectX disabled or unavailable)')}")
    lib = env.get("NPT_D3D11_LIBRARY_PATH", "")
    if lib:
        print(f"library    {lib}")
    fw = env.get("D3DMETAL_FRAMEWORK_PATH")
    if fw:
        info = Path(fw).parents[2] / "Resources" / "Info.plist" if "Versions" in fw else None
        ver = "?"
        if info and info.exists():
            import plistlib
            d = plistlib.loads(info.read_bytes())
            ver = f"{d.get('CFBundleShortVersionString')} ({d.get('CFBundleVersion')})"
        print(f"D3DMetal   {ver}  {fw}")
    ws = tree["workers"]
    for w in ws:
        print(f"worker     pid {w['pid']} ctx {w['ctx_id']} {w['ctx_name']} {w['arch']}")
    if not ws:
        print("no render worker yet (one spawns per guest process that opens the GPU; "
              "on Windows DWM opens one once the driver is installed)")
    else:
        # a D3DMetal worker runs x86_64 under Rosetta, DXMT and Venus ones arm64
        want = {"d3dmetal": "x86_64", "dxmt": "arm64"}.get(env.get("NPT_BACKEND", ""))
        if want is None and any(w["arch"] == "x86_64" for w in ws):
            # no NPT_BACKEND (a UTM that does not pass the environment): an
            # x86_64 worker can only be D3DMetal under Rosetta
            want = "x86_64"
        # Venus (Vulkan) contexts are arm64 whatever the DirectX backend, so an
        # arm64 worker alone proves nothing about D3D
        good = [w for w in ws if w["arch"] == want]
        if good and want == "x86_64":
            print(f"BACKEND d3dmetal confirmed ({len(good)}/{len(ws)} workers x86_64)")
        elif want == "x86_64":
            print("BACKEND NOT CONFIRMED: no x86_64 worker. Start a D3D app: Venus (Vulkan) "
                  "contexts are arm64 and cannot show the DirectX backend")
        else:
            print(f"BACKEND {env.get('NPT_BACKEND', '?')}: workers are arm64 (DXMT and Venus "
                  "look the same here; the render-server log's 'loaded D3D11 library' line "
                  "is the proof)")
    from .utm import current_log_text
    for lbl, p in log_sources(vm):
        # the render server logs which library it loaded into the VM's debug.log
        text = current_log_text(vm) if lbl == "debug.log" else p.read_text(errors="replace")
        for l in text.splitlines():
            if re.search(r"loaded (D3D11|D3D12|DXGI) library|neptune backend", l):
                print(f"{lbl}: {l.strip()}")
                break


# ---- host state ---------------------------------------------------------------

def screen_locked():
    rc, out = run(["ioreg", "-n", "Root", "-d1", "-a"])
    return b"CGSSessionScreenIsLocked" in out.encode() and \
        re.search(r"<key>CGSSessionScreenIsLocked</key>\s*<true/>", out) is not None


def check_host(vm=None, quiet=True):
    """Conditions that silently ruin measurements; returns a list of problems."""
    problems = []
    if screen_locked():
        problems.append("the Mac's screen is locked: the VM window is not composited, App Nap "
                        "applies and screen recordings capture the lock screen")
    if vm:
        tree = procs.vm_tree(vm.name)
        if tree["qemu"]:
            pri = procs.napped(tree["qemu"]["pid"])
            if pri and max(pri) <= 4:
                problems.append("QEMU is App-Napped (thread priority 4): bring UTM to the "
                                "front (utmbot front); napped vCPUs run ~4x slower")
    rc, out = run(["pmset", "-g", "assertions"])
    m = re.search(r"PreventUserIdleDisplaySleep\s+(\d+)", out)
    if m and m[1] == "0":
        problems.append("nothing holds the display awake (`vm up` does while its VM runs): a "
                        "sleeping display stops the VM window drawing (captures freeze)")
    st = os.statvfs(str(Path.home()))
    free_gb = st.f_bavail * st.f_frsize / 2**30
    if free_gb < 20:
        problems.append(f"only {free_gb:.0f} GB free on the home volume (crash cores in "
                        "/cores and logs can fill it; a full disk looks like a dead guest)")
    rc, out = run(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used = ([\d.]+)M", out)
    if m and float(m[1]) > 4096:
        problems.append(f"{float(m[1]) / 1024:.1f} GB swap in use: the host is under memory "
                        "pressure")
    rc, out = run(["ps", "-Ao", "pcpu=,comm="])
    hogs = [l.strip() for l in out.splitlines()
            if re.search(r"(mds_stores|mdworker|backupd|softwareupdated)", l) and
            float(l.split()[0]) > 30]
    if hogs:
        problems.append("background CPU hogs: " + "; ".join(hogs))
    if not quiet or problems:
        for p in problems:
            log("HOST: " + p)
        if not problems and not quiet:
            log("HOST: ok")
    return problems


# ---- kill-watcher ------------------------------------------------------------

def _kw_paths(vm):
    d = vm.session_dir()
    return d / "killwatch.pid", d / "killwatch.heartbeat", d / "killwatch.fired"


def killwatch_arm(vm, grace=2.0):
    pidf, hb, fired = _kw_paths(vm)
    killwatch_disarm(vm, quiet=True)
    if fired.exists():
        fired.unlink()
    p = subprocess.Popen([sys.executable, "-m", "utmbot", "--vm", vm.name, "_killwatch",
                          str(grace)], cwd=str(Path(__file__).resolve().parents[1]),
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=open(vm.session_dir() / "killwatch.log", "a"))
    pidf.write_text(str(p.pid))
    time.sleep(1.5)
    if p.poll() is not None:
        die("kill-watcher exited immediately; see " + str(vm.session_dir() / "killwatch.log"))
    log(f"kill-watcher armed (pid {p.pid})")


def killwatch_disarm(vm, quiet=False):
    pidf, hb, _ = _kw_paths(vm)
    if pidf.exists():
        pid = pidf.read_text().strip()
        if pid_alive(pid):
            os.kill(int(pid), signal.SIGTERM)
        pidf.unlink()
        if not quiet:
            log("kill-watcher disarmed")


def killwatch_state(vm):
    pidf, hb, fired = _kw_paths(vm)
    if fired.exists() and fired.stat().st_mtime >= (Session(vm).get("started") or 0):
        return "FIRED: " + fired.read_text().strip()
    if not pidf.exists():
        return "not armed"
    if not pid_alive(pidf.read_text().strip()):
        return "DEAD (armed but not running — memory pressure can kill it; re-arm)"
    age = time.time() - hb.stat().st_mtime if hb.exists() else 1e9
    return "armed" if age < 10 else f"STALLED (no heartbeat for {age:.0f}s)"


def killwatch_loop(vm, grace):
    """Watch every host log and the GPU event reports; stop the VM on a GPU fault."""
    pidf, hb, fired = _kw_paths(vm)
    from .utm import osa
    t_arm = time.time()
    offsets = {}
    seen_gpu = {f for _, f in crash_reports()}

    def trigger(reason):
        fired.write_text(reason + "\n")
        print(f"{time.strftime('%H:%M:%S')} FIRED: {reason}", file=sys.stderr, flush=True)
        time.sleep(grace)
        tree = procs.vm_tree(vm.name)
        for p in [tree["qemu"]] + tree["servers"] + tree["workers"]:
            if p:
                try:
                    os.kill(p["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            osa("kill", vm.name, timeout=30)
        except Exception:
            pass

    while True:
        hb.touch()
        tree = procs.vm_tree(vm.name)
        if not tree["qemu"] and time.time() - t_arm > 120:
            return
        for lbl, p in log_sources(vm):
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                continue
            off = offsets.get(str(p))
            if off is None:
                # only lines written after arming count
                offsets[str(p)] = size if lbl == "debug.log" else 0
                continue
            if size < off:
                off = 0
            if size > off:
                with open(p, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read(size - off).decode("utf-8", "replace")
                offsets[str(p)] = size
                m = FATAL_RE.search(chunk)
                if m:
                    line = next(l for l in chunk.splitlines() if FATAL_RE.search(l))
                    return trigger(f"{lbl}: {line.strip()[:300]}")
        for _, f in crash_reports(t_arm):
            if f not in seen_gpu and Path(f).name.startswith("gpuEvent"):
                seen_gpu.add(f)
                return trigger(f"GPU event report {Path(f).name}: {_summarize_ips(f)}")
        time.sleep(0.5)


# ---- sampling ----------------------------------------------------------------

def gpu_util():
    rc, out = run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])
    m = re.search(r'"PerformanceStatistics" = \{([^}]*)\}', out)
    if not m:
        return {}
    vals = dict(re.findall(r'"(Device|Renderer|Tiler) Utilization %"=(\d+)', m[1]))
    mem = re.search(r'"In use system memory"=(\d+)', m[1])
    if mem:
        vals["gpu_mem_mb"] = int(mem[1]) // 2**20
    return vals


def perfcap(vm, secs=60, sample_at=None, sample_secs=10):
    """Per-second GPU utilization and per-process CPU; optionally one `sample`.

    `sample` and LLDB stop the target while they read it (~1 s for sample):
    the guest sees a hitch, and a long stop can trip the guest's GPU timeout.
    Never use vmmap on a render server: it stalls GPU submission long enough
    for the guest to reset the GPU (TDR)."""
    rd = Session(vm).run_dir()
    out = rd / f"perfcap-{time.strftime('%H%M%S')}.txt"
    with open(out, "w") as fh:
        for i in range(secs):
            tree = procs.vm_tree(vm.name)
            g = gpu_util()
            line = f"t={i:3d} gpu dev={g.get('Device', '?')}% ren={g.get('Renderer', '?')}% " \
                   f"til={g.get('Tiler', '?')}%"
            if tree["qemu"]:
                line += f" qemu={tree['qemu']['cpu']}%"
            for w in tree["workers"]:
                line += f" w{w['ctx_id']}={w['cpu']}%"
            fh.write(line + "\n")
            print(line, flush=True)
            if sample_at is not None and i == sample_at:
                busiest = max(tree["workers"], key=lambda w: w["cpu"], default=None)
                for name, p in (("qemu", tree["qemu"]), ("worker", busiest)):
                    if p:
                        run(["sample", str(p["pid"]), str(sample_secs), "-file",
                             str(rd / f"sample-{name}-{p['pid']}.txt")],
                            timeout=sample_secs + 30)
            time.sleep(1)
    print(f"PERFCAP {out}")


def memsample(vm, secs=0, every=5):
    """Host memory pressure + QEMU/render-server RSS (swap, not free pages, is
    the signal: macOS keeps free pages near zero by design)."""
    def once():
        rc, sw = run(["sysctl", "-n", "vm.swapusage"])
        rc, lvl = run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
        tree = procs.vm_tree(vm.name)
        rss = " ".join(f"{k}={p['rss_mb']}MB" for k, p in
                       [("qemu", tree["qemu"])] + [(f"w{w['ctx_id']}", w) for w in tree["workers"]]
                       if p)
        g = gpu_util()
        print(f"{time.strftime('%H:%M:%S')} swap[{sw.strip()}] pressure={lvl.strip()} "
              f"gpu_mem={g.get('gpu_mem_mb', '?')}MB {rss}", flush=True)
    once()
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(every)
        once()


def attach(vm, which, ident=None, cmds=()):
    tree = procs.vm_tree(vm.name)
    target = None
    if which == "qemu":
        target = tree["qemu"]
    elif which == "server":
        target = tree["servers"][0] if tree["servers"] else None
    else:
        ws = tree["workers"]
        if ident:
            ws = [w for w in ws if str(w["ctx_id"]) == ident or w["ctx_name"] == ident]
        target = max(ws, key=lambda w: w["pid"], default=None)
    if not target:
        die(f"no {which} process for '{vm.name}'")
    if not cmds:
        print(f"lldb -p {target['pid']}   # interactive; the target stops while attached")
        return
    args = ["lldb", "--batch", "-p", str(target["pid"])]
    for c in cmds:
        args += ["-o", c]
    args += ["-o", "detach"]
    log(f"attaching to {which} pid {target['pid']} (the VM stalls while attached)")
    rc, out = run(args, timeout=300)
    print(out)
