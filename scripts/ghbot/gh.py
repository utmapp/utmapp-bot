"""A small GitHub REST client for the poller: the signed-in `gh` account's
token, status codes kept (a reaction answers 201 when this call created it
and 200 when the account already had it: the claim depends on that),
conditional requests, pagination and rate-limit back-off."""

import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, status, msg, retry_at=None):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status
        self.retry_at = retry_at


class Response:
    def __init__(self, status, headers, data):
        self.status, self.headers, self.data = status, headers, data


class GitHub:
    def __init__(self, token=None):
        self._token = token
        self._token_ts = 0

    def token(self):
        # `gh` refreshes its stored token; read it again now and then
        if not self._token or (self._token_ts and time.time() - self._token_ts > 600):
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
            if r.returncode != 0 or not r.stdout.strip():
                raise GitHubError(401, "gh is not signed in (`gh auth login` as the bot account)")
            self._token, self._token_ts = r.stdout.strip(), time.time()
        return self._token

    def request(self, method, path, body=None, headers=None, ok=(200, 201, 204), timeout=60):
        url = path if path.startswith("http") else API + path
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "utmapp-bot-ghbot", "Authorization": f"Bearer {self.token()}"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status, hdrs = r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            raw, status, hdrs = e.read(), e.code, dict(e.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(0, f"{method} {url}: {e}")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode("utf-8", "replace")
        if status in ok or status == 304:
            return Response(status, hdrs, payload)
        retry_at = None
        if status in (403, 429) and (hdrs.get("X-RateLimit-Remaining") == "0" or hdrs.get("Retry-After")):
            retry_at = (time.time() + int(hdrs["Retry-After"])) if hdrs.get("Retry-After") else \
                float(hdrs.get("X-RateLimit-Reset") or time.time() + 600)
        msg = payload.get("message") if isinstance(payload, dict) else str(payload)[:200]
        raise GitHubError(status, f"{method} {path}: {msg}", retry_at)

    def get(self, path, **kw):
        return self.request("GET", path, **kw).data

    def paged(self, path, params=None, limit=2000):
        """Every item of a list endpoint (follows Link: next)."""
        q = dict(params or {})
        q.setdefault("per_page", 100)
        url = f"{path}?{urllib.parse.urlencode(q)}"
        out = []
        while url and len(out) < limit:
            r = self.request("GET", url)
            out += r.data or []
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", "") or "")
            url = m[1] if m else None
        return out

    def status(self, method, path, ok=(200, 201, 204, 404, 302), **kw):
        """The status code alone (membership checks answer 204 or 404)."""
        return self.request(method, path, ok=ok, **kw).status
