"""Ask macOS, at setup time, for every permission the dispatched sessions need,
so the first unattended session does not stop on a dialog nobody sees.

macOS attributes these to the responsible process, which for the agents is
the "UTM Bot Agent" launcher (install.py). So the requests run in a one-shot
LaunchAgent under that launcher, exactly as a session would make them:

  local network   connect to an address on the local network (the VMs on
                  UTM's shared network are reached this way)
  automation      Apple Events to UTM (utmbot drives UTM with AppleScript)
  app data        UTM's preferences in its app container (`defaults` on
                  com.utmapp.UTM: the D3D backend, debug logging). macOS
                  has no prompt for it: the user adds the launcher to Full
                  Disk Access (setup opens the pane and shows the app)
  screen          Screen Recording (window screenshots, frame capture)
  accessibility   post keyboard and mouse events (`utmbot cx input` drives a
                  game in a CrossOver bottle). Its dialog only points to
                  System Settings, so setup opens the Accessibility pane,
                  shows the launcher in Finder, and waits

`request_all()` runs it and prints what is granted. The job itself is
`python3 permissions.py run OUT UTM_APP AEPERM SCREENACCESS LAUNCHER_APP WININPUT`.
"""

import json
import os
import plistlib
import socket
import subprocess
import sys
import time
from pathlib import Path

WAIT = 300  # seconds the user has to answer the dialogs


def _out(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def _bundle_id(app):
    try:
        return plistlib.loads(Path(app, "Contents", "Info.plist").read_bytes())["CFBundleIdentifier"]
    except (OSError, KeyError, ValueError):
        return "com.utmapp.UTM"


def _gateway():
    _, out = _out(["route", "-n", "get", "default"], 10)
    for line in out.splitlines():
        k, _, v = line.strip().partition(":")
        if k == "gateway" and v.strip()[:1].isdigit():
            return v.strip()
    return None


def _local_network(deadline):
    """'granted', 'denied' (still no route when time ran out) or a note."""
    gw = _gateway()
    if not gw:
        return "not checked: no IPv4 default gateway to test against"
    while True:
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect((gw, 80))
            return "granted"
        except OSError as e:
            # "No route to host" to the default gateway is the Local Network
            # denial (or the prompt still open); refused or timed out means
            # the packet went out
            if e.errno != 65:
                return "granted"
        finally:
            s.close()
        if time.time() > deadline:
            return "denied"
        time.sleep(3)


def _automation(app, aeperm, deadline):
    bid = _bundle_id(app)
    rc, out = _out([aeperm, bid], 30)
    if out.endswith("not-running"):
        _, running = _out(["pgrep", "-x", "UTM"], 10)
        if running:
            return "not checked: another UTM.app is running; it is asked the first time"
        _out(["open", "-g", "-j", app], 30)
        for _ in range(20):
            time.sleep(1)
            rc, out = _out([aeperm, bid], 30)
            if not out.endswith("not-running"):
                break
    if out.endswith("consent"):
        rc, out = _out([aeperm, bid, "--ask"], max(30, deadline - time.time()))
    last = out.splitlines()[-1] if out else "?"
    return {"ok": "granted", "denied": "denied"}.get(last, f"not granted ({last})")


def _container_ok(bid):
    """Can this process read and write UTM's preferences file? (`defaults`
    cannot tell: a domain it may not see reads as empty.)"""
    prefs = Path.home() / "Library" / "Containers" / bid / "Data" / "Library" / "Preferences"
    target = prefs / f"{bid}.plist"
    try:
        if target.exists():
            open(target, "r+b").close()  # write access, nothing written
        else:
            os.listdir(prefs)
        return True
    except FileNotFoundError:
        return True  # UTM never ran: nothing to protect yet
    except PermissionError:
        return False


def _app_data(app, launcher, deadline):
    """UTM's container: macOS shows no prompt for it, and its per-app "UTM"
    switch under Files and Folders does not stick, so the grant is Full Disk
    Access. Open that pane, show the launcher in Finder, and wait."""
    bid = _bundle_id(app)
    if _container_ok(bid):
        return "granted"
    _out(["open", "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"], 30)
    _out(["open", "-R", launcher], 30)
    while time.time() < deadline:
        time.sleep(3)
        if _container_ok(bid):
            return "granted"
    return "denied"


def _screen(screenaccess):
    _, out = _out([screenaccess], 60)
    return "granted" if "granted" in out else "asked (takes effect when the agents restart)"


def _accessibility(wininput, launcher, deadline):
    """Accessibility: macOS's prompt (AXIsProcessTrustedWithOptions) only
    lists the app and points to System Settings. Open the pane, show the
    launcher in Finder, and wait for the switch."""
    def trusted():
        return _out([wininput, "trusted"], 30)[0] == 0
    if trusted():
        return "granted"
    _out([wininput, "trusted", "--prompt"], 30)
    _out(["open", "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"], 30)
    _out(["open", "-R", launcher], 30)
    while time.time() < deadline:
        time.sleep(3)
        if trusted():
            return "granted"
    return "denied"


def job(out, app, aeperm, screenaccess, launcher, wininput):
    deadline = time.time() + WAIT
    res = {}

    def put(k, v):
        res[k] = v
        Path(out).write_text(json.dumps(res))

    put("local network", _local_network(deadline))
    put("automation", _automation(app, aeperm, deadline))
    put("app data", _app_data(app, launcher, deadline + 120))
    put("screen", _screen(screenaccess))
    put("accessibility", _accessibility(wininput, launcher, time.time() + WAIT))
    put("done", True)


# ---- the setup side -------------------------------------------------------------------------

HOW = {
    "local network": "System Settings > Privacy & Security > Local Network: UTM Bot Agent",
    "automation": "System Settings > Privacy & Security > Automation: UTM Bot Agent > UTM",
    "app data": "System Settings > Privacy & Security > Full Disk Access: add UTM Bot Agent "
                "(~/.utmapp-bot/agents/UTM Bot Agent.app; again after the launcher is rebuilt)",
    "screen": "System Settings > Privacy & Security > Screen & System Audio Recording: UTM Bot Agent",
    "accessibility": "System Settings > Privacy & Security > Accessibility: add UTM Bot Agent "
                     "(~/.utmapp-bot/agents/UTM Bot Agent.app) and switch it on",
}


def request_all(launcher, python, plugin, utm_app, data, say, warn):
    """Run the requests under the launcher; True when everything is granted."""
    sys.path.insert(0, str(Path(plugin) / "scripts"))
    os.environ.setdefault("UTMAPP_BOT_DATA", str(data))
    from utmbot.capture import tool  # builds the Swift helpers into DATA/bin
    aeperm, screenaccess, wininput = tool("aeperm"), tool("screenaccess"), tool("wininput")
    label = "app.getutm.utmapp-bot.permissions"
    out = Path(data) / "agents" / "permissions.json"
    out.unlink(missing_ok=True)
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label, "RunAtLoad": True, "KeepAlive": False, "ProcessType": "Interactive",
        "ProgramArguments": [str(launcher), str(python), str(Path(__file__).resolve()), "run",
                             str(out), str(utm_app), aeperm, screenaccess,
                             str(Path(launcher).parents[2]), wininput]}))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True)
    if r.returncode != 0:
        plist.unlink(missing_ok=True)
        warn(f"could not start the permission requests: {r.stderr.strip()}")
        return False
    say("asking macOS for the agents' permissions: click Allow on each \"UTM Bot Agent\" dialog "
        f"(Local Network, controlling UTM, Screen Recording); waiting up to {WAIT // 60} min. "
        "If System Settings opens at Full Disk Access with UTM Bot Agent shown in Finder, drag it "
        "into the list and switch it on (UTM's settings; macOS has no dialog for that one). Then "
        "it opens Accessibility the same way: add UTM Bot Agent there too and switch it on "
        "(keyboard and mouse input to CrossOver games)")
    res, shown = {}, set()
    try:
        end = time.time() + 2 * WAIT + 300
        while time.time() < end:
            try:
                res = json.loads(out.read_text())
            except (OSError, ValueError):
                res = {}
            for k, v in res.items():
                if k != "done" and k not in shown:
                    shown.add(k)
                    say(f"  {k:14s} {v}")
            if res.get("done"):
                break
            time.sleep(1)
    finally:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        plist.unlink(missing_ok=True)
    missing = [k for k in HOW if not str(res.get(k, "")).startswith("granted")]
    for k in missing:
        warn(f"{k}: {res.get(k, 'no answer')}. Turn it on in {HOW[k]}, or re-run the setup with --permissions")
    return not missing


if __name__ == "__main__":
    if len(sys.argv) == 8 and sys.argv[1] == "run":
        job(*sys.argv[2:])
    else:
        sys.exit(__doc__)
