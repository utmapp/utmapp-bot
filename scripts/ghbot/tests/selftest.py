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
        self.comment_eyes = []
        self.force_201 = False  # simulate both Macs winning the race
        self.notified = set()
        self.opened = []  # PRs the bot opened
        self.actor = BOT  # who the next label is put on by

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
                       "title": f"Issue {n}", "state": "open", "node_id": f"I_{n}"}
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
        if p == "/graphql":
            v = body["variables"]
            tgt = next(i for i in w.prs.values() if i.get("node_id") == v["i"])
            dup = next(i for i in w.prs.values() if i.get("node_id") == v["d"])
            tgt.update(state="closed", state_reason="duplicate", duplicate_of=dup["number"])
            w.timeline.append({"pr": tgt["number"], "id": w.nid(), "event": "closed", "created_at": ts(0),
                               "actor": w.user(BOT)})
            return Response(200, {}, {"data": {"closeIssue": {"issue": {"number": tgt["number"]}}}})
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
        if parts[3] == "issues" and len(parts) == 4:
            return Response(200, {}, sorted((i for i in w.prs.values() if i.get("created_at")), key=lambda i: i["created_at"], reverse=True))
        if parts[3] == "issues" and len(parts) == 5:
            if method == "PATCH" and "type" in body:
                w.prs[n]["type"] = {"name": body["type"]}
            elif method == "PATCH":
                w.prs[n].update(state=body["state"], state_reason=body.get("state_reason"))
                w.timeline.append({"pr": n, "id": w.nid(), "event": "closed" if body["state"] == "closed" else "reopened",
                                   "created_at": ts(0), "actor": w.user(BOT)})
            return Response(200, {}, w.prs[n])
        if parts[3] == "issues" and parts[5:6] == ["labels"]:
            ls = w.prs[n].setdefault("labels", [])
            if method == "POST":
                for l in body["labels"]:
                    if l not in [x["name"] for x in ls]:
                        ls.append({"name": l})
                    w.timeline.append({"pr": n, "id": w.nid(), "event": "labeled", "label": {"name": l},
                                       "created_at": ts(0), "actor": w.user(w.actor)})
            elif method == "DELETE":
                name = parts[6].replace("%20", " ")
                w.prs[n]["labels"] = [x for x in ls if x["name"] != name]
            return Response(200, {}, w.prs[n]["labels"])
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
                     "commit_id": body["commit_id"], "html_url": "https://x/review",
                     "comments": body.get("comments")}
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
            w.comment_eyes.append(int(parts[5]))
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


def open_pr(mac, n, rnd, branch="fix/x", repo=None):
    """--check, then what utm-submit does (a PR whose body ends with the
    marker), then --record; the exit code of the first step that stops."""
    with mac:
        M.GitHub = lambda: FakeGH(mac.world)
        M.S.config = lambda: mac.cfg
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                M.cmd_pr(SimpleNamespace(issue=n, round=rnd, check=True, record=None, repo=repo))
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
                M.cmd_pr(SimpleNamespace(issue=n, round=rnd, check=False, record=str(num), repo=repo))
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
    check(open_pr(a, 10, "c999999") not in (0, None) and not w.opened, "a round the poller never delivered cannot open a PR")
    check(open_pr(a, 10, rounds["osy"]) in (0, None) and len(w.opened) == 1, "a member's round opens the PR")
    check(a.rec(10).get("opened_prs"), "the PR utm-submit opened is recorded")
    rec_b = dict(a.rec(10), rounds={}, owner="mine")
    with b:
        S.pr_save(rec_b)  # Mac B won the same race
    check(open_pr(b, 10, rounds["osy"], "fix/y") == 3 and len(w.opened) == 1,
          "the same round from a second Mac is DUPLICATE: one PR")

    # a fix in another repository of the stack: a PR only where the org owns it
    check(open_pr(a, 10, rounds["osy"], "fix/v", repo="osy/virtio-win-mesa") not in (0, None),
          "a repository the org does not own gets no PR (a fix branch only)")
    check(open_pr(a, 10, "c999999", "fix/v", repo="utmapp/virglrenderer") not in (0, None),
          "an undelivered round opens nothing on the org's other repositories either")
    check(open_pr(a, 10, rounds["osy"], "fix/v", repo="utmapp/virglrenderer") in (0, None)
          and a.rec(10).get("opened_elsewhere", [None])[0].startswith("utmapp/virglrenderer#")
          and len(a.rec(10)["opened_prs"]) == 1,
          "a member's round opens a PR on the org's repository, recorded apart from UTM's")

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

    # a triage run outside the poller (no record here): this account commented, and the
    # issue's author answers without a mention
    w.issue(40, "reporter2", "it does not boot", at=60)
    w.comment(40, "reporter2", "still happens on 5.0.8", 61)
    a.poll()
    check(not a.rec(40).get("owner"), "the author's comment before the bot ever spoke is not for the bot")
    w.comment(40, BOT, "I could not reproduce this. Which macOS version? Please attach the log.", 62)
    w.comment(40, "randomuser", "same here", 63)
    out = a.poll()
    check("nothing" in out[40] and not a.rec(40).get("owner"), "a stranger's comment after the bot's is still ignored")
    w.comment(40, "reporter2", "macOS 26.1, the log is attached", 64)
    a.poll()
    r40 = a.rec(40)
    check(r40.get("owner") == "mine" and [(i["kind"], i["author"]) for i in r40.get("pending", [])] == [("reply", "reporter2")]
          and not r40.get("initiated_by"),
          "the author's answer after the bot's comment is queued as a reply: no mention, no member, no record needed")
    w.comment(40, "reporter2", "one more detail: it is an external disk", 65)
    a.poll()
    check([i["kind"] for i in a.rec(40)["pending"]] == ["reply", "reply"], "their next comment is a reply too")
    with a:
        st = S.load()
        for r in S.pr_all():
            if r["pr"] != 40 and r["job"].get("status") in ("running", "queued", "waiting"):
                r["job"]["status"] = "idle"; S.pr_save(r)
        jobs.schedule(st, a.cfg)
        S.save(st)
    time.sleep(1.5)
    last = (Path(root) / "claude.args").read_text().split("=====RUN=====")[-2]
    check("--session-id" in last and "continues work this account already did" in last and "external disk" in last
          and "answer-utm-issue 40 --auto" in last and not last.lstrip().startswith("-p /utmapp-bot:"),
          "the reply starts a session that continues the earlier triage instead of starting over")
    rnd40 = a.rec(40)["delivered"][-1]
    before = len(w.opened)
    check(open_pr(a, 40, rnd40) in (0, None) and len(w.opened) == before + 1 and a.rec(40).get("opened_prs"),
          "a fix found from the author's reply opens a PR in that round (the small-fix rule is the skill's)")
    code, _ = a.post(40, rnd40, "reproduced with an external disk")
    check(code in (0, None) and a.rec(40)["rounds"].get(rnd40), "the reply is posted and recorded")
    code, _ = a.post(10, a.rec(10)["delivered"][0], "the first answer on an issue")
    check(code in (0, None) and a.rec(10)["rounds"], "a first-round answer on an issue is recorded (an issue has no head)")

    # a review of someone else's PR that proposes a small change: request changes, with a suggestion
    with a:
        M.GitHub = lambda: FakeGH(w)
        M.S.config = lambda: a.cfg
        bf, cf = a.dir / "rv.md", a.dir / "rv.json"
        bf.write_text("One line must change; see the comment above.")
        cf.write_text(json.dumps([{"path": "a.swift", "start_line": 4, "line": 6,
                                   "body": "Why.\n\n```suggestion\nlet x = 1\n```"}]))
        with contextlib.redirect_stdout(io.StringIO()):
            M.cmd_review(SimpleNamespace(pr=3, round=a.rec(3)["delivered"][0], event="request-changes",
                                         body_file=str(bf), comments_file=str(cf)))
    v = w.reviews[-1]
    check(v["state"] == "CHANGES_REQUESTED" and v["comments"] == [
        {"path": "a.swift", "line": 6, "side": "RIGHT", "start_line": 4, "start_side": "RIGHT",
         "body": "Why.\n\n```suggestion\nlet x = 1\n```"}],
          "a review requests changes and carries its suggestions as inline comments on the new side")

    # an org member who is the author: a mention calls the bot as always; without one it is a reply
    w.issue(42, "maint", "a member's own issue", at=66)
    w.comment(42, BOT, "Which version?", 67)
    w.comment(42, "maint", "5.0.8", 68)
    a.poll()
    r42 = a.rec(42)
    check([(i["kind"], i["member"]) for i in r42["pending"]] == [("reply", True)],
          "a member author's answer is a reply too, in a member's round")
    w.issue(43, "maint", "another", at=66)
    w.comment(43, BOT, "Which version?", 67)
    w.comment(43, "maint", "@UTMapp-bot 5.0.8, please fix it", 68)
    a.poll()
    r43 = a.rec(43)
    check(r43["pending"][0]["kind"] == "review" and r43.get("initiated_by") == "maint",
          "a member author's mention still calls the bot the usual way")

    # catch-up: a reply the cursor already passed (dropped before replies counted)
    w.issue(41, "reporter3", "black screen", at=70)
    w.comment(41, BOT, "Which display card?", 71)
    w.comment(41, "reporter3", "virtio-gpu-gl-pci", 72)
    with a:
        r = Poller(FakeGH(w), a.cfg).new_record(41, ts(73))
        r.update(kind="issue", author="reporter3", title="Issue 41", state="open", own=False)
        S.pr_save(r)
        w.notified.discard(41)
        p = Poller(FakeGH(w), a.cfg)
        found = p.unanswered()
        check([f[0]["pr"] for f in found] == [41], "catch-up finds the thread whose author wrote after the bot's last comment")
        M.GitHub = lambda: FakeGH(w)
        M.S.config = lambda: a.cfg
        with contextlib.redirect_stdout(io.StringIO()):
            M.cmd_catchup(SimpleNamespace(apply=True))
        check([i["kind"] for i in S.pr_load(41).get("pending", [])] == ["reply"], "catchup --apply queues it as a reply")

    # someone else in the thread (a maintainer here) asks the author something: the author's
    # answers are not clearly for the bot (issue #7788)
    w.issue(44, "reporter4", "sound cuts off", at=74)
    w.comment(44, BOT, "I measured the audio path; it plays in full here.", 75)
    w.comment(44, "reporter4", "I tried CoreAudio, it does not help", 76)
    a.poll()
    r44 = a.rec(44)
    check([(i["kind"], i.get("others")) for i in r44["pending"]] == [("reply", None)],
          "an answer while only the bot and the author talk is for the bot")
    w.comment(44, "maint", "@reporter4 Have you tried UTM 5.0.9?", 77)
    out = a.poll()
    check("nothing" in out[44] and len(a.rec(44)["pending"]) == 1,
          "the maintainer's question to the author does not wake the bot by itself")
    c78 = w.comment(44, "reporter4", "Not yet but I will!", 78)
    w.comment(44, "reporter4", "On 5.0.9 the deal sound does not play at all", 79)
    w.comment(44, "reporter4", "> it plays in full here.\n\nNot here, see the attached log", 80)
    w.comment(44, "reporter4", "@UTMapp-bot any idea?", 81)
    a.poll()
    p44 = a.rec(44)["pending"]
    check([i.get("others") for i in p44] == [None, ["maint"], ["maint"], None, None],
          "once someone else is in the thread, the author's comments are not clearly for the bot "
          "unless they quote or mention it, read from the whole thread")
    check(c78["id"] not in w.comment_eyes, "no 👀 on a comment that is not clearly for the bot")
    prompt = jobs.reply_prompt(a.rec(44), p44, "/plug")
    check(prompt.count("Not clearly for you: @maint") == 2 and "unless it is actionable" in prompt
          and "Have you tried" not in prompt,
          "the session is told which comments may not be for it, and to act on them only when actionable")
    check("Not clearly for you" in jobs.followup_prompt(dict(a.rec(44), auto=True), p44, "/plug"),
          "a follow-up round says so too")

    # a stranger asks the author a different question on a thread the bot took up
    w.issue(45, "reporter5", "crash on resume", at=80)
    with a:
        r = Poller(FakeGH(w), a.cfg).new_record(45, ts(80))
        r.update(kind="issue", author="reporter5", title="Issue 45", state="open", own=False, auto=True,
                 auto_seen=True)
        S.pr_save(r)
    w.comment(45, BOT, "Which macOS version?", 81)
    w.comment(45, "randomuser", "@reporter5 are you on an external disk?", 82)
    w.comment(45, "reporter5", "yes, external disk", 83)
    a.poll()
    p45 = a.rec(45)["pending"]
    check([(i["kind"], i.get("others")) for i in p45] == [("followup", ["randomuser"])],
          "an answer to a stranger's question is not clearly for the bot either")

    # ---- unasked triage: every new issue, and pull requests by people outside the org ----
    def cli(mac, fn, **kw):
        with mac:
            M.GitHub = lambda: FakeGH(w)
            M.S.config = lambda: mac.cfg
            kw.setdefault("allow", None); kw.setdefault("allow_link", None)
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    fn(SimpleNamespace(**kw))
                return 0
            except SystemExit as e:
                return e.code

    def new(mac):
        with mac:
            st = S.load()
            p = Poller(FakeGH(w), mac.cfg)
            out = {n: p.process(st, n) for n in p.poll_new(st)}
            S.save(st)
            return out

    w.issue(50, "olduser", "opened before the switch", at=80)
    w.notified.clear()
    check(new(a) == {} and a.rec(50) == {}, "switching unasked triage on takes up nothing that already exists")
    with a:
        st = S.load(); st["auto_since"] = ts(81); S.save(st)
    w.issue(51, "newuser", "UTM crashes when I resize the window", at=90)
    w.issue(52, "maint", "a maintainer's issue", at=90)
    w.pr(53, "newuser", "fixes the crash", at=90)
    w.pr(54, "maint", "a maintainer's PR", at=90)
    w.pr(55, BOT, "the bot's own PR", at=90)
    w.pr(56, "newuser", "work in progress", at=90); w.prs[56]["draft"] = True
    w.issue(57, "evil[bot]", "a bot's issue", at=90); w.prs[57]["user"]["type"] = "Bot"
    w.issue(61, "newuser", "please add a dark icon", at=90); w.prs[61]["type"] = {"name": "Feature"}
    w.issue(62, "newuser", "### What happened?\ncrash", at=90); w.prs[62]["type"] = {"name": "Bug"}
    w.notified.clear()
    out = new(a)
    kinds = {n: [i["kind"] for i in a.rec(n).get("pending", [])] for n in list(range(50, 58)) + [61, 62]}
    check(not kinds[61] and kinds[62] == ["triage"],
          "an issue of type Feature is never taken up; one of type Bug (or with no type) is")
    check(kinds[51] == ["triage"] and kinds[52] == ["triage"], "a new issue is taken up unasked, a maintainer's too")
    check(kinds[53] == ["triage"], "so is a pull request by someone outside the org")
    check(not kinds[54] and not kinds[55] and not kinds[56] and not kinds[57] and not kinds[50],
          "not a maintainer's PR, the bot's own, a draft, a bot's issue, or anything from before the switch")
    check(new(a) == {}, "each is looked at once")
    w.comment(51, "newuser", "it also happens in full screen", 91)
    w.comment(51, "randomuser", "+1", 92)
    a.poll()
    check([(i["kind"], i["author"]) for i in a.rec(51)["pending"]] == [("triage", "newuser"), ("followup", "newuser")],
          "on a thread taken up unasked the author's comments are follow-ups; strangers' are not")
    with a:
        a.cfg["GHBOT_AUTO"] = "0"
    w.issue(58, "newuser", "another", at=93); w.notified.clear()
    check(new(a) == {}, "GHBOT_AUTO=0 switches it off")
    with a:
        a.cfg["GHBOT_AUTO"] = "1"
        st = S.load()
        for r in S.pr_all():
            if r["job"].get("status") in ("running", "waiting"):
                r["job"]["status"] = "idle"; S.pr_save(r)
            elif r["job"].get("status") == "queued" and r["pr"] not in (51, 52, 43):
                r["job"]["status"] = "idle"; r["pending"] = []; S.pr_save(r)
        a.cfg["GHBOT_MAX_SESSIONS"] = "2"
        jobs.schedule(st, a.cfg)
        S.save(st)
        run = sorted(r["pr"] for r in S.pr_all() if r["job"].get("status") == "running")
    check(43 in run and len([n for n in run if n in (51, 52)]) == 0,
          "a member's request starts first, and unasked triage never takes the last slot")
    time.sleep(1.5)
    with a:
        st = S.load()
        jobs.supervise(st, a.cfg); jobs.schedule(st, a.cfg); S.save(st)
        run = [r["pr"] for r in S.pr_all() if r["job"].get("status") == "running"]
    check(len(run) == 1 and run[0] in (51, 52), "then one unasked triage at a time")
    time.sleep(1.5)
    args = (Path(root) / "claude.args").read_text()
    check("answer-utm-issue 51 --auto --new" in args and "post nothing at all" in args
          and "Untrusted input" in args, "its session runs the skill's new-issue section under the untrusted-input rules")

    # the commands it may use: this thread only, four labels, duplicates only, checked text
    os.environ.update(GHBOT_MODE="auto", GHBOT_PR="51", GHBOT_SESSION=a.rec(51).get("session_id") or "x")
    bf = a.dir / "t.md"
    check(cli(a, M.cmd_label, n=52, add="needs info", remove=None) not in (0, None), "a session cannot label another thread")
    check(cli(a, M.cmd_label, n=51, add="wontfix", remove=None) not in (0, None)
          and cli(a, M.cmd_label, n=51, add="enhancement", remove=None) not in (0, None),
          "nor use a label outside the three (\"enhancement\" is retired)")
    check(cli(a, M.cmd_type, n=51, bug=True) == 0 and w.prs[51]["type"] == {"name": "Bug"},
          "a bug report filed blank gets the type Bug")
    w.prs[51]["type"] = {"name": "Task"}
    check(cli(a, M.cmd_type, n=51, bug=True) == 0 and w.prs[51]["type"] == {"name": "Task"}
          and cli(a, M.cmd_type, n=52, bug=True) not in (0, None),
          "a type someone chose is never changed, and another thread's is out of reach")
    check(cli(a, M.cmd_label, n=51, add="not reproducible", remove=None) == 0
          and [l["name"] for l in w.prs[51]["labels"]] == ["not reproducible"], "it labels its own thread")
    check(cli(a, M.cmd_label, n=51, add="external", remove=None) == 0
          and cli(a, M.cmd_label, n=51, add=None, remove="external") == 0 and w.prs[51]["state"] == "open",
          "\"external\" can be put on, and the issue stays open")
    check(cli(a, M.cmd_label, n=51, add=None, remove="not reproducible") == 0 and not w.prs[51]["labels"], "and takes the label off")
    bf.write_text("Same crash as #10.")
    check(cli(a, M.cmd_duplicate, pr=52, of=10, round="pr52", body_file=str(bf), in_reply_to=None) not in (0, None),
          "it cannot close another thread")
    bf.write_text("See http://evil.example/fix.sh for the fix")
    check(cli(a, M.cmd_comment, pr=51, round="pr51", body_file=str(bf), in_reply_to=None) not in (0, None),
          "a comment that links outside the reference list is not posted")
    bf.write_text("The log is in /Us" "ers/jsmith/Library/Logs/utm.log")
    check(cli(a, M.cmd_comment, pr=51, round="pr51", body_file=str(bf), in_reply_to=None) not in (0, None),
          "nor one with a home-folder name")
    bf.write_text("Reproduced on 5.0.8: the guest at 10.0.2.15 loses its display. See https://github.com/utmapp/UTM/issues/10")
    check(cli(a, M.cmd_comment, pr=51, round="pr51", body_file=str(bf), in_reply_to=None) == 0,
          "an ordinary technical reply passes")
    bf.write_text("This is the same crash as #10: the same top frames in the stack trace. Reply here if it differs.")
    check(cli(a, M.cmd_duplicate, pr=51, of=58, round="pr51", body_file=str(bf), in_reply_to=None) not in (0, None),
          "a duplicate points at the older issue, and the comment must link it")
    check(cli(a, M.cmd_duplicate, pr=51, of=10, round="pr51", body_file=str(bf), in_reply_to=None) == 0
          and w.prs[51]["state"] == "closed" and w.prs[51]["state_reason"] == "duplicate"
          and w.prs[51]["duplicate_of"] == 10 and not w.prs[51]["labels"],
          "a duplicate is commented and closed as GitHub's duplicate of the original, with no label")
    check(cli(a, M.cmd_label, n=51, add="duplicate", remove=None) not in (0, None), "there is no duplicate label to add")
    check(cli(a, M.cmd_reopen, n=51) == 0 and w.prs[51]["state"] == "open", "and reopened when the author disagrees")
    w.issue(60, "newuser", "it is broken", at=96)
    os.environ["GHBOT_PR"] = "60"
    bf.write_text("Too little to go on.")
    check(cli(a, M.cmd_no_template, pr=60, round="pr60", body_file=str(bf), in_reply_to=None) not in (0, None),
          "closing a blank issue needs a comment that asks for a template")
    bf.write_text("There is too little here to look into. Please open it again with one of the issue templates: "
                  "https://github.com/utmapp/UTM/issues/new/choose")
    check(cli(a, M.cmd_no_template, pr=60, round="pr60", body_file=str(bf), in_reply_to=None) == 0
          and w.prs[60]["state"] == "closed" and w.prs[60]["state_reason"] == "not_planned" and not w.prs[60].get("labels"),
          "a blank issue with nothing to go on is closed as not planned, with the comment and no label")
    os.environ["GHBOT_PR"] = "52"
    w.prs[52]["state"] = "closed"
    w.timeline.append({"pr": 52, "id": w.nid(), "event": "closed", "created_at": ts(0), "actor": w.user("maint")})
    check(cli(a, M.cmd_reopen, n=52) not in (0, None), "an issue someone else closed is not reopened")
    w.prs[52]["state"] = "open"

    # needs info: the comment, the label, and the close after 14 days without an answer
    bf.write_text("Which UTM version and guest?")
    check(cli(a, M.cmd_needs_info, pr=52, round="pr52", body_file=str(bf), in_reply_to=None) not in (0, None),
          "a needs-info comment must announce the automatic close")
    bf.write_text("Which UTM version and guest? This issue is closed automatically if they are not added within 14 days.")
    check(cli(a, M.cmd_needs_info, pr=52, round="pr52", body_file=str(bf), in_reply_to=None) == 0
          and "needs info" in [l["name"] for l in w.prs[52]["labels"]] and a.rec(52).get("needs_info"),
          "needs-info posts the marked comment, adds the label and starts the clock")
    for k in ("GHBOT_MODE", "GHBOT_PR", "GHBOT_SESSION"):
        os.environ.pop(k, None)
    w.issue(59, "newuser", "a maintainer asks for info here", at=94)
    w.actor = "maint"
    FakeGH(w).request("POST", f"/repos/{REPO}/issues/59/labels", {"labels": ["needs info"]})
    w.actor = BOT
    with a:
        r = Poller(FakeGH(w), a.cfg).new_record(59, ts(95)); r.update(kind="issue", needs_info=ts(-40 * 86400)); S.pr_save(r)
        p = Poller(FakeGH(w), a.cfg)
        check(p.close_stale(S.load()) == [] and w.prs[52]["state"] == "open", "nothing is closed before the 14 days are over")
        old = ts(-15 * 86400)
        for c in w.comments:
            if c["pr"] == 52 and c["user"]["login"] == BOT:
                c["created_at"] = old
        check(p.close_stale(S.load()) == [52] and w.prs[52]["state"] == "closed" and w.prs[59]["state"] == "open",
              "after 14 days without an answer the bot's needs-info issue is closed; a maintainer's label is left alone")
        w.prs[52]["state"] = "open"
        r = S.pr_load(52); r.update(needs_info=old, needs_info_body="0000"); S.pr_save(r)
        w.prs[52]["updated_at"] = ts(0)
        check(p.close_stale(S.load()) == [] and w.prs[52]["state"] == "open",
              "an issue whose description was edited after the ask is not closed")
        r = S.pr_load(52); r["needs_info"] = old; S.pr_save(r)
        w.comment(52, "maint", "UTM 5.0.8, Windows 11", 0)   # the author (maint opened #52) answered
        check(p.close_stale(S.load()) == [] and w.prs[52]["state"] == "open" and not S.pr_load(52).get("needs_info"),
              "an issue whose author answered is not closed")

    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'} ({root})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
