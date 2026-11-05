"""Installing an agent's code and its LaunchAgent: the agents run a copy of
the scripts (the plugin directory is replaced on updates), swapped in whole
and only when it changed."""

import hashlib
import os
import plistlib
import shutil
import subprocess
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
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{UID}", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap {path}: {r.stderr.strip()}")
    return "restarted" if loaded else "loaded"


def unload_agent(short):
    path = LA / f"{label(short)}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{UID}/{label(short)}"], capture_output=True)
    path.unlink(missing_ok=True)


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
