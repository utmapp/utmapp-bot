"""Backups of the games database, taken through the internal API.

usage: python3 -m gamesdb.backup run [--force] | verify FILE | list | prune [--dry-run]
       (also: gamesdb backup …)

run      take a backup when the newest valid one is 12 hours old (or with
         --force), validate it, then prune.  The backup agent that db-setup
         installs runs this every hour, so a Mac that slept catches up.
verify   check an archive the way `run` does.
list     the archives and which retention slot keeps each.
prune    delete what no slot keeps.

Retention, over one series of archives (each is complete on its own):
  the 2 newest (12-hourly), the newest of each of the last 7 days, of each of
  the last 4 ISO weeks, and of each of the last 12 months.  The newest valid
  archive is never deleted.

Validation (an archive is kept only if all of it holds): the tar reads to
its end without unsafe paths; manifest.json lists files and every one is in
the archive with exactly its size (a stream cut between members still
looks like a clean end of archive, so this is what proves completeness);
env, README.txt and db/gamesdb.sqlite are there; env sets SECRET_KEY; the
database passes PRAGMA integrity_check and every table has the row count
the manifest states.

Archives hold the site's SECRET_KEY and its TLS and DKIM keys: the
directory is 700 and the files 600.  GAMESDB_BACKUP_DIR moves it (default
~/.utmapp-bot/gamesdb/backups).  Status: <dir>/status.json.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gamesdb import tunnel  # noqa: E402
from gamesdb.tunnel import Fail  # noqa: E402

EVERY = dt.timedelta(hours=12)
KEEP_RECENT, KEEP_DAYS, KEEP_WEEKS, KEEP_MONTHS = 2, 7, 4, 12
NAME = re.compile(r"^utmdb-(\d{8}T\d{6}Z)\.tar$")
REQUIRED = ("manifest.json", "README.txt", "env", "db/gamesdb.sqlite")
TEMPFAIL = 75


def backup_dir():
    d = Path(tunnel.config().get("GAMESDB_BACKUP_DIR") or tunnel.STATE / "backups").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def log(msg):
    tunnel.append_log(tunnel.STATE / "logs" / "backup.log", msg, echo=True)


def stamp_of(p):
    m = NAME.match(p.name)
    return dt.datetime.strptime(m[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc) if m else None


def archives(d):
    out = [(stamp_of(p), p) for p in d.iterdir() if p.is_file() and stamp_of(p)]
    return sorted(out, reverse=True)


# ---- validation ----------------------------------------------------------------

def verify(path):
    """Problems with an archive ([] when it is complete and sound)."""
    problems = []
    try:
        with tarfile.open(path, "r:") as t:
            members = {}
            for m in t:  # reads to the end: a torn final member raises here
                if m.name.startswith("/") or ".." in Path(m.name).parts:
                    problems.append(f"unsafe path {m.name!r}")
                if m.isfile():
                    members[m.name] = m
            for r in REQUIRED:
                if r not in members:
                    problems.append(f"missing {r}")
            if "manifest.json" not in members:
                return problems
            man = json.load(t.extractfile(members["manifest.json"]))
            files = man.get("files") or []
            if not files:
                problems.append("manifest lists no files")
            for f in files:
                m = members.get(f["path"])
                if m is None:
                    problems.append(f"{f['path']} is in the manifest but not in the archive")
                elif m.size != f["size"]:
                    problems.append(f"{f['path']}: {m.size} bytes, manifest says {f['size']}")
            listed = {f["path"] for f in files} | {"manifest.json", "README.txt"}
            extra = [n for n in members if n not in listed]
            if extra:
                problems.append(f"{len(extra)} file(s) not in the manifest (e.g. {extra[0]})")
            if "env" in members:
                env = t.extractfile(members["env"]).read().decode("utf-8", "replace")
                if not re.search(r"(?m)^\s*SECRET_KEY\s*=\s*['\"]?[^'\"\s]{16,}", env):
                    problems.append("env has no SECRET_KEY (every account would be orphaned)")
            if "db/gamesdb.sqlite" in members:
                with tempfile.TemporaryDirectory() as td:
                    dbp = Path(td) / "db.sqlite"
                    with t.extractfile(members["db/gamesdb.sqlite"]) as src, open(dbp, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
                    try:
                        ic = con.execute("PRAGMA integrity_check").fetchall()
                        if ic != [("ok",)]:
                            problems.append(f"database integrity_check: {ic[:3]}")
                        for table, n in (man.get("tables") or {}).items():
                            if not re.fullmatch(r"[a-z_0-9]+", table):
                                problems.append(f"odd table name {table!r} in the manifest")
                                continue
                            got = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                            if got != n:
                                problems.append(f"table {table}: {got} rows, manifest says {n}")
                        if not man.get("tables"):
                            problems.append("manifest has no table counts")
                    except sqlite3.DatabaseError as e:
                        problems.append(f"database unreadable: {e}")
                    finally:
                        con.close()
    except (tarfile.TarError, OSError, ValueError, KeyError, EOFError) as e:
        problems.append(f"archive unreadable: {e}")
    return problems


# ---- retention ---------------------------------------------------------------------

def keep_set(stamps, now=None):
    """{stamp: [reasons]} for the archives retention keeps."""
    now = now or dt.datetime.now(dt.timezone.utc)
    keep = {}

    def add(s, why):
        keep.setdefault(s, []).append(why)
    ordered = sorted(stamps, reverse=True)
    for s in ordered[:KEEP_RECENT]:
        add(s, "12-hourly")
    if ordered:
        add(ordered[0], "newest")
    local = lambda s: s.astimezone()  # noqa: E731 (calendar slots in local time)
    today = local(now).date()

    def newest_in(slots, keyf, label):
        seen = set()
        for s in ordered:
            k = keyf(local(s))
            if k in slots and k not in seen:
                seen.add(k)
                add(s, label)
    days = {today - dt.timedelta(days=i) for i in range(KEEP_DAYS)}
    newest_in(days, lambda t: t.date(), "daily")
    iso = lambda d: d.isocalendar()[:2]  # noqa: E731
    weeks = {iso(today - dt.timedelta(weeks=i)) for i in range(KEEP_WEEKS)}
    newest_in(weeks, lambda t: iso(t.date()), "weekly")
    months, y, m = set(), today.year, today.month
    for _ in range(KEEP_MONTHS):
        months.add((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    newest_in(months, lambda t: (t.year, t.month), "monthly")
    return keep


def prune(d, dry_run=False):
    arch = archives(d)
    keep = keep_set([s for s, _ in arch])
    removed = []
    for s, p in arch:
        if s not in keep:
            removed.append(p.name)
            if not dry_run:
                p.unlink()
    for p in d.glob("*.partial"):
        if time.time() - p.stat().st_mtime > 24 * 3600:
            removed.append(p.name)
            if not dry_run:
                p.unlink()
    return removed


# ---- taking one ---------------------------------------------------------------------

def write_status(d, **kw):
    p = d / "status.json"
    try:
        st = json.loads(p.read_text())
    except (OSError, ValueError):
        st = {}
    st.update(kw, updated=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    p.write_text(json.dumps(st, indent=2))
    os.chmod(p, 0o600)


def run(force=False):
    d = backup_dir()
    lockf = open(d / ".lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another backup run is in progress")
        return 0
    arch = archives(d)
    now = dt.datetime.now(dt.timezone.utc)
    if arch and not force and now - arch[0][0] < EVERY:
        nxt = (arch[0][0] + EVERY).astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"not due: newest backup {arch[0][1].name}; next after {nxt}")
        return 0
    free = shutil.disk_usage(d).free
    need = int((arch[0][1].stat().st_size if arch else 0) * 1.5) + (1 << 30)
    if free < need:
        msg = f"only {free >> 20} MiB free in {d}; need about {need >> 20} MiB"
        log(msg)
        write_status(d, last_error=msg, last_attempt=now.isoformat())
        return 1
    try:
        base, where = tunnel.base_url()
    except Fail as e:
        log(f"unreachable, trying again next hour: {e}")
        write_status(d, last_error=f"unreachable: {e}", last_attempt=now.isoformat())
        return TEMPFAIL
    name = f"utmdb-{now.strftime('%Y%m%dT%H%M%SZ')}.tar"
    part = d / (name + ".partial")
    log(f"backup from {where} -> {d / name}")
    t0 = time.time()
    try:
        req = urllib.request.Request(base + "/internal/backup")
        with urllib.request.urlopen(req, timeout=600) as r, open(part, "wb") as f:
            os.chmod(part, 0o600)
            shutil.copyfileobj(r, f, 1 << 20)
    except urllib.error.HTTPError as e:
        part.unlink(missing_ok=True)
        body = e.read()[:300].decode("utf-8", "replace")
        msg = f"server answered {e.code}: {body.strip()}"
        log(msg + (" (another backup is running; next hour)" if e.code == 409 else ""))
        write_status(d, last_error=msg, last_attempt=now.isoformat())
        return TEMPFAIL if e.code in (409, 502, 503, 504) else 1
    except (OSError, urllib.error.URLError) as e:
        part.unlink(missing_ok=True)
        log(f"download failed, trying again next hour: {e}")
        write_status(d, last_error=f"download failed: {e}", last_attempt=now.isoformat())
        return TEMPFAIL
    problems = verify(part)
    if problems:
        bad = d / (name + ".invalid")
        part.replace(bad)
        msg = f"backup failed validation, kept as {bad.name}: " + "; ".join(problems[:5])
        log(msg)
        write_status(d, last_error=msg, last_attempt=now.isoformat())
        return 1
    final = d / name
    part.replace(final)
    size = final.stat().st_size
    removed = prune(d)
    for p in d.glob("*.invalid"):
        if time.time() - p.stat().st_mtime > 7 * 24 * 3600:
            p.unlink()
    log(f"ok: {name}, {size >> 20} MiB in {int(time.time() - t0)} s; pruned {len(removed)}")
    write_status(d, last_success=name, last_success_at=now.isoformat(), last_size=size,
                 last_error=None, last_attempt=now.isoformat())
    print(f"BACKUP {final}")
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="gamesdb backup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("verify"); s.add_argument("file")
    sub.add_parser("list")
    s = sub.add_parser("prune"); s.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "run":
        return run(a.force)
    if a.cmd == "verify":
        problems = verify(Path(a.file))
        for x in problems:
            print(f"PROBLEM {x}")
        print("VALID" if not problems else "INVALID")
        return 0 if not problems else 1
    d = backup_dir()
    if a.cmd == "list":
        arch = archives(d)
        keep = keep_set([s for s, _ in arch])
        for s, f in arch:
            print(f"{f.name}  {f.stat().st_size >> 20:6d} MiB  {', '.join(keep.get(s, ['(pruned next run)']))}")
        try:
            st = json.loads((d / "status.json").read_text())
            print(f"last success {st.get('last_success_at')}; last error: {st.get('last_error')}")
        except (OSError, ValueError):
            pass
        return 0
    removed = prune(d, a.dry_run)
    print(("would delete " if a.dry_run else "deleted ") + (", ".join(removed) or "nothing"))
    return 0


if __name__ == "__main__":
    tunnel.cli(main)
