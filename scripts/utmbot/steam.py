"""Steam in a Windows test VM, without clicks: the client's own UI API.

Steam's UI is Chromium (CEF).  With an empty `.cef-enable-remote-debugging`
file in the Steam directory, CEF serves the Chrome DevTools Protocol on the
guest's 127.0.0.1:8080, and the hidden `SharedJSContext` page holds the
`SteamClient` object the UI itself calls (install, run, settings, ...).  The
file survives Steam's self-restarts and autostart, which the
-cef-enable-debugging flag does not.  This is how Decky Loader and Millennium
reach the client; Valve does not document it, so `steam js` exists to look
at the live API when a method moves.

The host reaches 8080 through an SSH forward on the VM's shared connection;
the protocol is spoken with a minimal stdlib websocket client.  The account
logs in once, by hand, in the VM window (db-steam-load); no credential ever
reaches the host.

The same client runs in a CrossOver bottle on the Mac (crossover.py).  There
the "VM" argument is a crossover.Bottle (`cx_bottle` is true): Wine's
networking is the host's, so DevTools is on the Mac's own 127.0.0.1:8080,
and the few places below that reach the guest over SSH (starting and
stopping the client, finding the game's processes) ask the bottle instead.
Everything spoken over DevTools is shared.
"""

import base64
import json
import os
import random
import re
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import remote
from .common import DATA, Fail, die, ensure_dir, log, run
from .wait import Outcome, until

SETUP_URL = "https://cdn.akamai.steamstatic.com/client/installer/SteamSetup.exe"
CDP_GUEST_PORT = 8080


# ---- guest paths -------------------------------------------------------------

def _bottle(vm):
    return bool(getattr(vm, "cx_bottle", False))


def _run_dir(vm):
    from .common import Session
    return vm.run_dir() if _bottle(vm) else Session(vm).run_dir()


def steam_dir(vm, required=True):
    if _bottle(vm):
        return vm.steam_win(required)
    rc, out = remote.ps1(vm, r"""
$p = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
if (-not $p) { $p = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam' -ErrorAction SilentlyContinue).InstallPath }
if (-not $p) { $p = (Get-ItemProperty 'HKLM:\SOFTWARE\Valve\Steam' -ErrorAction SilentlyContinue).InstallPath }
if ($p -and (Test-Path (Join-Path $p 'steam.exe'))) { "STEAMDIR=" + (Resolve-Path $p).Path }
""", timeout=60)
    m = re.search(r"STEAMDIR=(.+)", out)
    if not m:
        if required:
            die("Steam is not installed in the guest (`utmbot steam setup`)")
        return None
    return m[1].strip()


def running(vm):
    if _bottle(vm):
        return vm.steam_running()
    rc, out = remote.ssh(vm, 'tasklist /fi "imagename eq steam.exe" /nh', timeout=30)
    return "steam.exe" in out.lower()


# ---- install and start -------------------------------------------------------------

def setup(vm):
    """Install the client silently and turn on its DevTools endpoint."""
    if _bottle(vm):
        die("a CrossOver bottle gets Steam from CrossOver (its Steam install); "
            "`utmbot cx steam start` turns DevTools on")
    d = steam_dir(vm, required=False)
    if not d:
        cache = ensure_dir(DATA / "cache" / "downloads")
        exe = cache / "SteamSetup.exe"
        if not exe.exists() or time.time() - exe.stat().st_mtime > 7 * 86400:
            log(f"downloading {SETUP_URL}")
            rc, out = run(["curl", "-sSfL", "-o", str(exe) + ".part", SETUP_URL], timeout=600)
            if rc != 0:
                die(f"download failed: {out[-300:]}")
            os.replace(str(exe) + ".part", exe)
        remote.ssh(vm, "if not exist C:\\utmbot mkdir C:\\utmbot", timeout=30)
        remote.put(vm, exe, "C:/utmbot/SteamSetup.exe")
        log("installing Steam (silent)")
        rc, out = remote.ssh(vm, "C:\\utmbot\\SteamSetup.exe /S", timeout=900)
        d = steam_dir(vm, required=False)
        if not d:
            die(f"SteamSetup.exe /S finished ({rc}) but no steam.exe was found: {out[-400:]}")
    remote.ps1(vm, f"New-Item -ItemType File -Force -Path '{d}\\.cef-enable-remote-debugging' | Out-Null",
               timeout=60, check=True)
    print(f"STEAM {d} (DevTools enabled)")
    return d


def start(vm, wait_s=600, extra=()):
    """Start the client in the console session (where it has a window) and
    wait for its DevTools endpoint."""
    d = steam_dir(vm)
    if not running(vm):
        if _bottle(vm):
            vm.launch_steam(["-cef-enable-debugging", *extra])
        else:
            from . import guest
            args = " ".join(["-cef-enable-debugging", *extra])
            # `start` returns at once, so the task does not wait for Steam
            guest.console_task(vm, f'start "" "{d}\\steam.exe" {args}', out_name="steam-start.txt",
                               hidden=True)
        log("Steam started; waiting for its UI (it updates itself on the first start)")
    else:
        log("Steam is already running")

    def ready():
        try:
            t = shared_target(vm)
        except Fail as e:
            return False, str(e)[:100]
        return bool(t), "SharedJSContext " + ("up" if t else "not yet")
    o, obs = until(ready, wait_s, "Steam UI", every=30, vm=vm, interval=5)
    if o != Outcome.DONE:
        die(f"Steam's DevTools endpoint never came up ({o}: {obs}). Look at the VM window "
            "(utmbot shot): an update or a first-run prompt may be waiting.")

    # a remembered account signs itself in shortly after the UI is up, and the
    # client reloads its UI once while doing so.  The sign-in window shows for
    # a moment during that automatic sign-in too (seen in a CrossOver bottle:
    # signed in 1 s after it appeared), so only one that stays means signed out.
    seen = {}

    def settled():
        st = status(vm)
        if st["signed_in"] and st["init_after_login"]:
            return True, f"signed in as {st['account']}"
        if any(t.get("title") == "Sign in to Steam" for t in targets(vm)):
            seen.setdefault("t", time.time())
            if time.time() - seen["t"] > 45:
                return True, "sign-in window"
            return False, "sign-in window (an automatic sign-in may follow)"
        seen.pop("t", None)
        return st["signed_in"] and st["init_after_login"], (
            f"signed in as {st['account']}" if st["signed_in"] else "signing in")
    o, obs = until(settled, 180, "Steam sign-in", every=60, vm=vm, interval=5)
    st = status(vm)
    if st["signed_in"]:
        time.sleep(5)  # past the UI reload that follows sign-in
        print(f"STEAM UP signed in as {st['account']}")
    else:
        print("STEAM UP signed OUT (the sign-in window is showing; db-steam-load signs in)")


def shutdown(vm, wait_s=120):
    """Exit the client cleanly (it writes its config on exit)."""
    if not running(vm):
        return
    try:
        evaluate(vm, "SteamClient.User.StartShutdown(false)", await_promise=False)
    except Fail:
        if _bottle(vm):
            vm.kill()
            return
        d = steam_dir(vm)
        remote.ssh(vm, f'"{d}\\steam.exe" -shutdown', timeout=60)

    def gone():
        return not running(vm), "steam.exe " + ("exited" if not running(vm) else "running")
    o, _ = until(gone, wait_s, "Steam exit", every=30, interval=3)
    if o != Outcome.DONE:
        log("Steam did not exit in time; killing it")
        if _bottle(vm):
            vm.kill()
        else:
            remote.ssh(vm, "taskkill /f /im steam.exe /t", timeout=60)


# ---- DevTools over SSH ---------------------------------------------------------------

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def forward(vm):
    """A host port forwarded to the guest's DevTools port, on the VM's shared
    SSH connection (one per VM lifetime, remembered in the session).  A
    bottle's client listens on the Mac itself."""
    if _bottle(vm):
        return vm.cdp_port()
    from .common import Session
    sess = Session(vm)
    port = sess.get("steam_cdp_port")
    if port and _http_ok(port):
        return port
    port = _free_port()
    remote.ensure_master(vm)
    d = remote.dest(vm)
    rc = subprocess.run(["ssh"] + remote.ssh_opts(vm) + ["-O", "forward", "-L",
                        f"127.0.0.1:{port}:127.0.0.1:{CDP_GUEST_PORT}", d],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    if rc.returncode != 0:
        die(f"could not forward the DevTools port: {rc.stderr.decode()[-300:]}")
    sess["steam_cdp_port"] = port
    return port


def _http_ok(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5).read()
        return True
    except OSError:
        return False


def targets(vm):
    port = forward(vm)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as r:
            return json.load(r)
    except OSError as e:
        raise Fail(f"no DevTools endpoint in the guest ({e}); is Steam running with "
                   "remote debugging enabled (`utmbot steam setup`, `utmbot steam start`)?")


def shared_target(vm):
    for t in targets(vm):
        if t.get("title") == "SharedJSContext" and t.get("webSocketDebuggerUrl"):
            return t
    return None


class WebSocket:
    """Just enough RFC 6455 for DevTools: masked text frames out, text or
    fragmented frames in, pings answered."""

    def __init__(self, url, timeout=60):
        m = re.match(r"ws://([^/:]+):(\d+)(/.*)", url)
        if not m:
            raise Fail(f"unexpected websocket URL {url}")
        host, port, path = m[1], int(m[2]), m[3]
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        # no Origin header: CEF refuses foreign origins, and we are not a page
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Fail("websocket handshake: connection closed")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise Fail(f"websocket handshake refused: {head[:200]!r}")

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise Fail("websocket closed by the guest")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(hdr + mask + body)

    def send(self, text):
        self._send(0x1, text.encode())

    def recv(self):
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:      # ping
                self._send(0xA, data)
                continue
            if opcode == 0x8:
                raise Fail("websocket closed by the guest")
            if opcode in (0x1, 0x2, 0x0):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self._send(0x8, b"")
            self.sock.close()
        except OSError:
            pass


class CDP:
    def __init__(self, ws_url, timeout=120):
        self.ws = WebSocket(ws_url, timeout)
        self.next_id = random.randint(1000, 9000)

    def call(self, method, params=None):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise Fail(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        self.ws.close()


def _evaluate_once(vm, expr, await_promise, timeout, target):
    t = target or shared_target(vm)
    if not t:
        raise Fail("Steam's SharedJSContext is not there; is Steam running (`utmbot steam start`)?")
    port = forward(vm)
    url = re.sub(r"ws://[^/]+", f"ws://127.0.0.1:{port}", t["webSocketDebuggerUrl"])
    c = CDP(url, timeout)
    try:
        return c.call("Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise,
                                           "returnByValue": True, "userGesture": True,
                                           "timeout": timeout * 1000})
    finally:
        c.close()


# The client reloads SharedJSContext once while it finishes starting (and on
# sign-in): a call that lands then loses its context.  Those are retried.
_TRANSIENT = ("Execution context was destroyed", "websocket closed", "Cannot find context",
              "SharedJSContext is not there", "handshake", "Connection refused", "timed out")


def evaluate(vm, expr, await_promise=True, timeout=120, target=None, retries=5):
    """Evaluate a JS expression in SharedJSContext (or another target) and
    return its value as JSON."""
    for attempt in range(retries + 1):
        try:
            r = _evaluate_once(vm, expr, await_promise, timeout, target)
            break
        except (Fail, OSError) as e:
            if attempt == retries or not any(t in str(e) for t in _TRANSIENT) or target:
                raise Fail(str(e)) if not isinstance(e, Fail) else e
            time.sleep(4)
    if r.get("exceptionDetails"):
        d = r["exceptionDetails"]
        ex = d.get("exception") or {}
        msg = ex.get("description") or ex.get("value") or d.get("text")
        raise Fail(f"JS exception: {msg}")
    return (r.get("result") or {}).get("value")


# ---- state ---------------------------------------------------------------------------

# App.m_CurrentUser exists before sign-in too, holding account id 0
_STATUS_JS = r"""(async () => {
  const u = App.m_CurrentUser || {};
  const zero = "76561197960265728";
  const signedIn = !!u.strAccountName && !!u.strSteamID && u.strSteamID !== zero;
  let info = {};
  try { info = await SteamClient.System.GetSystemInfo(); } catch (e) {}
  let persona = null;
  try { persona = friendStore.m_FriendsUIFriendStore.m_eUserPersonaState; } catch (e) {}
  return {signed_in: signedIn, account: u.strAccountName || null,
          steamid: signedIn ? u.strSteamID : null, offline_mode: !!u.bIsOfflineMode,
          login_state: App.m_eLoginState, init_after_login: !!App.m_bStartedInitAfterLogin,
          persona_state: persona, client_version: info.nSteamVersion || null,
          client_build: info.sSteamBuildDate || null};
})()"""


def status(vm):
    return evaluate(vm, _STATUS_JS)


def wait_login(vm, timeout_s=1800):
    def check():
        try:
            st = status(vm)
        except Fail as e:
            return False, str(e)[:80]
        ok = st["signed_in"] and st["init_after_login"]
        return ok, f"signed in as {st['account']}" if ok else "waiting for sign-in in the VM window"
    o, obs = until(check, timeout_s, "Steam sign-in", every=60, vm=vm, interval=10)
    if o != Outcome.DONE:
        die(f"no account signed in after {timeout_s}s ({o}: {obs})")
    print(f"SIGNED IN {status(vm)['account']}")


# The baseline's settings.  Client settings are the protobuf-backed ones the
# Settings dialog edits (settingsStore.GetClientSetting returns [value,
# setter]); the friends settings and the persona state belong to the friends
# UI.  Persona 7 is "Invisible" (appear offline); with bSignIntoFriends off,
# the client does not sign in to friends at all when it starts.
BASELINE_CLIENT = {
    "start_page": "library",               # open in the Library, not the store
    "show_store_content_on_home": False,   # no store content on the Library home
    "enable_marketing_messages": False,    # no special-offer / marketing pop-ups
    "run_at_startup": True,                # start with Windows (HKCU Run, -silent)
    "cef_remote_debugging_enabled": True,  # keep this module's DevTools endpoint on
}
PERSONA_INVISIBLE = 7

_CONFIGURE_JS = r"""(async (want, persona) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const errors = [];
  for (const [k, v] of Object.entries(want)) {
    try { settingsStore.GetClientSetting(k)[1](v); } catch (e) { errors.push(k + ": " + e); }
  }
  try { settingsStore.UpdateFriendSetting("bSignIntoFriends", false); } catch (e) { errors.push("bSignIntoFriends: " + e); }
  try { friendStore.m_FriendsUIFriendStore.SetUserPersonaState(persona); } catch (e) { errors.push("persona: " + e); }
  await sleep(3000);
  return {errors};
})"""

_READ_SETTINGS_JS = r"""((keys) => {
  const out = {};
  for (const k of keys) out[k] = settingsStore.clientSettings[k];
  out.bSignIntoFriends = settingsStore.friendSettings.bSignIntoFriends;
  const f = friendStore.m_FriendsUIFriendStore;
  out.persona_state = f ? f.m_eUserPersonaState : null;
  out.persona_preference = f ? f.GetPersonaStatePreference() : null;
  return out;
})"""


def read_settings(vm):
    return evaluate(vm, f"{_READ_SETTINGS_JS}({json.dumps(list(BASELINE_CLIENT))})")


def configure(vm):
    st = status(vm)
    if not st["signed_in"]:
        die("no account is signed in; settings are per account (`steam wait-login`)")
    if _bottle(vm):
        die("`configure` makes the VM's baseline; a bottle's client keeps the settings "
            "it was set up with")
    r = evaluate(vm, f"{_CONFIGURE_JS}({json.dumps(BASELINE_CLIENT)}, {PERSONA_INVISIBLE})")
    got = read_settings(vm)
    want = dict(BASELINE_CLIENT, bSignIntoFriends=False, persona_state=PERSONA_INVISIBLE)
    bad = {k: got.get(k) for k, v in want.items() if got.get(k) != v}
    # Steam writes the startup entry itself; check it really is there
    rc, out = remote.ssh(vm, 'reg query "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v Steam',
                         timeout=30)
    autorun = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else None
    for k, v in got.items():
        print(f"  {k:30s} {v!r}" + ("   NOT SET" if k in bad else ""))
    print(f"  {'HKCU Run Steam':30s} {autorun or 'MISSING   NOT SET'}")
    if r.get("errors"):
        print("  errors: " + "; ".join(r["errors"]))
    if bad or not autorun or r.get("errors"):
        die("some settings did not take (NOT SET above); look at them with `steam js`")
    print("CONFIGURED")


# ---- the Steam console: licenses, installs ------------------------------------------

_CONSOLE_JS = r"""(async (cmd, ms) => {
  const out = [];
  const h = SteamClient.Console.RegisterForSpewOutput(m => { if (m.spew_type !== "input") out.push(m.spew); });
  SteamClient.Console.ExecCommand(cmd);
  await new Promise(r => setTimeout(r, ms));
  try { h.unregister(); } catch (e) {}
  return out.join("");
})"""


def console(vm, cmd, wait_ms=3000):
    """Run a Steam console command and return what it printed."""
    out = evaluate(vm, f"{_CONSOLE_JS}({json.dumps(cmd)}, {int(wait_ms)})", timeout=60 + wait_ms // 1000)
    return re.sub(r"STARTUP SlowJSMethodCall[^\n]*\n", "", out or "")


def app_status(vm, appid):
    """`app_status` parsed: owned (Subscribed), free, install state, dir, size,
    download progress."""
    t = console(vm, f"app_status {int(appid)}")
    st = {"appid": int(appid), "raw": t}
    m = re.search(r"AppID \d+ \(([^)]*)\)", t)
    st["name"] = m[1] if m else None
    rel = re.search(r"release state: [^(]*\(([^)]*)\)", t)
    flags = [f.strip() for f in (rel[1] if rel else "").split(",") if f.strip()]
    st["flags"] = flags
    st["owned"] = "Subscribed" in flags
    st["free"] = "Free" in flags or "Free on Demand" in flags
    m = re.search(r"install state: ([^\n]*)", t)
    st["install_state"] = m[1].strip().rstrip(",") if m else None
    st["installed"] = bool(m and "Fully Installed" in m[1])
    m = re.search(r'install dir: "([^"]*)"', t)
    st["install_dir"] = m[1] if m else None
    m = re.search(r"size on disk: (\d+) bytes", t)
    st["size_bytes"] = int(m[1]) if m else None
    m = re.search(r"downloaded: (\d+)/(\d+) MB", t)
    st["downloaded_mb"] = [int(m[1]), int(m[2])] if m else None
    m = re.search(r"update state:\s*([^\n]*)", t)
    st["update_state"] = m[1].strip() if m else None
    return st


LIBRARY = DATA / "steam" / "library.json"


def library_cache():
    """The account's games as last seen in the client (no VM needed)."""
    try:
        return json.loads(LIBRARY.read_text())
    except (OSError, ValueError):
        return None


def owned(vm, appid=None, entries=False):
    if appid is not None:
        st = app_status(vm, appid)
        return {k: st[k] for k in ("appid", "name", "owned", "free", "installed")}
    apps = evaluate(vm, r"""collectionStore.allGamesCollection.allApps.map(a => ({
        appid: a.appid, name: a.display_name, type: a.app_type, installed: !!a.installed}))""")
    # remembered, so a skill can tell what the account owns without a boot (the
    # VM's account: a bottle may be signed in to another one)
    if _bottle(vm):
        return apps if not entries else _owned_entries(vm, apps)
    ensure_dir(LIBRARY.parent)
    LIBRARY.write_text(json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "updated_ts": time.time(), "apps": apps}, indent=2))
    if not entries:
        return apps
    return _owned_entries(vm, apps)


def _owned_entries(vm, apps):
    # entries for the site's owned list; free-to-play games, demos and free
    # licenses are marked free
    out = []
    for a in apps:
        st = app_status(vm, a["appid"])
        if st["owned"]:
            out.append({"storefront": "steam", "storefront_id": str(a["appid"]),
                        "free": bool(st["free"]), "name": a["name"]})
    return out


def license_request(vm, appid, wait_s=60):
    """Claim the free license of a free game or demo (no purchase)."""
    st = app_status(vm, appid)
    if st["owned"]:
        print(f"OWNED {appid} {st['name']} (already)")
        return
    console(vm, f"app_license_request {int(appid)}", 3000)

    def got():
        s2 = app_status(vm, appid)
        return s2["owned"], "owned" if s2["owned"] else "not yet"
    o, _ = until(got, wait_s, "free license", every=30, interval=5)
    if o != Outcome.DONE:
        die(f"no license for {appid} after {wait_s}s: it is probably not free (buy or gift it)")
    print(f"LICENSED {appid} {app_status(vm, appid)['name']}")


def install(vm, appid, wait=True, timeout_s=7200, stall_s=900):
    st = app_status(vm, appid)
    if not st["owned"]:
        die(f"{appid} ({st['name']}) is not owned by the signed-in account")
    if st["installed"]:
        print(f"INSTALLED {appid} {st['name']} {st['size_bytes']} (already)")
        return st
    console(vm, f"app_install {int(appid)}", 3000)
    log(f"install of {appid} ({st['name']}) queued")
    if not wait:
        return st
    t0, last_change, last = time.time(), time.time(), None
    while True:
        s2 = app_status(vm, appid)
        if s2["installed"]:
            print(f"INSTALLED {appid} {s2['name']} {s2['size_bytes']} in {int(time.time() - t0)}s")
            return s2
        prog = s2["downloaded_mb"]
        if prog != last:
            last, last_change = prog, time.time()
            if prog:
                log(f"{appid}: {prog[0]}/{prog[1]} MB ({100 * prog[0] // max(prog[1], 1)}%), "
                    f"{s2['install_state']}")
        if time.time() - last_change > stall_s:
            print(f"STALLED {appid}: no progress for {stall_s // 60} min; state "
                  f"{s2['install_state']!r}, update {s2['update_state']!r}")
            raise Fail("install stalled (rerun the same command after fixing the cause; it resumes)")
        if time.time() - t0 > timeout_s:
            raise Fail(f"install of {appid} did not finish in {timeout_s}s")
        time.sleep(30)


# ---- running games --------------------------------------------------------------------

def _game_processes(vm, install_dir):
    if _bottle(vm):
        return vm.game_processes(install_dir)
    rc, out = remote.ps1(vm, f"""
$d = '{install_dir.replace("'", "''")}'
Get-Process | Where-Object {{ $_.Path -and $_.Path.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) }} |
  ForEach-Object {{ "PROC " + $_.Id + " " + $_.Name }}""", timeout=60)
    return [l[5:] for l in out.splitlines() if l.startswith("PROC ")]


# A launch is a "game action" in the client: a series of tasks (updating, cloud
# sync, EULAs, creating the process), some of which ask the user.  The UI
# answers most of those itself at once (ShowInterstitials, CreatingProcess);
# the ones it cannot leave the action waiting (bWaitingForUI) behind a dialog
# nobody sees in an unattended run.  The answers below are the client's own:
# Steam's UI (steamui, LaunchApp's OnGameActionUserRequest) answers its
# "Cloud Out of Date" dialog (SynchronizingCloud "pendingcloudsessions",
# after the same account played on another PC, e.g. the Steam VM) with
# ContinueGameAction(id, "IgnorePendingCloudSessions") for "Play anyway".
_ACTIONS_JS = r"""SteamClient.Apps.GetActiveGameActions().then(acts => (acts || [])
  .filter(a => String(a.gameid) === %s)
  .map(a => ({id: a.nGameActionID, action: a.strActionName, task: a.strTaskName,
              details: a.strTaskDetails, waiting: !!a.bWaitingForUI})))"""

# The triage account's standing answers: (task, details) -> ContinueGameAction's argument
STANDING_ANSWERS = {
    ("SynchronizingCloud", "pendingcloudsessions"): "IgnorePendingCloudSessions",   # "Play anyway"
}

# The Wine log (bottles) has the same request when DevTools cannot be asked
_WAITING_LOG = re.compile(r'GameAction \[AppID (\d+), ActionID (\d+)\] : (\w+) waiting for user '
                          r'response to (\w+) \\?"([^"\\]*)')


def game_actions(vm, appid):
    """The client's active game actions for appid: id, action, task, details,
    waiting (for an answer from the UI)."""
    return evaluate(vm, _ACTIONS_JS % json.dumps(str(int(appid)))) or []


def _close_cloud_dialog(vm):
    """The client's own "Cloud Out of Date" dialog stays open after the action
    was answered through the API.  Its "Play anyway" then only closes it (the
    action is over; checked: no second launch)."""
    js = r"""(() => { const b = [...document.querySelectorAll("button, [role=button], .DialogButton")]
      .filter(x => /^Play anyway$/i.test((x.innerText || "").trim()));
      b.forEach(x => x.click()); return b.length; })()"""
    for t in targets(vm):
        if t.get("type") == "page" and t.get("title") not in ("SharedJSContext",) and \
                t.get("webSocketDebuggerUrl"):
            try:
                if evaluate(vm, js, target=t, timeout=15):
                    return True
            except Fail:
                pass
    return False


class _LaunchWatch:
    """Watches a launch's game action: answers what the triage account has a
    standing answer for, and fails fast on any other request that waits."""

    def __init__(self, vm, appid, patience_s=10):
        self.vm, self.appid, self.patience = vm, int(appid), patience_s
        self.first_seen = {}       # (id, task, details) -> when it was first seen waiting
        self.answered = set()
        self.api_failures = 0
        self.last = None
        self.log = Path(vm.state().get("log") or "") if _bottle(vm) else None
        self.log_pos = self.log.stat().st_size if self.log and self.log.is_file() else 0

    def _record(self, line):
        with open(_run_dir(self.vm) / "game-actions.txt", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")

    def _waiting_in_log(self):
        """(task, details) of the newest "waiting for user response" line since the launch."""
        if not self.log or not self.log.is_file():
            return None
        with open(self.log, "rb") as f:
            f.seek(self.log_pos)
            text = f.read().decode("utf-8", "replace")
        hits = [m for m in _WAITING_LOG.finditer(text) if int(m[1]) == self.appid]
        return (hits[-1][4], hits[-1][5]) if hits else None

    def check(self):
        """None while all is well, else a description of the request nobody answers."""
        try:
            acts = self.last = game_actions(self.vm, self.appid)
            self.api_failures = 0
        except Fail as e:
            self.last = None
            self.api_failures += 1
            if self.api_failures >= 2:
                w = self._waiting_in_log()
                if w and w not in {(k[1], k[2]) for k in self.answered}:
                    return (f"Steam is waiting for an answer to {w[0]} \"{w[1]}\" (from the Wine log; "
                            f"the client's DevTools did not answer: {e})")
            return None
        now = time.time()
        for a in acts:
            if not a["waiting"]:
                continue
            key = (a["id"], a["task"], a["details"])
            answer = STANDING_ANSWERS.get((a["task"], a["details"]))
            if answer and key not in self.answered:
                evaluate(self.vm, f"SteamClient.Apps.ContinueGameAction({int(a['id'])}, {json.dumps(answer)})",
                         await_promise=False)
                self.answered.add(key)
                self._record(f"app {self.appid} action {a['id']}: {a['task']} \"{a['details']}\" -> "
                             f"ContinueGameAction {answer}")
                log(f"Steam asked {a['task']} \"{a['details']}\"; answered {answer} (the triage account's "
                    "standing answer: launch anyway)")
                if _close_cloud_dialog(self.vm):
                    log("closed the client's leftover dialog")
                continue
            t0 = self.first_seen.setdefault(key, now)
            if now - t0 >= self.patience and key not in self.answered:
                evaluate(self.vm, f"SteamClient.Apps.CancelGameAction({int(a['id'])})", await_promise=False)
                self._record(f"app {self.appid} action {a['id']}: {a['task']} \"{a['details']}\" "
                             f"waited {int(now - t0)}s -> CancelGameAction")
                return (f"Steam's launch ({a['action']}, action {a['id']}) is waiting for an answer to "
                        f"{a['task']} \"{a['details']}\", which has no standing answer; the launch was "
                        "cancelled")
        return None

    def status(self):
        acts = self.last
        if acts is None:
            return "client not answering"
        if not acts:
            return "no launch action in the client"
        return "; ".join(f"{a['task'] or a['action']}{' ' + repr(a['details']) if a['details'] else ''}"
                         f"{' (waiting for an answer)' if a['waiting'] else ''}" for a in acts)


def run_game(vm, appid, wait_s=900, launch_args=""):
    """Accept the game's EULAs (the triage account's standing decision), start
    it through the client, and wait for a process from its install directory.
    First starts run the game's redistributable installers first, which can
    take minutes.  The launch's game action is watched meanwhile: a request
    with a standing answer is answered, any other one that waits for the user
    fails the launch within ~15 s instead of waiting out wait_s."""
    st = app_status(vm, appid)
    if not st["installed"]:
        die(f"{appid} is not installed ({st['install_state']})")
    eulas = evaluate(vm, f"""Promise.resolve().then(() => SteamClient.Apps.LoadEula({int(appid)})).catch(() => []).then(es => {{
        (es || []).forEach(e => SteamClient.Apps.MarkEulaAccepted({int(appid)}, e.id, e.version));
        return (es || []).map(e => ({{id: e.id, version: e.version, url: e.url}})); }})""")
    if eulas:
        rd = _run_dir(vm)
        with open(rd / "eulas-accepted.txt", "a") as f:
            for e in eulas:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} app {appid} {e['id']} v{e['version']} {e['url']}\n")
        log(f"accepted {len(eulas)} EULA(s) for {appid} (recorded in {rd / 'eulas-accepted.txt'})")
    watch = _LaunchWatch(vm, appid)
    evaluate(vm, f"SteamClient.Apps.RunGame({json.dumps(str(int(appid)))}, {json.dumps(launch_args)}, -1, 100)",
             await_promise=False)
    log(f"started {appid} ({st['name']}); waiting for its process (first start runs installers)")
    stuck = []

    def up():
        p = _game_processes(vm, st["install_dir"])
        if p:
            return True, ", ".join(p)
        why = watch.check()
        if why:
            stuck.append(why)
            return True, why
        return False, f"no game process yet; Steam: {watch.status()}"
    o, obs = until(up, wait_s, "game process", every=30, vm=vm, interval=5)
    if stuck:
        die(f"{appid} did not start: {stuck[0]}. Look at the client's windows (`steam targets`, "
            "`steam js 'document.body.innerText' --target '<title>'`) and the run dir's game-actions.txt")
    if o != Outcome.DONE:
        die(f"{appid} did not start ({o}: {obs}); look at the client's windows: "
            "`steam targets`, then `steam js 'document.body.innerText' --target '<title>'`")
    print(f"RUNNING {appid} {obs}")


def stop_game(vm, appid):
    st = app_status(vm, appid)
    evaluate(vm, f"SteamClient.Apps.TerminateApp({json.dumps(str(int(appid)))}, false)", await_promise=False)

    def gone():
        p = _game_processes(vm, st["install_dir"]) if st["install_dir"] else []
        return not p, ", ".join(p) or "exited"
    o, obs = until(gone, 60, "game exit", every=30, interval=3)
    if o != Outcome.DONE and _bottle(vm):
        vm.kill_game(st["install_dir"])
        log("the game did not exit when asked; killed its processes")
    elif o != Outcome.DONE:
        remote.ps1(vm, f"""Get-Process | ? {{ $_.Path -and $_.Path.StartsWith('{st['install_dir']}', 'OrdinalIgnoreCase') }} | Stop-Process -Force""")
        log("the game did not exit when asked; killed its processes")
    print(f"STOPPED {appid}")


# ---- web pages in the client's session (gifts) -------------------------------------------

def open_page(vm, url, wait_s=60):
    """Open a page in the client (its cookies: signed in on the store and the
    community) and return its DevTools target."""
    port = forward(vm)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        t = json.load(r)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        cur = next((x for x in targets(vm) if x.get("id") == t["id"]), None)
        if cur and cur.get("title") and cur.get("url", "").startswith(url.split("#")[0][:30]):
            try:
                if evaluate(vm, "document.readyState", target=cur) == "complete":
                    return cur
            except Fail:
                pass
        time.sleep(2)
    die(f"{url} did not load in the client")


def close_page(vm, target):
    port = forward(vm)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target['id']}", timeout=10).read()
    except OSError:
        pass


GIFTS_URL = "https://store.steampowered.com/gifts/"

# Each pending gift is a block on the store's gifts page; read what it says
# and which controls it has, by their visible labels, so nothing depends on
# the page's internal function names.
_GIFTS_JS = r"""(() => {
  const root = document.querySelector(".pending_gifts") || document.body;
  const none = !!document.querySelector(".no_gifts") && getComputedStyle(document.querySelector(".no_gifts")).display !== "none";
  const blocks = [...root.querySelectorAll("[id^=pending_gift], [data-giftid], [data-gift-id], .pending_gift, .gift_row, .giftpopup_gift")];
  const gifts = blocks.map((b, i) => {
    const ids = (b.id || "") + " " + JSON.stringify(b.dataset);
    const apps = [...b.querySelectorAll("a[href*='/app/']")].map(a => (a.href.match(/\/app\/(\d+)/) || [])[1]).filter(Boolean);
    const controls = [...b.querySelectorAll("a, button, [role=button], .btn_green_steamui, .btn_grey_steamui")]
      .map(c => (c.innerText || "").trim()).filter(Boolean);
    return {index: i, id: (ids.match(/\d{6,}/) || [null])[0], apps: [...new Set(apps)],
            text: (b.innerText || "").replace(/\s+/g, " ").trim().slice(0, 300), controls};
  });
  return {signed_in: typeof g_AccountID !== "undefined" && !!g_AccountID, none, gifts,
          page_text: gifts.length ? null : document.body.innerText.replace(/\s+/g, " ").slice(0, 400)};
})()"""

_ACCEPT_JS = r"""((index, label) => {
  const root = document.querySelector(".pending_gifts") || document.body;
  const blocks = [...root.querySelectorAll("[id^=pending_gift], [data-giftid], [data-gift-id], .pending_gift, .gift_row, .giftpopup_gift")];
  const b = blocks[index];
  if (!b) return "no gift block " + index;
  const c = [...b.querySelectorAll("a, button, [role=button], .btn_green_steamui")]
    .find(x => (x.innerText || "").trim().toLowerCase().startsWith(label));
  if (!c) return "no control labelled " + label + " in gift " + index;
  c.click();
  return "clicked";
})"""


def _settled_gifts(vm, t, wait_s=120):
    """The gifts page once it says something: signed in, and either gifts or
    "no pending gifts".  It first shows a transient "Verifying login
    information…" page (not signed in, nothing listed), and sometimes "error
    communicating with the Steam servers" (reloaded, twice at most).  Read
    too early, either looks just like an account with no gifts."""
    deadline, reloads, g = time.time() + wait_s, 0, {}
    while True:
        try:
            g = evaluate(vm, _GIFTS_JS, target=t)
        except Fail:
            g = {}
        text = (g.get("page_text") or "").lower()
        if g.get("signed_in") and (g.get("gifts") or g.get("none")):
            return g
        if "error communicating with the steam servers" in text and reloads < 2:
            reloads += 1
            log(f"gifts page: Steam server error, reloading ({reloads}/2)")
            try:
                evaluate(vm, "location.reload()", target=t)
            except Fail:
                pass
            time.sleep(5)
            continue
        if time.time() >= deadline:
            die("the store's gifts page never settled (last: " + (g.get("page_text") or "no text")[:200]
                + "). If the client shows signed in, restart it (`steam stop`, `steam start`) and retry")
        time.sleep(3)


def gifts(vm):
    t = open_page(vm, GIFTS_URL)
    try:
        return _settled_gifts(vm, t)
    finally:
        close_page(vm, t)


def accept_gift(vm, index=None, appid=None):
    t = open_page(vm, GIFTS_URL)
    try:
        g = _settled_gifts(vm, t)
        if not g["signed_in"]:
            die("the store page is not signed in in the client")
        if not g["gifts"]:
            die("no pending gifts on the store's gifts page" + (f": {g['page_text']}" if g["page_text"] else ""))
        pick = [x for x in g["gifts"] if (index is None or x["index"] == index) and
                (appid is None or str(appid) in x["apps"] or str(appid) in x["text"])]
        if not pick:
            die(f"no pending gift matches; pending: {json.dumps(g['gifts'], indent=2)}")
        out = []
        for x in pick:
            r = evaluate(vm, f"{_ACCEPT_JS}({x['index']}, 'accept')", target=t)
            if r != "clicked":
                die(f"could not accept gift {x['index']}: {r}. The page changed; read it with "
                    f"`steam js 'document.body.innerText' --target Gifts` after `steam gifts`")
            time.sleep(3)
            # a confirmation step (e.g. "Add to my library") if the page asks for one
            for label in ("add to", "accept", "ok", "continue"):
                try:
                    evaluate(vm, f"""(() => {{ const c = [...document.querySelectorAll(".newmodal a, .newmodal button, .modal_frame button, .btn_green_steamui")]
                        .find(e => (e.innerText||"").trim().toLowerCase().startsWith({json.dumps(label)})); if (c) {{ c.click(); return 1; }} return 0; }})()""", target=t)
                except Fail:
                    pass
            out.append(x)
        time.sleep(5)
        return out
    finally:
        close_page(vm, t)


# ---- the public store (host side, no VM) ------------------------------------------

STORE = "https://store.steampowered.com"


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "utmapp-bot", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def appinfo(appid):
    """Store facts about an app: name, type (game, demo, dlc, ...), free,
    the full game a demo belongs to, Windows support, and the install size
    it asks for."""
    d = _get_json(f"{STORE}/api/appdetails?appids={int(appid)}&l=english&cc=us")
    e = d.get(str(appid)) or {}
    if not e.get("success"):
        return {"appid": int(appid), "found": False}
    x = e["data"]
    req = (x.get("pc_requirements") or {})
    req = req.get("minimum", "") if isinstance(req, dict) else ""
    m = re.search(r"Storage:</strong>\s*([\d.]+)\s*(GB|MB)", req)
    size_gb = (float(m[1]) / (1024 if m[2] == "MB" else 1)) if m else None
    return {"appid": int(appid), "found": True, "name": x.get("name"), "type": x.get("type"),
            "is_free": bool(x.get("is_free")),
            "fullgame": (x.get("fullgame") or {}).get("appid"),
            "demos": [dm.get("appid") for dm in x.get("demos") or []],
            "windows": bool((x.get("platforms") or {}).get("windows")),
            "required_storage_gb": size_gb,
            "release_date": (x.get("release_date") or {}).get("date")}


def resolve(term):
    """Store search for a name; exact (case-insensitive) title matches first."""
    q = urllib.parse.quote(term)
    d = _get_json(f"{STORE}/api/storesearch/?term={q}&l=english&cc=us")
    items = [{"appid": it["id"], "name": it["name"], "type": it.get("type")}
             for it in d.get("items", [])]
    norm = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()  # noqa: E731
    items.sort(key=lambda it: (norm(it["name"]) != norm(term), len(it["name"])))
    return items


# ---- command line ------------------------------------------------------------

USAGE = """utmbot steam <command>
  setup                  install the client silently (cached SteamSetup.exe) and
                         enable its DevTools endpoint
  start [--wait S]       start it in the console session, wait for its UI
  stop                   exit it cleanly
  status                 signed in? account, Steam id, persona state, client version
  wait-login [--timeout S]   wait until someone signs in in the VM window
  configure              the baseline settings: Library start page, no store or
                         marketing pop-ups, run at startup, DevTools on, friends:
                         invisible and no sign-in at start; read back and checked
  settings               show those settings
  owned [--appid N] [--entries]
                         owned games (library), or one app's ownership; --entries:
                         the site's owned-list entries (free marked), for
                         `gamesdb owned-add --json`
  app-status APPID       the console's app_status, parsed (owned, free, install state,
                         dir, size, download progress)
  license APPID          claim the free license of a free game or demo
  install APPID [--no-wait] [--timeout S]
                         install into the default library through the console, with
                         progress; INSTALLED / STALLED
  run APPID [--wait S] [--args ARGS]
                         accept its EULAs (logged in the run dir), start it, wait for
                         a process from its install dir
  stop-game APPID        terminate it through the client
  gifts                  pending gifts on the store's gifts page (in the client)
  accept-gift [--index I | --appid N | --all]
  console CMD            any Steam console command, its output
  targets                DevTools targets (pages) of the running client
  resolve NAME           store search: candidate app ids, exact title first (no VM)
  appinfo APPID          store facts: type (game/demo/...), free, demos, size (no VM)
  library                the account's games as last seen by `steam owned` (no VM)
  js EXPR [--no-await] [--target TITLE]
                         evaluate JavaScript in SharedJSContext (or the page with
                         that title) and print the JSON result: for looking at the
                         live SteamClient API"""


def main(vm, rest):
    import argparse
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub, args = rest[0], rest[1:]
    if sub == "resolve":
        print(json.dumps(resolve(" ".join(args)), indent=2, ensure_ascii=False))
        return 0
    if sub == "appinfo":
        print(json.dumps(appinfo(int(args[0])), indent=2, ensure_ascii=False))
        return 0
    if sub == "library":
        lib = library_cache()
        if not lib:
            die("no library seen yet: `steam owned` in a boot of the Steam VM records it")
        age = (time.time() - lib.get("updated_ts", 0)) / 86400
        print(json.dumps({"updated": lib["updated"], "age_days": round(age, 1), "apps": lib["apps"]},
                         indent=2, ensure_ascii=False))
        return 0
    if sub == "setup":
        setup(vm)
    elif sub == "start":
        p = argparse.ArgumentParser(prog="utmbot steam start")
        p.add_argument("--wait", type=int, default=600)
        a = p.parse_args(args)
        start(vm, a.wait)
    elif sub == "stop":
        shutdown(vm)
        print("STEAM STOPPED")
    elif sub == "status":
        print(json.dumps(status(vm), indent=2))
    elif sub == "wait-login":
        p = argparse.ArgumentParser(prog="utmbot steam wait-login")
        p.add_argument("--timeout", type=int, default=1800)
        wait_login(vm, p.parse_args(args).timeout)
    elif sub == "configure":
        configure(vm)
    elif sub == "settings":
        print(json.dumps(read_settings(vm), indent=2))
    elif sub == "owned":
        p = argparse.ArgumentParser(prog="utmbot steam owned")
        p.add_argument("--appid", type=int)
        p.add_argument("--entries", action="store_true")
        a = p.parse_args(args)
        print(json.dumps(owned(vm, a.appid, a.entries), indent=2, ensure_ascii=False))
    elif sub == "app-status":
        st = app_status(vm, int(args[0]))
        st.pop("raw")
        print(json.dumps(st, indent=2, ensure_ascii=False))
    elif sub == "license":
        license_request(vm, int(args[0]))
    elif sub == "install":
        p = argparse.ArgumentParser(prog="utmbot steam install")
        p.add_argument("appid", type=int)
        p.add_argument("--no-wait", action="store_true")
        p.add_argument("--wait", action="store_true", help="(the default)")
        p.add_argument("--timeout", type=int, default=7200)
        a = p.parse_args(args)
        install(vm, a.appid, not a.no_wait, a.timeout)
    elif sub == "run":
        p = argparse.ArgumentParser(prog="utmbot steam run")
        p.add_argument("appid", type=int)
        p.add_argument("--wait", type=int, default=900)
        p.add_argument("--args", default="")
        a = p.parse_args(args)
        run_game(vm, a.appid, a.wait, a.args)
    elif sub == "stop-game":
        stop_game(vm, int(args[0]))
    elif sub == "gifts":
        print(json.dumps(gifts(vm), indent=2, ensure_ascii=False))
    elif sub == "accept-gift":
        p = argparse.ArgumentParser(prog="utmbot steam accept-gift")
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--index", type=int)
        g.add_argument("--appid", type=int)
        g.add_argument("--all", action="store_true")
        a = p.parse_args(args)
        done = accept_gift(vm, a.index, a.appid)
        print(json.dumps(done, indent=2, ensure_ascii=False))
        print(f"ACCEPTED {len(done)} gift(s); confirm with `steam owned --appid N`")
    elif sub == "console":
        print(console(vm, " ".join(args)))
    elif sub == "targets":
        for t in targets(vm):
            print(f"{t.get('type', ''):8s} {t.get('title', '')[:50]:50s} {t.get('url', '')[:90]}")
    elif sub == "js":
        p = argparse.ArgumentParser(prog="utmbot steam js")
        p.add_argument("expr")
        p.add_argument("--no-await", action="store_true")
        p.add_argument("--target")
        a = p.parse_args(args)
        tgt = None
        if a.target:
            tgt = next((t for t in targets(vm) if t.get("title") == a.target), None)
            if not tgt:
                die(f"no DevTools target titled {a.target!r}")
        print(json.dumps(evaluate(vm, a.expr, not a.no_await, target=tgt), indent=2,
                         ensure_ascii=False))
    else:
        raise Fail(USAGE)
    return 0
