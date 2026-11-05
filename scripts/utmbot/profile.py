"""Full-stack profiling of an in-game window: guest ETW, then host xctrace.

  * `ingame` waits until the guest display is animating (two `capture
    screen` grabs 0.5 s apart differ, several times in a row).  A loading
    screen changes by ~0.1 (mean channel difference), a game by 20-100; a
    fade into a loading screen gives a couple of large values, hence the
    run of consecutive hits.  Time-based triggers missed: loading time
    varies run to run.
  * Guest: sampled CPU profiling (WPR CPU/GPU profiles, xperf PROFILE) is
    refused ("The request is not supported", 0x80070032): Hypervisor.framework
    emulates the PMU registers itself on this host, so QEMU's PMU emulation
    never sees the guest's accesses and Windows registers no profile source
    (`xperf -pmcsources` lists only Timer).  What works: the kernel logger with
    context-switch and ready-thread stacks (where threads waited, who woke
    them) and the DxgKrnl/DXGI providers (queue/DMA packets, presents).
    `etw start/stop` records those; `etw analyze` dumps the trace with
    symbols on the debugger dev VM (WPT is there) and summarizes it.
  * Host: one `xctrace --all-processes` session (kperf allows a single
    sampling session system-wide: two attached Time Profilers fail with
    "could not lock kperf").  Time Profiler names x86_64 (Rosetta) frames in
    the render workers; Metal System Trace gives GPU time per process.
"""

import subprocess
import sys
import time
from pathlib import Path

from . import procs, remote
from .common import DATA, PLUGIN, VM, Session, die, ensure_dir, log, run

ANALYZE = PLUGIN / "scripts" / "utmbot" / "analyze"
WPT_CACHE = DATA / "cache" / "wpt"
GUEST_WPT = r"C:\utmbot\wpt"
XPERF = GUEST_WPT + r"\xperf.exe"
DEV_XPERF = r"C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit\xperf.exe"


# ---- in-game trigger ----------------------------------------------------------

def frame_diff(vm, d):
    from . import capture, utm
    a, b = d / ".ingame-a.png", d / ".ingame-b.png"
    utm.osa("capture-screen", vm.name, str(a))
    time.sleep(0.5)
    utm.osa("capture-screen", vm.name, str(b))
    rc, out = run([capture.tool("imgdiff"), str(a), str(b)], timeout=30)
    try:
        return float(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0.0


def wait_ingame(vm, threshold=6.0, need=6, timeout=180):
    """Return when the display has been animating for `need` samples in a row."""
    d = ensure_dir(Session(vm).run_dir())
    hits, t0, seen = 0, time.time(), []
    while time.time() - t0 < timeout:
        v = frame_diff(vm, d)
        seen.append(v)
        hits = hits + 1 if v > threshold else 0
        if hits >= need:
            log(f"in-game after {time.time() - t0:.0f}s (diffs {', '.join(f'{x:.1f}' for x in seen[-need:])})")
            return True
    die(f"display never animated for {need} samples in {timeout}s (last diffs "
        f"{', '.join(f'{x:.1f}' for x in seen[-8:])})")


# ---- guest ETW ------------------------------------------------------------------

def _ensure_wpt(vm):
    rc, out = remote.ssh(vm, f'if exist "{XPERF}" (echo yes) else (echo no)')
    if "yes" in out:
        return
    if not (WPT_CACHE / "xperf.exe").exists():
        die(f"no Windows Performance Toolkit in {WPT_CACHE}: copy "
            r"'C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit' from a dev VM "
            "there once (scp -r)")
    log("pushing the Windows Performance Toolkit to the guest")
    remote.put(vm, WPT_CACHE, GUEST_WPT.replace("\\", "/"), recursive=True, timeout=900)


def etw_start(vm):
    _ensure_wpt(vm)
    remote.ssh(vm, f'"{XPERF}" -stop utmbot-dxg >nul 2>&1 & "{XPERF}" -stop >nul 2>&1 & '
                   r'del C:\utmbot\etw-*.etl 2>nul & exit 0')
    rc, out = remote.ssh(
        vm, f'"{XPERF}" -on PROC_THREAD+LOADER+CSWITCH+DISPATCHER -stackwalk CSwitch+ReadyThread '
            r'-BufferSize 1024 -MinBuffers 256 -MaxBuffers 1024 -f C:\utmbot\etw-k.etl '
            '-start utmbot-dxg -on Microsoft-Windows-DxgKrnl+Microsoft-Windows-DXGI '
            r'-BufferSize 1024 -MinBuffers 128 -f C:\utmbot\etw-u.etl')
    if rc != 0 or "error" in out.lower():
        die(f"xperf did not start: {out.strip()[-400:]}")
    Session(vm)["etw_started"] = time.time()
    print("ETW recording (context switches + ready threads with stacks, DxgKrnl, DXGI)")


def etw_stop(vm, name="etw"):
    rc, out = remote.ssh(vm, f'"{XPERF}" -stop utmbot-dxg -stop -d C:\\utmbot\\{name}.etl', timeout=600)
    if "Merged Etl" not in out:
        die(f"xperf stop failed: {out.strip()[-400:]}")
    rd = Session(vm).run_dir()
    remote.get(vm, f"C:/utmbot/{name}.etl", rd / f"{name}.etl", timeout=1800)
    secs = time.time() - (Session(vm).get("etw_started") or time.time())
    print(f"ETW {rd / (name + '.etl')} ({secs:.0f}s)")
    return rd / f"{name}.etl"


def etw_analyze(vm, etl, process, dev=None):
    """Dump `etl` with symbols on the dev VM (WPT and the symbol cache live there)
    and summarize it for `process` (an image-name substring)."""
    etl = Path(etl)
    dname = dev or vm.debugger
    if not dname:
        die("no dev VM to analyze on: pass --dev NAME (any VM made by provision-windows-dev-vm)")
    dvm = VM(dname)
    if not remote.reachable(dvm):
        die(f"dev VM '{dname}' is not reachable; start it")
    remote.ssh(dvm, r"mkdir C:\utmbot\symbols 2>nul & exit 0")
    pdbs = sorted((DATA / "drivers" / "ARM64").glob("*.pdb"))
    for p in pdbs:
        remote.put(dvm, p, f"C:/utmbot/symbols/{p.name}")
    remote.put(dvm, etl, f"C:/utmbot/{etl.name}", timeout=1800)
    txt = etl.with_suffix(".txt")
    log(f"dumping {etl.name} with symbols on '{dname}' (the first run downloads ~2 GB of "
        "Microsoft symbols)")
    rc, out = remote.ssh(
        dvm, r"set _NT_SYMBOL_PATH=srv*C:\symbols*https://msdl.microsoft.com/download/symbols;"
             rf'C:\utmbot\symbols&& "{DEV_XPERF}" -i C:\utmbot\{etl.name} -tle -tti -symbols -quiet '
             rf"-o C:\utmbot\{txt.name} -a dumper", timeout=7200)
    remote.get(dvm, f"C:/utmbot/{txt.name}", txt, timeout=3600)
    rc, out = run([sys.executable, "-I", str(ANALYZE / "etwstat.py"), str(txt), process, "15"],
                  timeout=3600)
    rep = etl.with_name(etl.stem + "-guest.txt")
    rep.write_text(out)
    print(out)
    if rc != 0:
        die(f"etwstat failed ({rc}); dump in {txt}")
    print(f"ETW-REPORT {rep}")


# ---- host xctrace ---------------------------------------------------------------

def hostprof(vm, secs=10, template="Time Profiler", ctx=None, name=None):
    """System-wide xctrace for `secs`; summarize this VM's QEMU and its busiest
    render worker (or the one whose guest context matches `ctx`)."""
    rd = Session(vm).run_dir()
    name = name or ("hostprof-" + ("metal" if "Metal" in template else "cpu") + time.strftime("-%H%M%S"))
    trace = rd / f"{name}.trace"
    t = procs.vm_tree(vm.name)
    workers = [w for w in t["workers"] if not ctx or ctx.lower() in (w.get("ctx_name") or "").lower()]
    worker = max(workers, key=lambda w: w["cpu"], default=None)
    qemu = t["qemu"]
    log(f"xctrace '{template}' system-wide for {secs}s (qemu {qemu and qemu['pid']}, worker "
        f"{worker and worker['pid']} {worker and worker.get('ctx_name')})")
    rc, out = run(["xcrun", "xctrace", "record", "--template", template, "--all-processes",
                   "--time-limit", f"{secs}s", "--output", str(trace)], timeout=secs + 1800)
    if not trace.exists():
        die(f"xctrace wrote no trace: {out.strip()[-400:]}")
    if "could not lock kperf" in out:
        die("another sampling session holds kperf (Instruments or a second xctrace); stop it")
    if "Metal" in template:
        xml = rd / f"{name}-gpu.xml"
        with open(xml, "w") as fh:
            subprocess.run(["xcrun", "xctrace", "export", "--input", str(trace), "--xpath",
                            '/trace-toc/run/data/table[@schema="metal-gpu-intervals"]'],
                           stdout=fh, stderr=subprocess.DEVNULL, timeout=3600)
        rc, rep = run([sys.executable, "-I", str(ANALYZE / "gpustat.py"), str(xml)], timeout=3600)
    else:
        xml = rd / f"{name}.xml"
        with open(xml, "w") as fh:
            subprocess.run(["xcrun", "xctrace", "export", "--input", str(trace), "--xpath",
                            '/trace-toc/run/data/table[@schema="time-profile"]'],
                           stdout=fh, stderr=subprocess.DEVNULL, timeout=3600)
        rep = ""
        for label, p in (("QEMU", qemu), ("render worker", worker)):
            if p:
                rc, o = run([sys.executable, "-I", str(ANALYZE / "xtstat.py"), str(xml), "25",
                             "--pid", str(p["pid"])], timeout=3600)
                rep += f"######## {label} pid {p['pid']}\n{o}\n"
    out_txt = rd / f"{name}.txt"
    out_txt.write_text(rep)
    print(rep)
    print(f"HOSTPROF {out_txt} (trace {trace}; open in Instruments for the timeline)")
