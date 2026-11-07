"""CrossOver on this Mac: the same Windows game in Wine on the host, as the
A/B partner of a UTM VM (skills/crossover-compare).

What this relies on, each checked against CrossOver 26.3:

* Bottles are directories in ~/Library/Application Support/CrossOver/Bottles.
  The graphics backend is CX_GRAPHICS_BACKEND (d3dmetal, dxmt, dxvk, wined3d;
  unset: CrossOver decides) in the bottle's cxbottle.conf, [EnvironmentVariables].
  Comparisons with UTM use only d3dmetal and dxmt, as the caller decides:
  D3D11 on the backend the VM ran, D3D12 on d3dmetal.
  `wine --env` is applied after the bottle's variables, so a launch can
  override it, and Wine children (the game Steam starts) inherit it.  The
  compatibility database (cxcompatdb) can still choose another backend for an
  app ("HACK: trying graphics backend"), so the setting proves nothing.  Proof
  comes from the log (`trace:process:set_graphics_backend using X as the
  graphics backend`, logged for every process whenever there is a --cx-log)
  and from the libraries mapped into the running game (lsof).  An explicit
  CX_GRAPHICS_BACKEND beats the database's rules for an app unless the rule
  is marked `force` (read from cxcompatdb.so's set_graphics_backend).
* A bottle's processes all talk to one wineserver, whose directory is
  /tmp/.wine-<uid>/server-<dev>-<inode> of the bottle directory.  `lsof -t
  +d` on it lists exactly that bottle's processes.  ps shows Wine processes
  by their Windows image path.
* Duplicate bottles are APFS clones (`cp -c`) plus `cxbottle --new-uuid`.
  That is what `cxbottle --copy` does, minus its tar copy and the 3-minute
  --restored step.  A clone of a 2.5 GB Steam bottle took 1.4 s and 4 MB.
  A clone costs only what changes in it, which is the game it installs.
* Steam in a bottle is the same client as in the VM.  Wine's networking is
  the Mac's, so its DevTools endpoint is the Mac's 127.0.0.1:8080, and
  steam.py drives it with a Bottle in place of a VM.
* Input (`cx input`, swift/wininput.swift) reaches a Wine window only while
  its app is frontmost, which the Accessibility API can make it from the
  background.  Keys go to the game's process; mouse buttons only count when
  posted at the HID level, so they are sent only while the screen is
  unlocked and the game is still in front.  A frontmost full-screen Wine
  window sits above window level 0, so windows are listed at every level.

The bottle Steam was set up in (signed in) is the *base*.  It is a seed that
nothing runs in.  Games go into per-game clones named steam-app-<appid>,
labelled in ~/.utmapp-bot/crossover/bottles.json like the VM's snapshots, and
deleted when space runs short.  Running anything in a bottle takes the UTM
lease, because a bottle shares the GPU with the VMs (and Steam's account with
the Steam VM).
"""

import calendar
import json
import ntpath
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .common import (DATA, Fail, current_run_dir, die, ensure_dir, human_secs, kv_list, log,
                     new_run_dir, now_iso, read_json, run, runs_root, slug, write_json)

APP = Path(os.environ.get("UTMBOT_CROSSOVER_APP", "/Applications/CrossOver.app"))
BOTTLES = Path(os.environ.get("UTMBOT_CROSSOVER_BOTTLES",
                              "~/Library/Application Support/CrossOver/Bottles")).expanduser()
STATE = DATA / "crossover"
META = STATE / "bottles.json"
# The backends a comparison with UTM uses, chosen by the caller: D3D11 on the
# one the VM ran (d3dmetal or dxmt), D3D12 on d3dmetal.
COMPARE = ("d3dmetal", "dxmt")
BACKENDS = ("auto",) + COMPARE
CLONE_PREFIX = "steam-app-"
STEAM_RELS = (Path("drive_c/Program Files (x86)/Steam"), Path("drive_c/Program Files/Steam"))
CDP_PORT = 8080
GiB = 1 << 30
# With a log file CrossOver turns on +timestamp,+pid,+seh,+unwind,+process,
# +module,+loaddll,+threadname.  Steam and its web helper raise an exception
# for every debug print, and tracing each one with its unwind made Steam's
# main loop stall until its watchdog killed it (235k lines in 3 min).  The
# trace class of those three goes; their warnings and errors (unhandled
# exceptions), +process (the backend line) and +loaddll stay.
QUIET_DEBUGMSG = "trace-seh,trace-unwind,trace-module"


# ---- CrossOver itself ----------------------------------------------------------

def cx_root(required=True):
    r = APP / "Contents" / "SharedSupport" / "CrossOver"
    if not (r / "bin" / "wine").exists():
        if required:
            die(f"CrossOver is not installed ({APP}; set UTMBOT_CROSSOVER_APP for another place)")
        return None
    return r


def cxbin(name):
    return str(cx_root() / "bin" / name)


def _plist(path, key):
    rc, out = run(["plutil", "-extract", key, "raw", "-o", "-", str(path)], timeout=30)
    return out.strip() if rc == 0 else None


def versions():
    r = cx_root()
    d3dm = r / "lib64" / "apple_gptk" / "external" / "D3DMetal.framework"
    plist = d3dm / "Versions" / "A" / "Resources" / "Info.plist"
    return {
        "crossover": _plist(APP / "Contents" / "Info.plist", "CFBundleVersion"),
        "d3dmetal": _plist(plist, "CFBundleVersion") if plist.exists() else None,
        "dxmt": (r / "lib" / "dxmt").is_dir(),
        "dxvk": (r / "lib" / "dxvk").is_dir(),
        "moltenvk": (r / "lib64" / "libMoltenVK.dylib").exists(),
        "vkd3d_proton": (r / "lib" / "vkd3d-proton").is_dir(),
    }


# ---- labels (the sidecar) -------------------------------------------------------

def load_meta():
    m = read_json(META, {})
    m.setdefault("bottles", {})
    m.setdefault("base", None)
    return m


def save_meta(m):
    write_json(META, m, sort_keys=True)


def label(name, **kv):
    m = load_meta()
    e = m["bottles"].setdefault(name, {})
    e.update({k: v for k, v in kv.items() if v is not None})
    save_meta(m)


def bottle_names():
    if not BOTTLES.is_dir():
        return []
    return sorted(p.name for p in BOTTLES.iterdir()
                  if p.is_dir() and not p.is_symlink() and (p / "cxbottle.conf").exists())


def steam_unix(path):
    for rel in STEAM_RELS:
        if (path / rel / "steam.exe").exists():
            return path / rel
    return None


def base_name(explicit=None):
    """The signed-in Steam bottle clones are made from: --base, $UTMBOT_CX_BASE,
    the one recorded with `cx base --set`, or the only bottle with Steam that
    is not one of our clones."""
    m = load_meta()
    name = explicit or os.environ.get("UTMBOT_CX_BASE") or m.get("base")
    if name:
        if not (BOTTLES / name / "cxbottle.conf").exists():
            die(f"no CrossOver bottle '{name}' in {BOTTLES}")
        if not steam_unix(BOTTLES / name):
            die(f"bottle '{name}' has no Steam (steam.exe) installed")
        return name
    cands = [b for b in bottle_names() if steam_unix(BOTTLES / b)
             and m["bottles"].get(b, {}).get("kind") != "game" and not b.startswith(CLONE_PREFIX)]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        die("no CrossOver bottle with Steam installed: install Steam into a bottle with "
            "CrossOver and sign in there once, then `utmbot cx base --set <bottle>`")
    die("several bottles have Steam (" + ", ".join(cands) + "): choose the signed-in one "
        "with `utmbot cx base --set <bottle>` (or --base / $UTMBOT_CX_BASE)")


# ---- the bottle's config file ------------------------------------------------------

_ENV_SECTION = re.compile(r"^\[EnvironmentVariables\]\s*$", re.M)
_BACKEND_LINE = re.compile(r'^"CX_GRAPHICS_BACKEND"\s*=\s*"([^"]*)"\s*\n?', re.M)


def _section_span(text, header_re):
    m = header_re.search(text)
    if not m:
        return None
    nxt = re.compile(r"^\[[^\]]+\]\s*$", re.M).search(text, m.end())
    return m.end(), (nxt.start() if nxt else len(text))


def conf_backend(path):
    t = (path / "cxbottle.conf").read_text(errors="replace")
    span = _section_span(t, _ENV_SECTION)
    if not span:
        return "auto"
    m = _BACKEND_LINE.search(t, span[0], span[1])
    return m[1] if m and m[1] else "auto"


def conf_set_backend(path, backend):
    f = path / "cxbottle.conf"
    t = f.read_text(errors="replace")
    span = _section_span(t, _ENV_SECTION)
    if not span:
        t = t.rstrip("\n") + "\n\n[EnvironmentVariables]\n"
        span = _section_span(t, _ENV_SECTION)
    start, end = span
    body = _BACKEND_LINE.sub("", t[start:end])
    if backend != "auto":
        body = body.lstrip("\n")
        body = f'\n"CX_GRAPHICS_BACKEND" = "{backend}"\n' + body
    t = t[:start] + body + t[end:]
    tmp = f.with_suffix(".conf.tmp")
    tmp.write_text(t)
    tmp.replace(f)


def conf_set(path, key, value):
    """Set an existing top-level "Key" = "value" line (used for MenuMode/AssocMode)."""
    f = path / "cxbottle.conf"
    t = f.read_text(errors="replace")
    t2, n = re.subn(rf'^(;;)?"{re.escape(key)}"\s*=\s*"[^"]*"', f'"{key}" = "{value}"', t,
                    count=1, flags=re.M)
    if n:
        f.write_text(t2)


# ---- a bottle ------------------------------------------------------------------------

class Bottle:
    """One CrossOver bottle.  It stands in for a VM where steam.py and wait.py
    take one (cx_bottle tells them apart)."""

    cx_bottle = True

    def __init__(self, name):
        self.name = name
        self.path = BOTTLES / name
        if not (self.path / "cxbottle.conf").exists():
            die(f"no CrossOver bottle '{name}' in {BOTTLES} (`utmbot cx list`)")
        self.slug = slug(name)

    # -- labels and the current session (one per `cx start` .. `cx stop`)
    def meta(self):
        return load_meta()["bottles"].get(self.name, {})

    def is_clone(self):
        return self.meta().get("kind") == "game"

    def _state_path(self):
        return STATE / "sessions" / f"{self.slug}.json"

    def state(self):
        return read_json(self._state_path(), {})

    def set_state(self, **kv):
        s = self.state()
        s.update(kv)
        write_json(self._state_path(), s)
        return s

    def clear_state(self):
        write_json(self._state_path(), {"run_dir": self.state().get("run_dir")})

    def runs_dir(self):
        return ensure_dir(runs_root() / f"cx-{self.slug}")

    def run_dir(self):
        return current_run_dir(self.runs_dir(), self.state().get("run_dir"))

    def new_run(self, tag=""):
        return new_run_dir(self.runs_dir(), tag)

    # -- paths
    def steam_dir_unix(self, required=True):
        d = steam_unix(self.path)
        if not d and required:
            die(f"bottle '{self.name}' has no Steam installed")
        return d

    def steam_win(self, required=True):
        d = self.steam_dir_unix(required)
        return self.to_win(d) if d else None

    def steamapps(self):
        return self.steam_dir_unix() / "steamapps"

    def to_win(self, p):
        p = Path(p)
        try:
            rel = p.relative_to(self.path / "drive_c")
            return "C:\\" + str(rel).replace("/", "\\")
        except ValueError:
            return "Z:" + str(p.resolve()).replace("/", "\\")

    def to_unix(self, wpath):
        m = re.match(r"^([A-Za-z]):\\?(.*)$", wpath)
        if not m:
            die(f"not a Windows path: {wpath}")
        drive = (self.path / "dosdevices" / f"{m[1].lower()}:").resolve()
        return drive / m[2].replace("\\", "/")

    # -- processes
    def server_dir(self):
        st = os.stat(self.path)
        return Path(f"/private/tmp/.wine-{os.getuid()}/server-{st.st_dev:x}-{st.st_ino:x}")

    def pids(self):
        sd = self.server_dir()
        if not sd.exists():
            return []
        rc, out = run(["lsof", "-t", "+d", str(sd)], timeout=60)
        return sorted({int(x) for x in out.split() if x.isdigit()})

    def processes(self):
        pids = self.pids()
        if not pids:
            return []
        rc, out = run(["ps", "-ww", "-o", "pid=,args=", "-p", ",".join(map(str, pids))], timeout=30)
        rows = []
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(.*)", line)
            if m:
                # Steam starts games with doubled backslashes in their command
                # line (C:\\Program Files (x86)\\Steam\\...)
                rows.append((int(m[1]), re.sub(r"\\{2,}", r"\\", m[2].strip())))
        return rows

    def running(self):
        return bool(self.pids())

    def steam_running(self):
        return any(re.search(r"(^|\\)steam\.exe(\s|$)", a, re.I) for _, a in self.processes())

    def _under(self, install_dir):
        """Prefixes a process of install_dir shows in ps (its Windows path; the
        Z: form too, should Steam ever start a game by its Unix path)."""
        d = install_dir.rstrip("\\") + "\\"
        out = [d.lower()]
        try:
            out.append(self.to_win(self.to_unix(d)).lower().rstrip("\\") + "\\")
            out.append(("Z:" + str(self.to_unix(d)).replace("/", "\\")).lower().rstrip("\\") + "\\")
        except Fail:
            pass
        return tuple(set(out))

    def game_pids(self, install_dir):
        pre = self._under(install_dir)
        return [(pid, a) for pid, a in self.processes() if a.lower().startswith(pre)]

    def game_processes(self, install_dir):
        """steam.py's view: "<pid> <image>" for each process from install_dir."""
        out = []
        for pid, a in self.game_pids(install_dir):
            m = re.match(r"(.+?\.exe)\b", a, re.I)
            out.append(f"{pid} {ntpath.basename(m[1] if m else a)}")
        return out

    def kill_game(self, install_dir):
        for pid, _ in self.game_pids(install_dir):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def kill(self, wait_s=20):
        """End every process in the bottle (what CrossOver's "Force Quit" does)."""
        if not self.running():
            return
        run([cxbin("wine"), "--bottle", self.name, "--no-update", "--wl-app", "wineboot.exe",
             "--", "--end-session", "--shutdown", "--force", "--kill"], timeout=120)
        deadline = time.time() + wait_s
        while time.time() < deadline and self.running():
            time.sleep(1)
        left = self.pids()
        if left:
            log(f"{self.name}: {len(left)} process(es) survived wineboot --kill; killing them")
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(2)
        if self.running():
            die(f"could not end the processes of bottle '{self.name}': {self.pids()}")

    def dead_reason(self):
        """For wait.until: a started session whose wineserver went away."""
        if self.state().get("started") and not self.pids():
            return "every process in the bottle ended (the wineserver is gone)"
        return None

    # -- launching
    def cdp_port(self):
        return CDP_PORT

    def launch(self, exe, args=(), wait=False, timeout=600, env=None, out=None):
        """Run a Windows program in the bottle with the session's log, backend
        and environment.  wait=False returns once Wine has started it; wait=True
        returns (rc, output) when it exits."""
        s = self.state()
        env = dict(s.get("env") or {}, **(env or {}))
        cmd = [cxbin("wine"), "--bottle", self.name, "--no-gui"]
        if s.get("log"):
            cmd += ["--cx-log", s["log"],
                    "--debugmsg", ",".join(x for x in (QUIET_DEBUGMSG, s.get("debugmsg")) if x)]
        if env:
            cmd += ["--env", " ".join(shlex.quote(f"{k}={v}") for k, v in env.items())]
        cmd += ["--no-wait"] if not wait else ["--wait"]
        # `-- EXE`: a Unix path anywhere (--cx-app only finds programs inside the
        # bottle's drive_c), and the program's own -options stay its own
        cmd += ["--", str(exe), *map(str, args)]
        out = Path(out) if out else self.run_dir() / f"launch-{time.strftime('%H%M%S')}.txt"
        # Output goes to a file, never a pipe: Wine's helper processes inherit it
        # and outlive this call, and a reader would wait for all of them.
        with open(out, "wb") as fh:
            fh.write((shlex.join(cmd) + "\n").encode())
            fh.flush()
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            try:
                # without --wait, wine returns once the program started (a first
                # start after a CrossOver update upgrades the bottle first)
                rc = p.wait(timeout=timeout if wait else 900)
            except subprocess.TimeoutExpired:
                p.kill()
                rc = 124
        text = out.read_text(errors="replace")
        if not wait and rc not in (0, None):
            die(f"wine did not start {exe} ({rc}): {text.strip()[-600:]}")
        return rc, text

    def launch_steam(self, args):
        if not self.state().get("started"):
            start_session(self, None, {}, None, tag="steam")
        d = self.steam_dir_unix()
        (d / ".cef-enable-remote-debugging").touch()   # DevTools on 127.0.0.1:8080
        self.launch(d / "steam.exe", args, out=self.run_dir() / "steam-launch.txt")


# ---- the GPU is shared --------------------------------------------------------------

def gpu_guard(bottle, what):
    from . import lease
    if lease._qemu_running():
        die(f"a UTM VM is running: a CrossOver bottle and a VM share the GPU (and the Steam VM "
            f"the Steam account), so {what} waits until it is down (`utmbot vm down`)")
    lease.acquire(purpose=what, vm=f"crossover:{bottle.name}")


def release_lease():
    from . import lease
    try:
        lease.release(implicit_only=True)
    except Fail:
        pass


# ---- sessions ---------------------------------------------------------------------

def _zpath(p):
    return "Z:" + str(Path(p).resolve()).replace("/", "\\")


def backend_env(backend, rd, verbose=False, hud=False):
    """The environment that selects a backend and sends its own log to the run dir."""
    env = {}
    if backend and backend != "auto":
        env["CX_GRAPHICS_BACKEND"] = backend
    logs = ensure_dir(rd / "backend-logs")
    if backend in ("dxmt", "auto", None):
        env["DXMT_LOG_PATH"] = _zpath(logs)
        if verbose:
            env["DXMT_LOG_LEVEL"] = "debug"
    if hud:
        env["MTL_HUD_ENABLED"] = "1"
    return env


def is_base(bottle):
    try:
        return bottle.name == base_name()
    except Fail:
        return bottle.name == load_meta().get("base")


def refuse_base(bottle, what):
    if is_base(bottle):
        die(f"'{bottle.name}' is the base Steam bottle: nothing runs or changes in it, because "
            f"every clone starts from it ({what} belongs in a game clone: `cx clone APPID`). "
            "`cx base refresh` is the one way to run it (Steam's sign-in or client update)")


def start_session(bottle, backend, env, debugmsg, tag="", verbose=False, hud=False, fresh=True,
                  allow_base=False):
    """A fresh Wine session in the bottle: everything in it ended, a new run
    directory, the log and backend for every program started until `cx stop`."""
    if not allow_base:
        refuse_base(bottle, f"cx {tag or 'start'}")
    gpu_guard(bottle, f"cx {tag or 'start'} {bottle.name}".strip())
    if fresh:
        bottle.kill()
    backend = backend or conf_backend(bottle.path)
    rd = bottle.new_run(tag or backend)
    e = backend_env(backend, rd, verbose, hud)
    e.update(env or {})
    info = {"bottle": bottle.name, "backend_requested": backend, "env": e, "debugmsg": debugmsg,
            "started": time.time(), "started_iso": now_iso(), "versions": versions()}
    (rd / "session.json").write_text(json.dumps(info, indent=2))
    bottle.set_state(run_dir=str(rd), log=str(rd / "wine.log"), **info)
    if bottle.is_clone():
        label(bottle.name, last_used=now_iso())
    print(f"CX SESSION {bottle.name} backend={backend} run={rd}")
    return rd


def stop_session(bottle, quiet=False):
    from . import steam
    if bottle.steam_running():
        try:
            steam.shutdown(bottle, 60)
        except Fail as e:
            log(f"Steam did not exit cleanly: {e}")
    bottle.kill()
    if bottle.state().get("started"):
        try:
            collect_logs(bottle)
        except Fail as e:
            log(f"collecting logs failed: {e}")
    bottle.clear_state()
    release_lease()
    if not quiet:
        print(f"CX STOPPED {bottle.name} (run {bottle.run_dir()})")


# ---- backend proof -------------------------------------------------------------------

_LOG_LINE = re.compile(r"^[\d.]+:([0-9a-f]{4,8}):[0-9a-f]{4,8}:(\w+):(\w+):(\S+) (.*)$")

# The comparison covers D3D11 (on d3dmetal or dxmt, as the VM ran it) and
# D3D12 (on d3dmetal: DXMT has none).  Other APIs never reach either backend:
# a D3D9 game ran on Wine's wined3d with dxmt set (a Ren'Py game).
API_OF = {"d3d9": "d3d9", "d3d10core": "d3d11", "d3d11": "d3d11", "d3d12": "d3d12",
          "d3d12core": "d3d12", "opengl32": "opengl", "vulkan-1": "vulkan"}

LIB_SIGNS = {
    "d3dmetal": ("/apple_gptk/", "D3DMetal.framework", "libd3dshared"),
    "dxmt": ("/lib/dxmt/", "winemetal.so"),
    "dxvk": ("/lib/dxvk/", "libMoltenVK"),
    "wined3d": ("wined3d.dll",),
}


def log_processes(log_path):
    """Per Wine process in a session log: its image, the backend cxcompatdb set,
    and the graphics DLLs it loaded."""
    procs = {}
    try:
        fh = open(log_path, errors="replace")
    except OSError:
        return procs
    with fh:
        for line in fh:
            if "set_graphics_backend" not in line and "Loaded L" not in line and "HACK" not in line:
                continue
            m = _LOG_LINE.match(line.rstrip("\n"))
            if not m:
                continue
            pid, _cls, chan, func, msg = m.groups()
            p = procs.setdefault(pid, {"image": None, "backend": None, "hacks": [], "dlls": []})
            if func == "set_graphics_backend":
                b = re.search(r"using (\w+) as the graphics backend", msg)
                if b:
                    p["backend"] = b[1]
                elif "unusable" in msg or "HACK" in msg:
                    p["hacks"].append(msg.strip())
            elif "HACK" in msg:
                p["hacks"].append(msg.strip())
            elif chan == "loaddll":
                d = re.search(r'Loaded L"([^"]+)"', msg)
                if not d:
                    continue
                path = d[1].replace("\\\\", "\\")
                if path.lower().endswith(".exe") and p["image"] is None:
                    p["image"] = path
                elif re.search(r"\\(d3d9|d3d10core|d3d11|d3d12|d3d12core|dxgi|winemetal|wined3d|"
                               r"vulkan-1|winevulkan|opengl32)\.dll$", path, re.I):
                    p["dlls"].append(path + msg[msg.rfind(":"):].strip().replace(":", " ", 1))
    return procs


def mapped_backends(pids):
    if not pids:
        return {}, []
    rc, out = run(["lsof", "-p", ",".join(map(str, pids)), "-Fn"], timeout=60)
    names = sorted({l[1:] for l in out.splitlines() if l.startswith("n/")})
    found = {}
    for b, signs in LIB_SIGNS.items():
        hits = [n for n in names if any(s in n for s in signs)]
        if hits:
            found[b] = [Path(h).name for h in hits][:6]
    return found, names


def proof(bottle, install_dir=None, image=None):
    """Which backend the game really runs on: cxcompatdb's choice in the log
    and the libraries mapped into its live process."""
    s = bottle.state()
    install_dir = install_dir or s.get("install_dir")
    req = s.get("backend_requested")
    procs = log_processes(s.get("log") or "")
    if install_dir:
        pre = install_dir.lower().rstrip("\\") + "\\"
        mine = {k: v for k, v in procs.items() if v["image"] and v["image"].lower().startswith(pre)}
    elif image:
        mine = {k: v for k, v in procs.items() if v["image"] and ntpath.basename(v["image"]).lower() == image.lower()}
    else:
        mine = {}
    live = [pid for pid, _ in bottle.game_pids(install_dir)] if install_dir else []
    mapped, _ = mapped_backends(live)
    chosen = sorted({v["backend"] for v in mine.values() if v["backend"]})
    apis = sorted({API_OF[ntpath.basename(d.split()[0]).lower()[:-4]] for v in mine.values()
                   for d in v["dlls"] if ntpath.basename(d.split()[0]).lower()[:-4] in API_OF})
    res = {"requested": req, "log_backend": chosen, "mapped": mapped, "apis": apis,
           "processes": mine, "live_pids": live}
    verdict = None
    if len(chosen) == 1 and (not mapped or chosen[0] in mapped):
        verdict = chosen[0]
    elif not chosen and len(mapped) == 1:
        verdict = next(iter(mapped))
    res["backend"] = verdict
    imgs = ", ".join(sorted({ntpath.basename(v["image"]) for v in mine.values()})) or "?"
    where = (f"log: {'/'.join(chosen) or 'no line'} for {imgs}; mapped: "
             + ("; ".join(f"{b} ({', '.join(f)})" for b, f in mapped.items()) or
                ("not running" if not live else "no backend library")))
    d3d = set(apis) & {"d3d11", "d3d12"}
    # The caller decided the backend; the run counts only if the game ran on
    # it, through D3D11 or D3D12, and a D3D12 game was not given DXMT.
    if not verdict:
        res["problem"] = "unproven"
        print(f"BACKEND UNPROVEN requested {req} ({where}): no comparison")
    elif verdict != req:
        res["problem"] = "mismatch"
        print(f"BACKEND MISMATCH requested {req}, runs {verdict} ({where}): a forced rule in "
              "CrossOver's compatibility database overrode the request. No comparison")
        for v in mine.values():
            for h in v["hacks"]:
                print(f"BACKEND   {h}")
    elif apis and not d3d:
        res["problem"] = "not-d3d11-12"
        print(f"BACKEND NOT COMPARABLE {verdict} is set, but the game draws through "
              f"{'/'.join(a.upper() for a in apis)}, not D3D11 or D3D12, so it renders with "
              f"{'/'.join(sorted(mapped)) or 'Wine'} ({where}). No comparison")
    elif "d3d12" in d3d and verdict == "dxmt":
        res["problem"] = "d3d12-on-dxmt"
        print(f"BACKEND WRONG FOR D3D12: the game uses D3D12, which DXMT does not implement; "
              f"compare D3D12 on d3dmetal ({where})")
    else:
        res["problem"] = None
        api = "/".join(a.upper() for a in sorted(d3d)) if d3d else "API not seen yet (`cx proof` once it draws)"
        print(f"BACKEND {verdict} {api} ({where})")
    res["respected"] = res["problem"] is None
    (bottle.run_dir() / "backend-proof.json").write_text(json.dumps(res, indent=2))
    return res


# ---- windows and captures ---------------------------------------------------------

def windows(bottle):
    """On-screen windows of the bottle's processes, at every window level: a
    frontmost full-screen Wine window sits above level 0 (26 seen)."""
    from . import capture
    pids = set(bottle.pids())
    rc, out = run([capture.tool("wininput"), "windows"], timeout=30)
    wins = []
    for line in out.splitlines():
        r = line.split("\t")
        if len(r) >= 7 and r[4].strip().isdigit() and int(r[4]) in pids:
            x, y, w, h = (int(v) for v in r[1].split(","))
            wins.append({"id": r[0], "x": x, "y": y, "w": w, "h": h, "owner": r[2], "title": r[3],
                         "pid": int(r[4]), "layer": int(r[5]), "scale": float(r[6])})
    return wins


def game_window(bottle):
    from . import capture
    s = bottle.state()
    wins = windows(bottle)
    game = {pid for pid, _ in bottle.game_pids(s["install_dir"])} if s.get("install_dir") else set()
    pick = [w for w in wins if w["pid"] in game] if game else \
        [w for w in wins if not re.match(r"(?i)steam(webhelper)?(\.exe)?$", w["owner"])]
    if not pick:
        if wins and all(not w["title"] for w in wins):
            capture.need_screen_recording()
        die("no on-screen window of the game in bottle "
            f"'{bottle.name}' ({len(wins)} window(s) of other processes in it): is it minimized, "
            "or full screen in another Space? Run games windowed or borderless (`cx windows`)")
    pick.sort(key=lambda w: -w["w"] * w["h"])
    return pick[0]


def shot(bottle, path=None):
    from . import capture
    w = game_window(bottle)
    path = Path(path) if path else bottle.run_dir() / f"shot-{time.strftime('%H%M%S')}.png"
    return capture.shot_window(w["id"], path)


def frames(bottle, secs, fps=120, save_every=0.0):
    from . import capture
    w = game_window(bottle)
    out = bottle.run_dir() / f"frames-{time.strftime('%H%M%S')}"
    return capture.frames_window(w["id"], secs, out, fps, save_every)


# ---- input ----------------------------------------------------------------------------

# macOS virtual key codes by the names `utmbot input` (the VM's) uses, plus
# the arrow and navigation keys
MAC_KEYS = {"esc": 53, "escape": 53, "backspace": 51, "tab": 48, "enter": 36, "return": 36,
            "ctrl": 59, "lshift": 56, "shift": 56, "rshift": 60, "alt": 58, "space": 49,
            "capslock": 57, "minus": 27, "equals": 24, "comma": 43, "period": 47, "slash": 44,
            "semicolon": 41, "quote": 39, "backquote": 50, "lbracket": 33, "rbracket": 30,
            "backslash": 42, "up": 126, "down": 125, "left": 123, "right": 124, "home": 115,
            "end": 119, "pageup": 116, "pagedown": 121, "delete": 117, "insert": 114,
            "kpenter": 76}
MAC_KEYS.update(zip("asdfhgzxcv", range(10)))
MAC_KEYS.update(zip("bqweryt", range(11, 18)))
MAC_KEYS.update({"1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28,
                 "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45,
                 "m": 46})
MAC_KEYS.update({f"f{i + 1}": c for i, c in enumerate((122, 120, 99, 118, 96, 97, 98, 100, 101, 109,
                                                        103, 111))})

INPUT_USAGE = """utmbot cx input <command> --bottle B     keyboard and mouse to the game's window

  key KEY [--hold S] [--times N] [--gap S]
  combo KEY+KEY[+KEY] [--hold S]          e.g. alt+enter, shift+w
  text TEXT                               US-layout ASCII, key by key
  click X Y [--button left|right|middle] [--hold S]
  move X Y                                hover (menus that highlight under the cursor)

X Y are pixels of the `cx shot` image of the window (its top-left is 0 0).
KEY is a name (esc, enter, space, tab, backspace, up, down, left, right, home,
end, pageup, pagedown, delete, w, a, s, d, f1-f12, shift, ctrl, alt, 0-9, a-z)
or vk:N, a macOS virtual key code in decimal.  The game's app is brought to
the front first: input needs the screen unlocked and the Accessibility
permission for the app utmbot runs under.
"""


def mac_key(name):
    k = name.lower()
    if k in MAC_KEYS:
        return MAC_KEYS[k]
    if k.startswith("vk:") and k[3:].isdigit() and int(k[3:]) < 128:
        return int(k[3:])
    die(f"unknown key {name!r}: use a name ({', '.join(sorted(MAC_KEYS)[:14])}, …) or vk:N for a "
        "macOS virtual key code")


def need_accessibility():
    """Stop with what to do; ask macOS once so the app shows up in the list."""
    from . import capture
    run([capture.tool("wininput"), "trusted", "--prompt"], timeout=30)
    agent = os.environ.get("GAMESDB_MODE") == "auto"
    who = ("\"UTM Bot Agent\" (the launcher the dispatched sessions run under; `utmbot` asked "
           "macOS for it at db-setup: re-run the setup with --permissions)") if agent else \
        "the terminal app this runs in"
    die("posting input needs the Accessibility permission, and this process does not have it: "
        f"turn it on for {who} in System Settings > Privacy & Security > Accessibility. Until "
        "then a game's menus cannot be driven in a bottle")


def _wininput(bottle, *cmd):
    from . import capture
    exe = capture.tool("wininput")
    rc, out = run([exe, "trusted"], timeout=30)
    if rc != 0:
        need_accessibility()
    rc, out = run([exe, "locked"], timeout=30)
    if rc == 0:
        die("the screen is locked: input to a window does not reach it, and clicks could reach "
            "the lock screen. Nothing was sent")
    rc, out = run([exe, *map(str, cmd)], timeout=120)
    if rc != 0:
        die(f"input to '{bottle.name}' failed: {out.strip()}")


def send_input(bottle, rest):
    import argparse
    sub = rest[0] if rest else ""
    if sub not in ("key", "combo", "text", "click", "move"):
        print(INPUT_USAGE)
        return 2
    p = argparse.ArgumentParser(prog=f"utmbot cx input {sub}")
    if sub == "key":
        p.add_argument("key")
        p.add_argument("--hold", type=float, default=0.15)
        p.add_argument("--times", type=int, default=1)
        p.add_argument("--gap", type=float, default=0.3)
    elif sub == "combo":
        p.add_argument("keys")
        p.add_argument("--hold", type=float, default=0.15)
    elif sub == "text":
        p.add_argument("text")
    else:
        p.add_argument("x", type=int)
        p.add_argument("y", type=int)
        if sub == "click":
            p.add_argument("--button", default="left", choices=["left", "right", "middle"])
            p.add_argument("--hold", type=float, default=0.1)
    a = p.parse_args(rest[1:])
    w = game_window(bottle)
    pid = w["pid"]
    if sub == "key":
        for i in range(a.times):
            _wininput(bottle, "key", pid, mac_key(a.key), a.hold)
            if i + 1 < a.times:
                time.sleep(a.gap)
    elif sub == "combo":
        _wininput(bottle, "key", pid, ",".join(str(mac_key(k)) for k in a.keys.split("+")), a.hold)
    elif sub == "text":
        _wininput(bottle, "text", pid, a.text)
    elif sub == "click":
        _wininput(bottle, "click", pid, w["id"], a.x, a.y, a.button, a.hold)
    else:
        _wininput(bottle, "move", pid, w["id"], a.x, a.y)
    print(f"INPUT {sub} {' '.join(rest[1:])}")
    return 0


# ---- logs --------------------------------------------------------------------------

_ERR = re.compile(r":(err|fixme):(\w+):|Unhandled exception|Unhandled page fault|"
                  r"wine: (Unhandled|Call from|could not load)|Assertion|EXCEPTION_", re.I)


def collect_logs(bottle):
    """Gather what this session left: Steam's logs and dumps, the game's own
    backend logs, macOS crash reports; summarize the Wine log."""
    s = bottle.state()
    rd = bottle.run_dir()
    t0 = s.get("started") or 0
    out = []
    sd = bottle.steam_dir_unix(required=False)
    if sd:
        for sub in ("logs", "dumps"):
            src = sd / sub
            if not src.is_dir():
                continue
            dst = ensure_dir(rd / f"steam-{sub}")
            for f in src.iterdir():
                if f.is_file() and f.stat().st_mtime >= t0 - 5:
                    shutil.copy2(f, dst / f.name)
                    out.append(dst / f.name)
    if s.get("install_dir"):
        gd = bottle.to_unix(s["install_dir"])
        if gd.is_dir():
            dst = ensure_dir(rd / "backend-logs")
            for f in list(gd.glob("*.log")) + list(gd.glob("*_d3d*.log")) + list(gd.glob("*_dxgi.log")):
                if f.is_file() and f.stat().st_mtime >= t0 - 5:
                    shutil.copy2(f, dst / f.name)
                    out.append(dst / f.name)
    reports = Path("~/Library/Logs/DiagnosticReports").expanduser()
    if reports.is_dir():
        dst = rd / "crash-reports"
        for f in reports.glob("*.ips"):
            try:
                if f.stat().st_mtime < t0:
                    continue
                head = f.read_text(errors="replace")[:20000]
            except OSError:
                continue
            if "CrossOver" in head or "wine" in head.lower():
                ensure_dir(dst)
                shutil.copy2(f, dst / f.name)
                out.append(dst / f.name)
    summary = summarize_log(s.get("log"))
    (rd / "log-summary.txt").write_text(summary)
    print(summary.rstrip())
    print(f"LOGS {rd} ({len(out)} file(s) gathered)")
    return out


def summarize_log(path, top=25):
    if not path or not Path(path).exists():
        return "WINE LOG none\n"
    counts, first, n = {}, {}, 0
    with open(path, errors="replace") as fh:
        for line in fh:
            n += 1
            if not _ERR.search(line):
                continue
            m = _LOG_LINE.match(line.rstrip("\n"))
            key = f"{m[2]}:{m[3]}:{m[4]}" if m else line.strip()[:80]
            counts[key] = counts.get(key, 0) + 1
            first.setdefault(key, line.strip()[:240])
    lines = [f"WINE LOG {path}: {n} lines, {sum(counts.values())} error/fixme/exception lines"]
    # crashes and errors first; Wine's fixme stubs are mostly noise
    def rank(kv):
        k, c = kv
        return (0 if re.search(r"(?i)unhandled|exception|page fault|assert", k + first[k])
                else 1 if ":err:" in first[k] else 2, -c)
    for k, c in sorted(counts.items(), key=rank)[:top]:
        lines.append(f"  {c:6d}  {first[k]}")
    return "\n".join(lines) + "\n"


# ---- bottles: clone, list, delete, clean up ------------------------------------------

def host_free():
    return shutil.disk_usage(BOTTLES if BOTTLES.exists() else Path.home()).free


def du_bytes(p):
    if not Path(p).exists():
        return 0
    rc, out = run(["du", "-sk", str(p)], timeout=600)
    try:
        return int(out.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def clone(appid, title=None, base=None, need_gb=None):
    name = f"{CLONE_PREFIX}{int(appid)}"
    dst = BOTTLES / name
    if dst.exists():
        print(f"BOTTLE {name} (exists)")
        return Bottle(name)
    src = Bottle(base_name(base))
    if src.running():
        die(f"the base bottle '{src.name}' is running; a clone of a live bottle is inconsistent. "
            f"`utmbot cx stop --bottle '{src.name}'` first")
    free = host_free()
    if need_gb and free < need_gb * 1.5 * GiB:
        die(f"the Mac has {free / GiB:.1f} GiB free; this game wants {need_gb:.1f} GiB (x1.5). "
            "Free space first (`utmbot cx cleanup`, /utmapp-bot:db-steam-cleanup)")
    t0 = time.time()
    rc, out = run(["cp", "-cpR", str(src.path), str(dst)], timeout=1800)
    if rc != 0:
        shutil.rmtree(dst, ignore_errors=True)
        die(f"APFS clone of '{src.name}' failed (is the volume APFS?): {out.strip()[-400:]}")
    run([cxbin("cxbottle"), "--bottle", name, "--new-uuid"], check=True, timeout=120)
    # clones stay out of the Applications menu and file associations
    conf_set(dst, "MenuMode", "ignore")
    conf_set(dst, "AssocMode", "ignore")
    for f in (dst / ".wineserver",):
        if f.exists():
            f.unlink()
    label(name, kind="game", appid=int(appid), title=title, base=src.name, created=now_iso(),
          last_used=now_iso(), source=None)
    print(f"CLONED {name} from {src.name} in {human_secs(time.time() - t0)} "
          f"(shares its blocks; costs what it installs)")
    return Bottle(name)


def listing():
    m = load_meta()
    base = None
    try:
        base = base_name()
    except Fail:
        pass
    rows = []
    for n in bottle_names():
        b = BOTTLES / n
        e = m["bottles"].get(n, {})
        kind = e.get("kind") or ("base" if n == base else "other")
        sd = steam_unix(b)
        apps = sorted(int(x.stem.split("_")[1]) for x in (sd / "steamapps").glob("appmanifest_*.acf")) \
            if sd else []
        rows.append({"name": n, "kind": kind, "appid": e.get("appid"), "title": e.get("title"),
                     "backend": conf_backend(b), "created": e.get("created"),
                     "last_used": e.get("last_used"), "source": e.get("source"),
                     "buildid": e.get("buildid"), "installed_apps": apps,
                     "steamapps_bytes": du_bytes(sd / "steamapps") if sd else 0,
                     "running": Bottle(n).running()})
    # labels of bottles deleted outside these tools
    gone = [n for n in m["bottles"] if n not in {r["name"] for r in rows}]
    if gone:
        for n in gone:
            m["bottles"].pop(n)
        save_meta(m)
    return rows


def delete(name, force=False):
    b = Bottle(name)
    try:
        base = base_name()
    except Fail:
        base = load_meta().get("base")
    if name == base:
        die(f"'{name}' is the base Steam bottle (signed in by hand); these tools never delete it")
    if not b.is_clone() and not force:
        die(f"'{name}' is not a game clone made by these tools; it is left alone "
            "(--force deletes another bottle, after the user agreed)")
    if b.running():
        stop_session(b, quiet=True)
    freed = du_bytes(b.steamapps()) if steam_unix(b.path) else 0
    before = host_free()
    run([cxbin("cxbottle"), "--bottle", name, "--delete", "--force"], check=True, timeout=600)
    if b.path.exists():
        shutil.rmtree(b.path, ignore_errors=True)
    m = load_meta()
    m["bottles"].pop(name, None)
    save_meta(m)
    print(f"DELETED {name} (its steamapps held {freed / GiB:.1f} GiB; host free "
          f"{before / GiB:.1f} -> {host_free() / GiB:.1f} GiB)")


def cleanup(older_than_days=14, free_gib=None, keep=(), dry_run=False):
    rows = [r for r in listing() if r["kind"] == "game" and not r["running"]
            and r["appid"] not in set(keep)]

    def used(r):
        t = r["last_used"] or r["created"] or "1970-01-01T00:00:00Z"
        return calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%SZ"))
    rows.sort(key=used)
    pick = []
    if free_gib:
        need = free_gib * GiB - host_free()
        for r in rows:
            if need <= 0:
                break
            pick.append(r)
            need -= r["steamapps_bytes"]
    else:
        cut = time.time() - older_than_days * 86400
        pick = [r for r in rows if used(r) < cut]
    for r in pick:
        print(f"{'WOULD DELETE' if dry_run else 'DELETE'} {r['name']} {r['title'] or ''} "
              f"last used {r['last_used']} ~{r['steamapps_bytes'] / GiB:.1f} GiB")
    if dry_run:
        return pick
    for r in pick:
        delete(r["name"])
    print(f"CLEANUP {len(pick)} bottle(s); host free {host_free() / GiB:.1f} GiB")
    return pick


def ready():
    """(ok, why): can a comparison run here, checked without starting anything?
    CrossOver installed, a base bottle with Steam in it, and Steam's remembered
    sign-in in that bottle (loginusers.vdf with RememberPassword, and
    local.vdf).  A remembered sign-in that Steam no longer accepts only shows
    when Steam starts (`cx base refresh`)."""
    if not cx_root(required=False):
        return False, f"CrossOver is not installed ({APP})"
    try:
        b = base_name()
    except Fail as e:
        return False, str(e)
    steam_dir = steam_unix(BOTTLES / b)
    users = steam_dir / "config" / "loginusers.vdf"
    remembered = users.exists() and re.search(r'"RememberPassword"\s+"1"', users.read_text(errors="replace"))
    local = list((BOTTLES / b / "drive_c" / "users").glob("*/AppData/Local/Steam/local.vdf"))
    if not remembered or not local:
        return False, (f"Steam in the bottle '{b}' has no remembered sign-in: open Steam in that bottle "
                       "with CrossOver and sign in once, with Remember me")
    return True, f"{b}: CrossOver {versions()['crossover']}, Steam installed, sign-in remembered"


def base_refresh(timeout_s=1800):
    """Run Steam in the base once: it updates the client and renews the
    remembered sign-in (a person signs in in its window when that expired).
    Clones made afterwards get the fresh sign-in; existing clones keep the
    one they were made with."""
    from . import steam
    b = Bottle(base_name())
    start_session(b, None, {}, None, tag="base-refresh", allow_base=True)
    try:
        steam.start(b, 900)
        if not steam.status(b).get("signed_in"):
            print(f"SIGN IN NEEDED: sign in, with 'Remember me', in the Steam window of the "
                  f"CrossOver bottle '{b.name}' (waiting up to {timeout_s // 60} min)")
            steam.wait_login(b, timeout_s)
        st = steam.status(b)
        print(f"BASE REFRESHED {b.name}: signed in as {st['account']}, client {st['client_version']}")
    finally:
        stop_session(b, quiet=True)


# ---- getting a game into a bottle from the VM -------------------------------------------

def import_from_vm(bottle, appid, vm_name=None, timeout_s=4 * 3600):
    """Copy a game the running Steam VM has installed (restored from its
    steam-app-<appid> snapshot and booted) into the bottle's steamapps: the
    same build the VM runs, without a download."""
    from . import remote, steam
    from .common import pick_vm
    refuse_base(bottle, "a game")
    vm = pick_vm(vm_name)
    if bottle.running():
        die(f"stop the bottle first (`cx stop --bottle {bottle.name}`): its Steam would see a "
            "half-copied game")
    st = steam.app_status(vm, appid)
    if not st["installed"] or not st["install_dir"]:
        die(f"{appid} is not fully installed in {vm.name} ({st['install_state']})")
    gdir = st["install_dir"].rstrip("\\")
    common, folder = ntpath.split(gdir)
    apps_dir = ntpath.dirname(common)
    manifest = f"{apps_dir}\\appmanifest_{int(appid)}.acf"
    rc, out = remote.ps1(vm, f"""$i = Get-ChildItem -LiteralPath '{gdir}' -Recurse -File -Force
"FILES " + $i.Count + " " + ($i | Measure-Object Length -Sum).Sum""", timeout=600)
    m = re.search(r"FILES (\d+) (\d+)", out)
    if not m:
        die(f"could not measure {gdir} in the guest: {out.strip()[-300:]}")
    nfiles, nbytes = int(m[1]), int(m[2])
    if host_free() < nbytes * 1.2 + 2 * GiB:
        die(f"the Mac has {host_free() / GiB:.1f} GiB free; the game is {nbytes / GiB:.1f} GiB. "
            "Free space first (`utmbot cx cleanup`)")
    dst_common = ensure_dir(bottle.steamapps() / "common")
    dst = dst_common / folder
    if dst.exists():
        log(f"replacing the bottle's existing {dst}")
        shutil.rmtree(dst)
    log(f"copying {nfiles} files, {nbytes / GiB:.2f} GiB from {vm.name}:{gdir}")
    remote.ensure_master(vm)
    t0 = time.time()
    ssh = subprocess.Popen(["ssh"] + remote.ssh_opts(vm) + [remote.dest(vm),
                           f'tar -cf - -C "{common}" "{folder}"'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = subprocess.Popen(["tar", "-xf", "-", "-C", str(dst_common)], stdin=ssh.stdout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    ssh.stdout.close()
    last = 0
    while tar.poll() is None:
        time.sleep(15)
        if time.time() - last > 60:
            have = du_bytes(dst)
            log(f"{appid}: {have / GiB:.2f}/{nbytes / GiB:.2f} GiB "
                f"({100 * have // max(nbytes, 1)}%) in {human_secs(time.time() - t0)}")
            last = time.time()
        if time.time() - t0 > timeout_s:
            ssh.kill()
            tar.kill()
            die(f"copy did not finish in {timeout_s}s")
    ssh.wait(60)
    if tar.returncode != 0 or ssh.returncode != 0:
        die(f"copy failed (ssh {ssh.returncode}, tar {tar.returncode}): "
            f"{(ssh.stderr.read() + tar.stderr.read()).decode(errors='replace')[-500:]}")
    got_files = got_bytes = 0
    for root, _, files in os.walk(dst):
        for f in files:
            got_files += 1
            got_bytes += os.lstat(os.path.join(root, f)).st_size
    if (got_files, got_bytes) != (nfiles, nbytes):
        die(f"copy incomplete: {got_files}/{nfiles} files, {got_bytes}/{nbytes} bytes")
    rc, text = remote.ssh(vm, f'type "{manifest}"', timeout=60)
    if rc != 0 or '"AppState"' not in text:
        die(f"could not read {manifest} in the guest: {text.strip()[-300:]}")
    local_manifest = bottle.steamapps() / f"appmanifest_{int(appid)}.acf"
    local_manifest.write_text(text)
    b = re.search(r'"buildid"\s+"(\d+)"', text)
    label(bottle.name, source=f"vm:{vm.name}", buildid=b[1] if b else None,
          imported=now_iso(), appid=int(appid), title=st["name"])
    print(f"IMPORTED {appid} {st['name']} {nbytes} bytes, {nfiles} files, build "
          f"{b[1] if b else '?'} in {human_secs(time.time() - t0)}")


def manifest_build(bottle, appid):
    f = bottle.steamapps() / f"appmanifest_{int(appid)}.acf"
    try:
        b = re.search(r'"buildid"\s+"(\d+)"', f.read_text(errors="replace"))
        return b[1] if b else None
    except OSError:
        return None


# ---- running programs and games ---------------------------------------------------------

def run_game(bottle, appid, backend, env, debugmsg, wait_s, args, tag, verbose, hud):
    from . import steam
    start_session(bottle, backend, env, debugmsg, tag=tag or f"{appid}-{backend or 'conf'}",
                  verbose=verbose, hud=hud)
    steam.start(bottle, 600)
    st = steam.status(bottle)
    if not st.get("signed_in"):
        die(f"Steam in '{bottle.name}' is signed out: someone signs in once in its window "
            "(then `utmbot cx steam --bottle B wait-login`); a clone of a signed-out base is "
            "signed out too")
    a = steam.app_status(bottle, appid)
    if not a["installed"]:
        die(f"{appid} is not installed in '{bottle.name}' ({a['install_state']}): "
            "`utmbot cx import` from the VM, or `utmbot cx steam --bottle B install`")
    bottle.set_state(appid=int(appid), install_dir=a["install_dir"], buildid=manifest_build(bottle, appid))
    steam.run_game(bottle, appid, wait_s, args)
    time.sleep(10)   # past the first device creation
    res = proof(bottle)
    if bottle.is_clone():
        label(bottle.name, last_used=now_iso())
    return res


def exec_program(bottle, exe, args, timeout):
    """Run a Windows program (a probe) in the session and wait for it."""
    exe = Path(exe)
    if not exe.exists():
        die(f"{exe} does not exist")
    rc, out = bottle.launch(exe, args, wait=True, timeout=timeout,
                            out=bottle.run_dir() / f"{exe.stem}-{time.strftime('%H%M%S')}.txt")
    print(out.split("\n", 1)[1].rstrip() if "\n" in out else out)
    print(f"EXIT {rc}")
    return rc


def verify(bottle, backend, env, probe=None):
    """CrossOver's twin of `utmbot verify`: which adapter and backend a D3D
    program in this bottle gets."""
    exe = Path(probe) if probe else DATA / "probes" / "x64" / "adaptercheck.exe"
    if not exe.exists():
        die(f"{exe} is missing: CrossOver runs x64 programs (not ARM64); build the x64 probes "
            "(/utmapp-bot:write-gpu-probe, scripts/build-probes.sh)")
    start_session(bottle, backend, env, None, tag=f"verify-{backend or 'conf'}")
    try:
        rc, out = bottle.launch(exe, [], wait=True, timeout=180,
                                out=bottle.run_dir() / f"{exe.stem}.txt")
        res = proof(bottle, image=exe.name)
        adapters = re.findall(r"^(adapter \d+: .*|wine: .*using.*|D3D1[12] FL .*)$", out, re.M)
        for a in adapters:
            print(a)
        # A probe built before it knew CrossOver (which hides wine_get_version)
        # stops after listing the adapters, without creating a device.
        devices = "D3D11 FL" in out
        ok = res["backend"] == backend and any(a.startswith("adapter ") for a in adapters) and \
            (not devices or "ADAPTERCHECK PASS" in out)
        print(f"VERIFY {'PASS' if ok else 'FAIL'} backend={res['backend'] or 'unproven'} "
              f"(requested {res['requested']})" + ("" if devices else
              "; adapters only: this adaptercheck build creates no device under CrossOver "
              "(rebuild the x64 probes)"))
        return ok
    finally:
        stop_session(bottle, quiet=True)


# ---- command line --------------------------------------------------------------------

USAGE = """utmbot cx <command>          CrossOver on this Mac (skills/crossover-compare)

  info                        INSTALLED <version> | NOT INSTALLED; backends, base bottle
  ready                       READY | NOT READY: <what is missing>: CrossOver, a base
                              bottle with Steam, its remembered sign-in (starts nothing)
  list [--json]               bottles: kind (base/game/other), game, default backend,
                              steamapps size, last used, running; the Mac's free space
  base [--set NAME]           the signed-in Steam bottle that clones are made from
                              (never run or changed by the other commands)
  base refresh [--timeout S]  run Steam in the base once: client update, renewed sign-in
                              (a person signs in in its window when it expired)
  clone APPID [--title T] [--base B] [--need-gb G]
                              steam-app-<appid>: an APFS clone of the base (seconds,
                              costs what it installs); out of CrossOver's menus
  delete NAME [--force]       a clone (cxbottle --delete); --force: any other bottle
  cleanup [--older-than D] [--free GiB] [--keep APPID ...] [--dry-run]
                              delete clones not used lately, least recently used first
  backend BOTTLE [auto|d3dmetal|dxmt]
                              show or set the bottle's default backend (cxbottle.conf)

  start --bottle B [--backend d3dmetal|dxmt] [--env K=V ...] [--debugmsg CH] [--verbose] [--hud] [--tag T]
                              a fresh session: the bottle's processes ended, a new run
                              dir, the Wine log (+loaddll, +process ...) and backend
                              for everything started in it; takes the UTM lease
  run APPID --bottle B --backend d3dmetal|dxmt [--env K=V ...] [--args ARGS] [--wait S]
          [--debugmsg CH] [--verbose] [--hud] [--tag T]
                              start + Steam + its EULAs + the game through the client;
                              the backend is the caller's decision (D3D11: the VM's,
                              D3D12: d3dmetal). Exit 0 only when the game provably runs
                              on it through D3D11/D3D12 (BACKEND line); 2 otherwise
  proof --bottle B            BACKEND <x> <API> | MISMATCH | NOT COMPARABLE | WRONG FOR
                              D3D12 | UNPROVEN, from the log and the game's mapped libraries
  exec --bottle B EXE [ARGS ...] [--timeout S]
                              run a Windows program (an x64 probe) in the session, wait
  verify --bottle B --backend d3dmetal|dxmt [--env K=V ...] [--probe EXE]
                              adaptercheck (x64) in a fresh session: adapter + backend
  steam --bottle B <steam command>
                              the `utmbot steam` commands against the bottle's client
                              (status, owned, app-status, license, install, run,
                              stop-game, console, targets, js, wait-login, stop)
  import APPID --bottle B [--vm NAME]
                              copy the game the running Steam VM has installed into the
                              bottle (same build, no download); bottle stopped
  windows --bottle B          on-screen windows of the bottle's processes (every level)
  shot --bottle B [PATH]      the game's window (Screen Recording permission)
  input --bottle B key KEY [--hold S] [--times N] | combo KEY+KEY | text TEXT |
          click X Y [--button B] | move X Y
                              keyboard and mouse to the game's window, like the VM's
                              `utmbot input` plus arrow keys; X Y are pixels of the
                              `cx shot` image. Brings the game to the front; needs
                              Accessibility and an unlocked screen (`cx input` alone: help)
  frames SECS --bottle B [--fps N] [--save-every S]
                              distinct fps, holds, black and tinted frames of its window
  logs --bottle B             gather Steam logs and dumps, backend logs, crash reports
                              into the run dir; summarize the Wine log
  ps [--bottle B]             processes of a bottle (all bottles: which ones run)
  stop --bottle B             quit the game and Steam, end the bottle, gather the logs,
                              give the lease back
"""


def main(rest):
    import argparse
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub, args = rest[0], rest[1:]

    if sub == "info":
        r = cx_root(required=False)
        if not r:
            print(f"NOT INSTALLED ({APP})")
            return 1
        v = versions()
        print(f"INSTALLED CrossOver {v['crossover']} ({APP})")
        print(f"backends  d3dmetal {v['d3dmetal'] or 'MISSING'} (D3D11, D3D12), "
              f"dxmt {'yes' if v['dxmt'] else 'MISSING'} (D3D11)")
        print(f"bottles   {BOTTLES} ({len(bottle_names())})")
        try:
            print(f"base      {base_name()}")
        except Fail as e:
            print(f"base      none: {e}")
        print(f"free      {host_free() / GiB:.1f} GiB on the Mac")
        return 0

    if sub == "ready":
        ok, why = ready()
        print(("READY " if ok else "NOT READY: ") + why)
        return 0 if ok else 1

    if sub == "list":
        rows = listing()
        if "--json" in args:
            print(json.dumps({"bottles": rows, "host_free_bytes": host_free()}, indent=2))
            return 0
        for r in rows:
            print(f"{r['name']:24s} {r['kind']:5s} {str(r['appid'] or ''):>8s} {r['backend']:8s} "
                  f"{r['steamapps_bytes'] / GiB:6.1f} GiB  last {r['last_used'] or '-':20s} "
                  f"{'RUNNING ' if r['running'] else ''}{r['title'] or ''}")
        print(f"host free {host_free() / GiB:.1f} GiB")
        return 0

    if sub == "base":
        p = argparse.ArgumentParser(prog="utmbot cx base")
        p.add_argument("action", nargs="?", choices=["refresh"])
        p.add_argument("--set")
        p.add_argument("--timeout", type=int, default=1800)
        a = p.parse_args(args)
        if a.action == "refresh":
            base_refresh(a.timeout)
            return 0
        if a.set:
            base_name(a.set)
            m = load_meta()
            m["base"] = a.set
            save_meta(m)
        print(f"BASE {base_name()}")
        return 0

    if sub == "clone":
        p = argparse.ArgumentParser(prog="utmbot cx clone")
        p.add_argument("appid", type=int)
        p.add_argument("--title")
        p.add_argument("--base")
        p.add_argument("--need-gb", type=float)
        a = p.parse_args(args)
        clone(a.appid, a.title, a.base, a.need_gb)
        return 0

    if sub == "delete":
        p = argparse.ArgumentParser(prog="utmbot cx delete")
        p.add_argument("name")
        p.add_argument("--force", action="store_true")
        a = p.parse_args(args)
        delete(a.name, a.force)
        return 0

    if sub == "cleanup":
        p = argparse.ArgumentParser(prog="utmbot cx cleanup")
        p.add_argument("--older-than", type=float, default=14)
        p.add_argument("--free", type=float)
        p.add_argument("--keep", type=int, action="append", default=[])
        p.add_argument("--dry-run", action="store_true")
        a = p.parse_args(args)
        cleanup(a.older_than, a.free, a.keep, a.dry_run)
        return 0

    if sub == "backend":
        p = argparse.ArgumentParser(prog="utmbot cx backend")
        p.add_argument("bottle")
        p.add_argument("value", nargs="?", choices=BACKENDS)
        a = p.parse_args(args)
        b = Bottle(a.bottle)
        if a.value:
            refuse_base(b, "a backend setting")
            if b.running():
                die("the bottle is running; its processes keep the backend they started with "
                    f"(`cx stop --bottle {b.name}` first)")
            conf_set_backend(b.path, a.value)
        print(f"BACKEND DEFAULT {b.name} {conf_backend(b.path)}")
        return 0

    if sub == "ps":
        p = argparse.ArgumentParser(prog="utmbot cx ps")
        p.add_argument("--bottle")
        a = p.parse_args(args)
        names = [a.bottle] if a.bottle else bottle_names()
        for n in names:
            procs = Bottle(n).processes()
            if procs or a.bottle:
                print(f"{n}: {len(procs)} process(es)")
                for pid, cmd in procs:
                    print(f"  {pid:7d} {cmd[:150]}")
        return 0

    # everything below works on one bottle
    p = argparse.ArgumentParser(prog=f"utmbot cx {sub}")
    p.add_argument("--bottle", required=True)
    if sub in ("start", "run", "verify"):
        # run/verify: the caller's decision (D3D11: the VM's; D3D12: d3dmetal).
        # start without it: sessions that run no game (installs).
        p.add_argument("--backend", choices=COMPARE, required=sub != "start")
        p.add_argument("--env", action="append")
    if sub in ("start", "run"):
        p.add_argument("--debugmsg")
        p.add_argument("--verbose", action="store_true")
        p.add_argument("--hud", action="store_true")
        p.add_argument("--tag", default="")
    if sub == "run":
        p.add_argument("appid", type=int)
        p.add_argument("--args", default="")
        p.add_argument("--wait", type=int, default=900)
    if sub == "verify":
        p.add_argument("--probe")
    if sub == "import":
        p.add_argument("appid", type=int)
        p.add_argument("--vm")
    if sub == "exec":
        p.add_argument("--timeout", type=int, default=600)
        p.add_argument("exe")
    if sub == "shot":
        p.add_argument("path", nargs="?")
    if sub == "frames":
        p.add_argument("secs", type=float)
        p.add_argument("--fps", type=int, default=120)
        p.add_argument("--save-every", type=float, default=0.0)
    if sub in ("steam", "input"):
        a, steam_args = p.parse_known_args(args)
    elif sub == "exec":
        a, exe_args = p.parse_known_args(args)
    else:
        a = p.parse_args(args)
    b = Bottle(a.bottle)

    if sub == "start":
        start_session(b, a.backend, kv_list(a.env), a.debugmsg, a.tag, a.verbose, a.hud)
    elif sub == "run":
        res = run_game(b, a.appid, a.backend, kv_list(a.env), a.debugmsg, a.wait, a.args, a.tag,
                       a.verbose, a.hud)
        return 0 if res["respected"] else 2
    elif sub == "proof":
        r = proof(b)
        return 0 if r["respected"] else 2
    elif sub == "exec":
        if not b.state().get("started"):
            die(f"no session in '{b.name}': `utmbot cx start --bottle {b.name} --backend X` first")
        gpu_guard(b, f"cx exec {b.name}")
        return exec_program(b, a.exe, exe_args, a.timeout)
    elif sub == "verify":
        return 0 if verify(b, a.backend, kv_list(a.env), a.probe) else 1
    elif sub == "steam":
        from . import steam
        if steam_args[:1] in (["setup"], ["configure"]):
            die(f"`steam {steam_args[0]}` is for the Steam VM")
        if steam_args[:1] == ["start"]:
            gpu_guard(b, f"cx steam start {b.name}")
        return steam.main(b, steam_args)
    elif sub == "input":
        return send_input(b, steam_args)
    elif sub == "import":
        import_from_vm(b, a.appid, a.vm)
    elif sub == "windows":
        for w in windows(b):
            print(f"{w['id']:>8s} pid {w['pid']:<7d} {w['w']}x{w['h']}+{w['x']}+{w['y']} "
                  f"(shot {int(w['w'] * w['scale'])}x{int(w['h'] * w['scale'])} px"
                  f"{', level ' + str(w['layer']) if w['layer'] else ''}) {w['owner']}: {w['title']}")
    elif sub == "shot":
        shot(b, a.path)
    elif sub == "frames":
        frames(b, a.secs, a.fps, a.save_every)
    elif sub == "logs":
        collect_logs(b)
    elif sub == "stop":
        stop_session(b)
    else:
        die(USAGE)
    return 0
