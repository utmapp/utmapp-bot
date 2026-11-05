"""Keep the SSH forward to the games database's internal API up.

usage: python3 tunneld.py run | once

Run by the LaunchAgent that db-setup installs (KeepAlive, so it is up for as
long as the user is logged in).  It runs

    ssh -N -L 127.0.0.1:<port>:<socket> _triage@host

in the foreground and watches two things:

- ssh exits (network gone, host rebooting for an update, the Mac woke from
  sleep and the connection is dead after ServerAlive gave up): reconnect,
  with a cooldown that grows from 5 s to 5 min and resets once a connection
  has held for 2 minutes;
- ssh runs but /internal/healthz does not answer (the server is restarting,
  or the socket went away): keep the connection and wait; after 5 minutes of
  that, drop it and reconnect, in case the forward itself is stale.

The account on the server can only forward (no shell), so there is nothing
else to run there.  State goes to ~/.utmapp-bot/gamesdb/tunnel.status.json
(`gamesdb tunnel status` shows it) and the log to
~/.utmapp-bot/gamesdb/logs/tunnel.log.  One instance runs at a time.

`once` connects, checks healthz and exits: a test for setup.
"""

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gamesdb import tunnel  # noqa: E402

BACKOFF_MIN, BACKOFF_MAX = 5, 300
STABLE_AFTER = 120          # a connection this old resets the cooldown
CHECK_EVERY = 15            # watch ssh and the port this often
HEALTH_EVERY = 60           # ask the API this often
UNHEALTHY_RESTART = 300     # ssh up but no API this long: reconnect

LOG = tunnel.STATE / "logs" / "tunnel.log"
_child = None
_status = {}


def log(msg):
    tunnel.append_log(LOG, msg)


def set_status(state, **kw):
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if _status.get("state") != state:
        _status["since"] = now
        log(f"state {state}" + (f": {kw.get('last_error')}" if kw.get("last_error") else ""))
    _status.update(state=state, updated=now, pid=os.getpid(), **kw)
    for k in [k for k, v in _status.items() if v is None]:
        _status.pop(k)
    p = tunnel.STATUS_FILE
    tunnel.state_dir()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(_status, indent=2))
    tmp.replace(p)


def port_in_use(port):
    """Something listens there (a connect, not a bind: a bind fails for a while
    after our own connections close, while ssh's listener reuses the port)."""
    s = socket.socket()
    s.settimeout(2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def ssh_cmd(cfg):
    port = tunnel.local_port(cfg)
    sock = cfg.get("GAMESDB_SOCKET") or tunnel.DEFAULT_SOCKET
    return ["ssh", "-N", "-T",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-L", f"127.0.0.1:{port}:{sock}", cfg["GAMESDB_SSH"]]


def stop_child():
    global _child
    if _child and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(10)
        except subprocess.TimeoutExpired:
            _child.kill()
    _child = None


def connect(cfg, errf):
    """Start ssh; return (process, None) once the API answers through it, or
    (None, error)."""
    global _child
    port = tunnel.local_port(cfg)
    url = f"http://127.0.0.1:{port}"
    if port_in_use(port):
        if tunnel.healthy(url):
            return None, (f"127.0.0.1:{port} already answers, but not through this agent "
                          "(an old tunnel?); stop it, the agent takes over")
        return None, f"127.0.0.1:{port} is taken by another program"
    errf.seek(0)
    errf.truncate()
    _child = subprocess.Popen(ssh_cmd(cfg), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=errf, start_new_session=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        if _child.poll() is not None:
            errf.flush()
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace").strip().splitlines()
            return None, f"ssh exited ({_child.returncode}): {(err[-1] if err else 'no message')[:300]}"
        if tunnel.healthy(url, timeout=5):
            return _child, None
        time.sleep(1)
    # ssh is up but the API does not answer: still a connection; the watch
    # loop decides what to do with it
    return _child, "connected, but the internal API does not answer yet"


def run(once=False):
    lockf = open(tunnel.state_dir() / "tunneld.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another tunnel agent is running", file=sys.stderr)
        return 0 if not once else 1

    def bye(*_):
        stop_child()
        set_status("stopped")
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    errf = open(tunnel.state_dir() / "tunnel.ssh-stderr", "w+b")
    backoff, attempts = BACKOFF_MIN, 0
    while True:
        cfg = tunnel.config()
        if not cfg.get("GAMESDB_SSH"):
            set_status("unconfigured", last_error=f"no GAMESDB_SSH in {tunnel.CONFIG_FILE}")
            if once:
                return 1
            time.sleep(60)
            continue
        attempts += 1
        set_status("connecting", attempts=attempts, next_try=None)
        proc, err = connect(cfg, errf)
        if proc is None:
            set_status("waiting", last_error=err, attempts=attempts,
                       next_try=time.strftime("%H:%M:%S", time.localtime(time.time() + backoff)))
            if once:
                return 1
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        port = tunnel.local_port(cfg)
        url = f"http://127.0.0.1:{port}"
        up_since = time.time()
        last_health, unhealthy_since = 0, (time.time() if err else None)
        set_status("connected" if not err else "server-unreachable", last_error=err,
                   attempts=attempts, ssh_pid=proc.pid)
        if once:
            ok = not err
            stop_child()
            set_status("tested", last_error=err)
            return 0 if ok else 1
        while proc.poll() is None:
            time.sleep(CHECK_EVERY)
            if proc.poll() is not None:
                break
            if time.time() - last_health >= HEALTH_EVERY:
                last_health = time.time()
                if tunnel.healthy(url, timeout=10):
                    if unhealthy_since:
                        unhealthy_since = None
                        set_status("connected", last_error=None, ssh_pid=proc.pid)
                else:
                    unhealthy_since = unhealthy_since or time.time()
                    set_status("server-unreachable", ssh_pid=proc.pid,
                               last_error="ssh is connected but the internal API does not answer "
                                          "(server restarting?)")
                    if time.time() - unhealthy_since > UNHEALTHY_RESTART:
                        log("no API for 5 minutes through a live connection; reconnecting")
                        stop_child()
                        break
            if time.time() - up_since > STABLE_AFTER and not unhealthy_since:
                backoff, attempts = BACKOFF_MIN, 0
        errf.flush()
        errf.seek(0)
        tail = errf.read().decode("utf-8", "replace").strip().splitlines()
        set_status("waiting", last_error=f"connection ended: {(tail[-1] if tail else 'ssh exited')[:300]}",
                   ssh_pid=None,
                   next_try=time.strftime("%H:%M:%S", time.localtime(time.time() + backoff)))
        time.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd not in ("run", "once"):
        print(__doc__)
        sys.exit(2)
    sys.exit(run(once=(cmd == "once")))
