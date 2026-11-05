"""The games database's internal API: one client for every caller.

The endpoint is the tunnel's (or GAMESDB_URL's; see tunnel.py).  Every
write is appended to ~/.utmapp-bot/gamesdb/actions.log.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from gamesdb import tunnel
from gamesdb.tunnel import die, note

# Moderator edits (redaction on a deletion request) need endpoints newer than
# the first internal API; against a server without them a command stops and
# says so, and nothing is changed.
NEEDS_SERVER = ("this server has no {what} yet (requested for redaction on deletion requests: "
                "PATCH /internal/reports/{{id}}, PATCH /internal/comments/{{id}}, DELETE "
                "/internal/attachments/{{id}}, deletion-request status \"redacted\"); nothing was "
                "changed")


class Api:
    def __init__(self, quiet=False):
        self.base, self.where = tunnel.base_url()
        self.holder = tunnel.holder()
        # Dispatched sessions (auto mode) write as this machine, under the
        # lease the dispatcher holds for them. A person's session (interactive)
        # names no machine: it takes no lease, and the server then treats
        # in_progress as work by hand that the dispatchers leave alone (a
        # machine that names itself must hold the lease to set in_progress).
        self.as_machine = os.environ.get("GAMESDB_MODE") == "auto"
        if not quiet:
            note(f"target: {self.where}")

    def call(self, method, path, body=None, raw=False, timeout=120):
        data = None
        # which triage machine calls: the server refuses triage writes on a
        # report another machine holds the lease of (409)
        headers = {"Accept": "application/json"}
        if self.as_machine:
            headers["X-Triage-Holder"] = self.holder
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                status, ctype = r.status, r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            payload = e.read()
            status, ctype = e.code, e.headers.get("Content-Type", "")
        if method != "GET":
            audit(self.where, method, path, body, status)
        if raw:
            return status, payload
        if "json" not in ctype:
            # 201 answers of older servers carry no JSON content type
            try:
                return status, json.loads(payload or b"null")
            except ValueError:
                # Go's mux answers unknown routes in plain text: an older server
                return status, {"error": payload.decode("utf-8", "replace").strip(),
                                "_not_json": True}
        return status, json.loads(payload or b"null")

    def ok(self, method, path, body=None, needs=None):
        """The answer of a call that must succeed.  needs: the endpoint's
        name, for the error when an older server does not have it."""
        status, out = self.call(method, path, body)
        if needs and no_route(status, out):
            die(NEEDS_SERVER.format(what=needs))
        if status >= 300:
            die(f"{method} {path} -> {status}: {json.dumps(out)}")
        return out


def no_route(status, res):
    """Go's router answers a route it does not have in plain text ("404 page
    not found", "Method Not Allowed"); the API's own errors are JSON."""
    err = str(res.get("error", "")) if isinstance(res, dict) and res.get("_not_json") else ""
    return status in (404, 405) and ("page not found" in err or "Method Not Allowed" in err)


def audit(where, method, path, body, status):
    """Every write, for the record: who changed what on which target."""
    try:
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "target": where,
                "method": method, "path": path, "status": status}
        if body is not None:
            s = json.dumps(body)
            line["body"] = body if len(s) <= 2000 else s[:2000] + "…"
        with open(tunnel.state_dir() / "actions.log", "a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def q(**kw):
    """A query string from the parameters that are set."""
    kw = {k: v for k, v in kw.items() if v not in (None, "", False)}
    return ("?" + urllib.parse.urlencode(kw)) if kw else ""
