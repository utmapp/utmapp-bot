"""gamesdb — the games database's internal API from this Mac.

usage: python3 <plugin>/scripts/gamesdb <command> ...   (bin/gamesdb)

API commands use the endpoint the tunnel agent keeps up (db-setup installs
it; commands never open SSH and fail at once when it does not answer) or,
with GAMESDB_URL set, a local instance.  Output is JSON on stdout; the
target is named on stderr; errors are one ERROR line and exit 1.  Writes are
appended to ~/.utmapp-bot/gamesdb/actions.log.

Connection:
  tunnel status                  the tunnel agent's state and whether the API answers
  health
  work [--since T] [--limit N]   the work feed: reports changed since T, open queues, counts
  state-dir                      where this Mac keeps the db skills' local state
  machine                        this Mac's name among the triage machines (GAMESDB_HOLDER)

Reports and the triage agent:
  reports [--since T] [--game ID] [--visibility V] [--triage-status S|none] [--limit N]
  report ID                      {"report": {..., "triage_status", "comments": [...],
                                 "attachments": [...]}, "game": {...}}; triage_status,
                                 comments and attachments are absent when there are none;
                                 "lease": which machine holds (or last held) its triage
                                 lease, until when ("active")
  attachments ID [--out DIR]     download every attachment of a report (private dir,
                                 manifest.json beside them); prints the directory
  attachment AID --out FILE
  comment ID (--body TEXT | --file PATH) [--name LITERAL ...] [--allow REGEX ...]
             [--allow-link DOMAIN ...] [--private-source] [--dry-run]
                                 post as Auto-Triage (Markdown, links allowed); refused
                                 when the personal-data lint finds anything or a link
                                 leaves the reference sites (LINK_DOMAINS, CODE_HOSTS in
                                 redact.py); --private-source (db-summarize): also refused
                                 when it shows its data came from attachments
                                 (placeholders, "attachment"/"uploaded"/"your log",
                                 wall-clock times).  Kept in ~/.utmapp-bot/gamesdb/comments/
                                 once posted; --dry-run only checks (works offline)
  triage-status ID STATUS        pending|in_progress|needs_clarification|fix_proposed|
                                 wont_fix|cannot_reproduce|clear
  fix-branch REPO --branch NAME [--ref REF] [--dry-run]
                                 publish a validated fix as a new branch on the bot's GitHub
                                 fork (never a pull request); fix-branch --whoami: the gh
                                 login, or an error
  visibility report|comment ID visible|hidden|removed

Moderation:
  flags [--limit N]              content users reported, newest flag first
  flag-ignore report|comment ID
  spam [--pending] [--limit N] [--with-network]
                                 Akismet rejections; the visitor's IP, user agent and
                                 referrer are withheld unless --with-network
  spam-feedback ID spam|ham      tell Akismet; marks the entry reviewed
  user ID                        an account with activity counts
  ban ID | unban ID
  deletion-requests [--status open|done|rejected|all]
  deletion-request ID done|rejected|redacted
                                 done removes the report; redacted (or rejected on a
                                 server without it) closes the request and keeps it
  report-edit ID [--description FILE] [--repro FILE]   moderator edit of a report's text
                                 (- for stdin, an empty file clears it); rating,
                                 configuration, +1s, comments and status are kept
  comment-edit ID --body FILE    moderator edit of a comment's text (- for stdin)
  attachment-delete AID          remove one attachment (file and record)

Auto-Triage availability:
  owned                          the owned-games list
  owned-add STOREFRONT ID [--free]         add one entry, keeping the others
  owned-add --json FILE|-        add many: [{"storefront":"steam","storefront_id":"620","free":false}, ...]
                                 (utmbot steam owned --entries prints exactly that)
  owned-remove STOREFRONT ID
  triage-requests [--status open|done|rejected|all]   gift/demo offers from users
  triage-request ID open|done|rejected
  versions                       UTM versions
  game-refresh GAME_ID           have the server re-resolve a game's details (store links,
                                 cover) through its own catalog sync; prints the game

Attachments (users' files: untrusted; attachments.py; these work offline):
  frames VIDEO OUTDIR [COUNT] [--at S ...] [--every S [--from S] [--to S]]
                                 frames + change metric (videoframes.swift),
                                 decoded in a sandbox (attachment.sb: no network, no
                                 other programs, no home-folder reads or writes)
  plist FILE OUTDIR              binary plist to XML, in the same sandbox
  unzip ZIP DIR                  extract an attachment archive safely, entries by index

Personal data (redact.py; these work offline):
  redact FILE... [--name LITERAL ...] [--out DIR]   placeholders for personal data
  lint FILE [--name LITERAL ...] [--allow REGEX ...] exit 1 if anything looks personal
  qemu-args LOG                  a UTM debug log's QEMU command line, one option per line,
                                 without names, ids, MACs, serials or host paths

Sessions (state.py):
  session note REPORT [SESSION]  this (or that) Claude session works on REPORT
  session get REPORT | session list
  session blocked --reason TEXT [--scope job|steam|utm|github|claude|server|all]
                  [--details TEXT|@FILE] [--keep-vm]
                                 (auto mode, last resort) this session cannot go on without
                                 a person: releases its leases (--keep-vm: keeps UTM and the
                                 running VM for the person); the dispatcher resumes it with
                                 Remote Control so it can notify the user
  session unblocked [--note TEXT]   the person answered: the dispatcher ends Remote Control
                                 and resumes the work headless
  session later --reason TEXT [--hours H] [--handoff FILE]
                                 (auto mode) the work has to wait: releases the leases; the
                                 dispatcher resumes this session after H hours (default 1)

Work dispatcher (dispatch.py; the dispatch agent runs `dispatch run`):
  dispatch status | tick [--dry-run] | pause | resume | retry JOB | session PROMPT

Backups (backup.py; the backup agent runs `backup run` hourly):
  backup run [--force] | verify FILE | list | prune [--dry-run]

Local instance (testing only; e2e-local.sh runs it with an e2e dispatcher):
  local from-backup --tar ARCHIVE [--data DIR] [--force]   its database and attachments
                                 (no secrets)
  local start [--checkout DIR] [--data DIR] [--port 18081]
  local stop | local status [--port 18081]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gamesdb import attachments, fixbranch, redact, state, tunnel  # noqa: E402
from gamesdb.api import Api, q  # noqa: E402
from gamesdb.tunnel import die, note  # noqa: E402

TRIAGE = ["pending", "in_progress", "needs_clarification", "fix_proposed", "wont_fix",
          "cannot_reproduce"]


def out(v):
    print(json.dumps(v, indent=2, ensure_ascii=False))


def read_arg(v):
    """The text a FILE argument names; - is stdin."""
    if v is None:
        return None
    return sys.stdin.read() if v == "-" else Path(v).read_text()


# ---- attachments and personal data (offline) --------------------------------------

def cmd_redact(a):
    r = redact.Redactor(a.name or ())
    for f in a.files:
        text = r.redact(Path(f).read_bytes().decode("utf-8", "replace"))
        dst = (Path(a.out) / (Path(f).name + ".redacted.txt")) if a.out else Path(str(f) + ".redacted.txt")
        attachments.private_dir(dst.parent)
        print(attachments.write_private(dst, text))
    print("redacted: " + (", ".join(f"{k}×{v}" for k, v in sorted(r.counts.items())) or "nothing"),
          file=sys.stderr)


def cmd_lint(a):
    found = redact.lint_text(Path(a.file).read_bytes().decode("utf-8", "replace"), a.name or (), a.allow or ())
    for cls, line, masked in found:
        print(f"{cls:8s} line {line}: {masked}")
    if found:
        print(f"LINT FAIL: {len(found)} possible personal data match(es)", file=sys.stderr)
        return 1
    print("LINT OK", file=sys.stderr)
    return 0


def cmd_qemu_args(a):
    line = redact.qemu_args(Path(a.log).read_text(errors="replace"))
    if line is None:
        die("no `Launching: qemu-system-…` line found")
    print(line)


def cmd_frames(a):
    return attachments.frames(a.video, a.out, a.count, a.at or (), a.every, a.start, a.end)


# ---- reports -------------------------------------------------------------------------

def cmd_attachments(api, a):
    rep = api.ok("GET", f"/internal/reports/{a.id}")["report"]
    d = attachments.private_dir(Path(a.out).expanduser() if a.out
                                else tunnel.state_dir() / "attachments" / str(a.id))
    manifest = []
    for att in rep.get("attachments") or []:
        status, payload = api.call("GET", f"/internal/attachments/{att['id']}", raw=True)
        if status != 200:
            manifest.append({"id": att["id"], "error": f"HTTP {status}"})
            continue
        # the uploader's file name may itself carry personal data: stored by id
        p = attachments.write_private(d / f"attachment-{att['id']}{attachments.plain_suffix(att.get('filename'))}",
                                      payload)
        manifest.append({"id": att["id"], "file": p.name, "mime": att.get("mime"),
                         "size_bytes": len(payload), "sha256": att.get("sha256"),
                         "original_filename": att.get("filename"),
                         "created_at": att.get("created_at")})
    attachments.write_private(d / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    note(f"{len(manifest)} attachment(s) in {d} (private: never commit or upload them)")
    print(d)


def cmd_attachment(api, a):
    status, payload = api.call("GET", f"/internal/attachments/{a.id}", raw=True)
    if status != 200:
        die(f"attachment {a.id}: HTTP {status}: {payload[:300]!r}")
    print(attachments.write_private(Path(a.out).expanduser(), payload))


def comment_body(a):
    """The comment's text once it passes every check (else an error)."""
    body = (a.body if a.body is not None else Path(a.file).read_text()).strip()
    problem = redact.comment_problems(body, a.name or (), a.allow or (), a.allow_link or (),
                                      a.private_source)
    if problem:
        die(f"not posted: {problem}")
    return body


def cmd_comment(api, a):
    body = comment_body(a)
    res = api.ok("POST", f"/internal/reports/{a.id}/comments", {"body_md": body})
    # what was posted, for the record (it is public anyway)
    d = tunnel.state_dir() / "comments"
    d.mkdir(exist_ok=True)
    keep = d / f"{a.id}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{res.get('comment_id', 'x')}.md"
    keep.write_text(body + "\n")
    note(f"posted; kept in {keep}")
    out(res)


def cmd_triage_status(api, a):
    st = "" if a.status == "clear" else a.status
    if st and st not in TRIAGE:
        die(f"status must be one of {', '.join(TRIAGE)} or clear")
    out(api.ok("POST", f"/internal/reports/{a.id}/triage", {"status": st}))


# ---- moderation ------------------------------------------------------------------------

def cmd_spam(api, a):
    res = api.ok("GET", "/internal/spam-log" + q(limit=a.limit, pending=1 if a.pending else None))
    if a.pending:
        # an older server ignores pending=: filter here too
        res["entries"] = [e for e in res.get("entries", []) if not e.get("feedback")]
    for e in res.get("entries", []):
        try:
            e["payload"] = json.loads(e["payload"])
        except (TypeError, ValueError):
            continue
        if not a.with_network and isinstance(e["payload"], dict):
            # the visitor's address and browser decide nothing here and
            # must not travel further
            for k in ("ip", "ua", "referrer"):
                if k in e["payload"]:
                    e["payload"][k] = "[withheld]"
    out(res)


def cmd_report_edit(api, a):
    body = {}
    for field, src in (("description_md", a.description), ("repro_md", a.repro)):
        t = read_arg(src)
        if t is not None:
            body[field] = t.strip()
    if not body:
        die("give --description FILE and/or --repro FILE (- for stdin; an empty file clears the field)")
    for field, text in body.items():
        if redact.links(text):
            die(f"{field} would contain a link; user content on the site never has links")
    out(api.ok("PATCH", f"/internal/reports/{a.id}", body, needs="PATCH /internal/reports/{id}"))


def cmd_comment_edit(api, a):
    text = (read_arg(a.body) or "").strip()
    if not text:
        die("--body FILE with the comment's new text (to take a comment down, use visibility)")
    out(api.ok("PATCH", f"/internal/comments/{a.id}", {"body_md": text}, needs="PATCH /internal/comments/{id}"))


def cmd_deletion_request(api, a):
    status, res = api.call("POST", f"/internal/deletion-requests/{a.id}/status", {"status": a.value})
    if a.value == "redacted" and status == 400 and "status must be" in str(res.get("error", "")):
        # a server without the status: "rejected" also keeps the report and
        # closes the request (authors only ever see "pending")
        note("this server has no \"redacted\" status; closing the request as \"rejected\" "
             "(the report stays, as redacted)")
        res = api.ok("POST", f"/internal/deletion-requests/{a.id}/status", {"status": "rejected"})
    elif status >= 300:
        die(f"POST /internal/deletion-requests/{a.id}/status -> {status}: {json.dumps(res)}")
    out(res)


# ---- Auto-Triage availability ------------------------------------------------------------

def _entry(storefront, sid, free=False):
    if storefront.lower() == "igdb":
        return {"igdb_game_id": int(sid), "free": free}
    return {"storefront": storefront.lower(), "storefront_id": str(sid), "free": free}


def cmd_owned_add(api, a):
    if a.json:
        new = [_entry(x.get("storefront", "igdb"), x.get("storefront_id") or x.get("igdb_game_id"),
                      bool(x.get("free"))) for x in json.loads(read_arg(a.json))]
    elif a.storefront and a.id:
        new = [_entry(a.storefront, a.id, a.free)]
    else:
        die("owned-add STOREFRONT ID [--free], or owned-add --json FILE")
    if not new:
        die("nothing to add")
    out(api.ok("POST", "/internal/triage/owned", new))


def cmd_owned_remove(api, a):
    sf, sid = a.storefront.lower(), str(a.id)
    out(api.ok("DELETE", f"/internal/triage/owned/{urllib.parse.quote(sf)}/{urllib.parse.quote(sid, safe='/')}"))


# ---- sessions --------------------------------------------------------------------------------

def _release_leases():
    """A blocked or waiting session gives UTM (and moderation) back, so the
    dispatcher can run other work meanwhile."""
    ub = Path(tunnel.config().get("GAMESDB_PLUGIN_DIR") or Path(__file__).resolve().parents[2]) / "bin" / "utmbot"
    released = []
    for name in ("utm", "moderation"):
        if not ub.exists():
            break
        r = subprocess.run([str(ub), "lock", "--name", name, "release"], capture_output=True, text=True)
        if "RELEASED" in r.stdout:
            released.append(name)
    return released


def cmd_session(a):
    if a.action == "list":
        out(state.sessions())
        return
    if a.action == "get":
        out(state.session_get(a.report) or {})
        return
    if a.action == "note":
        sid = a.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not a.report or not sid:
            die("session note REPORT [SESSION_ID] (default: this session's CLAUDE_CODE_SESSION_ID)")
        out(state.session_note(a.report, sid))
        return
    # blocked / unblocked / later: this session's own state, for the dispatcher
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        die(f"only a Claude Code session can report itself {a.action}")
    job = os.environ.get("GAMESDB_DISPATCH_JOB")
    if a.action == "blocked":
        if not a.reason or len(a.reason) > 200:
            die("--reason: one line (under 200 characters) saying what blocks you and what you need")
        if a.scope not in state.SCOPES:
            die(f"--scope must be one of {', '.join(state.SCOPES)}")
        details = a.details or ""
        if details.startswith("@"):
            details = Path(details[1:]).read_text()
        # --keep-vm: the person's part needs the VM as it is (Steam at its
        # sign-in window): keep the UTM lease, and with it the VM
        released = [] if a.keep_vm else _release_leases()
        rec = state.signal(sid, "blocked", reason=a.reason, scope=a.scope, details=details[:4000],
                           job=job, released=released, kept_vm=bool(a.keep_vm))
        note("recorded; end your turn now. The dispatcher brings the user in over Remote "
             "Control and resumes you" + (f" (released: {', '.join(released)})" if released else ""))
    elif a.action == "later":
        # nothing a person must do, but the work goes on later (a long build,
        # a handoff to pick up after a reset): the dispatcher resumes this session
        if not a.reason or len(a.reason) > 300:
            die("--reason: one line saying what to do when resumed (under 300 characters)")
        hours = float(a.hours or 1)
        if not 0.05 <= hours <= 72:
            die("--hours: between 0.05 and 72")
        rec = state.signal(sid, "later", reason=a.reason, hours=hours, handoff=a.handoff,
                           job=job, released=_release_leases())
        note(f"recorded; end your turn now. The dispatcher resumes this session in {hours:g} h")
    else:  # unblocked
        cur = state.read_signal(sid) or {}
        rec = state.signal(sid, "unblocked", reason=cur.get("reason"), scope=cur.get("scope"),
                           note=(a.note or "")[:2000], job=cur.get("job") or job)
        note("recorded; stop here. The dispatcher ends this Remote Control session and resumes the "
             "work headless")
    out(rec)


# ---- local instance (testing) -------------------------------------------------------------

LOCAL = tunnel.DATA / "gamesdb-local"
LOCAL_PID = LOCAL / "serve.pid"


def local_from_backup(a):
    """Test data from a backup archive (GET /internal/backup): its database
    and attachment files, never its settings or keys."""
    import tarfile
    if not a.tar:
        die("local from-backup --tar ARCHIVE [--data DIR]")
    data = Path(a.data).expanduser() if a.data else LOCAL / "data"
    if (data / "gamesdb.sqlite").exists() and not a.force:
        die(f"{data}/gamesdb.sqlite exists; pass --force to replace it (stop the local instance first)")
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data.parent, 0o700)
    with tarfile.open(a.tar) as t:
        for m in t.getmembers():
            if not m.isfile() or ".." in Path(m.name).parts or m.name.startswith("/"):
                continue
            if m.name == "db/gamesdb.sqlite":
                dst = data / "gamesdb.sqlite"
            elif m.name.startswith("uploads/"):
                dst = data / m.name
            else:
                continue  # env, tls/, dkim/: secrets a test instance must not have
            dst.parent.mkdir(parents=True, exist_ok=True)
            with t.extractfile(m) as src, open(dst, "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(dst, 0o600)
    for side in ("-wal", "-shm"):
        Path(str(data / "gamesdb.sqlite") + side).unlink(missing_ok=True)
    print(f"{data} (the catalog tables are empty in a backup; the server imports them when "
          "IGDB credentials are set)")


def local_start(a):
    cfg = tunnel.config()
    checkout = Path(a.checkout or cfg.get("GAMESDB_CHECKOUT", "")).expanduser()
    if not (checkout / "cmd" / "gamesdb").is_dir():
        die("pass --checkout DIR (a clone of utmapp/utm-games-db), or set GAMESDB_CHECKOUT")
    go = shutil.which("go") or str(tunnel.DATA / "tools" / "go" / "bin" / "go")
    if not Path(go).exists():
        die("Go is not installed (the repository's go.mod names the version); install it "
            f"or unpack the official toolchain to {tunnel.DATA}/tools/go")
    data = Path(a.data).expanduser() if a.data else LOCAL / "data"
    if not (data / "gamesdb.sqlite").exists():
        note(f"no database in {data}; the server creates an empty one (or `local from-backup`)")
    data.mkdir(parents=True, exist_ok=True)
    api = f"http://127.0.0.1:{a.port}"
    if tunnel.healthy(api):
        die(f"something already answers on 127.0.0.1:{a.port}; `local stop` first")
    binary = LOCAL / "gamesdb"
    p = subprocess.run([go, "build", "-o", str(binary), "./cmd/gamesdb"], cwd=checkout,
                       capture_output=True, text=True)
    if p.returncode != 0:
        die(f"go build failed: {p.stderr[-800:]}")
    # the site one port below the API; IGDB credentials, if any, come from
    # LOCAL/.env (the server reads .env in its working directory)
    env = dict(os.environ, MAIL_MODE="log", DATA_DIR=str(data),
               BASE_URL=f"http://127.0.0.1:{a.port - 1}", LISTEN_ADDR=f"127.0.0.1:{a.port - 1}",
               INTERNAL_LISTEN_ADDR=f"127.0.0.1:{a.port}", IGDB_SYNC_ON_START="false",
               AKISMET_KEY="", CDN_PURGE_URL="")
    logf = open(LOCAL / "serve.log", "a")
    proc = subprocess.Popen([str(binary), "serve"], cwd=LOCAL, env=env, stdin=subprocess.DEVNULL,
                            stdout=logf, stderr=logf, start_new_session=True)
    LOCAL_PID.write_text(str(proc.pid))
    for _ in range(40):
        if tunnel.healthy(api):
            print(f"local instance pid {proc.pid}: site http://127.0.0.1:{a.port - 1}, "
                  f"API {api}; log {LOCAL / 'serve.log'} (sign-in codes "
                  f"are printed there)\nuse it with: export GAMESDB_URL={api}")
            return
        time.sleep(0.5)
    die(f"the local instance did not come up; see {LOCAL / 'serve.log'}")


def cmd_local(a):
    if a.action == "from-backup":
        return local_from_backup(a)
    if a.action == "start":
        return local_start(a)
    pid = LOCAL_PID.read_text().strip() if LOCAL_PID.exists() else ""
    if a.action == "status":
        alive = bool(pid) and subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0
        print(f"pid      {pid or '-'} ({'running' if alive else 'not running'})")
        print(f"api      http://127.0.0.1:{a.port}  healthy={'yes' if tunnel.healthy(f'http://127.0.0.1:{a.port}') else 'no'}")
        print(f"site     http://127.0.0.1:{a.port - 1}")
        print(f"use      GAMESDB_URL=http://127.0.0.1:{a.port}")
        return
    # stop
    if pid:
        subprocess.run(["kill", pid], capture_output=True)
        LOCAL_PID.unlink()
        print(f"stopped {pid}")
    else:
        print("not running")


# ---- argument parsing ----------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="gamesdb", add_help=False)
    sub = p.add_subparsers(dest="cmd")

    def cmd(name, *args, **kw):
        """A subcommand with these positionals (all with the same options)."""
        s = sub.add_parser(name)
        for a in args:
            s.add_argument(a, **kw)
        return s

    # connection and local state
    cmd("tunnel", "action", choices=["status"])
    cmd("health")
    cmd("state-dir")
    cmd("site")
    cmd("machine")
    s = cmd("work"); s.add_argument("--since"); s.add_argument("--limit", type=int)

    # reports
    s = cmd("reports")
    for f in ("--since", "--game", "--visibility", "--triage-status"):
        s.add_argument(f)
    s.add_argument("--limit", type=int)
    cmd("report", "id", type=int)
    s = cmd("attachments", "id", type=int); s.add_argument("--out")
    s = cmd("attachment", "id", type=int); s.add_argument("--out", required=True)
    s = cmd("comment", "id", type=int)
    g = s.add_mutually_exclusive_group(required=True); g.add_argument("--body"); g.add_argument("--file")
    s.add_argument("--name", action="append", help="extra literal to refuse (reporter name, ...)")
    s.add_argument("--allow", action="append", help="regex of a lint match checked to be public")
    s.add_argument("--allow-link", action="append",
                   help="domain (or code-host/owner, e.g. github.com/owner) checked to be a public reference")
    s.add_argument("--private-source", action="store_true",
                   help="notes built from private attachments: refuse anything that shows it")
    s.add_argument("--dry-run", action="store_true", help="only check; works offline")
    s = cmd("triage-status", "id", type=int); s.add_argument("status")
    s = cmd("fix-branch")
    s.add_argument("repo", nargs="?"); s.add_argument("--branch"); s.add_argument("--ref", default="HEAD")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--whoami", action="store_true")
    s = cmd("visibility", "kind", choices=["report", "comment"])
    s.add_argument("id", type=int); s.add_argument("value", choices=["visible", "hidden", "removed"])

    # moderation
    s = cmd("flags"); s.add_argument("--limit", type=int)
    s = cmd("flag-ignore", "kind", choices=["report", "comment"]); s.add_argument("id", type=int)
    s = cmd("spam"); s.add_argument("--pending", action="store_true"); s.add_argument("--limit", type=int)
    s.add_argument("--with-network", action="store_true")
    s = cmd("spam-feedback", "id", type=int); s.add_argument("value", choices=["spam", "ham"])
    for name in ("user", "ban", "unban", "attachment-delete", "game-refresh"):
        cmd(name, "id", type=int)
    s = cmd("deletion-requests"); s.add_argument("--status", default="open")
    s = cmd("deletion-request", "id", type=int); s.add_argument("value", choices=["done", "rejected", "redacted"])
    s = cmd("report-edit", "id", type=int); s.add_argument("--description"); s.add_argument("--repro")
    s = cmd("comment-edit", "id", type=int); s.add_argument("--body", required=True)

    # Auto-Triage availability
    cmd("owned")
    s = cmd("owned-add"); s.add_argument("storefront", nargs="?"); s.add_argument("id", nargs="?")
    s.add_argument("--free", action="store_true"); s.add_argument("--json", help="file (or -) with a JSON array of entries")
    cmd("owned-remove", "storefront", "id")
    s = cmd("triage-requests"); s.add_argument("--status", default="open")
    s = cmd("triage-request", "id", type=int); s.add_argument("value", choices=["open", "done", "rejected"])
    cmd("versions")

    # attachments and personal data (offline)
    s = cmd("frames", "video", "out")
    s.add_argument("count", nargs="?", type=int, default=12); s.add_argument("--at", type=float, action="append")
    s.add_argument("--every", type=float); s.add_argument("--from", dest="start", type=float)
    s.add_argument("--to", dest="end", type=float)
    cmd("plist", "plist", "out")
    cmd("unzip", "zip", "dest")
    s = cmd("redact", "files", nargs="+"); s.add_argument("--name", action="append"); s.add_argument("--out")
    s = cmd("lint", "file"); s.add_argument("--name", action="append"); s.add_argument("--allow", action="append")
    cmd("qemu-args", "log")

    # sessions
    s = cmd("session", "action", choices=["note", "get", "list", "blocked", "unblocked", "later"])
    s.add_argument("report", nargs="?"); s.add_argument("session_id", nargs="?")
    s.add_argument("--reason"); s.add_argument("--scope", default="job")
    s.add_argument("--details", help="text, or @FILE"); s.add_argument("--note")
    s.add_argument("--keep-vm", action="store_true",
                   help="blocked: keep the UTM lease and the running VM for the person (a Steam sign-in)")
    s.add_argument("--hours", help="later: resume after this many hours (default 1)")
    s.add_argument("--handoff", help="later: the handoff file to resume from")

    # local instance (testing)
    s = cmd("local", "action", choices=["from-backup", "start", "stop", "status"])
    s.add_argument("--checkout"); s.add_argument("--data"); s.add_argument("--port", type=int, default=18081)
    s.add_argument("--tar"); s.add_argument("--force", action="store_true")
    return p


# commands that need no API
OFFLINE = {
    "state-dir": lambda a: print(tunnel.state_dir()),
    "site": lambda a: print(tunnel.site()),
    "machine": lambda a: print(tunnel.holder()),
    "tunnel": lambda a: tunnel.status(),
    "fix-branch": fixbranch.main,
    "session": cmd_session,
    "local": cmd_local,
    "frames": cmd_frames,
    "plist": lambda a: attachments.plist(a.plist, a.out),
    "unzip": lambda a: attachments.unzip(a.zip, a.dest),
    "redact": cmd_redact,
    "lint": cmd_lint,
    "qemu-args": cmd_qemu_args,
}

# API commands
COMMANDS = {
    "attachments": cmd_attachments, "attachment": cmd_attachment, "comment": cmd_comment,
    "triage-status": cmd_triage_status, "spam": cmd_spam, "deletion-request": cmd_deletion_request,
    "report-edit": cmd_report_edit, "comment-edit": cmd_comment_edit,
    "owned-add": cmd_owned_add, "owned-remove": cmd_owned_remove,
}


def simple(api, a):
    """API commands that are one call: their answer is the output."""
    st = lambda v: "" if v == "all" else v  # noqa: E731
    return {
        "health": lambda: api.ok("GET", "/internal/healthz"),
        "work": lambda: api.ok("GET", "/internal/work" + q(since=a.since, limit=a.limit)),
        "reports": lambda: api.ok("GET", "/internal/reports" + q(
            since=a.since, game=a.game, visibility=a.visibility, triage_status=a.triage_status,
            limit=a.limit)),
        "report": lambda: api.ok("GET", f"/internal/reports/{a.id}"),
        "visibility": lambda: api.ok("POST", f"/internal/{a.kind}s/{a.id}/visibility", {"visibility": a.value}),
        "flags": lambda: api.ok("GET", "/internal/flags" + q(limit=a.limit)),
        "flag-ignore": lambda: api.ok("POST", f"/internal/flags/{a.kind}/{a.id}/ignore"),
        "spam-feedback": lambda: api.ok("POST", f"/internal/spam-log/{a.id}/feedback", {"feedback": a.value}),
        "user": lambda: api.ok("GET", f"/internal/users/{a.id}"),
        "ban": lambda: api.ok("POST", f"/internal/users/{a.id}/ban"),
        "unban": lambda: api.ok("POST", f"/internal/users/{a.id}/unban"),
        # an explicit empty status is "all" here: without one the server lists the open ones
        "deletion-requests": lambda: api.ok("GET", f"/internal/deletion-requests?status={st(a.status)}"),
        "attachment-delete": lambda: api.ok("DELETE", f"/internal/attachments/{a.id}",
                                            needs="DELETE /internal/attachments/{id}"),
        "owned": lambda: api.ok("GET", "/internal/triage/owned"),
        "triage-requests": lambda: api.ok("GET", "/internal/triage/requests" + q(status=st(a.status))),
        "triage-request": lambda: api.ok("POST", f"/internal/triage/requests/{a.id}/status", {"status": a.value}),
        "versions": lambda: api.ok("GET", "/internal/utm-versions"),
        "game-refresh": lambda: api.ok("POST", f"/internal/games/{a.id}/refresh"),
    }.get(a.cmd)


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] == "backup":
        from gamesdb import backup
        return backup.main(argv[1:])
    if argv[0] == "dispatch":
        from gamesdb import dispatch
        return dispatch.main(argv[1:])
    a = parser().parse_args(argv)
    if a.cmd in OFFLINE:
        return OFFLINE[a.cmd](a) or 0
    if a.cmd == "comment" and a.dry_run:
        body = comment_body(a)
        note(f"checks passed, {len(body)} characters; not posted (--dry-run)")
        return 0
    api = Api()
    call = simple(api, a)
    if call:
        out(call())
        return 0
    return COMMANDS[a.cmd](api, a) or 0


if __name__ == "__main__":
    tunnel.cli(main)
