"""One user of UTM at a time, across every session on this Mac (and other
named leases: `--name moderation` keeps two moderation sessions apart).

Running a VM takes the GPU, UTM itself (one app, scripted over AppleEvents)
and, for the test VMs, their disks and snapshots.  Two Claude sessions
driving them at once wreck each other's work, so the harness keeps a lease:
~/.utmapp-bot/locks/utm.lease.

- Owner: the Claude Code session (CLAUDE_CODE_SESSION_ID, alive while
  CLAUDE_PID runs), or UTMBOT_OWNER, or the terminal (`tty:<name>`).
- `vm up` takes it (implicitly; `vm down` / `vm shutdown` give it back).
  A skill that needs UTM across several boots (restore, boot, shut down,
  snapshot) takes it explicitly with `lock acquire`, and only `lock
  release` gives that back.
- Every command that touches a VM, its guest or its snapshots needs the
  lease to be free or its own; otherwise it stops and says who has it.
- A lease whose owner is gone (its Claude process ended), or that nobody
  touched for LEASE_IDLE with no VM running, is taken over.
- CrossOver bottles take the GPU too: `utmbot cx` commands that run Windows
  programs take the same lease (vm "crossover:<bottle>"), and refuse while
  a VM runs; `vm up` refuses while a bottle runs.
- It records the holder's mode: `auto` for sessions the games-db dispatcher
  started (GAMESDB_MODE=auto, with the job in GAMESDB_DISPATCH_JOB), else
  `interactive`.  An interactive session that finds auto work holding UTM
  tells the user and waits; the dispatcher starts no UTM work while anyone
  else holds it.
"""

import fcntl
import os
import subprocess
import time
from contextlib import contextmanager

from .common import DATA, die, ensure_dir, log, pid_alive, read_json, write_json

LEASE_IDLE = 30 * 60


NAME = "utm"


def _paths(name=None):
    d = ensure_dir(DATA / "locks")
    n = name or NAME
    return d / f"{n}.lease", d / f"{n}.lock"


@contextmanager
def _locked():
    _, lockp = _paths()
    with open(lockp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def me():
    mode = "auto" if os.environ.get("GAMESDB_MODE") == "auto" else "interactive"
    extra = {"mode": mode, "job": os.environ.get("GAMESDB_DISPATCH_JOB") or None}
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return {"owner": f"session:{sid}", "pid": int(os.environ.get("CLAUDE_PID") or 0) or None, **extra}
    if os.environ.get("UTMBOT_OWNER"):
        return {"owner": os.environ["UTMBOT_OWNER"], "pid": None, **extra}
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = f"ppid{os.getppid()}"
    return {"owner": f"tty:{tty}", "pid": None, **extra}


def _qemu_running():
    r = subprocess.run(["pgrep", "-f", "qemu-system|QEMULauncher"], capture_output=True)
    return r.returncode == 0


def _crossover_running():
    """A CrossOver bottle has processes (its wineserver runs).  It takes the
    GPU like a VM does, so it keeps the lease from going stale too."""
    r = subprocess.run(["pgrep", "-f", "CrossOver.app/.*/wineserver"], capture_output=True)
    return r.returncode == 0


def read():
    return read_json(_paths()[0])


def _write(d):
    write_json(_paths()[0], d)


def stale(lease):
    """Why a lease may be taken over, or None while it holds."""
    if not lease:
        return "free"
    # None: no owner process to watch (a terminal), so only idleness ends it
    alive = pid_alive(lease["pid"]) if lease.get("pid") else None
    if alive is False:
        return f"its owner's process {lease['pid']} has ended"
    idle = time.time() - lease.get("touched", 0)
    if alive is None and idle > LEASE_IDLE and not _qemu_running() and not _crossover_running():
        return f"untouched for {int(idle // 60)} min and no VM or CrossOver bottle runs"
    return None


def describe(lease):
    if not lease:
        return "free"
    age = int((time.time() - lease.get("since", time.time())) // 60)
    who = (f"auto-triage job {lease['job']}" if lease.get("mode") == "auto" and lease.get("job")
           else "an auto-triage session" if lease.get("mode") == "auto" else "an interactive session")
    return (f"held by {who} ({lease['owner']}) for {age} min ({lease.get('purpose') or 'no purpose given'}"
            f"{', VM ' + lease['vm'] if lease.get('vm') else ''}"
            f"{', explicit' if lease.get('explicit') else ''})")


def acquire(purpose="", vm=None, explicit=False, wait_s=0):
    """Take the lease (or refresh our own); stop when someone else holds it."""
    who = me()
    deadline = time.time() + wait_s
    while True:
        with _locked():
            cur = read()
            why = stale(cur)
            if cur and why is None and cur["owner"] != who["owner"]:
                held = describe(cur)
            else:
                if cur and cur["owner"] != who["owner"]:
                    log(f"UTM lease: taking over from {cur['owner']} ({why})")
                now = time.time()
                mine = cur if cur and cur["owner"] == who["owner"] else {"since": now}
                mine.update(who, touched=now, purpose=purpose or mine.get("purpose", ""),
                            vm=vm or mine.get("vm"),
                            explicit=explicit or mine.get("explicit", False))
                _write(mine)
                return mine
        if time.time() >= deadline:
            die(f"UTM is in use: {held}. One session drives UTM at a time; wait for it "
                f"(`utmbot lock wait`), or, if that session is gone for good, "
                f"`utmbot lock release --force`.")
        time.sleep(15)


def require(what="this command"):
    """For commands that touch a VM: the lease must be free or ours.  A free
    lease is not taken here (`vm up` and `lock acquire` take it)."""
    who = me()
    with _locked():
        cur = read()
        if cur and stale(cur) is None and cur["owner"] != who["owner"]:
            die(f"UTM is in use: {describe(cur)}; {what} would disturb it. Wait for it "
                f"(`utmbot lock wait`).")
        if cur and cur["owner"] == who["owner"]:
            cur["touched"] = time.time()
            _write(cur)


def release(force=False, implicit_only=False):
    who = me()
    with _locked():
        cur = read()
        if not cur:
            return False
        if cur["owner"] != who["owner"] and not force:
            if stale(cur) is None:
                die(f"the UTM lease is not ours: {describe(cur)}")
        if implicit_only and cur.get("explicit") and cur["owner"] == who["owner"]:
            return False  # a skill holds it across boots; `lock release` ends it
        leasep, _ = _paths()
        leasep.unlink(missing_ok=True)
        return True


def main(rest):
    import argparse
    global NAME
    if "--name" in rest:
        i = rest.index("--name")
        NAME = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    sub = rest[0] if rest else "status"
    p = argparse.ArgumentParser(prog=f"utmbot lock {sub}")
    if sub in ("acquire",):
        p.add_argument("--purpose", default="")
        p.add_argument("--wait", type=int, default=0, help="seconds to wait for a busy lease")
    if sub == "wait":
        p.add_argument("--timeout", type=int, default=6 * 3600)
    if sub == "release":
        p.add_argument("--force", action="store_true")
    a = p.parse_args(rest[1:])
    if sub == "status":
        cur = read()
        why = stale(cur)
        print(f"lease    {NAME}: {describe(cur)}" + (f" — may be taken over: {why}" if cur and why else ""))
        if cur and not why:
            print(f"mode     {cur.get('mode', 'interactive')}" + (f", job {cur['job']}" if cur.get("job") else ""))
        m = me()
        print(f"me       {m['owner']} ({m['mode']})")
    elif sub == "acquire":
        l = acquire(a.purpose, explicit=True, wait_s=a.wait)
        print(f"LEASED {l['owner']}")
    elif sub == "wait":
        deadline = time.time() + a.timeout
        while True:
            cur = read()
            if stale(cur) is not None or (cur and cur["owner"] == me()["owner"]):
                print("FREE")
                return 0
            if time.time() > deadline:
                die(f"still in use after {a.timeout}s: {describe(cur)}")
            time.sleep(30)
    elif sub == "release":
        print("RELEASED" if release(force=a.force) else "not held")
    else:
        die("utmbot lock [--name NAME] status | acquire [--purpose TEXT] [--wait S] | "
            "wait [--timeout S] | release [--force]")
    return 0
