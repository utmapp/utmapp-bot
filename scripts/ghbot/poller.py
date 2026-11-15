"""From GitHub notifications to work on pull requests and issues (one
record per number: GitHub numbers both from one counter).

Who may talk to the bot: members of the org (GHBOT_ORG).  The pull
request's author may too, but only once a member has started the
conversation (initiated_by).  The author of an issue or pull request is
also answered, mention or not, once this account has commented there (a
"reply": the bot asked something and they answered), whoever started that
work: a triage run by hand leaves no record here, so GitHub's own thread
is what counts (spoke_before).  Everyone else, and every bot account, is
ignored without a reply.

Unasked (GHBOT_AUTO, on by default): every issue and pull request opened
after this was switched on is taken up as a "triage" round without anyone
calling the bot (poll_new: new issues send this account no notification,
so the issue list is read).  Not the account's own, not a pull request an
org member opened, not a draft, and not an issue whose type is anything
but Bug (the Feature template sets its type: nothing to do there; no type
means the blank form, and the session decides whether it is a bug).  What the session then does (duplicate,
needs info, a reproduction, nothing at all) is the skills'
business; its author is a stranger, so everything it may write to GitHub
goes through `utmbot gh`'s thread-bound, checked commands.  On such a
thread the author's and members' later comments are follow-ups.

The account's own pull requests (a fix utm-submit opened) are never
reviewed, but every comment a member leaves there is for the bot (no
mention needed) and is answered, in the session of the issue that opened
the PR when this Mac has it.

Ownership among the Macs that share the bot account: a 👀 reaction on the
pull request.  POST answers 201 when this call created it (this Mac owns
the PR) and 200 when the account already had it (another Mac does: never
touch the PR again until that reaction is gone).  Deleting the reaction
releases the PR.  Two Macs that both get 201 in a race both do the work.
The posting commands (`utmbot gh comment|review`) then refuse a round the
account already answered, so the later one discards its work.
"""

import re
import time

from ghbot import state as S
from ghbot.gh import GitHubError

MEMBER_CACHE = 24 * 3600
MEMBER_ASSOC = ("MEMBER", "OWNER")
NEEDS_INFO = "needs info"
NEEDS_INFO_MARK = "<!-- utmapp-bot needs-info -->"


def _digest(text):
    import hashlib
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def auto_on(cfg):
    return str(cfg.get("GHBOT_AUTO", "1")).strip().lower() not in ("0", "", "no", "false", "off")


def _ts(iso):
    try:
        return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (TypeError, ValueError):
        return 0.0


class Poller:
    def __init__(self, gh, cfg=None):
        self.gh = gh
        self.cfg = cfg or S.config()
        self.repo = self.cfg["GHBOT_REPO"]
        self.org = self.cfg["GHBOT_ORG"]
        self._login = None
        self.dry = False  # report what would be queued; claim and save nothing

    # ---- who ---------------------------------------------------------------------------

    def login(self):
        if not self._login:
            self._login = self.gh.get("/user")["login"]
        return self._login

    def is_member(self, st, user, assoc=None):
        if not user:
            return False
        if assoc in MEMBER_ASSOC:
            return True
        cache = st.setdefault("members", {})
        hit = cache.get(user.lower())
        if hit and time.time() - hit[1] < MEMBER_CACHE:
            return hit[0]
        code = self.gh.status("GET", f"/orgs/{self.org}/members/{user}")
        yes = code == 204
        cache[user.lower()] = [yes, time.time()]
        return yes

    def mentions_me(self, body):
        """@login outside code and quoted lines (a quote of the bot's own
        comment is not a call)."""
        if not body:
            return False
        text = re.sub(r"```.*?```", "", body, flags=re.S)
        text = re.sub(r"`[^`\n]*`", "", text)
        text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
        return re.search(rf"(?<![\w/@-])@{re.escape(self.login())}\b(?!-)", text, re.I) is not None

    def spoke_before(self, n, at, rec=None):
        """Did this account comment on #n before `at`?  From GitHub, not from
        this Mac's records: a triage run by hand, or by another Mac, leaves
        none here.  The earliest such comment is kept in the record."""
        first = (rec or {}).get("spoke_at")
        if first is None:
            me = self.login().lower()
            mine = [c["created_at"] for c in self.gh.paged(f"/repos/{self.repo}/issues/{n}/comments")
                    if (c.get("user") or {}).get("login", "").lower() == me]
            first = min(mine) if mine else ""
            if rec is not None and first:
                rec["spoke_at"] = first
        return bool(first) and first < at

    # ---- the claim ------------------------------------------------------------------

    def my_eyes(self, n):
        """This account's 👀 reactions on the pull request (normally one)."""
        rs = self.gh.paged(f"/repos/{self.repo}/issues/{n}/reactions", {"content": "eyes"}, limit=500)
        return [r for r in rs if (r.get("user") or {}).get("login", "").lower() == self.login().lower()]

    def claim(self, n):
        """('mine', id), ('other', id) or ('error', message)."""
        try:
            r = self.gh.request("POST", f"/repos/{self.repo}/issues/{n}/reactions", {"content": "eyes"})
        except GitHubError as e:
            return "error", str(e)
        rid = (r.data or {}).get("id")
        if r.status == 201:
            return "mine", rid
        return "other", rid  # 200: the account already reacted: another Mac owns it

    def release(self, n, rid=None):
        """Delete this account's 👀 on the pull request; True when one was."""
        ids = [rid] if rid else [r["id"] for r in self.my_eyes(n)]
        done = False
        for i in ids:
            try:
                self.gh.request("DELETE", f"/repos/{self.repo}/issues/{n}/reactions/{i}", ok=(204, 404))
                done = True
            except GitHubError as e:
                S.log(f"#{n}: could not delete reaction {i}: {e}")
        return done

    def ack(self, item):
        """👀 on a comment the owner picked up (best effort)."""
        kind, cid = item.get("type"), item.get("comment_id")
        path = {"issue_comment": f"/repos/{self.repo}/issues/comments/{cid}/reactions",
                "review_comment": f"/repos/{self.repo}/pulls/comments/{cid}/reactions"}.get(kind)
        if not path or not cid:
            return
        try:
            self.gh.request("POST", path, {"content": "eyes"})
        except GitHubError:
            pass

    # ---- activity ---------------------------------------------------------------------

    def activity(self, n, pr, since, kind="pr"):
        """Everything on the PR or issue created after `since` (ISO Z),
        oldest first: the description (a new one), comments, and, on a PR,
        review comments, reviews with text, and requests for this account's
        review."""
        R = self.repo
        out = []
        if pr.get("created_at", "") > since:
            out.append({"type": "description", "id": f"pr{n}", "author": pr["user"]["login"],
                        "author_type": pr["user"].get("type"), "assoc": pr.get("author_association"),
                        "at": pr["created_at"], "body": pr.get("body") or "", "url": pr["html_url"]})
        for c in self.gh.paged(f"/repos/{R}/issues/{n}/comments", {"since": since}):
            if c["created_at"] > since:
                out.append({"type": "issue_comment", "id": f"c{c['id']}", "comment_id": c["id"],
                            "author": c["user"]["login"], "author_type": c["user"].get("type"),
                            "assoc": c.get("author_association"), "at": c["created_at"],
                            "body": c.get("body") or "", "url": c["html_url"]})
        if kind != "pr":
            out.sort(key=lambda i: i["at"])
            return out
        for c in self.gh.paged(f"/repos/{R}/pulls/{n}/comments", {"since": since}):
            if c["created_at"] > since:
                out.append({"type": "review_comment", "id": f"rc{c['id']}", "comment_id": c["id"],
                            "author": c["user"]["login"], "author_type": c["user"].get("type"),
                            "assoc": c.get("author_association"), "at": c["created_at"],
                            "body": c.get("body") or "", "url": c["html_url"], "path": c.get("path"),
                            "line": c.get("line"), "in_reply_to": c.get("in_reply_to_id"),
                            "thread_root": c.get("in_reply_to_id") or c["id"]})
        for v in self.gh.paged(f"/repos/{R}/pulls/{n}/reviews"):
            at = v.get("submitted_at") or ""
            if at > since and (v.get("body") or "").strip():
                out.append({"type": "review", "id": f"rv{v['id']}", "author": v["user"]["login"],
                            "author_type": v["user"].get("type"), "assoc": v.get("author_association"),
                            "at": at, "body": v["body"], "url": v.get("html_url"), "state": v.get("state")})
        me = self.login().lower()
        for e in self.gh.paged(f"/repos/{R}/issues/{n}/timeline"):
            if e.get("event") == "review_requested" and e.get("created_at", "") > since and \
                    ((e.get("requested_reviewer") or {}).get("login") or "").lower() == me:
                actor = e.get("actor") or e.get("review_requester") or {}
                out.append({"type": "review_request", "id": f"rr{e['id']}", "author": actor.get("login"),
                            "author_type": actor.get("type"), "assoc": None, "at": e["created_at"],
                            "body": "", "url": pr["html_url"]})
        out.sort(key=lambda i: i["at"])
        return out

    # ---- one pull request ------------------------------------------------------------------

    def new_record(self, n, since):
        return {"pr": int(n), "owner": None, "reaction_id": None, "cursor": since,
                "initiated_by": None, "session_id": None, "pending": [], "rounds": {},
                "job": {"status": "idle"}}

    def process(self, st, n):
        """Read what is new on PR n and turn it into queued input for its
        session.  Returns a short description of what happened.  Holds the
        state lock throughout: the posting commands sessions run write the
        same record (never call it with the lock held)."""
        with S.locked():
            return self._process(st, n)

    def _process(self, st, n):
        rec = S.pr_load(n) or self.new_record(n, st.get("installed_at") or S.utc())
        # a pull request or an issue: the issues endpoint answers for both
        pr = self.gh.get(f"/repos/{self.repo}/issues/{n}")
        kind = "pr" if pr.get("pull_request") else "issue"
        if kind == "pr":
            pr = self.gh.get(f"/repos/{self.repo}/pulls/{n}")
        since = rec.get("cursor") or st.get("installed_at") or S.utc()
        items = self.activity(n, pr, since, kind)
        newest = max([since] + [i["at"] for i in items])
        rec.update(kind=kind, author=pr["user"]["login"], title=pr.get("title"), state=pr.get("state"),
                   merged=pr.get("merged"), head_sha=(pr.get("head") or {}).get("sha"))

        # another Mac's PR: only see whether it was released
        if rec.get("owner") == "other":
            if self.my_eyes(n):
                if not self.dry:
                    rec["cursor"] = newest
                    S.pr_save(rec)
                return "owned by another machine"
            S.log(f"#{n}: released by the other machine")
            rec.update(owner=None, reaction_id=None, lost_race=False)
        elif rec.get("owner") == "mine" and rec.get("reaction_id"):
            if not any(r["id"] == rec["reaction_id"] for r in self.my_eyes(n)):
                S.log(f"#{n}: our 👀 is gone: released")
                rec.update(owner=None, reaction_id=None, released=S.utc())

        me = self.login().lower()
        author = pr["user"]["login"].lower()
        # opened after unasked triage was switched on: taken up without a call
        # (not the account's own, not a member's or a draft pull request)
        auto_new = (auto_on(self.cfg) and bool(st.get("auto_since")) and pr.get("created_at", "") > st["auto_since"]
                    and pr.get("state") == "open"
                    and (kind == "pr" or ((pr.get("type") or {}).get("name") or "Bug") == "Bug")
                    and author != me and pr["user"].get("type") != "Bot" and not author.endswith("[bot]")
                    and not rec.get("auto_seen")
                    and not (kind == "pr" and (pr.get("draft") or self.is_member(st, pr["user"]["login"],
                                                                             pr.get("author_association")))))
        rec["auto_seen"] = True
        # its own PR (or issue), e.g. a fix utm-submit opened: never reviewed,
        # but a member's comment (mention or not) is answered (kind "answer"),
        # in the session of the issue that opened it when this Mac has it
        own = author == me
        rec["own"] = own
        first = "answer" if own else "review"
        inputs = []
        for it in items:
            who = it.get("author") or ""
            if who.lower() == me or it.get("author_type") == "Bot" or who.endswith("[bot]"):
                continue
            member = self.is_member(st, who, it.get("assoc"))
            started = (bool(rec.get("initiated_by")) or bool(rec.get("auto"))
                       or any(i["kind"] in ("review", "answer", "triage") for i in inputs))
            if it["type"] == "review_request":
                if member and not own:
                    it["kind"] = "review" if not started else "followup"
                    it["member"] = True
                    inputs.append(it)
                continue
            it["mentions"] = self.mentions_me(it["body"])
            if auto_new and it["type"] == "description" and not started and not (member and it["mentions"]):
                it.update(kind="triage", member=member)
                inputs.append(it)
                continue
            # the author answering after this account commented (it asked them
            # something): for the bot, whoever started that work
            # (a member's mention still calls the bot the usual way)
            reply = (who.lower() == author and not own and not started and not (member and it["mentions"])
                     and it["type"] != "description" and self.spoke_before(n, it["at"], rec))
            if not (member or reply or (who.lower() == author and started)):
                continue  # may not talk to the bot: no reply
            if reply:
                it["kind"] = "reply"
            elif not started and (it["mentions"] or (own and member)):
                it["kind"] = first  # on its own PR any member's comment is for the bot
            elif started:
                it["kind"] = "followup"
            else:
                continue  # people talking among themselves before the bot was called
            it["member"] = member
            inputs.append(it)

        if self.dry:
            return ("would queue " + ", ".join(f"{i['kind']} {i['id']} by {i['author']}" for i in inputs)
                    if inputs else "nothing for the bot") + f" (owner: {rec.get('owner') or 'nobody'})"
        if not inputs:
            rec["cursor"] = newest
            S.pr_save(rec)
            return "nothing for the bot"

        if rec.get("owner") != "mine":
            got, rid = self.claim(n)
            if got == "error":
                rec["auto_seen"] = not auto_new
                S.pr_save(rec)  # cursor kept: the same activity is tried again
                return f"could not claim: {rid} (tried again at the next poll)"
            if got == "other":
                rec.update(owner="other", reaction_id=rid, cursor=newest, pending=[])
                S.pr_save(rec)
                S.log(f"#{n}: another machine on this account owns it (its 👀 was already there)")
                return "owned by another machine"
            rec.update(owner="mine", reaction_id=rid, claimed=S.utc())
            S.log(f"#{n}: claimed (👀 {rid})")
        if any(i["kind"] == "triage" for i in inputs):
            rec["auto"] = True
        if not rec.get("initiated_by"):
            # a reply starts nothing: the author stays limited to replies
            rec["initiated_by"] = next((i["author"] for i in inputs if i["kind"] in ("review", "answer")), None)
        if own and not rec.get("session_id"):
            origin = next((r for r in S.pr_all() if int(n) in (r.get("opened_prs") or []) and r.get("session_id")),
                          None)
            if origin:
                # resume the session that wrote the fix: it has the context
                rec.update(session_id=origin["session_id"], origin=origin["pr"])
                S.log(f"#{n}: answered in issue #{origin['pr']}'s session {origin['session_id']}")
        for it in inputs:
            if it["type"] != "description" and (it.get("mentions") or it["kind"] in ("review", "answer", "reply")):
                self.ack(it)
            it.pop("assoc", None)
            it["body"] = it["body"][:20000]
            rec.setdefault("pending", []).append(it)
        rec.update(cursor=newest, last_activity=newest)
        job = rec.setdefault("job", {"status": "idle"})
        if job.get("status") in ("idle", "done", None):
            job.update(status="queued", queued=S.utc())
        S.pr_save(rec)
        kinds = ", ".join(f"{i['kind']} {i['id']} by {i['author']}" for i in inputs)
        S.log(f"#{n}: queued {kinds}")
        return f"queued {kinds}"

    # ---- notifications ---------------------------------------------------------------------

    def poll(self, st):
        """PR numbers with new notifications (conditional request: an
        unchanged feed costs nothing).  Read state is never used: every Mac
        on the account shares it."""
        since = st.get("since") or st.get("installed_at") or S.utc(time.time() - 3600)
        hdrs = {"If-Modified-Since": st["last_modified"]} if st.get("last_modified") else {}
        r = self.gh.request("GET", f"/notifications?all=true&since={since}&per_page=50", headers=hdrs)
        st["last_poll"] = S.utc()
        if r.status == 304:
            return []
        if r.headers.get("Last-Modified"):
            st["last_modified"] = r.headers["Last-Modified"]
        if r.headers.get("X-Poll-Interval"):
            st["poll_interval"] = int(r.headers["X-Poll-Interval"])
        items = list(r.data or [])
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", "") or "")
        if m:
            items += self.gh.paged(m[1].replace("https://api.github.com", ""), limit=1000)
        nums, newest = {}, since
        for t in items:
            newest = max(newest, t.get("updated_at") or since)
            if (t.get("repository") or {}).get("full_name", "").lower() != self.repo.lower():
                continue
            subj = t.get("subject") or {}
            if subj.get("type") not in ("PullRequest", "Issue"):
                continue
            mm = re.search(r"/(?:pulls|issues)/(\d+)$", subj.get("url") or "")
            if mm:
                nums.setdefault(int(mm[1]), set()).add(t.get("reason"))
        # a little overlap: a notification updated in the second of the
        # cursor is not lost (PR cursors make a second look harmless)
        st["since"] = S.utc(max(_ts(newest) - 60, _ts(since)))
        return sorted(nums)

    def poll_new(self, st):
        """Numbers of issues and pull requests opened since unasked triage was
        switched on that this Mac has not looked at yet (oldest first).  The
        first call only sets the start: nothing older is ever taken up."""
        if not auto_on(self.cfg):
            return []
        if not st.get("auto_since"):
            st["auto_since"] = S.utc()
            S.log(f"unasked triage: new issues and pull requests from {st['auto_since']} on")
            return []
        since, fresh, url = st["auto_since"], [], f"/repos/{self.repo}/issues?state=all&sort=created&direction=desc&per_page=50"
        while url and len(fresh) < 500:
            r = self.gh.request("GET", url)
            page = [i for i in (r.data or []) if i.get("created_at", "") > since]
            fresh += page
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", "") or "")
            url = m[1].replace("https://api.github.com", "") if m and len(page) == len(r.data or []) and page else None
        todo = [i["number"] for i in fresh if not (S.pr_load(i["number"]) or {}).get("auto_seen")]
        if fresh and not todo:
            st["auto_since"] = max(i["created_at"] for i in fresh)  # all looked at: the list need not grow
        return sorted(todo)

    def close_stale(self, st):
        """Close issues that still wait for details this account asked for
        GHBOT_NEEDS_INFO_DAYS ago.  Decided from GitHub alone, so any Mac may
        run it, and only where it was the bot that asked: its comment carries
        the marker, it put the label on, the label is still there, and since
        then the author has not commented, nobody edited the description, and
        no org member wrote (a person is on it).  A maintainer's own "needs
        info" is never touched."""
        days = float(self.cfg.get("GHBOT_NEEDS_INFO_DAYS") or 14)
        me, closed = self.login().lower(), []
        for rec in S.pr_all():
            if not rec.get("needs_info") or rec.get("kind") != "issue":
                continue
            n = rec["pr"]
            try:
                issue = self.gh.get(f"/repos/{self.repo}/issues/{n}")
                done = issue.get("state") != "open" or NEEDS_INFO not in [l["name"] for l in issue.get("labels") or []]
                asked = None
                if not done:
                    cs = self.gh.paged(f"/repos/{self.repo}/issues/{n}/comments")
                    marks = [c for c in cs if c["user"]["login"].lower() == me and NEEDS_INFO_MARK in (c.get("body") or "")]
                    asked = max((c["created_at"] for c in marks), default=None)
                    author = issue["user"]["login"].lower()
                    labeled = [e for e in self.gh.paged(f"/repos/{self.repo}/issues/{n}/timeline")
                               if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == NEEDS_INFO]
                    mine = bool(labeled) and ((labeled[-1].get("actor") or {}).get("login") or "").lower() == me
                    done = (not asked or not mine
                            or (issue.get("updated_at") and rec.get("needs_info_body") is not None
                                and _digest(issue.get("body")) != rec["needs_info_body"])
                            or any(c["created_at"] > asked and c["user"]["login"].lower() != me
                                   and (c["user"]["login"].lower() == author
                                        or self.is_member(st, c["user"]["login"], c.get("author_association")))
                                   for c in cs))
                if done:
                    with S.locked():
                        cur = S.pr_load(n) or rec
                        cur.pop("needs_info", None)
                        cur.pop("needs_info_body", None)
                        S.pr_save(cur)
                    continue
                if time.time() - _ts(asked) < days * 86400 or self.dry:
                    continue
                self.gh.request("POST", f"/repos/{self.repo}/issues/{n}/comments", {"body": (
                    f"Closing this because the details asked for above were not added within {days:g} days. "
                    "If you can add them, reply here and it will be looked at again.\n\n<!-- utmapp-bot needs-info-closed -->\n")})
                self.gh.request("PATCH", f"/repos/{self.repo}/issues/{n}", {"state": "closed", "state_reason": "not_planned"})
                with S.locked():
                    cur = S.pr_load(n) or rec
                    cur.pop("needs_info", None)
                    cur["closed_needs_info"] = S.utc()
                    S.pr_save(cur)
                closed.append(n)
                S.log(f"#{n}: closed: no details {days:g} days after they were asked for")
            except GitHubError as e:
                S.log(f"#{n}: needs-info check failed: {e}")
        return closed

    def unanswered(self):
        """Threads this Mac has seen where the author wrote after this
        account's last comment and nothing took it up (replies dropped
        before replies counted, or while the poller was down):
        [(record, the bot's last comment time, [the author's comments])]."""
        me = self.login().lower()
        out = []
        for rec in S.pr_all():
            if rec.get("own") or rec.get("owner") or rec.get("session_id") or rec.get("state") == "closed" \
                    or rec.get("pending") or (rec.get("job") or {}).get("status") not in ("idle", None):
                continue
            cs = self.gh.paged(f"/repos/{self.repo}/issues/{rec['pr']}/comments")
            mine = [c["created_at"] for c in cs if (c.get("user") or {}).get("login", "").lower() == me]
            if not mine:
                continue
            last = max(mine)
            author = (rec.get("author") or "").lower()
            late = [c for c in cs if c["created_at"] > last and (c.get("user") or {}).get("login", "").lower() == author
                    and author != me]
            if late:
                out.append((rec, last, late))
        return out

    def sweep(self):
        """Every PR this Mac owns or saw owned elsewhere: is the 👀 still
        there? (A release with no later activity sends no notification.)"""
        for rec in S.pr_all():
            if rec.get("owner") not in ("mine", "other"):
                continue
            eyes = self.my_eyes(rec["pr"])
            gone = (not eyes if rec["owner"] == "other" else
                    not any(r["id"] == rec.get("reaction_id") for r in eyes))
            if gone:
                with S.locked():
                    cur = S.pr_load(rec["pr"]) or rec
                    if cur.get("owner") == rec["owner"]:
                        cur.update(owner=None, reaction_id=None, released=S.utc(), lost_race=False)
                        S.pr_save(cur)
                S.log(f"#{rec['pr']}: its 👀 is gone: released")

    # ---- release when done --------------------------------------------------------------

    def auto_release(self, st):
        """Give up PRs that are closed and quiet for GHBOT_RELEASE_DAYS."""
        days = float(self.cfg.get("GHBOT_RELEASE_DAYS") or 7)
        for rec in S.pr_all():
            if rec.get("owner") != "mine" or rec["job"].get("status") not in ("idle", "done", "failed"):
                continue
            if time.time() - _ts(rec.get("last_activity") or rec.get("claimed") or S.utc()) < days * 86400:
                continue
            try:
                pr = self.gh.get(f"/repos/{self.repo}/pulls/{rec['pr']}")
            except GitHubError:
                continue
            if pr.get("state") != "closed":
                continue
            self.release(rec["pr"], rec.get("reaction_id"))
            with S.locked():
                cur = S.pr_load(rec["pr"]) or rec
                cur.update(owner=None, reaction_id=None, released=S.utc())
                S.pr_save(cur)
            S.log(f"#{rec['pr']}: closed and quiet for {days:g} days: released")
