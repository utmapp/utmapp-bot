#!/usr/bin/env python3
"""e2e-seed.py - synthetic content in the LOCAL games db, for end-to-end tests.

Testing only: writes straight into ~/.utmapp-bot/gamesdb-local/data/gamesdb.sqlite
(the instance e2e-local.sh runs), which the server reads live.  Never point it
at production.  Everything it creates is recorded in a ledger, and `cleanup` removes it.

  e2e-seed.py user NAME                                  a synthetic account (prints its id)
  e2e-seed.py report --game IGDB_ID [--user ID] [--rating R] [--issue T]
              [--backend dxmt|d3dmetal|unknown] [--utm 5.0.8] [--desc TEXT] [--repro TEXT]
              [--wine yes|no|unknown] [--attach FILE ...] [--status S]
  e2e-seed.py reply REPORT TEXT                           the reporter answers (a comment)
  e2e-seed.py comment REPORT TEXT [--user ID]             someone else comments
  e2e-seed.py flag report|comment ID [--times N]          N distinct users flag it
  e2e-seed.py spam comment|report TEXT [--user ID]        an Akismet-rejected submission
  e2e-seed.py deletion REPORT --reason TEXT               the author asks to delete it
  e2e-seed.py request gift|demo --game IGDB_ID [--demo-url URL] [--notes TEXT]
  e2e-seed.py status REPORT STATUS|clear                  set triage_status directly
  e2e-seed.py games [QUERY]                               games the local db knows
  e2e-seed.py cleanup                                     remove everything seeded
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("UTMAPP_BOT_DATA", "~/.utmapp-bot")).expanduser()
DB = Path(os.environ.get("E2E_DB", DATA / "gamesdb-local" / "data" / "gamesdb.sqlite"))
UPLOADS = DB.parent / "uploads"
LEDGER = DATA / "gamesdb-e2e" / "seeded.json"


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def db():
    if "gamesdb-local" not in str(DB) and not os.environ.get("E2E_DB"):
        die("refusing: not the local instance's database")
    if not DB.exists():
        die(f"no local db at {DB} (scripts/gamesdb/e2e-local.sh start CHECKOUT)")
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def remember(kind, rid):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    led = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
    led.append([kind, rid])
    LEDGER.write_text(json.dumps(led))


def now():
    return int(time.time())


def touch(c, report_id):
    c.execute("UPDATE reports SET updated_at=?, comment_count=(SELECT count(*) FROM comments "
              "WHERE report_id=? AND visibility='visible') WHERE id=?", (now(), report_id, report_id))


def user(c, name):
    # names are as given (a test may want a realistic one); cleanup goes by the ledger
    row = c.execute("SELECT id FROM users WHERE display_name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = c.execute("INSERT INTO users (email_hash, display_name, created_at, host_macos_version, "
                    "host_device_model, host_chip, host_ram_gb) VALUES (?,?,?,?,?,?,?)",
                    (secrets.token_hex(16), name, now(), "26.1", "Mac16,10", "Apple M4 Pro", 48))
    remember("user", cur.lastrowid)
    return cur.lastrowid


def report_author(c, rid):
    row = c.execute("SELECT user_id FROM reports WHERE id=?", (rid,)).fetchone()
    if not row:
        die(f"no report {rid}")
    return row["user_id"]


def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("user"); s.add_argument("name")
    s = sub.add_parser("report")
    s.add_argument("--game", type=int, required=True); s.add_argument("--user", type=int)
    s.add_argument("--rating", default="in_game_issues"); s.add_argument("--issue", default="graphics")
    s.add_argument("--backend", default="unknown"); s.add_argument("--utm", default="5.0.8")
    s.add_argument("--desc", default=""); s.add_argument("--repro", default="")
    s.add_argument("--wine", default="unknown"); s.add_argument("--attach", nargs="*", default=[])
    s.add_argument("--macos", default="26.1"); s.add_argument("--model", default="Mac16,10")
    s.add_argument("--chip", default="Apple M4 Pro"); s.add_argument("--ram", type=int, default=48)
    s.add_argument("--status")
    s = sub.add_parser("reply"); s.add_argument("report", type=int); s.add_argument("text")
    s = sub.add_parser("comment"); s.add_argument("report", type=int); s.add_argument("text")
    s.add_argument("--user", type=int)
    s = sub.add_parser("flag"); s.add_argument("kind", choices=["report", "comment"])
    s.add_argument("id", type=int); s.add_argument("--times", type=int, default=3)
    s = sub.add_parser("spam"); s.add_argument("kind", choices=["comment", "report"])
    s.add_argument("text"); s.add_argument("--user", type=int)
    s = sub.add_parser("deletion"); s.add_argument("report", type=int); s.add_argument("--reason", required=True)
    s = sub.add_parser("request"); s.add_argument("kind", choices=["gift", "demo"])
    s.add_argument("--game", type=int, required=True); s.add_argument("--demo-url"); s.add_argument("--notes")
    s = sub.add_parser("status"); s.add_argument("report", type=int); s.add_argument("status")
    s = sub.add_parser("games"); s.add_argument("query", nargs="?")
    sub.add_parser("cleanup")
    a = p.parse_args(argv)
    c = db()
    with c:
        if a.cmd == "user":
            print(user(c, a.name))
        elif a.cmd == "report":
            g = c.execute("SELECT id, name FROM igdb_games WHERE id=?", (a.game,)).fetchone()
            if not g:
                die(f"IGDB game {a.game} is not in the local db (`games QUERY`)")
            if not c.execute("SELECT 1 FROM games WHERE igdb_game_id=?", (a.game,)).fetchone():
                c.execute("INSERT INTO games (igdb_game_id, activated_at, updated_at) VALUES (?,?,?)",
                          (a.game, now(), now()))
            uid = a.user or user(c, "e2e-reporter")
            v = c.execute("SELECT id FROM utm_versions WHERE version=?", (a.utm,)).fetchone()
            if not v:
                mj, mn, pt = (int(x) for x in a.utm.split("."))
                vid = c.execute("INSERT INTO utm_versions (version, v_major, v_minor, v_patch, is_prerelease, "
                                "released_at, created_at) VALUES (?,?,?,?,0,?,?)",
                                (a.utm, mj, mn, pt, now(), now())).lastrowid
            else:
                vid = v["id"]
            cid = c.execute("INSERT INTO configurations (utm_version_id, guest_os, graphics_backend, "
                            "macos_version, device_model, chip, ram_gb, created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (vid, "windows", a.backend, a.macos, a.model, a.chip, a.ram, now())).lastrowid
            rid = c.execute("INSERT INTO reports (game_id, user_id, rating, config_id, issue_type, description_md, "
                            "repro_md, wine_same_issue, triage_status, triage_updated_at, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (a.game, uid, a.rating, cid, a.issue, a.desc, a.repro, a.wine, a.status,
                             now() if a.status else None, now(), now())).lastrowid
            remember("report", rid)
            for f in a.attach:
                f = Path(f)
                data = f.read_bytes()
                rel = f"u{uid}/{secrets.token_hex(8)}_{f.name}"
                (UPLOADS / f"u{uid}").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, UPLOADS / rel)
                mime = {".txt": "text/plain", ".log": "text/plain", ".png": "image/png", ".jpg": "image/jpeg",
                        ".mp4": "video/mp4", ".zip": "application/zip", ".json": "application/json"}.get(
                            f.suffix.lower(), "application/octet-stream")
                c.execute("INSERT INTO attachments (report_id, user_id, filename, mime, size_bytes, sha256, "
                          "storage_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
                          (rid, uid, f.name, mime, len(data), hashlib.sha256(data).hexdigest(), rel, now()))
            c.execute("UPDATE games SET report_count=(SELECT count(*) FROM reports WHERE game_id=?), updated_at=? "
                      "WHERE igdb_game_id=?", (a.game, now(), a.game))
            print(f"report {rid}: {g['name']} (user {uid}, {len(a.attach)} attachment(s))")
        elif a.cmd in ("reply", "comment"):
            uid = report_author(c, a.report) if a.cmd == "reply" else (a.user or user(c, "e2e-commenter"))
            cid = c.execute("INSERT INTO comments (report_id, user_id, body_md, created_at) VALUES (?,?,?,?)",
                            (a.report, uid, a.text, now())).lastrowid
            remember("comment", cid)
            touch(c, a.report)
            print(f"comment {cid} on report {a.report} by user {uid}")
        elif a.cmd == "flag":
            for i in range(a.times):
                uid = user(c, f"e2e-flagger{i + 1}")
                c.execute("INSERT OR IGNORE INTO content_flags (kind, target_id, user_id, created_at) VALUES (?,?,?,?)",
                          (a.kind, a.id, uid, now()))
            print(f"{a.kind} {a.id} flagged by {a.times} user(s)")
        elif a.cmd == "spam":
            uid = a.user or user(c, "e2e-spammer")
            field = "body" if a.kind == "comment" else "description"
            payload = json.dumps({"fields": {field: a.text}, "ip": "203.0.113.7", "ua": "Mozilla/5.0 (e2e)",
                                  "author": f"user {uid}", "type": a.kind})
            sid = c.execute("INSERT INTO spam_log (user_id, kind, payload_json, akismet_result, created_at) "
                            "VALUES (?,?,?,?,?)", (uid, a.kind, payload, "spam", now())).lastrowid
            remember("spam", sid)
            print(f"spam_log {sid}")
        elif a.cmd == "deletion":
            did = c.execute("INSERT INTO deletion_requests (report_id, user_id, reason, created_at) VALUES (?,?,?,?)",
                            (a.report, report_author(c, a.report), a.reason, now())).lastrowid
            remember("deletion", did)
            print(f"deletion request {did} on report {a.report}")
        elif a.cmd == "request":
            if not c.execute("SELECT 1 FROM games WHERE igdb_game_id=?", (a.game,)).fetchone():
                c.execute("INSERT INTO games (igdb_game_id, activated_at, updated_at) VALUES (?,?,?)",
                          (a.game, now(), now()))
            tid = c.execute("INSERT INTO triage_requests (game_id, user_id, kind, storefront, demo_url, notes, created_at) "
                            "VALUES (?,?,?,?,?,?,?)", (a.game, user(c, "e2e-donor"), a.kind, "steam", a.demo_url,
                                                        a.notes, now())).lastrowid
            remember("request", tid)
            print(f"{a.kind} request {tid} for game {a.game}")
        elif a.cmd == "status":
            st = None if a.status == "clear" else a.status
            c.execute("UPDATE reports SET triage_status=?, triage_updated_at=?, updated_at=? WHERE id=?",
                      (st, now(), now(), a.report))
            print(f"report {a.report}: triage_status {st}")
        elif a.cmd == "games":
            q = f"%{a.query or ''}%"
            for r in c.execute("SELECT g.id, g.name, (SELECT group_concat(w.url, ' ') FROM igdb_websites w "
                               "WHERE w.game_id=g.id AND w.url LIKE '%steampowered%') steam FROM igdb_games g "
                               "WHERE g.name LIKE ? ORDER BY g.id LIMIT 30", (q,)):
                print(f"{r['id']:8d} {r['name'][:50]:50s} {r['steam'] or ''}")
        elif a.cmd == "cleanup":
            led = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
            order = {"comment": 0, "spam": 1, "deletion": 2, "request": 3, "report": 4, "user": 5}
            table = {"comment": "comments", "spam": "spam_log", "deletion": "deletion_requests",
                     "request": "triage_requests", "report": "reports", "user": "users"}
            for kind, rid in sorted(led, key=lambda x: order[x[0]]):
                if kind == "user":
                    c.execute("DELETE FROM content_flags WHERE user_id=?", (rid,))
                c.execute(f"DELETE FROM {table[kind]} WHERE id=?", (rid,))
            LEDGER.unlink(missing_ok=True)
            print(f"removed {len(led)} seeded rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
