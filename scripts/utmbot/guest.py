"""Windows guest operations: console-session tasks, driver install/verify, dumps.

D3D must run in the interactive console session.  sshd runs on a service
window station where display devices do not enumerate, so a D3D app started
over SSH fails to find the adapter (or silently lands on WARP).  Everything
that touches the GPU therefore goes through `console_task`:

  * the command goes into a .cmd wrapper pushed to the guest, because
    `schtasks /tr` mangles embedded quotes (the task "runs" and does nothing);
  * every task has a unique name, because /run on a reused name has been seen
    to no-op silently late in a boot;
  * `start x.cmd` is never used (it runs under `cmd /K` and leaves a console
    holding the output redirect forever) — always `cmd /c`;
  * hidden tasks go through wscript, because a console window in the session
    takes the foreground and a fullscreen app started behind it loses focus;
  * completion is a marker line the wrapper appends, not the task status.
"""

import re
import tempfile
import time
from pathlib import Path

from . import remote
from .common import PLUGIN, Session, die, log, probe, write_text
from .wait import Outcome, until

GW = "C:\\utmbot"          # guest work dir (cmd paths)
GWS = "C:/utmbot"          # same, for scp
DONE = "__UTMBOT_EXIT__"


def _task_name(prefix="ub"):
    return f"{prefix}{int(time.time() * 1000) % 100000000}"


def console_task(vm, cmdline, out_name=None, hidden=True, env=None, cwd=GW):
    """Start cmdline in the console session; return (task, guest output path)."""
    tn = _task_name()
    out_name = out_name or f"{tn}.out.txt"
    out_path = f"{GW}\\{out_name}"
    sets = "".join(f'set "{k}={v}"\r\n' for k, v in (env or {}).items())
    body = ("@echo off\r\n" f"cd /d {cwd}\r\n" + sets +
            f"{cmdline} > \"{out_path}\" 2>&1\r\n"
            f"echo {DONE} %ERRORLEVEL% >> \"{out_path}\"\r\n")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td, f"{tn}.cmd")
        write_text(w, body)
        remote.put(vm, w, f"{GWS}/{tn}.cmd")
        if hidden:
            v = Path(td, f"{tn}.vbs")
            write_text(v, 'Set sh = CreateObject("WScript.Shell")\r\n'
                          f'sh.Run "cmd /c {GW}\\{tn}.cmd", 0, True\r\n')
            remote.put(vm, v, f"{GWS}/{tn}.vbs")
            tr = f"wscript.exe {GW}\\{tn}.vbs"
        else:
            tr = f"{GW}\\{tn}.cmd"
    remote.ssh(vm, f'del /q "{out_path}" 2>nul & schtasks /create /tn {tn} /tr "{tr}" '
                   f"/sc once /st 00:00 /ru {vm.user} /it /f >nul && schtasks /run /tn {tn}",
               check=True)
    return tn, out_path


def read_out(vm, out_path):
    rc, out = remote.ssh(vm, f'type "{out_path}" 2>nul', timeout=60)
    return out


def end_task(vm, tn, image=None):
    """Kill what a console task started: every process descended from the
    task's wrapper (wscript -> cmd -> the program), and, when the program
    detached from it (a launcher, a game started by its stub), processes of
    its image.  Deleting the task kills nothing."""
    img = Path(image).stem if image else ""
    rc, out = remote.ps1(vm, rf"""
$all = Get-CimInstance Win32_Process
$roots = @($all | Where-Object {{ $_.CommandLine -like '*{tn}.*' }})
$kill = New-Object System.Collections.Generic.List[int]
$queue = New-Object System.Collections.Generic.Queue[int]
foreach ($r in $roots) {{ $queue.Enqueue([int]$r.ProcessId) }}
while ($queue.Count) {{
  $p = $queue.Dequeue(); if ($kill.Contains($p)) {{ continue }}; $kill.Add($p)
  foreach ($c in $all | Where-Object {{ $_.ParentProcessId -eq $p }}) {{ $queue.Enqueue([int]$c.ProcessId) }}
}}
$img = '{img}'
if ($img) {{ foreach ($p in Get-Process -Name $img -ErrorAction SilentlyContinue) {{ if (-not $kill.Contains($p.Id)) {{ $kill.Add($p.Id) }} }} }}
$names = foreach ($p in $kill) {{ $x = $all | Where-Object {{ $_.ProcessId -eq $p }}; if ($x) {{ "$($x.Name)($p)" }} }}
foreach ($p in $kill) {{ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Milliseconds 500
$left = @($kill | Where-Object {{ Get-Process -Id $_ -ErrorAction SilentlyContinue }})
"ENDED $($names -join ' ') LEFT $($left -join ' ')"
""", timeout=60)
    m = re.search(r"ENDED (.*?) LEFT ?(.*)", out)
    if m:
        log(f"ended task {tn}: {m[1].strip() or 'nothing was running'}"
            + (f"; STILL RUNNING: {m[2].strip()}" if m[2].strip() else ""))
    else:
        log(f"could not end task {tn}: {out.strip()[-300:]}")


def wait_task(vm, tn, out_path, timeout_s, what, image=None):
    def check():
        out = read_out(vm, out_path)
        m = re.search(DONE + r" (-?\d+)", out)
        if m:
            return True, f"exit {m[1]}"
        lines = [l for l in out.splitlines() if l.strip()]
        return False, (lines[-1][:100] if lines else "no output yet")
    o, obs = until(check, timeout_s, what, every=20, vm=vm, interval=3)
    if o == Outcome.TIMEOUT:
        # the program is still running; a later test must not find it there
        end_task(vm, tn, image)
    if o != Outcome.VM_DEAD:
        remote.ssh(vm, f"schtasks /delete /tn {tn} /f >nul 2>&1 & exit 0", timeout=30)
    out = read_out(vm, out_path) if o != Outcome.VM_DEAD else ""
    m = re.search(DONE + r" (-?\d+)", out)
    rc = int(m[1]) if m else None
    out = re.sub(DONE + r".*\n?", "", out)
    return o, rc, out


def push_if_local(vm, exe):
    """A host file is pushed to the guest; a bare probe name (readback11.exe)
    means the built probe; anything else is a guest command or path."""
    p = Path(exe)
    if not p.exists() and "/" not in exe and "\\" not in exe and exe.lower().endswith(".exe"):
        p = probe(exe) or p
    if p.exists():
        remote.put(vm, p, f"{GWS}/{p.name}")
        return f"{GW}\\{p.name}"
    return exe


def quote_win(a):
    return f'"{a}"' if (" " in a and not a.startswith('"')) else a


def test(vm, exe, args, timeout_s=600, env=None, visible=False):
    """Run a program in the console session and wait for it to exit."""
    cmd = " ".join([quote_win(push_if_local(vm, exe))] + [quote_win(a) for a in args])
    tn, outp = console_task(vm, cmd, hidden=not visible, env=env)
    log(f"running in console session: {cmd}")
    o, rc, out = wait_task(vm, tn, outp, timeout_s, Path(exe).name, image=_image(exe))
    rd = Session(vm).run_dir()
    (rd / f"test-{Path(exe).stem}-{tn}.txt").write_text(out)
    print(out.rstrip())
    print(f"TEST {Path(exe).name}: {o} exit={rc}")
    return o, rc, out


def _image(exe):
    """The program's image name, when exe names a program (not a command line)."""
    n = Path(exe.replace("\\", "/")).name
    return n if n.lower().endswith(".exe") else None


def launch(vm, exe, args, env=None, visible=False):
    """Start a long-running/GUI program in the console session and return."""
    cmd = " ".join([quote_win(push_if_local(vm, exe))] + [quote_win(a) for a in args])
    tn, outp = console_task(vm, cmd, out_name="launch-out.txt", hidden=not visible, env=env)
    Session(vm)["last_launch"] = {"task": tn, "out": outp, "cmd": cmd}
    print(f"LAUNCHED {cmd} (task {tn}); output: utmbot launch-out")


def launch_out(vm):
    l = Session(vm).get("last_launch")
    if not l:
        die("nothing launched in this session")
    print(re.sub(DONE + r" (-?\d+)", r"[exited \1]", read_out(vm, l["out"])).rstrip())


def display_fix(vm):
    """Un-wedge the desktop after a driver install: restart DWM, then reset the
    display mode from the console session (CDS_RESET does nothing from the
    service window station)."""
    remote.put(vm, PLUGIN / "scripts" / "utmbot" / "guestfiles" / "cdsreset.ps1",
               f"{GWS}/cdsreset.ps1")
    remote.ssh(vm, "taskkill /f /im dwm.exe >nul 2>&1 & exit 0")
    time.sleep(10)  # dwm restarts by itself; reset only after it is back
    tn, outp = console_task(vm, f"powershell -NoProfile -ExecutionPolicy Bypass -File "
                                f"{GW}\\cdsreset.ps1")
    o, rc, out = wait_task(vm, tn, outp, 60, "display reset")
    print(out.strip() or f"display-fix: {o}")


def _cert_and_install_script(pkg):
    return rf"""
$ErrorActionPreference = 'Stop'
$pkg = '{pkg}'
$cat = Get-ChildItem $pkg -Filter *.cat | Select-Object -First 1
$sig = Get-AuthenticodeSignature $cat.FullName
if (-not $sig.SignerCertificate) {{ Write-Output "INSTALL-FAIL catalog $($cat.Name) is not signed"; exit 1 }}
foreach ($store in 'Root','TrustedPublisher') {{
  $s = New-Object System.Security.Cryptography.X509Certificates.X509Store($store, 'LocalMachine')
  $s.Open('ReadWrite'); $s.Add($sig.SignerCertificate); $s.Close()
}}
Write-Output "CERT $($sig.SignerCertificate.Subject) trusted (Root, TrustedPublisher)"
"""


def released_in_guest(vm):
    """The released driver on the guest tools drive `vm up` had UTM attach:
    (its folder on the guest, its INF date)."""
    # The OS architecture, from WMI (a native process): the environment of an
    # emulated x64 shell on ARM64 says AMD64.
    rc, out = remote.ps1(vm, r"""
$ErrorActionPreference = 'Stop'
try {
  $arch = if ((Get-CimInstance Win32_OperatingSystem).OSArchitecture -match 'ARM') { 'ARM64' } else { 'amd64' }
  $roots = @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue | ForEach-Object { $_.Root })
  foreach ($r in $roots) {
    $dir = Join-Path $r "Drivers\viogpu3d\w11\$arch"
    $inf = Join-Path $dir 'viogpu3d.inf'
    if (-not (Test-Path -LiteralPath $inf -ErrorAction SilentlyContinue)) { continue }
    $m = Select-String -LiteralPath $inf -Pattern '^\s*DriverVer\s*=\s*(\d+)/(\d+)/(\d{4})' | Select-Object -First 1
    if ($m) {
      $g = $m.Matches[0].Groups
      "RELEASED {0} {1}-{2:00}-{3:00}" -f $dir, [int]$g[3].Value, [int]$g[1].Value, [int]$g[2].Value
      exit 0
    }
    "NODATE $inf"
  }
  "NODRIVE $arch $($roots -join ' ')"
} catch { "PSERROR $($_.Exception.Message)"; exit 1 }
""", timeout=60)
    m = re.search(r"^RELEASED (.+?) (\d{4}-\d\d-\d\d)\s*$", out, re.M)
    if m:
        return m[1].strip(), m[2]
    bad = re.findall(r"^NODATE (.+?)\s*$", out, re.M)
    if bad:
        die(f"the guest tools have {', '.join(bad)} but no DriverVer = MM/DD/YYYY line in it to "
            "date the driver by")
    nd = re.search(r"^NODRIVE (\S+) ?(.*?)\s*$", out, re.M)
    if nd:
        gt = Session(vm).get("guest_tools") or "not recorded"
        die(f"no drive in the guest has Drivers\\viogpu3d\\w11\\{nd[1]} (drives: {nd[2] or 'none'}). "
            f"`vm up` has UTM put its guest tools in the VM's empty removable drive (this "
            f"boot: {gt})")
    err = re.search(r"^PSERROR (.*)$", out, re.M)
    detail = f"PowerShell: {err[1].strip()}" if err else f"exit {rc}: {out.strip()[-400:]}"
    die(f"looking for the guest tools in the guest failed ({detail})")


def install(vm, pkg_dir=None, released=False):
    """Install a whole signed driver package: a folder on this Mac, or with
    released=True the released driver from the guest tools drive in the VM.
    Returns how the driver is named (driver_identity, or "released driver
    <INF date>"), also kept in the session as installed_driver.

    Always the full package: pushing single files makes pnputil skip an
    'up-to-date' driver or reject a catalog whose hashes no longer match."""
    sess = Session(vm)
    if sess.get("writable"):
        die("this boot is writable: a driver under test is only ever installed in a "
            "disposable boot, so it never lands in the disk image or a snapshot")
    if sess.get("installed"):
        die("a driver was already installed in this boot; `vm down` + `vm up` first. "
            "Reinstalling over a driver that has run wedges the display and is not a clean test.")
    if released:
        src, date = released_in_guest(vm)
        ident = f"released driver {date}"
    else:
        src = Path(pkg_dir)
        if not (src / "viogpu3d.inf").exists():
            die(f"{src} has no viogpu3d.inf (expected a package from build-viogpu-driver, "
                "e.g. ~/.utmapp-bot/drivers/ARM64, or `install --released`)")
        ident = driver_identity(src)
    remote.ssh(vm, f'if exist {GW}\\pkg rmdir /s /q {GW}\\pkg & mkdir {GW}\\pkg', check=True)
    if released:
        log(f"{ident} from the guest tools: copying {src} in the guest")
        remote.ssh(vm, f'xcopy /e /i /q /y "{src}" {GW}\\pkg >nul', check=True, timeout=120)
    else:
        log(f"pushing {src} ({ident})")
        remote.put(vm, str(src) + "/.", f"{GWS}/pkg", recursive=True)
    rc, out = remote.ps1(vm, _cert_and_install_script(f"{GW}\\pkg"), timeout=120)
    print(out.strip())
    if rc != 0:
        die("could not trust the package's signing certificate")
    # an older resolution agent service would keep the old binaries in use
    remote.ssh(vm, "sc stop vgpusrv >nul 2>&1 & sc delete vgpusrv >nul 2>&1 & exit 0")
    rc, out = remote.ssh(vm, f"cd /d {GW}\\pkg && pnputil /add-driver viogpu3d.inf /install",
                         timeout=600)
    (sess.run_dir() / "install.txt").write_text(out)
    n = len(re.findall(r"installed on device", out, re.I))
    if n == 0:
        print(out.strip()[-2000:])
        die("pnputil did not install the package on any device (see install.txt). "
            "'up-to-date' means an equal-or-newer driver is already bound: start from a "
            "fresh disposable boot.")
    sess["installed"] = str(src)
    sess["installed_driver"] = ident
    rc, svc = remote.ssh(vm, "sc query vgpusrv")
    state = re.search(r"STATE\s*:\s*\d+\s+(\w+)", svc)
    log(f"driver installed ({n} device(s)); vgpusrv {state[1] if state else 'not present'}")
    fgl = probe("fglock.exe")
    if fgl:
        # the session's foreground lock otherwise denies a fullscreen app the
        # foreground after a mode switch, and DXGI silently stays windowed
        test(vm, str(fgl), [], timeout_s=60)
    print(f"INSTALLED {'guest tools' if released else src.name} ({ident})")
    return ident


def driver_identity(pkg_dir):
    """How comments and notes name a dev build: its commits and build time,
    from the build-info.json build-viogpu-driver writes beside the package."""
    import json
    pkg_dir = Path(pkg_dir)
    for f in (pkg_dir.parent / "build-info.json", pkg_dir / "build-info.json"):
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        rev = lambda v: (v.get("revision") if isinstance(v, dict) else v or "")[:8]  # noqa: E731
        bits = [f"{k} {rev(m[k])}" for k in ("mesa", "kmd") if m.get(k)]
        built = m.get("built_at") or m.get("built")
        return "dev driver" + (f" built {str(built)[:10]}" if built else "") + (f" ({', '.join(bits)})" if bits else "")
    return f"unidentified package {pkg_dir}"


def verify(vm):
    """Device started on the 3D driver, and D3D creates on the VirtIO adapter.

    D3D11CreateDevice(NULL, HARDWARE) can succeed on the Microsoft Basic Render
    Driver (WARP) — S_OK with no work done on our driver.  The probe picks the
    vendor 0x1af4 adapter explicitly."""
    ok = True
    rc, out = remote.ssh(vm, "pnputil /enum-devices /connected /class Display")
    blocks = [b for b in out.split("\n\n") if "VEN_1AF4" in b.upper()]
    for b in blocks:
        desc = re.search(r"Device Description:\s*(.*)", b)
        status = re.search(r"Status:\s*(.*)", b)
        print(f"device: {desc[1].strip() if desc else '?'} — {status[1].strip() if status else '?'}")
        if not status or "Started" not in status[1] or "3D" not in (desc[1] if desc else ""):
            ok = False
    if not blocks:
        print("device: no VirtIO GPU (VEN_1AF4) display device")
        ok = False
    ac = probe("adaptercheck.exe")
    if ac:
        o, rc2, out2 = test(vm, str(ac), [], timeout_s=120)
        if o != Outcome.DONE or rc2 != 0:
            ok = False
    else:
        log("adaptercheck.exe not built (scripts/build-probes.sh); only the PnP state was checked")
    print(f"VERIFY {'PASS' if ok else 'FAIL'}")
    return ok


DUMP_PS1 = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System; using System.IO; using System.Runtime.InteropServices;
public static class UbDump {
  [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint a, bool i, uint pid);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  [DllImport("dbghelp.dll", SetLastError=true)] static extern bool MiniDumpWriteDump(IntPtr h, uint pid,
      Microsoft.Win32.SafeHandles.SafeFileHandle f, uint type, IntPtr e, IntPtr u, IntPtr c);
  public static string Write(uint pid, string path, uint type) {
    IntPtr h = OpenProcess(0x0410 | 0x0040, false, pid);   // QUERY_INFORMATION | VM_READ | DUP_HANDLE
    if (h == IntPtr.Zero) return "OpenProcess failed: error " + Marshal.GetLastWin32Error();
    try {
      using (FileStream fs = new FileStream(path, FileMode.Create, FileAccess.ReadWrite)) {
        if (!MiniDumpWriteDump(h, pid, fs.SafeFileHandle, type, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero))
          return "MiniDumpWriteDump failed: 0x" + Marshal.GetLastWin32Error().ToString("x");
      }
      return "OK";
    } finally { CloseHandle(h); }
  }
}
'@
$p = Get-Process -Name '__IMAGE__' -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -Last 1
if (-not $p) { "DUMP-FAIL no guest process named __IMAGE__"; exit 1 }
$path = "__DIR__\__IMAGE__-$($p.Id)__SUFFIX__.dmp"
$r = [UbDump]::Write([uint32]$p.Id, $path, [uint32]__TYPE__)
if ($r -ne 'OK') { Remove-Item $path -ErrorAction SilentlyContinue; "DUMP-FAIL $r"; exit 1 }
"DUMP-OK $path $((Get-Item $path).Length)"
"""

# MiniDumpWithHandleData | WithUnloadedModules | WithProcessThreadData |
# WithThreadInfo | WithFullMemoryInfo (+ WithFullMemory for --full)
DUMP_NORMAL = 0x4 | 0x20 | 0x100 | 0x1000 | 0x800
DUMP_FULL = DUMP_NORMAL | 0x2


def dump(vm, image, full=False):
    """Minidump a guest process (dbghelp MiniDumpWriteDump) and pull it to the
    run dir.  (comsvcs' MiniDump from cmd over SSH fails silently: it needs
    SeDebugPrivilege enabled, and writes nothing when it is not.)"""
    image = image[:-4] if image.lower().endswith(".exe") else image
    script = (DUMP_PS1.replace("__IMAGE__", image).replace("__DIR__", GW)
              .replace("__TYPE__", hex(DUMP_FULL if full else DUMP_NORMAL))
              .replace("__SUFFIX__", "-full" if full else ""))
    rc, out = remote.ps1(vm, script, timeout=600)
    m = re.search(r"DUMP-OK (\S+) (\d+)", out)
    if not m:
        f = re.search(r"DUMP-FAIL (.*)", out)
        die(f"no dump of {image}: {f[1].strip() if f else out.strip()[-500:]}")
    gpath, size = m[1], int(m[2])
    rd = Session(vm).run_dir()
    local = rd / gpath.split("\\")[-1]
    remote.get(vm, gpath.replace("\\", "/"), local, timeout=1800)
    if not local.exists() or local.stat().st_size != size:
        die(f"the dump was written in the guest ({gpath}, {size} bytes) but did not arrive at {local}")
    remote.ssh(vm, f'del /q "{gpath}" 2>nul & exit 0', timeout=30)
    print(f"DUMP {local} ({size // 1024} KiB{', full memory' if full else ''})")


def ods_start(vm, seconds=1800):
    """Capture OutputDebugString from every process in the console session."""
    remote.put(vm, PLUGIN / "scripts" / "utmbot" / "guestfiles" / "ods.ps1", f"{GWS}/ods.ps1")
    console_task(vm, f"powershell -NoProfile -ExecutionPolicy Bypass -File "
                     f"{GW}\\ods.ps1 -Out {GW}\\ods.log -Seconds {seconds}")
    print(f"ODS listening for {seconds}s -> {GW}\\ods.log (utmbot guest 'type {GW}\\ods.log')")


def cdb_stacks(vm, image):
    """All thread stacks of a guest process with cdb (from the Windows SDK
    debugging tools), as a script file because cmd mangles `;` and `&`."""
    rc, out = remote.ssh(vm, 'where cdb 2>nul || dir /b /s "C:\\Program Files (x86)\\Windows '
                             'Kits\\10\\Debuggers\\arm64\\cdb.exe" 2>nul')
    cdb = next((l.strip() for l in out.splitlines() if l.strip().lower().endswith("cdb.exe")), "")
    if not cdb:
        die("cdb.exe not found in the guest; copy the Debugging Tools for Windows (arm64) "
            "from a dev VM's Windows Kits\\10\\Debuggers\\arm64")
    remote.ssh(vm, f'echo ~*kb 40 > {GW}\\stacks.cdb & echo qd >> {GW}\\stacks.cdb')
    rc, out = remote.ps1(vm, f"(Get-Process '{image}' | Select-Object -First 1).Id")
    pid = out.strip().splitlines()[-1] if out.strip() else ""
    if not pid.isdigit():
        die(f"no guest process named {image}")
    rc, out = remote.ssh(vm, f'"{cdb}" -p {pid} -cf {GW}\\stacks.cdb', timeout=300)
    rd = Session(vm).run_dir()
    (rd / f"stacks-{image}-{pid}.txt").write_text(out)
    print(out)
