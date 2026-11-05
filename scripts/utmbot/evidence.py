"""`snapshot`: everything worth knowing before forming a theory, in one place.

Most expensive mistakes in this stack came from theorising about the layer
under study while the real cause sat somewhere unexamined: an uncommitted
edit in another tree, a stale framework embedded in the app, a different
D3DMetal than the "good" run used, a halted kernel debugger, a host that was
asleep or swapping.  The snapshot puts all of those on one page.
"""

import contextlib
import io
import plistlib
import re
import time
from pathlib import Path

from . import host, remote
from .common import DATA, Session, run


def _section(title, fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn()
        except Exception as e:
            print(f"(failed: {e})")
    return f"## {title}\n\n```\n{buf.getvalue().rstrip()}\n```\n"


def _trees():
    """Source trees to report, one path per line in ~/.utmapp-bot/trees.conf."""
    conf = DATA / "trees.conf"
    if not conf.exists():
        print(f"no {conf}: list your source trees there (one path per line) to have their "
              "state checked here — uncommitted edits in a tree you are not looking at are "
              "a classic cause of 'impossible' symptoms")
        return
    for line in conf.read_text().splitlines():
        p = Path(line.strip()).expanduser()
        if not line.strip() or line.startswith("#"):
            continue
        if not (p / ".git").exists():
            print(f"{p}: not a git checkout")
            continue
        rc, head = run(["git", "-C", str(p), "log", "-1", "--format=%h %cd %s", "--date=short"])
        rc, br = run(["git", "-C", str(p), "rev-parse", "--abbrev-ref", "HEAD"])
        rc, st = run(["git", "-C", str(p), "status", "--short", "--untracked-files=no"])
        print(f"{p.name} [{br.strip()}] {head.strip()}")
        for l in st.splitlines()[:15]:
            print(f"    {l}")
        if len(st.splitlines()) > 15:
            print(f"    … {len(st.splitlines()) - 15} more modified files")


def _app(vm):
    from .utm import app_path, app_version
    app = Session(vm).get("app") or app_path()
    print(f"UTM.app   {app}  {app_version(app)}")
    fw = Path(app) / "Contents" / "Frameworks"
    for name in ("virglrenderer.1", "d3dmetal-native", "dxmt-native", "qemu-aarch64-softmmu",
                 "D3DMetal"):
        f = fw / f"{name}.framework"
        if f.exists():
            info = next(f.rglob("Info.plist"), None)
            ver = ""
            if info:
                with contextlib.suppress(Exception):
                    d = plistlib.loads(info.read_bytes())
                    ver = d.get("CFBundleShortVersionString", "")
            bins = [b for b in f.rglob(name) if b.is_file()]
            mt = time.strftime("%Y-%m-%d %H:%M", time.localtime(bins[0].stat().st_mtime)) if bins else "?"
            print(f"{name:22s} {ver:10s} built {mt}")
        else:
            print(f"{name:22s} MISSING")


def _guest(vm):
    if not remote.reachable(vm):
        print("guest not reachable")
        return
    if vm.is_windows:
        rc, out = remote.ps1(vm, r"""
$d = Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.HardWareID -like 'PCI\VEN_1AF4&DEV_1050*' }
foreach ($x in $d) { "GPU driver: $($x.DeviceName) $($x.DriverVersion) $($x.InfName) $($x.DriverDate)" }
$u = Get-ChildItem C:\Windows\System32\DriverStore\FileRepository -Recurse -Filter neptune_umd*.dll -ErrorAction SilentlyContinue |
     Sort-Object LastWriteTime -Descending | Select-Object -First 3
foreach ($f in $u) { "UMD: $($f.FullName) $($f.VersionInfo.FileVersion) $($f.LastWriteTime)" }
"OS: " + (Get-CimInstance Win32_OperatingSystem).Caption + " " + [Environment]::OSVersion.Version
"Boot: " + (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
"bcdedit debug: " + ((bcdedit /enum '{current}' | Select-String -Pattern '^debug\s') -join ' ')
"--- System log: display, TDR (LiveKernelEvent 141/117), bugchecks, last 2h"
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddHours(-2)} -ErrorAction SilentlyContinue |
  Where-Object { $_.ProviderName -match 'Display|dxgkrnl|viogpu|BugCheck|WER-SystemErrorReporting|Kernel-PnP' -or $_.Id -in 4101,1001,41 } |
  Select-Object -First 25 | ForEach-Object { "$($_.TimeCreated.ToString('HH:mm:ss')) $($_.ProviderName) $($_.Id): $(($_.Message -split "`n")[0])" }
"--- Application crashes, last 2h"
Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1002; StartTime=(Get-Date).AddHours(-2)} -ErrorAction SilentlyContinue |
  Select-Object -First 10 | ForEach-Object { "$($_.TimeCreated.ToString('HH:mm:ss')) $($_.Id): $(($_.Message -split "`n")[0..2] -join ' | ')" }
"--- dumps"
Get-ChildItem C:\Windows\Minidump, C:\Windows\LiveKernelReports -Recurse -ErrorAction SilentlyContinue | Select-Object -Last 5 | ForEach-Object { "$($_.FullName) $($_.LastWriteTime)" }
""", timeout=180)
        print(out.strip())
    else:
        rc, out = remote.ssh(vm, "uname -r; (vulkaninfo --summary 2>/dev/null | grep -E "
                                 "'deviceName|driverID|driverInfo' | head -6); "
                                 "dpkg -l 2>/dev/null | grep -E 'mesa-vulkan|wine|fex' | "
                                 "awk '{print $2, $3}' | head; "
                                 "journalctl -k -b --no-pager 2>/dev/null | grep -iE "
                                 "'virtio_gpu|drm|gpu' | tail -10", timeout=120)
        print(out.strip())


def _kd(vm):
    from . import kd
    if not vm.debugger:
        print("no kdnet debugger configured")
        return
    print("kd: " + kd.status(vm))
    _, _, klog, _ = kd._paths(vm)
    if klog.exists():
        lines = klog.read_text(errors="replace").splitlines()
        hits = [l for l in lines if re.search(r"Break instruction|Bugcheck|Fatal System Error|"
                                              r"Possible deadlock|Assertion|Access violation|"
                                              r"not signed correctly", l, re.I)]
        for l in hits[-8:]:
            print("  " + l)
        print("  … last line: " + (lines[-1] if lines else ""))


def snapshot(vm):
    rd = Session(vm).run_dir()
    out = rd / f"snapshot-{time.strftime('%H%M%S')}.md"
    from .utm import vm_status
    parts = [f"# Evidence snapshot: {vm.name} {time.strftime('%Y-%m-%d %H:%M:%S')}\n"]
    parts.append(_section("Host state", lambda: print("\n".join(host.check_host(vm)) or "ok")))
    parts.append(_section("VM and processes", lambda: vm_status(vm)))
    parts.append(_section("UTM app and embedded host libraries", lambda: _app(vm)))
    parts.append(_section("Backend", lambda: host.backend(vm)))
    parts.append(_section("Source trees", _trees))
    parts.append(_section("Host log errors (this VM lifetime)", lambda: host.hostlog(vm, n=30)))
    parts.append(_section("Crash reports, GPU events, worker deaths", lambda: host.crashes(vm)))
    parts.append(_section("Kernel debugger", lambda: _kd(vm)))
    parts.append(_section("Guest", lambda: _guest(vm)))
    out.write_text("\n".join(parts))
    print("\n".join(parts))
    print(f"SNAPSHOT {out}")
