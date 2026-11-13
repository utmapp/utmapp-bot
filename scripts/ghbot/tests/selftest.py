"""Self-test of the GitHub poller against an in-memory GitHub: who may call the
bot, the 👀 claim between two Macs on one account, release, the race on the
first claim (DUPLICATE on posting), and sessions started and resumed (a fake
`claude`).  Touches nothing real.

usage: python3 scripts/ghbot/tests/selftest.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ghbot import jobs, state as S  # noqa: E402
from ghbot.gh import GitHubError, Response  # noqa: E402
from ghbot.poller import Poller  # noqa: E402
import ghbot.__main__ as M  # noqa: E402

REPO, ORG, BOT = "utmapp/UTM", "utmapp", "UTMapp-bot"


def ts(i):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + i))


class World:
    """One GitHub: pull requests, comments, reviews, reactions, members."""

    def __init__(self):
        self.ids = 1000
        self.members = {"osy", "maint"}
        self.prs, self.comments, self.rcomments, self.reviews = {}, [], [], []
        self.timeline, self.reactions = [], []
        self.force_201 = False  # simulate both Macs winning the race
        self.notified = set()
        self.opened = []  # PRs the bot opened

    def nid(self):
        self.ids += 1
        return self.ids

    def user(self, login):
        return {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}

    def assoc(self, login):
        return "MEMBER" if login in self.members else "CONTRIBUTOR"

    def pr(self, n, author, body="", at=1):
        self.prs[n] = {"number": n, "user": self.user(author), "author_association": self.assoc(author),
                       "created_at": ts(at), "body": body, "html_url": f"https://x/pull/{n}",
                       "title": f"PR {n}", "state": "open", "merged": False, "head": {"sha": f"sha{n}a"},
                       "pull_request": {"url": f"https://x/pulls/{n}"}}
        self.notified.add(n)

    def issue(self, n, author, body="", at=1):
        self.prs[n] = {"number": n, "user": self.user(author), "author_association": self.assoc(author),
                       "created_at": ts(at), "body": body, "html_url": f"https://x/issues/{n}",
                       "title": f"Issue {n}", "state": "open"}
        self.notified.add(n)

    def comment(self, n, who, body, at):
        c = {"id": self.nid(), "pr": n, "user": self.user(who), "author_association": self.assoc(who),
             "created_at": ts(at), "body": body, "html_url": f"https://x/pull/{n}#c"}
        self.comments.append(c)
        self.notified.add(n)
        return c

    def request_review(self, n, actor, at):
        self.timeline.append({"pr": n, "id": self.nid(), "event": "review_requested",
                              "created_at": ts(at), "actor": self.user(actor),
                              "requested_reviewer": self.user(BOT)})
        self.notified.add(n)


class FakeGH:
    def __init__(self, world):
        self.w = world

    def request(self, method, path, body=None, headers=None, ok=(200, 201, 204)):
        w = self.w
        p = path.split("?")[0].replace("https://api.github.com", "")
        parts = p.strip("/").split("/")
        if p == "/user":
            return Response(200, {}, {"login": BOT})
        if p == "/notifications":
            data = [{"updated_at": ts(0), "reason": "mention", "repository": {"full_name": REPO},
                     "subject": {"type": "PullRequest", "url": f"https://api.github.com/repos/{REPO}/pulls/{n}"}}
                    for n in sorted(w.notified)]
            w.notified = set()
            return Response(200, {}, data)
        if parts[:2] == ["orgs", ORG] and parts[2] == "members":
            return Response(204 if parts[3] in w.members else 404, {}, None)
        n = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        if len(parts) == 3 and parts[:1] == ["repos"]:
            return Response(200, {}, {"default_branch": "main"})
        if parts[3] == "issues" and len(parts) == 5:
            return Response(200, {}, w.prs[n])
        if parts[3] == "pulls" and len(parts) == 5:
            if "head" not in w.prs[n]:
                raise GitHubError(404, "not a pull request")
            return Response(200, {}, w.prs[n])
        if parts[3] == "pulls" and len(parts) == 4:
            if method == "POST":
                pr = {"id": w.nid(), "number": w.nid(), "user": w.user(BOT), "body": body["body"],
                      "head": {"ref": body["head"].split(":")[1]}, "state": "open",
                      "html_url": "https://x/pull/new", "title": body["title"]}
                w.opened.append(pr)
                return Response(201, {}, pr)
            return Response(200, {}, list(w.opened))
        if parts[3] == "issues" and parts[5:] == ["comments"]:
            if method == "POST":
                c = w.comment(n, BOT, body["body"], 0)
                return Response(201, {}, c)
            return Response(200, {}, [c for c in w.comments if c["pr"] == n])
        if parts[3] == "pulls" and parts[5:] == ["comments"]:
            return Response(200, {}, [c for c in w.rcomments if c["pr"] == n])
        if parts[3] == "pulls" and parts[5:] == ["reviews"]:
            if method == "POST":
                v = {"id": w.nid(), "pr": n, "user": w.user(BOT), "body": body["body"], "submitted_at": ts(0),
                     "state": {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(body["event"], "COMMENTED"),
                     "commit_id": body["commit_id"], "html_url": "https://x/review"}
                w.reviews.append(v)
                return Response(200, {}, v)
            return Response(200, {}, [v for v in w.reviews if v["pr"] == n])
        if parts[3] == "issues" and parts[5:] == ["timeline"]:
            return Response(200, {}, [e for e in w.timeline if e["pr"] == n])
        if parts[3] == "issues" and parts[5] == "reactions":
            if method == "POST":
                mine = [r for r in w.reactions if r["pr"] == n and r["user"]["login"] == BOT]
                if mine and not w.force_201:
                    return Response(200, {}, mine[0])
                r = mine[0] if mine else {"id": w.nid(), "pr": n, "content": "eyes", "user": w.user(BOT)}
                if not mine:
                    w.reactions.append(r)
                return Response(201, {}, r)
            if method == "DELETE":
                w.reactions = [r for r in w.reactions if r["id"] != int(parts[6])]
                return Response(204, {}, None)
            return Response(200, {}, [r for r in w.reactions if r["pr"] == n])
        if parts[3] in ("issues", "pulls") and parts[4] == "comments" and parts[6] == "reactions":
            return Response(201, {}, {"id": w.nid()})
        raise GitHubError(404, f"fake: {method} {path}")

    def get(self, path, **kw):
        return self.request("GET", path).data

    def paged(self, path, params=None, limit=2000):
        return self.request("GET", path).data or []

    def status(self, method, path, ok=None, **kw):
        return self.request(method, path).status


FAILS = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


class Mac:
    def __init__(self, world, root, name):
        self.dir = Path(root) / name
        self.world = world
        self.cfg = dict(S.DEFAULTS, GHBOT_PLUGIN_DIR=str(Path(root) / "plugin"),
                        GHBOT_WORKDIR=str(Path(root) / f"work-{name}"), CLAUDE_BIN=str(Path(root) / "claude"))
        with self:
            S.private(S.D)
            S.save({"installed_at": ts(-10)})
        self.st = None

    def __enter__(self):
        S.use(self.dir)
        S.DATA = self.dir  # its own locks: this Mac's UTM lease is not the test's
        return self

    def __exit__(self, *a):
        pass

    def poll(self, schedule=False):
        with self:
            st = S.load()
            p = Poller(FakeGH(self.world), self.cfg)
            out = {n: p.process(st, n) for n in p.poll(st)}
            if schedule:
                jobs.schedule(st, self.cfg)
            S.save(st)
            return out

    def rec(self, n):
        with self:
            return S.pr_load(n) or {}

    def post(self, n, rnd, text="result"):
        """`utmbot gh comment` as a session on this Mac would run it."""
        with self:
            f = self.dir / "body.md"
            f.write_text(text)
            M.GitHub = lambda: FakeGH(self.world)
            M.S.config = lambda: self.cfg
            try:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    M.cmd_comment(SimpleNamespace(pr=n, round=rnd, body_file=str(f), in_reply_to=None))
                return 0, out.getvalue()
            except SystemExit as e:
                return e.code, ""


def open_pr(mac, n, rnd, branch="fix/x"):
    """--check, then what utm-submit does (a PR whose body ends with the
    marker), then --record; the exit code of the first step that stops."""
    with mac:
        M.GitHub = lambda: FakeGH(mac.world)
        M.S.config = lambda: mac.cfg
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                M.cmd_pr(SimpleNamespace(issue=n, round=rnd, check=True, record=None))
        except SystemExit as e:
            return e.code
        marker = out.getvalue().splitlines()[1]
        num = mac.world.nid()
        mac.world.opened.append({"id": mac.world.nid(), "number": num, "user": mac.world.user(BOT),
                                 "body": "the fix\n\n" + marker, "head": {"ref": branch}, "state": "open",
                                 "html_url": f"https://x/pull/{num}", "title": "fix"})
        mac.world.prs[num] = dict(mac.world.opened[-1])
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                M.cmd_pr(SimpleNamespace(issue=n, round=rnd, check=False, record=str(num)))
        except SystemExit as e:
            return e.code
        return 0


def main():
    root = tempfile.mkdtemp(prefix="ghbot-test-")
    (Path(root) / "plugin").mkdir()
    claude = Path(root) / "claude"
    claude.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" =====RUN===== >> \"$(dirname \"$0\")/claude.args\"\n"
                      "echo '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"ok\"}'\n")
    claude.write_text(claude.read_text() + 
                      "case \"$*\" in *--remote-control*) f=$(printf '%s' \"$*\" | sed -n 's/.*Then run `touch \\([^`]*\\)`.*/\\1/p'); "
                      "[ -n \"$f\" ] && touch \"$f\"; sleep 1;; esac\n")
    claude.chmod(0o755)
    w = World()
    a, b = Mac(w, root, "macA"), Mac(w, root, "macB")

    # who may call the bot
    w.pr(1, "stranger", "please look @UTMapp-bot")
    w.comment(1, "randomuser", "@UTMapp-bot review this", 1)
    out = a.poll()
    check("nothing" in out[1] and not a.rec(1).get("owner"), "a non-member's mention is ignored and claims nothing")
    w.comment(1, "stranger", "@UTMapp-bot hello? (PR author before any member)", 2)
    a.poll()
    check(not a.rec(1).get("owner"), "the PR author cannot start a conversation")
    w.comment(1, "osy", "> @UTMapp-bot quoted\n`@UTMapp-bot` in code", 3)
    a.poll()
    check(not a.rec(1).get("owner"), "a member's quoted or code mention is not a call")
    w.comment(1, "osy", "@UTMapp-bot can you review this?", 4)
    out = a.poll()
    r = a.rec(1)
    check(r.get("owner") == "mine" and r["pending"][0]["kind"] == "review", "a member's mention claims the PR and queues a review")
    check(r.get("initiated_by") == "osy", "the member who called the bot is recorded")
    w.comment(1, "stranger", "thanks! a question for the bot", 5)
    w.comment(1, "randomuser", "me too", 6)
    a.poll()
    r = a.rec(1)
    check([i["author"] for i in r["pending"]] == ["osy", "stranger"], "after a member started it, the author may follow up; others still may not")

    # Mac B on the same account
    w.notified.add(1)
    b.poll()
    rb = b.rec(1)
    check(rb.get("owner") == "other" and not rb.get("pending"), "the second Mac sees the 👀 (200) and keeps off")
    w.comment(1, "osy", "@UTMapp-bot one more", 7)
    b.poll()
    check(not b.rec(1).get("pending"), "the other Mac ignores later activity on an owned PR")

    # release: B may claim then
    with a:
        p = Poller(FakeGH(w), a.cfg)
        p.release(1)
        rr = S.pr_load(1)
        rr.update(owner=None, reaction_id=None)
        S.pr_save(rr)
    w.comment(1, "maint", "@UTMapp-bot please review again", 8)
    b.poll()
    rb = b.rec(1)
    check(rb.get("owner") == "mine" and rb["pending"][-1]["author"] == "maint", "after a release the other Mac claims on new activity")

    # the race: both get 201
    w.force_201 = True
    w.pr(2, "maint", "@UTMapp-bot review please", at=9)
    a.poll(); w.notified.add(2); b.poll()
    check(a.rec(2).get("owner") == "mine" and b.rec(2).get("owner") == "mine", "a race: both Macs got 201 and both queued it")
    rnd = a.rec(2)["pending"][0]["id"]
    code_a, _ = a.post(2, rnd)
    code_b, _ = b.post(2, rnd)
    check(code_a in (0, None) and code_b == 3, "the first post wins; the second is DUPLICATE (exit 3)")
    check(b.rec(2).get("owner") == "other", "the loser lets the PR go")
    code_a2, _ = a.post(2, rnd, "a second part")
    check(code_a2 in (0, None), "the winner may post again in its own round")
    w.force_201 = False

    # review request
    w.pr(3, "contributor", "fixes a thing", at=10)
    w.request_review(3, "osy", 11)
    a.poll()
    r3 = a.rec(3)
    check(r3.get("owner") == "mine" and r3["pending"][0]["type"] == "review_request", "a member's review request claims and queues a review")

    # bot comments are never input
    w.comment(3, "some[bot]", "@UTMapp-bot ping", 12)
    w.comment(3, BOT, "my own comment @UTMapp-bot", 13)
    a.poll()
    check(len(a.rec(3)["pending"]) == 1, "comments by bots and by the account itself are ignored")

    # sessions (Mac A owns PRs 2 and 3): start, finish, resume with the next input
    with a:
        st = S.load()
        a.cfg["GHBOT_MAX_SESSIONS"] = "3"
        jobs.schedule(st, a.cfg)
        S.save(st)
    running = [n for n in (2, 3) if a.rec(n).get("job", {}).get("status") == "running"]
    check(running == [2], "one new review at a time (PR 2 starts; PR 3 waits)")
    time.sleep(1.5)
    with a:
        st = S.load()
        jobs.supervise(st, a.cfg)
        jobs.schedule(st, a.cfg)
        S.save(st)
    check(a.rec(2)["job"]["status"] == "idle", "the session's end is seen (success: idle)")
    check(a.rec(3)["job"]["status"] == "running", "then the next review starts")
    args = (Path(root) / "claude.args").read_text()
    check("/utmapp-bot:review-utm-pr 2 --auto" in args and "--session-id" in args, "the first session runs the review skill")
    check("<<<github" in args and "untrusted data" in args, "GitHub text reaches the session fenced as data")
    a.post(2, a.rec(2)["delivered"][0], "the review")
    check(a.rec(2).get("reviewed_sha") == "sha2a", "a post in a review round records the head it reviewed")
    w.comment(2, "osy", "@UTMapp-bot why did macOS 12 fail?", 20)
    time.sleep(1.5)
    a.poll(schedule=True)
    r2 = a.rec(2)
    time.sleep(1)  # the fake claude writes its arguments after it starts
    last = (Path(root) / "claude.args").read_text().split("=====RUN=====")[-2]
    check("--resume" in last and r2["session_id"] in last and "why did macOS 12 fail" in last,
          "a follow-up resumes the same session with only the new comment")
    check("review please" not in last, "input already delivered is not sent again")

    # a held UTM lease holds a new review; a release with no activity is seen at startup
    w.pr(4, "contributor", "@UTMapp-bot? no: from maint below", at=30)
    w.comment(4, "maint", "@UTMapp-bot review please", 31)
    a.poll()
    with a:
        (a.dir / "locks").mkdir(exist_ok=True)
        (a.dir / "locks" / "utm.lease").write_text(json.dumps({"pid": os.getpid()}))
        for n in (2, 3):  # nothing else running or due
            r = S.pr_load(n); r["job"]["status"] = "idle"; S.pr_save(r)
        st = S.load()
        jobs.schedule(st, a.cfg)
        held = S.pr_load(4)["job"]
        (a.dir / "locks" / "utm.lease").unlink()
    check(held["status"] == "queued" and held.get("held_note") == "UTM is leased", "a held UTM lease holds a new review")
    with a:
        Poller(FakeGH(w), a.cfg).release(4)
        Poller(FakeGH(w), a.cfg).sweep()
        check(S.pr_load(4).get("owner") is None, "the startup sweep sees a PR released with no activity")

    # a blocked session reaches the phone (a notice session), and the notice ends
    with a:
        r = S.pr_load(4); r["job"]["status"] = "queued"; r["job"].pop("held_note", None)
        r["owner"] = "mine"; S.pr_save(r)
        st = S.load()
        jobs.schedule(st, a.cfg)
        r = S.pr_load(4)
        S.signal(r["session_id"], "blocked", reason="a macOS permission dialog")
        time.sleep(1.5)
        jobs.supervise(st, a.cfg)
        check(S.pr_load(4)["job"]["status"] == "blocked" and len(st.get("notices", [])) == 1,
              "a blocked session is marked blocked and starts a phone notice")
        for _ in range(20):
            jobs.supervise_notices(st)
            if not st["notices"]:
                break
            time.sleep(0.5)
        check(not st["notices"] and "not confirmed" not in S.LOG.read_text(),
              "the notice's session is ended once it has notified (its done file)")
        args = (Path(root) / "claude.args").read_text()
        check("--remote-control" in args and "PushNotification" in args and "#4 review is blocked" in args,
              "the notice is a Remote Control session told to push the blocker")

    # issues: the same rules; the session answers with answer-utm-issue; only a member's round opens a PR
    w.issue(10, "reporter", "@UTMapp-bot it crashes (reporter summoning)", at=40)
    a.poll()
    check(not a.rec(10).get("owner"), "an issue's reporter (not a member) cannot summon the bot")
    w.comment(10, "osy", "@UTMapp-bot can you reproduce this?", 41)
    a.poll()
    r10 = a.rec(10)
    check(r10.get("owner") == "mine" and r10.get("kind") == "issue" and r10["pending"][0]["kind"] == "review",
          "a member's mention on an issue claims it")
    w.comment(10, "reporter", "@UTMapp-bot please build the fix and open a PR", 42)
    a.poll()
    r10 = a.rec(10)
    check([i["author"] for i in r10["pending"]] == ["osy", "reporter"], "the issue's author may join once a member started it")
    with a:
        st = S.load()
        for n in (2, 3, 4):
            r = S.pr_load(n); r["job"]["status"] = "idle"; S.pr_save(r)
        jobs.schedule(st, a.cfg)
        S.save(st)
    time.sleep(1.5)
    args = (Path(root) / "claude.args").read_text().split("=====RUN=====")[-2]
    check("/utmapp-bot:answer-utm-issue 10 --auto" in args and "utmbot gh pr 10" in args
          and "utm-submit" in args and "accept every change the code review recommends" in args,
          "an issue session runs answer-utm-issue, and opens a PR only through utm-review and utm-submit")
    rounds = {i["author"]: i["id"] for i in r10["pending"]}
    check(open_pr(a, 10, rounds["reporter"]) not in (0, None), "the author's request cannot open a PR (not a member's round)")
    check(open_pr(a, 10, rounds["osy"]) in (0, None) and len(w.opened) == 1, "a member's round opens the PR")
    check(a.rec(10).get("opened_prs"), "the PR utm-submit opened is recorded")
    rec_b = dict(a.rec(10), rounds={}, owner="mine")
    with b:
        S.pr_save(rec_b)  # Mac B won the same race
    check(open_pr(b, 10, rounds["osy"], "fix/y") == 3 and len(w.opened) == 1,
          "the same round from a second Mac is DUPLICATE: one PR")

    # the account's own PR (the fix it opened for issue 10): members are answered without a
    # mention, in issue 10's session, never reviewed; everyone else is ignored
    num = w.opened[0]["number"]
    w.prs[num].update(pull_request={"url": f"https://x/pulls/{num}"}, head={"ref": "fix/x", "sha": f"sha{num}a"},
                      author_association="NONE", created_at=ts(43), merged=False)
    w.comment(num, "randomuser", "this breaks my setup", 44)
    out = a.poll()
    check("nothing" in out[num] and not a.rec(num).get("owner"), "a non-member's comment on the bot's own PR is ignored")
    w.request_review(num, "osy", 45)
    w.comment(num, "osy", "1) does this apply to QEMU VMs as well 2) why not in the Script handler?", 46)
    a.poll()
    rp = a.rec(num)
    check(rp.get("owner") == "mine" and [i["kind"] for i in rp.get("pending", [])] == ["answer"],
          "a member's comment on the bot's own PR is answered without a mention (no review request)")
    check(rp.get("session_id") == a.rec(10)["session_id"] and rp.get("origin") == 10,
          "the answer goes to the session of the issue that opened the PR")
    with a:
        st = S.load()
        r = S.pr_load(10); r["job"].update(status="running", pid=os.getpid()); S.pr_save(r)
        jobs.schedule(st, a.cfg)
        check(S.pr_load(num)["job"].get("held_note", "").startswith("its session is running"),
              "one session never runs twice (held while issue 10's session runs)")
        r = S.pr_load(10); r["job"].update(status="idle", pid=None); S.pr_save(r)
        jobs.schedule(st, a.cfg)
        S.save(st)
    time.sleep(1.5)
    last = (Path(root) / "claude.args").read_text().split("=====RUN=====")[-2]
    check("--resume" in last and rp["session_id"] in last and "own pull request" in last
          and "issue #10" in last and "review-utm-pr " + str(num) not in last and "Script handler" in last,
          "the issue's session is resumed with the comment and told to answer, not review")
    w.comment(num, "maint", "and what about iOS?", 47)
    a.poll()
    check([i["kind"] for i in a.rec(num)["pending"]] == ["followup"], "later member comments are follow-ups")

    w.pr(30, BOT, "a fix opened by hand", at=50)
    w.comment(30, "osy", "why here?", 51)
    a.poll()
    r30 = a.rec(30)
    check(r30.get("own") and [i["kind"] for i in r30["pending"]] == ["answer"] and not r30.get("session_id"),
          "an own PR with no issue session on this Mac gets a new session")
    with a:
        st = S.load()
        for n in list(S.pr_all()):
            if n["pr"] != 30 and n["job"].get("status") in ("running", "queued", "waiting"):
                n["job"]["status"] = "idle"; S.pr_save(n)
        jobs.schedule(st, a.cfg)
        S.save(st)
    time.sleep(1.5)
    last = (Path(root) / "claude.args").read_text().split("=====RUN=====")[-2]
    check("--session-id" in last and "own pull request" in last and "/utmapp-bot:review-utm-pr" not in last,
          "its first prompt answers the comment and does not start a review")

    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'} ({root})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
