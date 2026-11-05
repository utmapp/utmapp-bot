"""The one wait primitive: poll a condition with a hard deadline and progress.

Every long operation goes through `until()`, so nothing sleeps blindly and a
caller always sees how long it has waited and what it last observed.  It
stops early with VM-DEAD when the VM's QEMU process is gone or the kill
watcher fired, instead of waiting out the deadline on a dead VM.
"""

import sys
import time

from . import procs
from .common import Session, human_secs, log


class Outcome:
    DONE = "DONE"
    TIMEOUT = "TIMEOUT"
    VM_DEAD = "VM-DEAD"


def vm_dead_reason(vm):
    if hasattr(vm, "dead_reason"):   # not a VM: a CrossOver bottle says for itself
        return vm.dead_reason()
    s = Session(vm)
    fired = s.vm.session_dir() / "killwatch.fired"
    if fired.exists() and s.get("started") and fired.stat().st_mtime >= s.get("started"):
        return "killwatch fired: " + fired.read_text().strip()
    if s.get("qemu_pid") and not procs.vm_tree(vm.name)["qemu"]:
        return "the VM's QEMU process is gone"
    return None


def until(check, deadline_s, what, every=10, vm=None, interval=2.0):
    """check() -> (done: bool, observation: str).  Returns (Outcome, last obs)."""
    t0 = time.time()
    last_report = t0
    obs = ""
    while True:
        try:
            done, obs = check()
        except Exception as e:  # a transient ssh failure is an observation, not a crash
            done, obs = False, f"check failed: {e}"
        if done:
            log(f"{what}: done after {human_secs(time.time() - t0)} ({obs})")
            return Outcome.DONE, obs
        if vm is not None:
            dead = vm_dead_reason(vm)
            if dead:
                log(f"{what}: VM-DEAD after {human_secs(time.time() - t0)}: {dead}")
                return Outcome.VM_DEAD, dead
        now = time.time()
        if now - t0 >= deadline_s:
            log(f"{what}: TIMEOUT after {human_secs(now - t0)} (last: {obs})")
            return Outcome.TIMEOUT, obs
        if now - last_report >= every:
            log(f"{what}: waiting {human_secs(now - t0)}/{human_secs(deadline_s)}: {obs}")
            last_report = now
        sys.stderr.flush()
        time.sleep(interval)
