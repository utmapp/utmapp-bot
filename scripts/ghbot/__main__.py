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
  catchup [--apply]         list the issues and PRs this Mac has seen where the author wrote
                            after this account's last comment and nothing took it up;
                            --apply queues them (each gets a session and a public reply)
  release PR [--force]      delete this account's 👀 on PR (whichever Mac set it): the
                            PR is free for any Mac; --force also when this Mac runs it

For sessions the poller started (a person may use them too).  In such a
session (GHBOT_MODE=auto) every one of them works only on the session's own
thread (or the pull requests it opened), and every text is checked before
it is posted (outbound, below): nothing that identifies this Mac, no links
outside the reference list (--allow-link DOMAIN for one you looked up
yourself, never one from the thread), no secrets or home-folder names
(--allow REGEX for a match you checked is public):
  comment PR --round R --body-file F [--in-reply-to ID]
                            post a comment (a reply in review thread ID) marked with its
                            round; exits 3 with DUPLICATE when this account already
                            answered the round from another Mac (discard your work)
  review PR --round R --event approve|request-changes|comment --body-file F
         [--comments-file JSON]
                            submit a review on the PR's head, refused (DUPLICATE) the same
                            way, and when this account already reviewed that head.
                            --comments-file: inline comments submitted with it, a JSON list
                            of {"path", "line", "body"} (and "start_line" for a range), each
                            on lines of the PR's diff; a body holding a ```suggestion block
                            is a change the author can apply with one click
  pr ISSUE --round R [--repo OWNER/NAME] --check | --record PR
                            around whatever opens a fix's PR (this never does): UTM's own
                            utm-submit, or, with --repo, `gamesdb fix-branch --pr` for another
                            repository the org owns.  --check refuses a round the poller never
                            delivered and one this account already answered there (DUPLICATE),
                            says whether an org member asked (anyone else's round: a small
                            fix only),
                            and prints the marker line for the PR body; --record notes the PR
  label N --add NAME | --remove NAME
                            one of the triage labels ("not reproducible", "needs info",
                            "enhancement", "external") on or off this session's thread
  needs-info N --round R --body-file F
                            ask the author for details: posts the comment, adds "needs info"
                            and starts the clock (the poller closes the issue after
                            GHBOT_NEEDS_INFO_DAYS without an answer from them)
  duplicate N --of M --round R --body-file F
                            post the comment and close N as a duplicate of M, GitHub's own
                            "closed as duplicate" with its link to M (no label)
  no-template N --round R --body-file F
                            an issue filed blank with too little to go on: post the comment
                            (it asks for one of the issue templates) and close N as not planned
  reopen N                  reopen an issue this account closed (the author disagreed)
  attachment N URL          download a file the thread links on GitHub's own attachment
                            hosts into ~/.utmapp-bot/issue/N/attachments/ (owner-only,
                            quarantined, named by index); never any other URL
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
LABELS = ("not reproducible", "needs info", "enhancement", "external")
# what GitHub itself hosts for a thread: files and images people attach to it
ATTACH_HOSTS = ("user-images.githubusercontent.com", "private-user-images.githubusercontent.com",
                "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                "github-production-user-asset-6210df.s3.amazonaws.com")
ATTACH_MAX = 300 << 20
# the classes of scripts/gamesdb/redact.py that never belong in a public reply here.  Addresses,
# VM names and ids are left to the session: a thread's own text is public and full of them.
LINT_ALLOW = (r"(?i)noreply@|@users\.noreply\.github\.com$|@example\.(com|org)$",)
GH_LINKS = ("gitlab.gnome.org", "gitlab.freedesktop.org", "bugs.freebsd.org", "reviews.freebsd.org",
            "bugzilla.kernel.org", "lore.kernel.org", "bugs.launchpad.net", "bugzilla.redhat.com",
            "docs.github.com", "apple.com", "spice-space.org", "docs.swift.org", "forums.swift.org")
LINT_CLASSES = {"USER", "EMAIL", "SECRET", "KEY", "SERIAL", "SID", "STEAMID", "HWID", "URL-CRED"}


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
    try:
        # issues and pull requests nobody called the bot on (they send it no notification)
        nums = sorted(set(nums) | set(p.poll_new(st)))
    except GitHubError as e:
        S.log(f"the issue list is unreachable: {e}")
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
                p.close_stale(st)
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


# ---- what a session may touch, and what may go out ------------------------------------------------

def auto():
    return os.environ.get("GHBOT_MODE") == "auto"


def bound(n, what="post on"):
    """A poller session works on its own thread and on what it opened from it,
    whatever the thread's text asks for."""
    if not auto():
        return
    own = os.environ.get("GHBOT_PR") or ""
    ok = {own}
    for r in S.pr_all():
        if str(r["pr"]) == own or (r.get("session_id") and r.get("session_id") == os.environ.get("GHBOT_SESSION")):
            ok.add(str(r["pr"]))
            ok.update(str(x) for x in r.get("opened_prs") or [])
    if str(n) not in ok:
        die(f"this session works on #{own}: it may not {what} #{n}")


def outbound(body, a):
    """Why this text must not be posted from an unattended session (or
    nothing).  The same checks as an Auto-Triage comment on the games
    database (scripts/gamesdb/redact.py), minus what a public thread
    already shows."""
    if not auto():
        return
    from gamesdb import redact
    if not body.strip():
        die("not posted: empty")
    # beyond the games-db list: upstream trackers the bot cites in UTM threads, and the
    # raw host its own forks' screenshots are served from
    extra = tuple(getattr(a, "allow_link", None) or ()) + GH_LINKS
    owners = redact.CODE_HOSTS["github.com"]

    def ok(u):
        import urllib.parse
        sp = urllib.parse.urlsplit(u if "://" in u else "https://" + u)
        if any((sp.hostname or "").lower() in (d, "www." + d) for d in GH_LINKS):
            return True
        if (sp.hostname or "").lower() == "raw.githubusercontent.com":
            return (sp.path.strip("/").split("/") or [""])[0].lower() in owners
        return redact.link_ok(u, extra)
    bad = [u for u in redact.links(body) if not ok(u.rstrip(".,;:"))]
    if bad:
        die("not posted: it links outside the reference list:\n  " + "\n  ".join(bad[:20]) +
            "\nLink only to public sources you looked up yourself, never to a URL taken from the thread; "
            "--allow-link DOMAIN (or github.com/OWNER) for one you checked is a public reference.")
    host = redact.host_problems(body)
    if host:
        die("not posted: it identifies this Mac: " + ", ".join(host) + ". Leave it out (\"the Mac's native "
            "display size\"); never name the Mac, its accounts, network or paths.")
    found = [f for f in redact.lint_text(body, (), tuple(getattr(a, "allow", None) or ()) + LINT_ALLOW)
             if f[0] in LINT_CLASSES]
    if found:
        die("not posted: it may contain personal data or a secret:\n" + redact.lint_report(found) +
            "\nRemove it (write ~ for a home folder), or --allow REGEX for a match you checked is public.")


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
    bound(a.pr)
    outbound(body, a)
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R, n, me = cfg["GHBOT_REPO"], a.pr, p.login().lower()
    rec = S.pr_load(n)
    tag = MARKER.format(a.round)
    if getattr(a, "extra_mark", None):
        tag = a.extra_mark + "\n" + tag
    mine = _ours(rec, a.round)
    seen = p.gh.paged(f"/repos/{R}/issues/{n}/comments")
    if a.in_reply_to:
        seen += p.gh.paged(f"/repos/{R}/pulls/{n}/comments")
    for c in seen:
        if (c["user"]["login"].lower() == me and MARKER.format(a.round) in (c.get("body") or "")
                and str(c["id"]) not in mine):
            _duplicate(n, a.round, rec, f"comment {c['html_url']} already answers round {a.round}")
    text = body.rstrip() + "\n\n" + tag + "\n"
    if a.in_reply_to:
        r = p.gh.request("POST", f"/repos/{R}/pulls/{n}/comments/{a.in_reply_to}/replies", {"body": text})
    else:
        r = p.gh.request("POST", f"/repos/{R}/issues/{n}/comments", {"body": text})
    kind = ((rec or {}).get("round_kinds") or {}).get(a.round)
    # a review round's post records the head it reviewed (a pull request only:
    # an issue's first round is a "review" round too, and has no head)
    head = None
    if kind == "review" and (rec or {}).get("kind", "pr") == "pr":
        try:
            head = p.gh.get(f"/repos/{R}/pulls/{n}")["head"]["sha"]
        except GitHubError as e:  # the comment is posted: record it whatever happens here
            S.log(f"#{n}: could not read the head after posting: {e}")
    _record(n, a.round, r.data["id"], head)
    print(r.data.get("html_url"))


def cmd_pr(a):
    """Bracket UTM's own utm-submit for an issue's fix (it opens the PR; this
    never does): --check before it, --record after it."""
    _round_ok(a.round)
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R, n, me = cfg["GHBOT_REPO"], a.issue, p.login()
    other = bool(a.repo) and a.repo.lower() != R.lower()
    if other:
        # a fix in another repository of the stack: only one the org owns gets a PR
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", a.repo) or \
                a.repo.split("/")[0].lower() != cfg["GHBOT_ORG"].lower():
            die(f"{a.repo} is not a repository of the {cfg['GHBOT_ORG']} org: a fix there is published as a "
                "branch only (`gamesdb fix-branch`, no --pr)")
        R = a.repo
    rec = S.pr_load(n) or {}
    tag = MARKER.format(a.round)
    if a.check:
        if os.environ.get("GHBOT_MODE") == "auto" or rec.get("round_members"):
            # a round the poller delivered: an org member's, or the author's (their
            # answer in a triage).  Only a member's request builds a large fix.
            rm = rec.get("round_members") or {}
            if a.round not in rm:
                die(f"round {a.round} is not a round the poller delivered for #{n}: a PR answers one of those")
            if not rm[a.round]:
                print("NOTE: this round's author is not an org member. A fix found while answering them may "
                      "be opened when it is small by the skill's rule (cause confirmed, fix built and "
                      "validated before and after); a large fix, or a fix only because they ask for one, "
                      "needs an org member's request.")
        mine = _ours(rec, a.round)
        for pr in p.gh.paged(f"/repos/{R}/pulls", {"state": "all"}, limit=300):
            if pr["user"]["login"].lower() == me.lower() and str(pr["number"]) not in mine \
                    and f"{R}#{pr['number']}" not in mine \
                    and tag in (pr.get("body") or ""):
                _duplicate(n, a.round, rec, f"{pr['html_url']} already answers round {a.round}")
        how = (f"open it with `gamesdb fix-branch <checkout> --branch NAME --pr --title T --body-file F`"
               if other else "submit with UTM's utm-review, then utm-submit")
        print(f"OK: {how}. Put this line at the end of the PR body:\n{tag}\n"
              f"Then: utmbot gh pr {n} --round {a.round}{' --repo ' + R if other else ''} --record <PR number or URL>")
        return 0
    num = int(re.search(r"(\d+)$", a.record.rstrip("/"))[1])
    pr = p.gh.get(f"/repos/{R}/pulls/{num}")
    if pr["user"]["login"].lower() != me.lower():
        die(f"PR {num} is not this account's")
    if tag not in (pr.get("body") or ""):
        print(f"WARNING: PR {num}'s body lacks {tag}: another Mac cannot see that it answers this round",
              file=sys.stderr)
    _record(n, a.round, f"{R}#{num}" if other else str(num))
    with S.locked():
        cur = S.pr_load(n)
        # opened_prs: UTM's, whose comments the poller brings back to this session
        key, val = ("opened_elsewhere", f"{R}#{num}") if other else ("opened_prs", num)
        if cur is not None and val not in cur.setdefault(key, []):
            cur[key].append(val)
            S.pr_save(cur)
    print(pr.get("html_url"))


EVENTS = {"approve": "APPROVE", "request-changes": "REQUEST_CHANGES", "comment": "COMMENT"}


def _inline(path):
    """The review's inline comments, as GitHub's API takes them (the new
    side of the diff: a suggestion replaces the lines it is on)."""
    import json
    try:
        items = json.loads(Path(path).read_text())
    except ValueError as e:
        die(f"{path}: not JSON ({e})")
    if not isinstance(items, list) or not items:
        die(f"{path}: a non-empty JSON list of {{path, line, body}}")
    out = []
    for i, c in enumerate(items):
        if not isinstance(c, dict) or not isinstance(c.get("path"), str) or not isinstance(c.get("line"), int) \
                or not (c.get("body") or "").strip():
            die(f"{path}: item {i} needs \"path\", an integer \"line\" and a \"body\"")
        d = {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]}
        if c.get("start_line") is not None and c["start_line"] != c["line"]:
            if not isinstance(c["start_line"], int) or c["start_line"] > c["line"]:
                die(f"{path}: item {i}: start_line is the first line of the range, before line")
            d.update(start_line=c["start_line"], start_side="RIGHT")
        out.append(d)
    return out


def cmd_review(a):
    _round_ok(a.round)
    body = Path(a.body_file).read_text()
    bound(a.pr)
    outbound(body, a)
    if getattr(a, "comments_file", None):
        for c in _inline(a.comments_file):
            outbound(c["body"], a)
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
    payload = {"event": EVENTS[a.event], "commit_id": head, "body": body.rstrip() + "\n\n" + tag + "\n"}
    if getattr(a, "comments_file", None):
        payload["comments"] = _inline(a.comments_file)
    r = p.gh.request("POST", f"/repos/{R}/pulls/{n}/reviews", payload)
    _record(n, a.round, r.data["id"], head)
    print(r.data.get("html_url"))


# ---- triage of a thread nobody asked about ----------------------------------------------------------

def cmd_label(a):
    name = a.add or a.remove
    if name not in LABELS:
        die(f"only these labels: {', '.join(LABELS)}")
    bound(a.n, "label")
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R = cfg["GHBOT_REPO"]
    if a.add:
        p.gh.request("POST", f"/repos/{R}/issues/{a.n}/labels", {"labels": [name]})
    else:
        p.gh.request("DELETE", f"/repos/{R}/issues/{a.n}/labels/{name.replace(' ', '%20')}", ok=(200, 204, 404))
        if name == "needs info":
            with S.locked():
                rec = S.pr_load(a.n)
                if rec and rec.pop("needs_info", None):
                    S.pr_save(rec)
    print(f"#{a.n}: {'added' if a.add else 'removed'} \"{name}\"")


def cmd_needs_info(a):
    from ghbot.poller import NEEDS_INFO, NEEDS_INFO_MARK
    cfg = S.config()
    days = float(cfg.get("GHBOT_NEEDS_INFO_DAYS") or 14)
    body = Path(a.body_file).read_text()
    if not re.search(rf"\b{days:g}\s+days\b", body):
        die(f"the comment must tell the author that the issue is closed automatically when the details are not "
            f"added within {days:g} days")
    a.extra_mark = NEEDS_INFO_MARK
    cmd_comment(a)
    p = Poller(GitHub(), cfg)
    p.gh.request("POST", f"/repos/{cfg['GHBOT_REPO']}/issues/{a.pr}/labels", {"labels": [NEEDS_INFO]})
    with S.locked():
        rec = S.pr_load(a.pr)
        if rec is not None:
            from ghbot.poller import _digest
            rec["needs_info"] = S.utc()
            # the description as it was when asked: an edit to it is an answer too
            rec["needs_info_body"] = _digest(p.gh.get(f"/repos/{cfg['GHBOT_REPO']}/issues/{a.pr}").get("body"))
            S.pr_save(rec)
    print(f"#{a.pr}: \"{NEEDS_INFO}\" added; closed after {days:g} days without an answer from the author")


def cmd_duplicate(a):
    if a.of == a.pr:
        die("an issue is not a duplicate of itself")
    bound(a.pr, "close")
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R = cfg["GHBOT_REPO"]
    this, other = p.gh.get(f"/repos/{R}/issues/{a.pr}"), p.gh.get(f"/repos/{R}/issues/{a.of}")
    if this.get("pull_request") or other.get("pull_request"):
        die("only an issue is closed as a duplicate, and only of another issue")
    if other["created_at"] > this["created_at"]:
        die(f"#{a.of} is newer than #{a.pr}: the newer one is the duplicate")
    body = Path(a.body_file).read_text()
    if f"#{a.of}" not in body:
        die(f"the comment must link the other issue (#{a.of}) and say why it is the same")
    cmd_comment(a)
    # GitHub's "close as duplicate": the issue then shows "duplicate of #M" itself.  Only
    # the GraphQL API takes the other issue; no label is involved.
    r = p.gh.request("POST", "/graphql", {
        "query": "mutation($i: ID!, $d: ID!) { closeIssue(input: {issueId: $i, stateReason: DUPLICATE, "
                 "duplicateIssueId: $d}) { issue { number state stateReason } } }",
        "variables": {"i": this["node_id"], "d": other["node_id"]}})
    if (r.data or {}).get("errors") or not ((r.data or {}).get("data") or {}).get("closeIssue"):
        die(f"the comment is posted, but #{a.pr} was not closed as a duplicate: "
            f"{str((r.data or {}).get('errors'))[:300]}")
    with S.locked():
        rec = S.pr_load(a.pr)
        if rec is not None:
            rec["closed_duplicate_of"] = a.of
            S.pr_save(rec)
    print(f"#{a.pr}: closed as a duplicate of #{a.of}")


def cmd_no_template(a):
    bound(a.pr, "close")
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R = cfg["GHBOT_REPO"]
    this = p.gh.get(f"/repos/{R}/issues/{a.pr}")
    if this.get("pull_request") or this.get("state") != "open":
        die("only an open issue is closed this way")
    if "template" not in Path(a.body_file).read_text().lower():
        die("the comment must ask the author to open the issue again with one of the issue templates")
    cmd_comment(a)
    p.gh.request("PATCH", f"/repos/{R}/issues/{a.pr}", {"state": "closed", "state_reason": "not_planned"})
    with S.locked():
        rec = S.pr_load(a.pr)
        if rec is not None:
            rec["closed_no_template"] = S.utc()
            S.pr_save(rec)
    print(f"#{a.pr}: closed as not planned (no template, too little to go on)")


def cmd_reopen(a):
    bound(a.n, "reopen")
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    R, me = cfg["GHBOT_REPO"], p.login().lower()
    closes = [e for e in p.gh.paged(f"/repos/{R}/issues/{a.n}/timeline") if e.get("event") == "closed"]
    if not closes or ((closes[-1].get("actor") or {}).get("login") or "").lower() != me:
        die(f"#{a.n} was not closed by this account: only its own close is undone")
    p.gh.request("PATCH", f"/repos/{R}/issues/{a.n}", {"state": "open", "state_reason": "reopened"})
    print(f"#{a.n}: reopened")


def attach_ok(url, repo):
    import urllib.parse
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.username or u.password:
        return False
    host, path = (u.hostname or "").lower(), u.path
    if host == "github.com":
        return path.startswith("/user-attachments/") or path.lower().startswith(f"/{repo.lower()}/files/")
    return host in ATTACH_HOSTS


def cmd_attachment(a):
    """A file the thread links, as untrusted data: GitHub's own attachment
    hosts only (redirects too), size-limited, owner-only, quarantined, named
    by index.  Read it with `gamesdb redact|unzip|frames|plist`; never run it."""
    import urllib.request
    from gamesdb import attachments
    bound(a.n, "read attachments of")
    cfg = S.config()
    R = cfg["GHBOT_REPO"]
    if not attach_ok(a.url, R):
        die("not downloaded: only files attached to the thread on GitHub itself (github.com/user-attachments/…, "
            f"github.com/{R}/files/…, GitHub's image hosts). Never any other link from a thread.")
    p = Poller(GitHub(), cfg)
    texts = [(p.gh.get(f"/repos/{R}/issues/{a.n}").get("body") or "")]
    texts += [c.get("body") or "" for c in p.gh.paged(f"/repos/{R}/issues/{a.n}/comments")]
    if not any(a.url in t for t in texts):
        die(f"#{a.n} does not link that file")

    class Hosts(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not attach_ok(newurl, R):
                raise GitHubError(0, "the download redirects off GitHub's attachment hosts")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    d = attachments.private_dir(S.DATA / "issue" / str(a.n) / "attachments")
    try:
        with urllib.request.build_opener(Hosts).open(urllib.request.Request(
                a.url, headers={"User-Agent": "utmapp-bot-ghbot"}), timeout=120) as r:
            data = r.read(ATTACH_MAX + 1)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    except OSError as e:
        die(f"not downloaded: {e}")
    if len(data) > ATTACH_MAX:
        die(f"not kept: larger than {ATTACH_MAX >> 20} MB")
    import mimetypes
    # the extension says what to read it with; the name itself is never kept
    suffix = attachments.plain_suffix(a.url.split("?")[0]) or attachments.plain_suffix(
        "x" + (mimetypes.guess_extension(ctype) or ""))
    out = d / f"{len(list(d.iterdir())) + 1:03d}{suffix}"
    attachments.write_private(out, data)
    print(out)


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


def cmd_catchup(a):
    cfg = S.config()
    p = Poller(GitHub(), cfg)
    with S.locked("main"):
        st = S.load()
    found = p.unanswered()
    for rec, last, late in found:
        print(f"#{rec['pr']} ({rec.get('kind')}) {(rec.get('title') or '')[:60]}: @{rec.get('author')} wrote "
              f"{len(late)} comment(s) after this account's of {last}: {late[-1]['html_url']}")
        if a.apply:
            with S.locked():
                cur = S.pr_load(rec["pr"])
                # read again from this account's last comment: only the author's
                # comments after it qualify (as replies)
                cur["cursor"] = last
                S.pr_save(cur)
            print(f"  {p.process(st, rec['pr'])}")
    if not found:
        print("nothing unanswered")
    elif not a.apply:
        print(f"{len(found)} unanswered; `utmbot gh catchup --apply` queues them (a session and a public reply each)")
    else:
        with S.locked("main"):
            S.save(st)


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
    s = sub.add_parser("catchup"); s.add_argument("--apply", action="store_true")
    s = sub.add_parser("release"); s.add_argument("pr", type=int); s.add_argument("--force", action="store_true")
    def checked(s):  # the outbound checks' overrides
        s.add_argument("--allow", action="append", metavar="REGEX")
        s.add_argument("--allow-link", action="append", metavar="DOMAIN")
    s = sub.add_parser("comment"); s.add_argument("pr", type=int); s.add_argument("--round", required=True)
    s.add_argument("--body-file", required=True); s.add_argument("--in-reply-to", type=int); checked(s)
    s = sub.add_parser("needs-info"); s.add_argument("pr", type=int); s.add_argument("--round", required=True)
    s.add_argument("--body-file", required=True); s.set_defaults(in_reply_to=None); checked(s)
    s = sub.add_parser("duplicate"); s.add_argument("pr", type=int); s.add_argument("--of", type=int, required=True)
    s.add_argument("--round", required=True); s.add_argument("--body-file", required=True)
    s.set_defaults(in_reply_to=None); checked(s)
    s = sub.add_parser("no-template"); s.add_argument("pr", type=int); s.add_argument("--round", required=True)
    s.add_argument("--body-file", required=True); s.set_defaults(in_reply_to=None); checked(s)
    s = sub.add_parser("label"); s.add_argument("n", type=int)
    g = s.add_mutually_exclusive_group(required=True); g.add_argument("--add"); g.add_argument("--remove")
    s = sub.add_parser("reopen"); s.add_argument("n", type=int)
    s = sub.add_parser("attachment"); s.add_argument("n", type=int); s.add_argument("url")
    s = sub.add_parser("review"); s.add_argument("pr", type=int); s.add_argument("--round", required=True)
    s.add_argument("--event", required=True, choices=sorted(EVENTS)); s.add_argument("--body-file", required=True)
    s.add_argument("--comments-file"); checked(s)
    s = sub.add_parser("pr"); s.add_argument("issue", type=int); s.add_argument("--round", required=True)
    s.add_argument("--repo", help="OWNER/NAME: another repository the org owns (default: GHBOT_REPO)")
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
        return {"label": cmd_label, "needs-info": cmd_needs_info, "duplicate": cmd_duplicate, "no-template": cmd_no_template,
                "reopen": cmd_reopen,
                "attachment": cmd_attachment, "catchup": cmd_catchup, "retry": cmd_retry, "release": cmd_release, "comment": cmd_comment,
                "review": cmd_review, "pr": cmd_pr, "session": cmd_session}[a.cmd](a) or 0
    except GitHubError as e:
        die(str(e))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
