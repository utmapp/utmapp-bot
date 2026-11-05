"""Shared plumbing: data dir, VM state, logging, subprocesses, sessions."""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
SCRIPTS = PLUGIN / "scripts"
UTM_SH = SCRIPTS / "lib" / "utm.sh"
DATA = Path(os.environ.get("UTMAPP_BOT_DATA", "~/.utmapp-bot")).expanduser()
UTM_DOMAIN = "com.utmapp.UTM"


class Fail(Exception):
    """A clean, reportable failure (printed without a traceback)."""


def die(msg):
    raise Fail(msg)


def log(msg):
    print(f"=== {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def run(cmd, timeout=None, check=False, input=None, env=None):
    """Run argv; return (rc, stdout+stderr as text).  Never raises on rc."""
    try:
        # stdin is never the caller's: ssh would otherwise consume it
        p = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout, env=env,
                           stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") + (e.stderr or b"")
        return 124, out.decode("utf-8", "replace")
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    if check and p.returncode != 0:
        die(f"{shlex.join(cmd)} failed ({p.returncode}): {out.strip()[-800:]}")
    return p.returncode, out


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def now_iso():
    """UTC, as every sidecar and log record here writes times."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def kv_list(items, what="NAME=value"):
    """["K=V", ...] (repeated --env/--genv/--set options) -> {K: V}."""
    out = {}
    for it in items or []:
        if "=" not in it:
            die(f"expected {what}, got {it!r}")
        k, v = it.split("=", 1)
        out[k] = v
    return out


def ensure_dir(p):
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path, default=None):
    """A JSON state file, or `default` when it is missing or unreadable."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data, sort_keys=False):
    """Replace a JSON state file atomically: a reader in another session sees
    the old file or the new one, never half of one."""
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=sort_keys) + "\n")
    tmp.replace(path)


def runs_root():
    root = ensure_dir(DATA / "runs")
    # Run logs must survive a host panic and should not be indexed (Spotlight
    # indexing a busy log directory has been seen to steal enough CPU to
    # distort measurements).
    marker = root / ".metadata_never_index"
    if not marker.exists():
        marker.touch()
    return root


def parse_env_file(path):
    """Parse a state.env written with `printf %q` quoting."""
    vals = {}
    for line in Path(path).read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split("=", 1)
        try:
            parts = shlex.split(v)
        except ValueError:
            parts = [v]
        vals[k.strip()] = parts[0] if parts else ""
    return vals


class VM:
    """A VM the plugin knows: provisioned, or registered with `vm register`."""

    def __init__(self, name):
        self.name = name
        self.slug = slug(name)
        self.state_dir = DATA / "provision" / self.slug
        env_file = self.state_dir / "state.env"
        if not env_file.exists():
            die(f"'{name}' is not a VM this plugin knows; provision one, or register an "
                f"existing VM with: utmbot vm register '{name}' --user USER [--os linux]")
        self.state = parse_env_file(env_file)
        self.role = self.state.get("S_ROLE", "test")
        self.user = self.state.get("S_USER", "UTM")
        self.mac = self.state.get("MAC", "")
        self.pubkey = self.state.get("S_PUBKEY", "")
        self.os = self.state.get("S_OS", "windows")
        self.debugger = self.state.get("S_DEBUGGER", "")
        self.kd_port = self.state.get("KD_PORT", "")
        self.kdnet_ip = self.state.get("KDNET_IP", "")
        self.known_hosts = self.state_dir / "known_hosts"
        self._ip = None
        self._bundle = None

    @property
    def is_windows(self):
        return self.os == "windows"

    def key(self):
        k = self.pubkey[:-4] if self.pubkey.endswith(".pub") else self.pubkey
        return k if k and Path(k).exists() else None

    def ssh_port(self):
        """The forwarded SSH port on 127.0.0.1 when the first NIC is emulated:
        provisioned that way (SSH_PORT, a UTM without vmnet), or switched to it
        for this VM lifetime by `vm up` (the session's ssh_port).  None means
        vmnet, where the guest is reached at its DHCP address on port 22."""
        p = Session(self).get("ssh_port") or self.state.get("SSH_PORT")
        return int(p) if p else None

    def host(self, refresh=False):
        """The address SSH connects to (with ssh_port(), the host itself)."""
        if self.ssh_port():
            return "127.0.0.1"
        if self._ip is None or refresh:
            rc, out = run(["bash", str(UTM_SH), "ip", self.mac])
            self._ip = out.strip() or None
        return self._ip

    def ssh_dest(self):
        """user@host[:port], for messages."""
        h, p = self.host(), self.ssh_port()
        return f"{self.user}@{h or '?'}" + (f" port {p}" if p else "")

    def bundle(self):
        """The VM's .utm bundle: UTM's answer while it runs, recorded in the
        VM's state dir for when it does not (its logs after a UTM crash)."""
        if self._bundle is None:
            from .utm import vm_file
            rec = self.state_dir / "bundle"
            b = vm_file(self.name)
            if b:
                if not rec.exists() or rec.read_text().strip() != str(b):
                    rec.write_text(f"{b}\n")
            elif rec.exists() and Path(rec.read_text().strip()).exists():
                b = Path(rec.read_text().strip())
            self._bundle = b
        return self._bundle

    def debug_log(self):
        b = self.bundle()
        return b / "Data" / "debug.log" if b else None

    def session_dir(self):
        return ensure_dir(runs_root() / self.slug)


def pick_vm(name=None, os_kind=None):
    """--vm, $UTMBOT_VM, this Mac's test VM (`utmbot machine`), or the only
    provisioned test VM."""
    name = name or os.environ.get("UTMBOT_VM")
    if name:
        return VM(name)
    if os_kind in (None, "windows"):
        from . import machines
        try:
            return VM(machines.get("test_vm"))
        except machines.Missing:
            # a db skill's `UTMBOT_VM="$(utmbot machine get test_vm)"` that
            # failed, or a dispatched session: never fall back to another VM
            if "UTMBOT_VM" in os.environ or os.environ.get("GAMESDB_MODE") == "auto":
                raise
    cands = []
    for f in sorted((DATA / "provision").glob("*/state.env")):
        st = parse_env_file(f)
        if st.get("S_ROLE", "test") in ("test", "linux") and \
                (os_kind is None or st.get("S_OS", "windows") == os_kind):
            cands.append(st.get("NAME", f.parent.name))
    if len(cands) == 1:
        return VM(cands[0])
    if not cands:
        die("no test VM found; provision one (provision-windows-test-vm) or pass --vm")
    die("several test VMs: pass --vm NAME or set UTMBOT_VM (" + ", ".join(cands) + ")")


# ---- run directories and sessions ------------------------------------------

def new_run_dir(parent, tag=""):
    """A new timestamped run directory under parent, with parent/latest
    pointing at it."""
    name = time.strftime("%Y%m%d-%H%M%S") + (f"-{slug(tag)}" if tag else "")
    d = ensure_dir(Path(parent) / name)
    latest = Path(parent) / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(d.name)
    return d


def current_run_dir(parent, recorded):
    """The recorded run directory while it exists; else a scratch one for the
    day, so commands outside a session still have somewhere to write."""
    if recorded and Path(recorded).exists():
        return Path(recorded)
    return ensure_dir(Path(parent) / time.strftime("adhoc-%Y%m%d"))


class Session:
    """One per VM lifetime (vm up .. vm down): its run directory and state."""

    def __init__(self, vm):
        self.vm = vm
        self.path = vm.session_dir() / "session.json"
        self.data = read_json(self.path, {})

    def save(self):
        write_json(self.path, self.data)

    def get(self, k, default=None):
        return self.data.get(k, default)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()

    def run_dir(self):
        return current_run_dir(self.vm.session_dir(), self.data.get("run_dir"))

    def new_run(self, tag=""):
        d = new_run_dir(self.vm.session_dir(), tag)
        self.data = {"run_dir": str(d), "started": time.time()}
        self.save()
        return d

    def clear(self):
        # an emulated-network switch that could not be undone stays recorded
        keep = {k: self.data[k] for k in ("run_dir", "net_prev", "ssh_port") if k in self.data}
        self.data = keep
        self.save()


def probe(name):
    """A built probe: the native ARM64 build, else the x64 one (emulated)."""
    for arch in ("ARM64", "x64"):
        p = DATA / "probes" / arch / name
        if p.exists():
            if arch == "x64":
                log(f"using the x64 {name} (runs emulated; build probes on a dev VM for a "
                    "native arm64 one)")
            return p
    return None


def write_text(path, text):
    Path(path).write_text(text, newline="")


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def human_secs(s):
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
