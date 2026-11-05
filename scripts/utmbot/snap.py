"""Disk snapshots of a stopped VM, and the writable boots that make them.

UTM keeps snapshots inside the VM's qcow2 images (the
system disk and the UEFI variable store) and manages them with qemu-img while
the VM is off: `utmctl snapshot`.  Only frozen, disk-only snapshots are made
here.  A live snapshot would also hold the guest's memory, and restoring it
resumes a running guest, which a disposable (-snapshot) boot cannot do.

UTM records when a snapshot was made but not when it was last used, and
nothing says whether the disk still equals one.  A sidecar per VM,
~/.utmapp-bot/snapshots/<vm>.json, keeps both:

  disk       name of the snapshot the disk equals, "dirty" after a writable
             boot nobody saved, or null when unknown
  snapshots  {name: {created, last_used, ...labels}}

A restore over a dirty disk would throw away work (a game install, a Steam
login), so it is refused without --force.  Deleting a snapshot frees its
clusters inside the qcow2 for reuse; the image file does not shrink, so the
host gets space back only as the VM reuses those clusters.
"""

import json
from pathlib import Path

from .common import DATA, die, kv_list, log, now_iso, read_json, run, write_json
from . import procs


def utmctl():
    from .utm import app_path, ensure_app
    ensure_app()
    p = Path(app_path()) / "Contents" / "MacOS" / "utmctl"
    if not p.exists():
        die(f"{p} does not exist")
    rc, out = run([str(p), "snapshot", "--help"], timeout=30)
    if "restore" not in out:
        die(f"{app_path()} has no snapshot support in utmctl: the UTM is too old for this plugin. Update UTM")
    return str(p)


def ctl(*args, timeout=1800):
    rc, out = run([utmctl(), *map(str, args)], timeout=timeout)
    if rc != 0:
        die(f"utmctl {' '.join(map(str, args))} failed ({rc}): {out.strip()[-600:]}")
    return out.strip()


# ---- sidecar ---------------------------------------------------------------

def meta_path(vm):
    return DATA / "snapshots" / f"{vm.slug}.json"


def load_meta(vm):
    return read_json(meta_path(vm)) or {"disk": None, "snapshots": {}}


def save_meta(vm, m):
    write_json(meta_path(vm), m, sort_keys=True)


def set_disk(vm, state):
    m = load_meta(vm)
    m["disk"] = state
    save_meta(vm, m)


def label(vm, name, **labels):
    """Attach labels (appid, title, kind, ...) to a snapshot's sidecar entry."""
    m = load_meta(vm)
    e = m["snapshots"].setdefault(name, {})
    e.update({k: v for k, v in labels.items() if v is not None})
    save_meta(vm, m)


# ---- operations ------------------------------------------------------------

def stopped(vm):
    if procs.vm_tree(vm.name)["qemu"]:
        die(f"'{vm.name}' is running; snapshots are taken and restored with the VM off "
            "(`vm shutdown` after a writable boot, `vm down` after a disposable one)")


def listing(vm):
    """UTM's snapshots, newest first, merged with the sidecar."""
    rows = json.loads(ctl("snapshot", "list", vm.name, "--json", timeout=300) or "[]")
    m = load_meta(vm)
    for r in rows:
        r.update({k: v for k, v in m["snapshots"].get(r["name"], {}).items()
                  if k not in r})
    return rows, m


def names(vm):
    return {r["name"] for r in listing(vm)[0]}


def create(vm, name, overwrite=False, **labels):
    stopped(vm)
    existing = names(vm)
    now = now_iso()
    if name in existing:
        if not overwrite:
            die(f"'{vm.name}' already has a snapshot '{name}'; pass --overwrite to replace it")
        log(f"overwriting snapshot '{name}' of '{vm.name}' with the current disks")
        ctl("snapshot", "overwrite", vm.name, "--snapshot", name)
    else:
        log(f"saving the disks of '{vm.name}' as snapshot '{name}'")
        got = ctl("snapshot", "create", vm.name, "--name", name)
        if got.splitlines()[-1].strip() != name:
            die(f"UTM named the snapshot '{got}' instead of '{name}'")
    m = load_meta(vm)
    e = m["snapshots"].setdefault(name, {})
    e.update({"created": now, "last_used": now})
    e.update({k: v for k, v in labels.items() if v is not None})
    m["disk"] = name
    save_meta(vm, m)
    print(f"SNAPSHOT {vm.name} {name}")


def restore(vm, name, force=False):
    stopped(vm)
    rows, m = listing(vm)
    row = next((r for r in rows if r["name"] == name), None)
    if not row:
        die(f"'{vm.name}' has no snapshot '{name}' (has: {', '.join(r['name'] for r in rows) or 'none'})")
    if row.get("includesRunningState"):
        die(f"'{name}' includes a running state; only disk snapshots are used here")
    if m.get("disk") == "dirty" and not force:
        die(f"the disks of '{vm.name}' hold changes from a writable boot that were not saved "
            "as a snapshot; save them (`vm snap create NAME`) or pass --force to discard them")
    log(f"restoring '{vm.name}' to snapshot '{name}'")
    ctl("snapshot", "restore", vm.name, "--snapshot", name)
    m = load_meta(vm)
    m["snapshots"].setdefault(name, {})["last_used"] = now_iso()
    m["disk"] = name
    save_meta(vm, m)
    print(f"RESTORED {vm.name} {name}")


def delete(vm, name):
    stopped(vm)
    if name not in names(vm):
        die(f"'{vm.name}' has no snapshot '{name}'")
    log(f"deleting snapshot '{name}' of '{vm.name}'")
    ctl("snapshot", "delete", vm.name, "--snapshot", name)
    m = load_meta(vm)
    m["snapshots"].pop(name, None)
    if m.get("disk") == name:
        m["disk"] = None  # the disks are unchanged, but no snapshot names them now
    save_meta(vm, m)
    print(f"DELETED {vm.name} {name}")


def image_usage(vm):
    """Host bytes the VM's images take (allocated blocks, not apparent size)."""
    b = vm.bundle()
    if not b:
        return None
    total = 0
    for f in (Path(b) / "Data").glob("*"):
        if f.is_file():
            total += f.stat().st_blocks * 512
    return total


def show(vm):
    rows, m = listing(vm)
    print(f"disk      {m.get('disk') or 'unknown'}")
    use = image_usage(vm)
    if use is not None:
        print(f"images    {use / 2**30:.1f} GiB on the host")
    for r in rows:
        extra = " ".join(f"{k}={r[k]}" for k in ("kind", "appid", "title") if r.get(k))
        print(f"{r['name']:32s} created {r['created'][:19]}  last used "
              f"{(r.get('last_used') or '-')[:19]:19s}  "
              f"{'RUNNING STATE ' if r.get('includesRunningState') else ''}{extra}")


def main(vm, rest):
    import argparse
    sub = rest[0] if rest else "list"
    p = argparse.ArgumentParser(prog=f"utmbot vm snap {sub}")
    if sub in ("create", "restore", "delete", "label"):
        p.add_argument("name")
    if sub == "create":
        p.add_argument("--overwrite", action="store_true")
    if sub == "restore":
        p.add_argument("--force", action="store_true")
    if sub in ("create", "label"):
        p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    if sub == "list":
        p.add_argument("--json", action="store_true")
    a = p.parse_args(rest[1:])
    labels = kv_list(getattr(a, "set", []), "KEY=VALUE")
    if sub == "list":
        if a.json:
            rows, m = listing(vm)
            print(json.dumps({"disk": m.get("disk"), "image_bytes": image_usage(vm),
                              "snapshots": rows}, indent=2))
        else:
            show(vm)
    elif sub == "create":
        create(vm, a.name, a.overwrite, **labels)
    elif sub == "restore":
        restore(vm, a.name, a.force)
    elif sub == "delete":
        delete(vm, a.name)
    elif sub == "label":
        if a.name not in names(vm):
            die(f"'{vm.name}' has no snapshot '{a.name}'")
        label(vm, a.name, **labels)
    else:
        die("vm snap list [--json] | create NAME [--overwrite] [--set K=V] | "
                   "restore NAME [--force] | delete NAME | label NAME --set K=V")
    return 0
