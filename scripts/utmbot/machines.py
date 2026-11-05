"""Which VM does what on this Mac.

~/.utmapp-bot/machine.json names the VM for each role:

  test_vm   the Windows test VM triage and the Steam skills run on (the one
            with the Steam baseline); provision-windows-test-vm claims it
  dev_vm    the Windows dev VM that builds drivers and probes (and is the
            kdnet debugger); provision-windows-dev-vm claims it

A provisioning run claims a role that is unset (or names a VM that no
longer exists); `machine set` changes it on purpose.  Skills ask for a role
(`machine get`) instead of naming VMs; when it is missing they stop with the
provisioning command to run (interactive: offer to run it; auto: fail).

usage: utmbot machine [show] | get ROLE | set ROLE VM | claim ROLE VM
       utmbot dev up | dev down     start / shut down the dev VM (holds the UTM lease)
"""

import sys

from .common import DATA, VM, Fail, die, log, now_iso, parse_env_file, read_json, slug, write_json

ROLES = {
    "test_vm": ("test", "/utmapp-bot:provision-windows-test-vm <windows-11-arm64.iso> "
                "--name 'Windows Steam' --disk 256, then /utmapp-bot:db-steam-load"),
    "dev_vm": ("dev", "/utmapp-bot:provision-windows-dev-vm <windows-11-arm64.iso>"),
}
MISSING = 3  # exit status of `machine get` for an unset role


def path():
    return DATA / "machine.json"


def load():
    return read_json(path(), {})


def save(m):
    write_json(path(), m)


def known(name):
    return (DATA / "provision" / slug(name) / "state.env").exists()


def get(role):
    if role not in ROLES:
        die(f"unknown role {role!r} (roles: {', '.join(ROLES)})")
    name = load().get(role)
    if name and known(name):
        return name
    why = f"names '{name}', which this Mac no longer has" if name else "is not set"
    raise Missing(f"the {role} role {why}. Provision one: {ROLES[role][1]} (or `utmbot machine "
                  f"set {role} <existing VM>`)")


class Missing(Fail):
    pass


def set_role(role, name, only_if_unset=False):
    if role not in ROLES:
        die(f"unknown role {role!r} (roles: {', '.join(ROLES)})")
    if not known(name):
        die(f"'{name}' is not a VM this plugin knows (provision it, or `utmbot vm register`)")
    st = parse_env_file(VM(name).state_dir / "state.env")
    want = ROLES[role][0]
    if st.get("S_ROLE", "test") != want:
        die(f"'{name}' is a {st.get('S_ROLE', 'test')} VM, not a {want} VM")
    m = load()
    cur = m.get(role)
    if only_if_unset and cur and known(cur):
        print(f"{role} stays '{cur}' (`utmbot machine set {role} '{name}'` to switch)")
        return
    m[role] = name
    m.setdefault("history", []).append({"role": role, "vm": name, "time": now_iso()})
    save(m)
    print(f"{role} = '{name}'")


def show():
    m = load()
    for role in ROLES:
        name = m.get(role)
        state = "ok" if name and known(name) else ("MISSING VM" if name else "not set")
        print(f"{role:8s} {name or '-':28s} {state}")


# ---- the dev VM ----------------------------------------------------------------------

def dev(action):
    from . import lease, procs, remote, utm
    from .wait import Outcome, until
    name = get("dev_vm")
    vm = VM(name)
    if action == "up":
        lease.acquire(purpose="dev VM up (builds)", vm=name)
        utm.ensure_app()
        if not procs.vm_tree(name)["qemu"]:
            log(f"starting the dev VM '{name}'")
            utm.osa("start", name)
        o, obs = until(lambda: (remote.reachable(vm), f"ssh {vm.ssh_dest()}"), 360, "dev VM SSH",
                       every=30, interval=5)
        if o != Outcome.DONE:
            die(f"the dev VM '{name}' never answered SSH ({obs})")
        print(f"DEV UP {name} ssh={vm.ssh_dest()}")
    elif action == "down":
        lease.require("`utmbot dev down`")
        if procs.vm_tree(name)["qemu"]:
            remote.ssh(vm, "shutdown /s /t 0", timeout=60)
            remote.close(vm)
            until(lambda: (not procs.vm_tree(name)["qemu"], "QEMU"), 300, "dev VM shutdown", every=30)
        lease.release(implicit_only=True)  # a skill's `lock acquire` outlives this
        print(f"DEV DOWN {name}")
    else:
        die("utmbot dev up | dev down")


def main(rest):
    sub = rest[0] if rest else "show"
    if sub == "show":
        show()
    elif sub == "get" and len(rest) == 2:
        try:
            print(get(rest[1]))
        except Missing as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return MISSING
    elif sub in ("set", "claim") and len(rest) == 3:
        set_role(rest[1], rest[2], only_if_unset=(sub == "claim"))
    else:
        print(__doc__)
        return 2
    return 0
