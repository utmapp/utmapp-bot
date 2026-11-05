"""Running unattended Claude Code sessions: reading how one ended (its
stream-json log), recognising usage limits, login loss and outages, the
login's expiry, the permission rules that keep a session out of the plugin,
and a notification on the Mac."""

import datetime as dt
import json
import os
import re
import subprocess
import time


def plugin_guard(plugin):
    """Permission rules that keep sessions from editing the plugin."""
    root = "/" + plugin.rstrip("/")  # `//abs/path` is an absolute path in a rule
    # (an Edit rule covers NotebookEdit too.)  --disallowedTools takes every
    # word after it up to the next option: it goes last, after the prompt
    return ["--disallowedTools", f"Edit({root}/**)", f"Write({root}/**)",
            f"Bash(git -C {plugin}:*)"]


def login_expiry():
    """When the Claude Code login (its OAuth refresh token) runs out, from the
    login keychain; None when it cannot be read (API key, locked keychain)."""
    r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return None
    try:
        v = json.loads(r.stdout).get("claudeAiOauth", {}).get("refreshTokenExpiresAt")
    except ValueError:
        return None
    return v / 1000 if v and v > 1e11 else v


def last_result(path):
    """The final `result` event of a stream-json log, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            return ev
    return None


def tail_text(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            return f.read().decode("utf-8", "replace")[-4000:]
    except OSError:
        return ""


LIMIT_RE = re.compile(r"(?i)(usage limit|limit reached|rate limit|hit your limit|limit will reset|resets? (at|in)|session limit|weekly limit)")
AUTH_RE = re.compile(r"(?i)(invalid api key|please run /login|not logged in|oauth token (has )?expired|authentication_error|401)")
OUTAGE_RE = re.compile(r"(?i)(overloaded|529|api error|internal server error|\b5\d\d\b|econnreset|enotfound|could not connect|network error|timed? ?out|service unavailable)")


def reset_time(text):
    """When a usage limit resets, if the message says (epoch, or a clock time)."""
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return float(m[1])
    m = re.search(r"(?i)resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([A-Za-z_]+/[A-Za-z_/]+)\))?", text)
    if m:
        h, mi = int(m[1]), int(m[2] or 0)
        if m[3]:
            h = h % 12 + (12 if m[3].lower() == "pm" else 0)
        tz = None
        if m[4]:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(m[4])
            except Exception:
                tz = None
        t = dt.datetime.now(tz).replace(hour=h % 24, minute=mi, second=0, microsecond=0)
        if t.timestamp() <= time.time():
            t += dt.timedelta(days=1)
        return t.timestamp() + 120
    return None


def notify(msg, title="utmapp-bot"):
    """A notification on this Mac."""
    esc = msg.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{esc}" with title "{title}"'],
                   capture_output=True)


def lease_busy(data_dir, name="utm"):
    """Someone holds a utmbot lease (~/.utmapp-bot/locks/<name>.lease): an
    interactive session or another agent's job. Auto work that needs it
    waits rather than starting a session that would only sit waiting."""
    p = data_dir / "locks" / f"{name}.lease"
    if not p.exists():
        return False
    try:
        lease = json.loads(p.read_text())
    except ValueError:
        return False
    pid = lease.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
    return True
