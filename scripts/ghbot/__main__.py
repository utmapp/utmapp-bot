"""utmbot gh — pick up GitHub pull request reviews and issues for the bot account.

usage: utmbot gh <command> ...   (python3 <plugin>/scripts/ghbot)

The poller (a LaunchAgent github-setup installs; no games-db needed):
  run                       poll notifications every GHBOT_POLL seconds (default 120) and
                            look after the sessions every minute
  poll [--dry-run]          one poll and one round of starting work (--dry-run: what
                            would be queued and started; claims nothing)
  status [--scan]           the account, the cursor, every PR this Mac knows; --scan
                            also lists the open PRs that carry this account's 👀
  setup [--paused] [--check] [--uninstall]   see skills/github-setup
  pause | resume            stop starting sessions (running ones go on) / start again
  retry PR                  resume a failed or blocked PR's session at the next round
  release PR [--force]      delete this account's 👀 on PR (whichever Mac set it): the
                            PR is free for any Mac; --force also when this Mac runs it

For sessions the poller started (a person may use them too):
  comment PR --round R --body-file F [--in-reply-to ID]
                            post a comment (a reply in review thread ID) marked with its
                            round; exits 3 with DUPLICATE when this account already
                            answered the round from another Mac (discard your work)
  review PR --round R --event approve|request-changes|comment --body-file F
                            submit a review on the PR's head, refused (DUPLICATE) the same
                            way, and when this account already reviewed that head
  pr ISSUE --round R --check | --record PR
                            around UTM's own utm-submit, which opens a fix's PR (this never
                            does): --check refuses a round no org member asked in and one this
                            account already answered (DUPLICATE), and prints the marker line
                            for the PR body; --record notes the PR utm-submit opened
  session blocked --reason TEXT | later --reason TEXT [--hours H]
                            this session needs a person / wants to be resumed later

Who may call the bot, and how the Macs that share the account divide the
work: scripts/ghbot/poller.py.  How sessions run: scripts/ghbot/jobs.py.
Settings: ~/.utmapp-bot/github.env (GHBOT_REPO, GHBOT_ORG, GHBOT_POLL,
GHBOT_REVIEWS_PER_DAY, GHBOT_MAX_SESSIONS, GHBOT_RELEASE_DAYS, GHBOT_MODEL,
GHBOT_PLUGIN_DIR, GHBOT_WORKDIR, CLAUDE_BIN).  State: ~/.utmapp-bot/github/.
"""

import argparse
import fcntl
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentlib.claude import login_expiry  # noqa: E402
from ghbot import jobs, state as S  # noqa: E402
from ghbot.gh import GitHub, GitHubError  # noqa: E402
from ghbot.poller import Poller  # noqa: E402

MARKER = "<!-- utmapp-bot round={} -->"


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---- the poller ---------------------------------------------------------------------------

def poll_once(st, p, cfg, dry_run=False):
    p.dry = dry_run
    if (st.get("gh_cooldown_until") or 0) > time.time() and not dry_run:
        return
    try:
        nums = p.poll(st)
    except GitHubError as e:
        n = st.get("poll_failures", 0) + 1
        st["poll_failures"] = n
        st["gh_cooldown_until"] = e.retry_at or time.time() + min(120 * 2 ** n, 3600)
        S.log(f"notifications unreachable ({n} in a row): {e}")
        return
    st["poll_failures"] = 0
    for n in nums:
        try:
            what = p.process(st, n)
        except GitHubError as e:
            what = f"error: {e}"
            if e.retry_at:
                st["gh_cooldown_until"] = e.retry_at
                break
        if dry_run or not what.startswith("nothing"):
            print(f"#{n}: {what}")
    jobs.schedule(st, cfg, dry_run)


def check_login(st, cfg):
    exp = login_expiry()
    if not exp:
        return
    sent = st.setdefault("login_notices", {})
    for days in (3, 1):
        k = f"{int(exp)}:{days}"
        if exp - time.time() < days * 86400 and k not in sent:
            sent[k] = S.utc()
            jobs.phone(st, cfg, f"Claude Code login on the GitHub review Mac expires "
                                f"{time.strftime('%a %b %-d %H:%M', time.localtime(exp))}: run `claude` there "
                                "and /login, or reviews stop.")
            break


def run():
    S.private(S.D)
    lockf = open(S.D / "run.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another poller is running", file=sys.stderr)
        return 0
    cfg = S.config()
    if not S.load().get("installed_at"):
        S.log("not set up on this Mac (`utmbot gh setup`); the poller does nothing until then")
        while not S.load().get("installed_at"):
            time.sleep(300)
    p = Poller(GitHub(), cfg)
    plugin, work = jobs.cfg_paths(cfg)
    S.log(f"poller up: {cfg['GHBOT_REPO']} (org {cfg['GHBOT_ORG']}), plugin {plugin}, workdir {work}")
    try:
        p.sweep()  # PRs released while this poller was not running
    except GitHubError as e:
        S.log(f"startup check of owned PRs failed: {e}")
    last_poll = last_daily = last_login = 0
    while True:
        try:
            with S.locked("main"):
                st = S.load()
            every = max(int(cfg.get("GHBOT_POLL") or 120), int(st.get("poll_interval") or 60))
            jobs.supervise(st, cfg)
            jobs.supervise_notices(st)
            if time.time() - last_poll >= every:
                last_poll = time.time()
                poll_once(st, p, cfg)
            else:
                jobs.schedule(st, cfg)
            if time.time() - last_daily >= 86400:
                last_daily = time.time()
                p.auto_release(st)
            if time.time() - last_login >= 3600:
                last_login = time.time()
                check_login(st, cfg)
            with S.locked("main"):
                S.save(st)
        except Exception as e:  # keep the agent alive; launchd would restart it anyway
            S.log(f"error: {type(e).__name__}: {e}")
        time.sleep(60)


# ---- status -----------------------------------------------------------------------------

def status(scan=False):
    cfg = S.config()
    st = S.load()
    print(f"repo       {cfg['GHBOT_REPO']} (members of {cfg['GHBOT_ORG']} may call the bot)")
    print(f"paused     {'YES: ' + S.PAUSED.read_text().strip() if S.PAUSED.exists() else 'no'}")
    print(f"last poll  {st.get('last_poll')}  since {st.get('since')}  installed {st.get('installed_at')}")
    if st.get("poll_failures"):
        print(f"github     unreachable {st['poll_failures']} time(s) in a row")
    if (st.get("limit_until") or 0) > time.time():
        print(f"limit      usage limit; no new sessions before {S.utc(st['limit_until'])}")
    print(f"reviews    {jobs.reviews_today(st)} started in the last 24 h (cap {cfg['GHBOT_REVIEWS_PER_DAY'] if int(cfg['GHBOT_REVIEWS_PER_DAY'] or 0) > 0 else 'none'})")
    for r in S.pr_all():
        j = r.get("job") or {}
        extra = ""
        if j.get("status") == "waiting":
            extra = f" until {S.utc(j.get('resume_after') or 0)} ({j.get('wait_reason')})"
        if j.get("status") == "blocked":
            extra = f": {j.get('blocker')}"
        own = {"mine": "this Mac", "other": "another Mac", None: "nobody"}.get(r.get("owner"), r.get("owner"))
        print(f"PR {r['pr']:<6} owner {own:11s} {j.get('status', 'idle'):8s} "
              f"session {(r.get('session_id') or '-')[:8]} pending {len(r.get('pending') or [])}{extra}"
              f"  {(r.get('title') or '')[:50]}")
    if scan:
        p = Poller(GitHub(), cfg)
        known = {r["pr"]: r for r in S.pr_all()}
        for pr in p.gh.paged(f"/repos/{cfg['GHBOT_REPO']}/pulls", {"state": "open"}, limit=500):
            if p.my_eyes(pr["number"]):
                k = known.get(pr["number"]) or {}
                who = "this Mac" if k.get("owner") == "mine" else "another Mac (or a lost record)"
                print(f"CLAIMED    PR {pr['number']} by {who}: {pr['title'][:60]}")


# ---- posting from a session -------------------------------------------------------------------

def _round_ok(r):
    if not re.fullmatch(r"[a-z]{1,3}\d{1,20}", r or ""):
        die(f"not a round: {r!r} (the id of the item you answer, e.g. c123 or rr456)")


def _duplicate(n, rnd, rec, why):
    with S.locked():
        cur = S.pr_load(n) or dict(rec or {}, pr=int(n), job={"status": "idle"})
        cur.update(owner="other", lost_race=True, pending=[])
        S.pr_save(cur)
    S.log(f"PR {n}: DUPLICATE round {rnd}: {why}; this Mac lets the PR go")
    print(f"DUPLICATE: {why}. Another Mac on this account already answered; discard your work.")
    sys.exit(3)


def _ours(rec, rnd):
    return set(map(str, ((rec or {}).get("rounds") or {}).get(rnd, [])))


def _record(n, rnd, post_id, reviewed_sha=None):
    with S.locked():
        rec = S.pr_load(n)
        if rec is None:
            return
        rec.setdefault("rounds", {}).setdefault(rnd, []).append(post_id)
        if reviewed_sha:
            rec["reviewed_sha"] = reviewed_sha
        rec["last_activity"] = S.utc()
        S.pr_save(rec)


def cmd_comment(a):
    _round_ok(a.round)
    body = Path(a.body_file).read_text()
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R, n, me = cfg["GHBOT_REPO"], a.pr, p.login().lower()
    rec = S.pr_load(n)
    tag = MARKER.format(a.round)
    mine = _ours(rec, a.round)
    seen = p.gh.paged(f"/repos/{R}/issues/{n}/comments")
    if a.in_reply_to:
        seen += p.gh.paged(f"/repos/{R}/pulls/{n}/comments")
    for c in seen:
        if (c["user"]["login"].lower() == me and tag in (c.get("body") or "")
                and str(c["id"]) not in mine):
            _duplicate(n, a.round, rec, f"comment {c['html_url']} already answers round {a.round}")
    text = body.rstrip() + "\n\n" + tag + "\n"
    if a.in_reply_to:
        r = p.gh.request("POST", f"/repos/{R}/pulls/{n}/comments/{a.in_reply_to}/replies", {"body": text})
    else:
        r = p.gh.request("POST", f"/repos/{R}/issues/{n}/comments", {"body": text})
    kind = ((rec or {}).get("round_kinds") or {}).get(a.round)
    head = p.gh.get(f"/repos/{R}/pulls/{n}")["head"]["sha"] if kind == "review" else None
    _record(n, a.round, r.data["id"], head)
    print(r.data.get("html_url"))


def cmd_pr(a):
    """Bracket UTM's own utm-submit for an issue's fix (it opens the PR; this
    never does): --check before it, --record after it."""
    _round_ok(a.round)
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R, n, me = cfg["GHBOT_REPO"], a.issue, p.login()
    rec = S.pr_load(n) or {}
    tag = MARKER.format(a.round)
    if a.check:
        if os.environ.get("GHBOT_MODE") == "auto" or rec.get("round_members"):
            # only an org member's round builds a fix (the poller recorded who asked)
            if not (rec.get("round_members") or {}).get(a.round):
                die(f"round {a.round} was not asked by an org member: only a member's request opens a PR "
                    "(answer with `gh comment` that a member has to ask)")
        mine = _ours(rec, a.round)
        for pr in p.gh.paged(f"/repos/{R}/pulls", {"state": "all"}, limit=300):
            if pr["user"]["login"].lower() == me.lower() and str(pr["number"]) not in mine \
                    and tag in (pr.get("body") or ""):
                _duplicate(n, a.round, rec, f"{pr['html_url']} already answers round {a.round}")
        print(f"OK: submit with UTM's utm-review, then utm-submit. Put this line at the end of the PR body:\n{tag}\n"
              f"Then: utmbot gh pr {n} --round {a.round} --record <PR number or URL>")
        return 0
    num = int(re.search(r"(\d+)$", a.record.rstrip("/"))[1])
    pr = p.gh.get(f"/repos/{R}/pulls/{num}")
    if pr["user"]["login"].lower() != me.lower():
        die(f"PR {num} is not this account's")
    if tag not in (pr.get("body") or ""):
        print(f"WARNING: PR {num}'s body lacks {tag}: another Mac cannot see that it answers this round",
              file=sys.stderr)
    _record(n, a.round, str(num))
    with S.locked():
        cur = S.pr_load(n)
        if cur is not None and num not in cur.setdefault("opened_prs", []):
            cur["opened_prs"].append(num)
            S.pr_save(cur)
    print(pr.get("html_url"))


EVENTS = {"approve": "APPROVE", "request-changes": "REQUEST_CHANGES", "comment": "COMMENT"}


def cmd_review(a):
    _round_ok(a.round)
    body = Path(a.body_file).read_text()
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R, n, me = cfg["GHBOT_REPO"], a.pr, p.login().lower()
    pr = p.gh.get(f"/repos/{R}/pulls/{n}")
    if pr["user"]["login"].lower() == me:
        die("this account wrote the PR: it cannot review it (post a comment instead)")
    head = pr["head"]["sha"]
    rec = S.pr_load(n)
    tag = MARKER.format(a.round)
    mine = _ours(rec, a.round)
    for v in p.gh.paged(f"/repos/{R}/pulls/{n}/reviews"):
        if v["user"]["login"].lower() != me or str(v["id"]) in mine:
            continue
        if tag in (v.get("body") or ""):
            _duplicate(n, a.round, rec, f"review {v.get('html_url')} already answers round {a.round}")
        if v.get("commit_id") == head and v.get("state") in ("APPROVED", "CHANGES_REQUESTED") \
                and EVENTS[a.event] != "COMMENT":
            _duplicate(n, a.round, rec, f"this account already reviewed {head[:10]} ({v['state'].lower()})")
    r = p.gh.request("POST", f"/repos/{R}/pulls/{n}/reviews",
                     {"event": EVENTS[a.event], "commit_id": head, "body": body.rstrip() + "\n\n" + tag + "\n"})
    _record(n, a.round, r.data["id"], head)
    print(r.data.get("html_url"))


def cmd_session(a):
    sid = os.environ.get("GHBOT_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid or not re.fullmatch(r"[0-9a-f-]{36}", sid):
        die("only a session the GitHub poller started can report itself (no GHBOT_SESSION)")
    if a.action == "blocked":
        S.signal(sid, "blocked", reason=a.reason)
        print("recorded; leave UTM clean and end your turn. The user is notified on this Mac.")
    else:
        S.signal(sid, "later", reason=a.reason, hours=a.hours)
        print(f"recorded; end your turn. The poller resumes this session in {a.hours:g} h")


# ---- by hand ------------------------------------------------------------------------------

def cmd_release(a):
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    with S.locked():
        rec = S.pr_load(a.pr)
        if rec and (rec.get("job") or {}).get("status") == "running" and not a.force:
            die(f"PR {a.pr} has a session running on this Mac; --force releases it anyway")
        gone = p.release(a.pr)
        if rec:
            rec.update(owner=None, reaction_id=None, released=S.utc(), lost_race=False)
            S.pr_save(rec)
    print(f"PR {a.pr}: " + ("released (this account's 👀 removed)" if gone else "this account had no 👀 on it"))


def cmd_retry(a):
    with S.locked():
        rec = S.pr_load(a.pr)
        if not rec:
            die(f"this Mac knows no PR {a.pr}")
        rec["job"].update(status="waiting", resume_after=0, attempts=0,
                          resume_prompt=rec["job"].get("resume_prompt") or (
                              "The blocker was cleared by hand. Continue where you stopped."
                              if rec["job"].get("status") == "blocked" else "Continue where you stopped."))
        S.clear_signal(rec.get("session_id") or "-")
        S.pr_save(rec)
    print(f"PR {a.pr} will be resumed at the next round")


def main(argv):
    if argv[:1] == ["setup"]:  # its own options (argparse would claim them here)
        from ghbot import setup
        return setup.main(argv[1:])
    ap = argparse.ArgumentParser(prog="utmbot gh", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    s = sub.add_parser("poll"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--scan", action="store_true")
    sub.add_parser("setup", help="see `utmbot gh setup --help`")
    sub.add_parser("pause"); sub.add_parser("resume")
    s = sub.add_parser("retry"); s.add_argument("pr", type=int)
    s = sub.add_parser("release"); s.add_argument("pr", type=int); s.add_argument("--force", action="store_true")
    s = sub.add_parser("comment"); s.add_argument("pr", type=int); s.add_argument("--round", required=True)
    s.add_argument("--body-file", required=True); s.add_argument("--in-reply-to", type=int)
    s = sub.add_parser("review"); s.add_argument("pr", type=int); s.add_argument("--round", required=True)
    s.add_argument("--event", required=True, choices=sorted(EVENTS)); s.add_argument("--body-file", required=True)
    s = sub.add_parser("pr"); s.add_argument("issue", type=int); s.add_argument("--round", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true"); g.add_argument("--record", metavar="PR")
    s = sub.add_parser("session"); s.add_argument("action", choices=["blocked", "later"])
    s.add_argument("--reason", required=True); s.add_argument("--hours", type=float, default=1.0)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            return run()
        if a.cmd == "poll":
            cfg = S.config()
            with S.locked("main"):
                st = S.load()
            if not st.get("installed_at"):
                die("not set up on this Mac: `utmbot gh setup` (skill github-setup)")
            poll_once(st, Poller(GitHub(), cfg), cfg, a.dry_run)
            if not a.dry_run:
                with S.locked("main"):
                    S.save(st)
            return 0
        if a.cmd == "status":
            status(a.scan)
            return 0
        if a.cmd == "pause":
            S.private(S.D)
            S.PAUSED.write_text("paused by hand\n")
            print("PAUSED (running sessions go on; nothing new starts)")
            return 0
        if a.cmd == "resume":
            S.PAUSED.unlink(missing_ok=True)
            print("RESUMED")
            return 0
        return {"retry": cmd_retry, "release": cmd_release, "comment": cmd_comment,
                "review": cmd_review, "pr": cmd_pr, "session": cmd_session}[a.cmd](a) or 0
    except GitHubError as e:
        die(str(e))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
