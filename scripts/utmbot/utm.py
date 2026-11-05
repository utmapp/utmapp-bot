"""UTM app selection and the VM lifecycle (vm up / down / status / register).

The UTM driven must be current: ensure_app refuses an older one, whose
scripting lacks what the plugin uses (the start environment, capture screen,
install guest tools, the file of a VM, the debug log and the other
configuration properties the provisioning scripts set).  Builds signed without the VM entitlements (ad hoc)
stay supported: emulated networking and the app group for shared memory make
up for those.
"""

import os
import plistlib
import re
import signal
import subprocess
import time
from pathlib import Path

from . import procs, remote
from .common import (DATA, SCRIPTS, UTM_DOMAIN, UTM_SH, Fail, Session, die, ensure_dir, log,
                     parse_env_file, pid_alive, run, slug)
from .wait import Outcome, until

DIRECTX = {"default": 0, "disabled": 1, "dxmt": 2, "d3dmetal": 3}
DIRECTX_NAME = {v: k for k, v in DIRECTX.items()}


# ---- which UTM.app -------------------------------------------------------

def app_path():
    """$UTM_APP, else the dev build `build-utm.sh` recorded, else the installed app."""
    p = os.environ.get("UTM_APP")
    if not p:
        rec = DATA / "utm-app"
        if rec.exists() and Path(rec.read_text().strip()).exists():
            p = rec.read_text().strip()
    if not p:
        p = "/Applications/UTM.app"
    if not Path(p, "Contents/MacOS/UTM").exists():
        die(f"no UTM.app at {p}; set UTM_APP or build one with build-utm")
    return str(Path(p).resolve())


def app_version(app):
    try:
        info = plistlib.loads(Path(app, "Contents/Info.plist").read_bytes())
        return f"{info.get('CFBundleShortVersionString')} ({info.get('CFBundleVersion')})"
    except Exception:
        return "?"


_VMNET = {}


def app_has_vmnet(app=None):
    """Can this UTM use vmnet (shared, host-only, bridged networks)?  The
    release app can; a build signed without the VM entitlement profiles
    (ad-hoc) cannot, and a VM with a vmnet NIC fails to start in it."""
    app = app or app_path()
    if app not in _VMNET:
        _VMNET[app] = run(["bash", str(UTM_SH), "vmnet", app])[1].strip() == "yes"
    return _VMNET[app]


def bundle_id(app):
    try:
        return plistlib.loads(Path(app, "Contents/Info.plist").read_bytes())["CFBundleIdentifier"]
    except Exception:
        return "com.utmapp.UTM"


def automation_check(app):
    """Stop with what to do when macOS would have to ask the user before this
    terminal may script UTM (the first time it does).  On a locked screen nobody
    sees the prompt, and each AppleScript call would wait for minutes."""
    from .capture import tool
    rc, out = run([tool("aeperm"), bundle_id(app)], timeout=30)
    state = out.strip().splitlines()[-1] if out.strip() else "?"
    if state == "consent":
        # show the prompt now, so it is waiting when the user looks
        subprocess.Popen([tool("aeperm"), bundle_id(app), "--ask"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        die(f"macOS needs the user's permission before this terminal may control {app} "
            "(Automation). Ask the user to unlock the Mac and click Allow in the prompt "
            "(or turn it on in System Settings > Privacy & Security > Automation), then "
            "retry.")
    if state == "denied":
        die(f"this terminal is not allowed to control {app}: turn it on in System Settings > "
            "Privacy & Security > Automation")


def osa_run(verb, *args, timeout=120):
    """utm.applescript VERB ARGS: (rc, output); rc 124 when osascript did not
    return within `timeout` (run's own limit, not an AppleEvent timeout)."""
    env = dict(os.environ, UTM_APP=app_path())
    rc, out = run(["osascript", str(SCRIPTS / "provision" / "utm.applescript"), verb,
                   *map(str, args)], timeout=timeout, env=env)
    return rc, out.strip()


def apple_event_timeout(out):
    return "-1712" in out or "timed out" in out


def osa(verb, *args, timeout=120):
    rc, out = osa_run(verb, *args, timeout=timeout)
    if rc == 124:
        die(f"osascript '{verb}' did not return within {timeout}s ({out[-300:] or 'no output'})")
    if rc != 0:
        if apple_event_timeout(out):
            die(f"UTM did not answer '{verb}' (AppleEvent timed out). UTM may be waiting on "
                "a prompt nobody can see (Automation permission for a new build, a "
                "folder-access prompt on a locked screen) or its main thread is blocked; "
                "run `utmbot app` and look at the Mac.")
        die(f"UTM '{verb}' failed: {out}")
    return out


def vm_file(name):
    """A VM's .utm bundle.  While the selected UTM runs, its `file` of the VM;
    otherwise UTM's own registry of VMs in its preferences, so that nothing
    launches UTM for this (`vm down` after UTM crashed must not start it
    again).  None when neither knows the VM."""
    app = app_path()
    if any(a and str(Path(a).resolve()) == app for _, a in running_apps()):
        try:
            p = osa("file", name, timeout=20)
        except Fail:
            return None  # no VM of that name, or UTM did not answer
        return Path(p.rstrip("/")) if p else None
    return registry_file(name)


def registry_file(name):
    rc, out = run(["defaults", "export", "com.utmapp.UTM", "-"], timeout=20)
    if rc != 0:
        return None
    try:
        reg = plistlib.loads(out.encode()).get("Registry", {})
    except (plistlib.InvalidFileException, ValueError):
        return None
    p = next((e.get("Package", {}).get("Path") for e in reg.values()
              if isinstance(e, dict) and e.get("Name") == name), None)
    return Path(p.rstrip("/")) if p else None


def running_apps():
    return [(r["pid"], procs.utm_app_of(r)) for r in procs.classify()["utm"]]


_MISSING = {}


def scripting_missing(app=None):
    """The scripting terms the plugin uses that this app's dictionary lacks:
    none on a current UTM (utm.sh keeps the list, for the provisioning scripts
    too)."""
    app = app or app_path()
    if app not in _MISSING:
        rc, out = run(["bash", "-c", '. "$1"; app_scripting_missing "$2"', "utm.sh", str(UTM_SH), app],
                      timeout=60)
        if rc != 0:
            die(f"could not read the scripting dictionary of {app}: {out.strip()[-300:]}")
        _MISSING[app] = [l.strip() for l in out.splitlines() if l.strip()]
    return _MISSING[app]


def require_automation(app):
    missing = scripting_missing(app)
    if missing:
        die(f"{app} ({app_version(app)}) is too old for this plugin: its scripting lacks "
            f"{', '.join(missing)}. Update UTM")


def ensure_app():
    """Exactly one UTM, the selected one, current,
    running and answering it.

    The release app and a dev build share a bundle id: with both running,
    AppleScript goes to whichever the system picks, and a stale binary
    silently ignores scripting parameters it does not know."""
    app = app_path()
    require_automation(app)
    others = [(pid, a) for pid, a in running_apps() if a and str(Path(a).resolve()) != app]
    if others:
        die("another UTM is running (" + ", ".join(f"{a} pid {p}" for p, a in others) +
            f"); quit it first so scripting reaches {app}")
    if not any(a for _, a in running_apps()):
        log(f"launching {app}")
        # `open`, never a backgrounded exec: a shell's background-nice setting
        # would be inherited by UTM and every VM process.  Never by an
        # AppleScript `tell` either: a UTM launched that way was seen to never
        # start a VM.
        run(["open", app], check=True)
        for _ in range(30):
            if any(a for _, a in running_apps()):
                break
            time.sleep(1)
    automation_check(app)
    for _ in range(30):
        try:
            osa("exists", "__utmbot_probe__", timeout=20)
            return app
        except Fail:
            time.sleep(2)
    die(f"{app} does not answer AppleScript")


def quit_app():
    app = app_path()
    for pid, a in running_apps():
        if a and str(Path(a).resolve()) == app:
            # UTM does not quit while VMs run: say which, instead of timing out
            vms = [re.search(r"-name\s+(.+?)\s+-", q["cmd"]) for q in procs.classify()["qemu"]]
            names = [m[1] for m in vms if m] or ["(unnamed)"] if vms else []
            if names:
                die(f"cannot quit {app}: VMs still running in it: {', '.join(names)}; stop them "
                    "first (the release and dev apps cannot run side by side)")
            run(["osascript", "-e", f'tell application "{app}" to quit'], timeout=60)
            for _ in range(30):
                if not pid_alive(pid):
                    return
                time.sleep(1)
            die("UTM did not quit (a VM still running?)")


# ---- defaults: DirectX backend -------------------------------------------

def get_directx():
    rc, out = run(["defaults", "read", UTM_DOMAIN, "QEMUDirectXDriver"])
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else None


def set_directx(value):
    if value is None:
        run(["defaults", "delete", UTM_DOMAIN, "QEMUDirectXDriver"])
    else:
        run(["defaults", "write", UTM_DOMAIN, "QEMUDirectXDriver", "-int", str(value)],
            check=True)


def check_backend_available(app, backend):
    """D3DMetal runs in an x86_64 render server under Rosetta.  An app built
    from an arm64-only sysroot embeds D3DMetal.framework but has no x86_64
    render server, and UTM's default then still picks D3DMetal: every D3D app
    fails in the guest without an error on the host."""
    fw = Path(app, "Contents/Frameworks/D3DMetal.framework")
    if backend not in ("d3dmetal", "default") or (backend == "default" and not fw.exists()):
        return
    rs = next(Path(app, "Contents").glob("XPCServices/*/Contents/MacOS/QEMURenderServer.app/"
                                         "Contents/MacOS/QEMURenderServer"), None)
    rc, archs = run(["lipo", "-archs", str(rs)]) if rs else (1, "")
    if "x86_64" not in archs:
        die(f"{app} has no x86_64 render server, so it has no D3DMetal backend, but backend "
            f"'{backend}' would use D3DMetal; pass --backend dxmt, or build UTM from a "
            "universal sysroot (build-utm)")


# ---- the VM's configuration ------------------------------------------------

def config_path(vm):
    b = vm.bundle()
    if not b:
        die(f"UTM has no VM named '{vm.name}' (or the selected UTM does not run)")
    return b / "config.plist"


def read_config(vm):
    return plistlib.loads(config_path(vm).read_bytes())


def set_debug_log(vm, on):
    """Turn the VM's Debug Log on or off (its "debug log" configuration
    property, saved through scripting; the VM must be stopped)."""
    cfg = read_config(vm)
    if bool(cfg.get("QEMU", {}).get("DebugLog")) == on:
        return
    for _ in range(20):  # a VM that was just stopped can take a moment to exit
        if not procs.vm_tree(vm.name)["qemu"]:
            break
        time.sleep(1)
    else:
        die(f"turning Debug Log {'on' if on else 'off'} for '{vm.name}' needs it stopped; "
            "stop it or pass --debug-log as-is")
    log(f"turning Debug Log {'on' if on else 'off'}")
    osa("debug-log", vm.name, "true" if on else "false")


# ---- debug.log --------------------------------------------------------------

def current_log_text(vm):
    """debug.log from the most recent 'Launching:' header on.

    Empty when the file predates this VM lifetime: with Debug Log off the
    previous lifetime's log stays behind and must not be read as this one's."""
    p = vm.debug_log()
    if not p or not p.exists():
        return ""
    started = Session(vm).get("started")
    if started and p.stat().st_mtime < started - 5:
        return ""
    t = p.read_text(errors="replace")
    i = t.rfind("Launching: qemu-system")
    return t[i:] if i >= 0 else t


def header_env(text):
    m = re.search(r"Environment variables:\n((?:    .*\n)+)", text)
    env = {}
    if m:
        for line in m[1].splitlines():
            k, _, v = line.strip().partition("=")
            env[k] = v
    return env


# ---- networking without vmnet -------------------------------------------------

SSH_PORT_BASE = 22300


def restore_network(vm, sess):
    """Undo emulated_network() (at vm down, or at the next vm up when the
    harness died before it)."""
    prev = sess.get("net_prev")
    if not prev:
        return
    log(f"restoring the network modes of '{vm.name}' ({' '.join(prev['modes'])})")
    osa("net-restore", vm.name, prev["port"], *prev["modes"])
    for k in ("net_prev", "ssh_port"):
        sess.data.pop(k, None)
    sess.save()


def emulated_network(vm, sess, app):
    """A UTM without the vmnet entitlement cannot start a VM with a shared,
    host-only or bridged NIC.  For this VM lifetime every NIC becomes emulated
    (QEMU's user network, no entitlement needed), with SSH forwarded from
    127.0.0.1; vm down puts the modes back.  The guest still reaches the
    internet, and the host at 10.0.2.2; guests cannot reach each other, so
    kdnet kernel debugging does not work."""
    if app_has_vmnet(app):
        return
    modes = osa("net-modes", vm.name).split()
    if all(m == "emulated" for m in modes):
        if not vm.ssh_port():
            die(f"'{vm.name}' has emulated networking but the plugin knows no SSH port "
                "forward for it; register it again (utmbot vm register) after adding a "
                "TCP forward to guest port 22 in UTM")
        return
    port = int(run(["bash", str(UTM_SH), "free-port", str(SSH_PORT_BASE)])[1].strip())
    prev = osa("net-emulated", vm.name, port).split()
    sess["net_prev"] = {"port": port, "modes": prev}
    sess["ssh_port"] = port
    log(f"{app} has no vmnet entitlement: '{vm.name}' runs with emulated networking for this "
        f"boot (SSH through 127.0.0.1:{port}; no guest-to-guest network, so no kdnet); "
        "`vm down` restores its network modes")


def unsigned_shmem_group(app, env):
    """An unsigned UTM marks its app group invalid and so passes no
    APP_SANDBOX_GROUP_ID; virglrenderer's shm_open is then denied by the
    sandbox, blob resources fail and the guest has no 3D at all.  Its
    entitlements do claim the group without a team prefix, which works.
    Pass it here (the same value, so harmless where UTM passes it)."""
    group = run(["bash", str(UTM_SH), "shmem-group", app])[1].strip()
    if group:
        env.setdefault("APP_SANDBOX_GROUP_ID", group)


# ---- lifecycle --------------------------------------------------------------

GUEST_TOOLS_TIMEOUT = 1800   # utm.applescript gives UTM 1700 s, for a download
CAFFEINATE_HOURS = 4         # bounded: it must not outlive a forgotten session


def attach_guest_tools(vm):
    """The guest tools in the VM's empty removable drive (D: in Windows), by
    UTM's own Install Windows Guest Tools: UTM decides where they come from,
    downloads them when its copy is missing or older than the release (one
    HEAD request when it is current), and does nothing when they are already
    in the drive.  `install --released` takes the driver from that drive.
    Returns what happened, for the session (`install --released` quotes it)."""
    rc, out = osa_run("guest-tools", vm.name, timeout=GUEST_TOOLS_TIMEOUT)
    if rc == 0:
        return "attached"
    if rc == 124:
        why = f"UTM's install guest tools did not return within {GUEST_TOOLS_TIMEOUT // 60} min"
    elif apple_event_timeout(out):
        why = ("UTM's install guest tools did not finish within the script's limit (a slow "
               "download of the guest tools?)")
    else:
        why = f"UTM could not put them in a drive: {out[-300:]}"
    log(f"guest tools not attached: {why}; `install --released` will not find them")
    return f"not attached: {why}"


def vm_up(vm, backend=None, env=None, debug_log=True, tag="", killwatch=True, nodebug=False,
          wait_min=6, require_vmnet=False, writable=False):
    from . import host, lease
    lease.acquire(purpose=f"vm up {tag}".strip(), vm=vm.name)
    if lease._crossover_running():
        die("a CrossOver bottle is running, and it shares the GPU (and the Steam account) "
            "with the VM: `utmbot cx stop --bottle <name>` first (`utmbot cx ps` lists them)")
    if writable and nodebug:
        die("--nodebug only in a disposable boot: it would remove kernel debugging from "
            "the disk image for good")
    app = ensure_app()
    sess = Session(vm)
    tree = procs.vm_tree(vm.name)
    if tree["qemu"]:
        die(f"'{vm.name}' is already running (pid {tree['qemu']['pid']}); `vm down` first. "
            "Always kill and relaunch; reinstalling into a VM that already ran the "
            "driver is not a clean test.")
    # refuse before changing anything
    if require_vmnet and not app_has_vmnet(app):
        die(f"{app} has no vmnet entitlement (com.apple.vm.networking), which --require-vmnet "
            "asks for. Use a UTM signed with the VM entitlements (the release app, or a build "
            "with the VM entitlement profiles; see build-utm)")
    check_backend_available(app, backend or DIRECTX_NAME.get(get_directx() or 0, "default"))

    restore_network(vm, sess)
    if debug_log is not None:
        set_debug_log(vm, debug_log)
    # a writable boot never gets a driver under test (`install` refuses there)
    guest_tools = attach_guest_tools(vm) if vm.is_windows and not writable else None

    run_dir = sess.new_run(tag)
    if guest_tools:
        sess["guest_tools"] = guest_tools
    sess["app"] = app
    sess["app_version"] = app_version(app)
    prev = get_directx()
    sess["directx_prev"] = prev
    if backend:
        set_directx(DIRECTX[backend])
    sess["backend_requested"] = backend or DIRECTX_NAME.get(prev or 0, "default")
    emulated_network(vm, sess, app)

    # No VIRGL_LOG_FILE: the render server and its workers inherit QEMU's
    # stderr, which the Debug Log captures.  A log file would have to live in
    # UTM's group container (the only place the sandboxed render server can
    # write), which macOS does not let other apps read.
    env = dict(env or {})
    if "VIRGL_LOG_FILE" in env:
        log("note: with VIRGL_LOG_FILE the render server's log leaves debug.log; a file "
            "in UTM's group container cannot be read from the terminal")
    unsigned_shmem_group(app, env)
    sess["env"] = env

    if writable:
        # Only to change the disk image on purpose (install software, then
        # save a snapshot); never to test a driver.  The disk is dirty from
        # here until a snapshot is saved or restored.
        sess["writable"] = True
        log(f"starting '{vm.name}' WRITABLE (disk changes are kept) on {app} "
            f"({sess['app_version']}), backend {sess['backend_requested']}, run dir {run_dir}")
        osa("start", vm.name, *[f"{k}={v}" for k, v in env.items()])
    else:
        log(f"starting '{vm.name}' disposable on {app} ({sess['app_version']}), "
            f"backend {sess['backend_requested']}, run dir {run_dir}")
        osa("start-disposable", vm.name, *[f"{k}={v}" for k, v in env.items()])

    def qemu_up():
        t = procs.vm_tree(vm.name)
        return bool(t["qemu"]), "QEMU process " + ("up" if t["qemu"] else "not yet")
    o, _ = until(qemu_up, 90, "QEMU start", every=15)
    if o != Outcome.DONE:
        die(f"QEMU for '{vm.name}' never started; UTM status: {osa('status', vm.name)}")
    tree = procs.vm_tree(vm.name)
    q = tree["qemu"]
    sess["qemu_pid"] = q["pid"]
    if writable:
        if "-snapshot" in q["cmd"]:
            osa("stop", vm.name)
            die("asked for a writable boot but QEMU runs with -snapshot; stopped it")
        from . import snap
        snap.set_disk(vm, "dirty")  # from now on the disk may differ from every snapshot
    elif "-snapshot" not in q["cmd"]:
        osa("stop", vm.name)
        die("QEMU is not running in disposable mode (-snapshot missing); stopped it. "
            "Every test boot must be disposable so a killed VM never corrupts the disk.")
    (run_dir / "qemu-cmdline.txt").write_text(q["cmd"] + "\n")

    if killwatch:
        host.killwatch_arm(vm)
    # A sleeping display stops the VM window drawing (captures freeze).  The
    # unattended Mac has sleep and the screen lock off for good (docs/gamesdb.md,
    # "This Mac"); this covers an interactive Mac for one VM lifetime.  All stdio
    # detached: an inherited pipe would keep a caller's `| tail` waiting for hours.
    p = subprocess.Popen(["caffeinate", "-d", "-i", "-t", str(CAFFEINATE_HOURS * 3600)],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sess["caffeinate_pid"] = p.pid

    remote.close(vm)

    def ssh_up():
        if not vm.host(refresh=True):
            return False, "no DHCP lease yet"
        return remote.reachable(vm), f"ssh {vm.ssh_dest()}"
    o, obs = until(ssh_up, wait_min * 60, "guest SSH", every=30, vm=vm, interval=5)
    if o != Outcome.DONE:
        die(f"guest never answered SSH ({o}: {obs}); take a screenshot (utmbot shot) "
            "to see where boot stopped")
    sess["boot_id"] = remote.boot_id(vm)
    prepare_guest(vm)
    if nodebug:
        disable_kernel_debugger(vm)
    bring_to_front()
    host.check_host(vm, quiet=False)
    print(f"UP {vm.name} ssh={vm.ssh_dest().replace(' port ', ':')} run_dir={run_dir} "
          f"backend={sess['backend_requested']} "
          f"net={'emulated' if sess.get('net_prev') else 'as-configured'}"
          f"{' WRITABLE' if writable else ''}")


def vm_shutdown(vm, timeout=420):
    """Shut the guest down cleanly and wait for QEMU to exit: the only way to
    end a writable boot whose disk is to be kept.  Then the same cleanup as
    `vm down`."""
    from . import host
    tree = procs.vm_tree(vm.name)
    if not tree["qemu"]:
        log(f"'{vm.name}' is not running")
        vm_down(vm)
        return
    host.killwatch_disarm(vm)
    log(f"shutting down the guest of '{vm.name}'")
    if vm.is_windows:
        remote.ssh(vm, "shutdown /s /t 0 /f", timeout=60)
    else:
        remote.ssh(vm, "sudo -n poweroff || systemctl poweroff", timeout=60)
    remote.close(vm)

    def qemu_gone():
        t = procs.vm_tree(vm.name)
        return not t["qemu"], "QEMU " + ("exited" if not t["qemu"] else "still running")
    o, obs = until(qemu_gone, timeout, "guest shutdown", every=30)
    if o != Outcome.DONE:
        die(f"the guest did not power off within {timeout}s ({obs}). Look at the VM window "
            "(utmbot shot); a force stop now may lose the writes this boot was for.")
    vm_down(vm)


def prepare_guest(vm):
    """Things every test boot needs (all discarded with the disposable boot)."""
    if vm.is_windows:
        # display off => the guest stops presenting and captures freeze;
        # conhost instead of Windows Terminal: Terminal renders through D3D, so
        # a broken driver hangs every console task
        remote.ssh(vm, "powercfg /change monitor-timeout-ac 0 & powercfg /change monitor-timeout-dc 0 & "
                       "powercfg /change standby-timeout-dc 0 & powercfg /change "
                       "standby-timeout-ac 0 & "
                       'reg add "HKCU\\Console\\%%Startup" /v DelegationConsole /t REG_SZ '
                       "/d {B23D10C0-E52E-411E-9D5B-C09FDF709C7D} /f & "
                       'reg add "HKCU\\Console\\%%Startup" /v DelegationTerminal /t REG_SZ '
                       "/d {B23D10C0-E52E-411E-9D5B-C09FDF709C7D} /f & "
                       "if not exist C:\\utmbot mkdir C:\\utmbot", timeout=60)
    else:
        remote.ssh(vm, "XDG_RUNTIME_DIR=/run/user/$(id -u) gsettings set "
                       "org.gnome.desktop.session idle-delay 0 2>/dev/null; mkdir -p ~/utmbot",
                   timeout=30)


def disable_kernel_debugger(vm):
    """bcdedit /debug off + one warm reboot, for titles whose anti-tamper raises
    exceptions when a kernel debugger is enabled.  Only ever in a disposable
    boot: the change is discarded when the VM stops."""
    if not vm.is_windows:
        return
    tree = procs.vm_tree(vm.name)
    if not tree["qemu"] or "-snapshot" not in tree["qemu"]["cmd"]:
        die("refusing --nodebug on a non-disposable VM: it would remove kernel debugging "
            "from the disk image for good")
    old = remote.boot_id(vm)
    remote.ssh(vm, "bcdedit /debug off", check=True)
    log("kernel debugger off; restarting the guest once")
    remote.ssh(vm, "shutdown /r /t 0")
    remote.close(vm)

    # sshd keeps answering for a while after shutdown /r returns: only a new
    # boot id proves the restart happened
    def new_boot():
        if not remote.reachable(vm):
            return False, "guest down"
        b = remote.boot_id(vm)
        return bool(b and b != old), f"boot id {b}"
    o, obs = until(new_boot, 300, "guest restart", every=30, vm=vm, interval=5)
    if o != Outcome.DONE:
        die(f"guest did not come back from the --nodebug restart ({obs})")
    Session(vm)["boot_id"] = remote.boot_id(vm)
    prepare_guest(vm)


def bring_to_front():
    """A background (napped) VM runs its vCPUs at low priority: measured ~4x slower."""
    app = app_path()
    run(["osascript", "-e", f'tell application "{app}" to activate'], timeout=20)


def vm_down(vm, force=False):
    from . import host
    sess = Session(vm)
    tree = procs.vm_tree(vm.name)
    if tree["qemu"] and sess.get("writable") and not force:
        die(f"'{vm.name}' runs a WRITABLE boot: a force stop can leave its disk half-written. "
            "Use `vm shutdown`; pass --force to stop it anyway (then restore a snapshot before "
            "trusting the disk).")
    host.killwatch_disarm(vm)
    if tree["qemu"]:
        try:
            osa("stop", vm.name, timeout=60)
        except Fail as e:
            log(f"stop by force failed ({e}); killing")
    gone = False
    for i in range(30):
        tree = procs.vm_tree(vm.name)
        if not tree["qemu"]:
            gone = True
            break
        if i == 10:
            try:
                osa("kill", vm.name, timeout=30)
            except Fail:
                pass
        if i == 20:
            for p in [tree["qemu"]] + tree["servers"] + tree["workers"]:
                os.kill(p["pid"], signal.SIGKILL)
        time.sleep(1)
    if not gone:
        log("WARNING: QEMU is still running after force stop, kill and SIGKILL")
    # render servers orphaned by a dead QEMU keep GPU queues alive
    cl = procs.classify()
    for p in cl["server"] + cl["worker"]:
        if p["ppid"] == 1 or (sess.get("qemu_pid") and p["ppid"] == sess.get("qemu_pid")):
            log(f"killing orphaned render process {p['pid']}")
            try:
                os.kill(p["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(1)
    stuck = [p for p in procs.ps() if "QEMU" in p["cmd"] and p["stat"].startswith("U")]
    if stuck:
        log("WARNING: processes stuck in uninterruptible state (a GPU hang can still take "
            "the host down; do not start another VM until they exit): " +
            ", ".join(str(p["pid"]) for p in stuck))
    if "directx_prev" in sess.data:
        set_directx(sess.get("directx_prev"))
    cp = sess.get("caffeinate_pid")
    if cp and pid_alive(cp):
        os.kill(cp, signal.SIGTERM)
    # keep the logs of this lifetime with the run
    rd = sess.run_dir()
    text = current_log_text(vm)
    if text:
        (rd / "debug.log").write_text(text)
    remote.close(vm)
    if gone:
        restore_network(vm, sess)
    sess.clear()
    if gone:
        from . import lease
        lease.release(implicit_only=True)
    print(f"DOWN {vm.name} logs={rd}")


def vm_status(vm):
    from . import host
    sess = Session(vm)
    tree = procs.vm_tree(vm.name)
    print(f"vm        {vm.name} ({vm.os}, role {vm.role})")
    print(f"app       {sess.get('app') or app_path()}")
    try:
        print(f"utm       {osa('status', vm.name, timeout=20)}")
    except Fail as e:
        print(f"utm       ? ({e})")
    q = tree["qemu"]
    if q:
        print(f"qemu      pid {q['pid']} cpu {q['cpu']}% rss {q['rss_mb']} MB "
              f"{'disposable' if '-snapshot' in q['cmd'] else 'NOT DISPOSABLE'}")
        for s in tree["servers"]:
            print(f"server    pid {s['pid']}")
        for w in tree["workers"]:
            print(f"worker    pid {w['pid']} ctx {w['ctx_id']} {w['ctx_name']} {w['arch']} "
                  f"cpu {w['cpu']}% stat {w['stat']}")
    else:
        print("qemu      not running")
    print(f"ssh       {vm.ssh_dest()}" + (" (emulated network for this boot)"
                                              if sess.get("net_prev") else ""))
    print(f"ssh up    {'yes' if q and remote.reachable(vm) else 'no'}")
    print(f"run dir   {sess.get('run_dir', '-')}")
    if sess.get("guest_tools"):
        print(f"guest tools {sess['guest_tools']}")
    print(f"killwatch {host.killwatch_state(vm)}")


def vm_register(name, user, os_kind, key=None):
    """Make an existing UTM VM usable by the harness (e.g. a Linux guest)."""
    sd = ensure_dir(DATA / "provision" / slug(name))
    if (sd / "state.env").exists():
        st = parse_env_file(sd / "state.env")
        if st.get("S_ROLE") in ("dev", "test") and st.get("S_OS", "windows") == "windows" \
                and os_kind == "windows":
            die(f"'{name}' was provisioned by the plugin already")
    ensure_app()
    bundle = vm_file(name)
    if not bundle:
        die(f"UTM has no VM named '{name}'")
    cfg = plistlib.loads((bundle / "config.plist").read_bytes())
    nets = cfg.get("Network") or []
    if not nets or not nets[0].get("MacAddress"):
        die("the VM has no network interface with a MAC address")
    # an emulated first NIC is reached through its TCP forward to port 22
    port = ""
    if nets[0].get("Mode") == "Emulated":
        port = next((str(f["HostPort"]) for f in nets[0].get("PortForward") or []
                     if f.get("GuestPort") == 22 and f.get("Protocol", "TCP") == "TCP"), "")
        if not port:
            die("the VM's first network interface is emulated without a TCP port forward to "
                "guest port 22; add one in UTM (host address 127.0.0.1) and register again")
    # without --key, ssh uses its own configuration and agent
    import shlex
    (sd / "state.env").write_text(
        f"NAME={shlex.quote(name)}\nS_ROLE=test\nS_OS={os_kind}\nS_USER={shlex.quote(user)}\n"
        f"S_PUBKEY={shlex.quote(key or '')}\nMAC={nets[0]['MacAddress']}\n"
        + (f"SSH_PORT={port}\n" if port else ""))
    print(f"registered '{name}' ({os_kind}, user {user}, MAC {nets[0]['MacAddress']}"
          + (f", SSH through 127.0.0.1:{port}" if port else "") + ")")
