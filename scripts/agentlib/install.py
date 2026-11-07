"""Installing an agent's code and its LaunchAgent: the agents run a copy of
the scripts (the plugin directory is replaced on updates), swapped in whole
and only when it changed."""

import hashlib
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

DATA = Path(os.environ.get("UTMAPP_BOT_DATA", "~/.utmapp-bot")).expanduser()
AGENTS = DATA / "agents"
LIB = AGENTS / "lib"
LA = Path.home() / "Library" / "LaunchAgents"
LABEL = "app.getutm.utmapp-bot"
UID = os.getuid()

AGENTLIB = Path(__file__).resolve().parent


def package_files(src):
    """The files of a package directory that agents run (no caches)."""
    return sorted(p.name for p in src.iterdir()
                  if p.is_file() and not p.name.startswith(".") and p.suffix not in (".pyc",))


def install_package(src, name=None, files=None):
    """Copy package directory src to LIB/<name>; True when it changed."""
    name = name or src.name
    files = files or package_files(src)
    dst = LIB / name
    h = hashlib.sha256()
    for f in files:
        h.update(f.encode() + (src / f).read_bytes())
    digest = h.hexdigest()[:16]
    stamp = dst / ".version"
    if stamp.exists() and stamp.read_text().strip() == digest:
        return False
    new = LIB / f"{name}.new-{os.getpid()}"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    for f in files:
        shutil.copy2(src / f, new / f)
    (new / ".version").write_text(digest + "\n")
    old = LIB / f"{name}.old"
    shutil.rmtree(old, ignore_errors=True)
    if dst.exists():
        dst.replace(old)
    new.replace(dst)
    shutil.rmtree(old, ignore_errors=True)
    return True


def install_agentlib():
    return install_package(AGENTLIB, "agentlib")


def label(short):
    return f"{LABEL}.{short}"


def plist(short, args, env, logs, keepalive=False, interval=None, nice=None):
    d = {"Label": label(short), "ProgramArguments": [str(a) for a in args],
         "EnvironmentVariables": env, "RunAtLoad": True,
         "StandardOutPath": str(logs / f"{short}.out.log"),
         "StandardErrorPath": str(logs / f"{short}.err.log"),
         "ProcessType": "Background"}
    if keepalive:
        d["KeepAlive"] = True
        d["ThrottleInterval"] = 30
    if interval:
        d["StartInterval"] = interval
    if nice is not None:
        d["Nice"] = nice
    return d


def load_agent(short, d, restart=False):
    """Write and (re)load a LaunchAgent; returns what changed."""
    LA.mkdir(parents=True, exist_ok=True)
    path = LA / f"{label(short)}.plist"
    data = plistlib.dumps(d)
    same = path.exists() and path.read_bytes() == data
    loaded = subprocess.run(["launchctl", "print", f"gui/{UID}/{label(short)}"],
                            capture_output=True).returncode == 0
    if same and loaded and not restart:
        return "unchanged"
    if loaded:
        subprocess.run(["launchctl", "bootout", f"gui/{UID}/{label(short)}"], capture_output=True)
    path.write_bytes(data)
    for _ in range(10):
        # right after a bootout the old job may still be exiting: "5: Input/output error"
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{UID}", str(path)], capture_output=True, text=True)
        if r.returncode == 0 or not loaded:
            break
        time.sleep(1)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap {path}: {r.stderr.strip()}")
    return "restarted" if loaded else "loaded"


def unload_agent(short):
    path = LA / f"{label(short)}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{UID}/{label(short)}"], capture_output=True)
    path.unlink(missing_ok=True)


# ---- the launcher the session-starting agents run under ----------------------------------
#
# macOS attributes a process's local-network connections (to the VMs on UTM's
# shared network) to its *responsible* process: for a LaunchAgent, the job's
# own program. Under Homebrew's Python as that program the grant did not
# hold (Allow helped only the running process), and an Apple program such as
# /usr/bin/python3 cannot pass one down past a non-Apple child like `claude`.
# So the agents that start sessions run under this small app (ad-hoc
# signed: no certificate needed), which
# posix_spawns the real command and waits: one Allow covers every session,
# and the grant survives restarts and Python upgrades.
#
# macOS matches a blocked process to an app by its executable's build UUID
# through Launch Services. Two binaries built alike share that UUID, and then
# the prompt goes to (or is skipped for) the other app, so the launcher is
# linked with a random UUID and registered with Launch Services before it
# first runs. Ad hoc, the grant was kept across fresh launches when tested on
# macOS 27; a rebuild changes the code hash, so it is rebuilt only when its
# source changes.

LAUNCHER_SRC = AGENTLIB / "launcher"
LAUNCHER_APP = AGENTS / "UTM Bot Agent.app"
LAUNCHER = LAUNCHER_APP / "Contents" / "MacOS" / "utmbot-agent"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")



def install_launcher():
    """Build and sign the launcher app when its source changed; returns
    (path of its executable, what happened). Rebuilding changes its code
    hash, so it is rebuilt only when the source changes."""
    h = hashlib.sha256(b"adhoc,random_uuid")  # the build recipe: a change rebuilds
    for f in ("launcher.c", "Info.plist"):
        h.update(f.encode() + (LAUNCHER_SRC / f).read_bytes())
    digest = h.hexdigest()[:16]
    stamp = AGENTS / ".launcher-version"
    valid = LAUNCHER.exists() and _run(["codesign", "--verify", "--strict", str(LAUNCHER_APP)]).returncode == 0
    if valid and stamp.exists() and stamp.read_text().strip() == digest:
        return LAUNCHER, "unchanged"
    new = AGENTS / f"launcher.new-{os.getpid()}" / LAUNCHER_APP.name
    shutil.rmtree(new.parent, ignore_errors=True)
    (new / "Contents" / "MacOS").mkdir(parents=True)
    shutil.copy2(LAUNCHER_SRC / "Info.plist", new / "Contents" / "Info.plist")
    r = _run(["clang", "-O2", "-arch", "arm64", "-arch", "x86_64", "-Wl,-random_uuid", "-o",
              str(new / "Contents" / "MacOS" / LAUNCHER.name), str(LAUNCHER_SRC / "launcher.c")])
    if r.returncode != 0:
        raise RuntimeError(f"building the agent launcher failed (Xcode command line tools?): {r.stderr.strip()}")
    r = _run(["codesign", "--force", "--sign", "-", str(new)])
    if r.returncode != 0:
        raise RuntimeError(f"signing the agent launcher failed: {r.stderr.strip()}")
    if LAUNCHER_APP.exists():
        shutil.rmtree(LAUNCHER_APP)
    new.replace(LAUNCHER_APP)
    shutil.rmtree(new.parent, ignore_errors=True)
    # known to Launch Services before its first run, or macOS cannot match it
    # to its Local Network entry and denies without asking
    _run([LSREGISTER, "-f", str(LAUNCHER_APP)])
    stamp.write_text(digest + "\n")
    return LAUNCHER, "built"


def launcher_check():
    """'' when the launcher is installed and signed, else what is wrong."""
    if not LAUNCHER.exists():
        return f"{LAUNCHER_APP} is missing (re-run the setup)"
    r = _run(["codesign", "--verify", "--strict", str(LAUNCHER_APP)])
    return "" if r.returncode == 0 else f"{LAUNCHER_APP} has a broken signature: {r.stderr.strip()}"



def agent_path(claude_bin=None, python=None):
    """PATH for an agent: claude, python, Homebrew (gh), the system."""
    return ":".join(dict.fromkeys(
        [str(Path(claude_bin).parent) if claude_bin else "",
         str(Path(python).parent) if python else "", "/opt/homebrew/bin", "/usr/local/bin",
         str(Path.home() / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]).keys()).strip(":")


# ---- checks both setups make -------------------------------------------------------------

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def say(msg):
    print(f"==> {msg}", flush=True)


def warn(msg):
    print(f"WARNING: {msg}", flush=True)


def power_check():
    """Unattended VM work needs the Mac awake and its screen unlocked (a
    locked screen stops the VM window from being composited and lets macOS
    nap UTM, QEMU and the render server). Warn; the user decides."""
    import re
    asleep_ok = re.search(r"SleepDisabled\s+1", _run(["pmset", "-g"]).stdout) is not None
    lk = _run(["sysadminctl", "-screenLock", "status"])
    lock_off = "screenLock is off" in (lk.stdout + lk.stderr)
    ss = _run(["defaults", "-currentHost", "read", "com.apple.screensaver", "idleTime"])
    saver_off = ss.returncode == 0 and ss.stdout.strip() == "0"  # unset: the 20-minute default
    if asleep_ok and lock_off and saver_off:
        say("power: system sleep disabled, screen lock off, no screen saver (as unattended work needs)")
        return
    if not saver_off:
        warn("the screen saver starts when idle, and it locks the screen: System Settings > "
             "Lock Screen > \"Start Screen Saver when inactive\": Never")
    if not asleep_ok:
        warn("system sleep is enabled. Unattended work needs `sudo pmset -a disablesleep 1` "
             "(and keeps working with the lid closed)")
    if not lock_off:
        warn("the screen locks. A locked screen breaks frame capture and timing in the VMs: "
             "System Settings > Lock Screen > \"Require password after screen saver begins or display "
             "is turned off\": Never")


def trust_workdir(path):
    """Mark the sessions' working directory trusted, as accepting Claude
    Code's trust dialog there would: a Remote Control session cannot start in
    an untrusted folder, and nobody is there to answer the dialog."""
    import json
    cfgp = Path.home() / ".claude.json"
    try:
        data = json.loads(cfgp.read_text())
    except (OSError, ValueError):
        warn(f"could not read {cfgp}; run `claude` once in {path} and accept the trust prompt")
        return
    proj = data.setdefault("projects", {}).setdefault(str(path), {})
    if proj.get("hasTrustDialogAccepted"):
        return
    proj["hasTrustDialogAccepted"] = True
    tmp = cfgp.with_suffix(".json.utmapp-bot")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(cfgp)
    say(f"marked {path} trusted for Claude Code (sessions run there)")
