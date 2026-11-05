"""Settings and local state of the GitHub poller (this Mac only).

~/.utmapp-bot/github.env     settings (KEY=VALUE; the environment wins)
~/.utmapp-bot/github/
  state.json                 poll cursor, the account's login, membership cache
  prs/<n>.json               one pull request: who owns it, its session, the
                             activity already handled, its job
  sessions/                  the sessions' stream-json logs
  signals/<session>.json     what a session says about itself (blocked, later)
  ghbot.log
"""

import fcntl
import json
import os
import shlex
import time
from contextlib import contextmanager
from pathlib import Path

DATA = Path(os.environ.get("UTMAPP_BOT_DATA", "~/.utmapp-bot")).expanduser()
CONFIG_FILE = DATA / "github.env"
D = Path(os.environ["GHBOT_STATE"]).expanduser() if os.environ.get("GHBOT_STATE") else DATA / "github"
STATE_FILE = D / "state.json"
PRS = D / "prs"
SIGNALS = D / "signals"
SESSIONS = D / "sessions"
LOG = D / "ghbot.log"
PAUSED = D / "PAUSED"

DEFAULTS = {"GHBOT_REPO": "utmapp/UTM", "GHBOT_ORG": "utmapp", "GHBOT_POLL": "120",
            "GHBOT_REVIEWS_PER_DAY": "0", "GHBOT_RELEASE_DAYS": "7", "GHBOT_MAX_SESSIONS": "2"}


def config():
    vals = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            try:
                parts = shlex.split(v)
            except ValueError:
                parts = [v.strip()]
            vals[k] = parts[0] if parts else ""
    for k, v in os.environ.items():
        if k.startswith("GHBOT_") and k != "GHBOT_STATE" or k in vals:
            vals[k] = v
    return vals


def write_config(updates):
    lines, seen = [], set()
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            k = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if k in updates:
                seen.add(k)
                if updates[k] is None:
                    continue
                line = f"{k}={shlex.quote(str(updates[k]))}"
            lines.append(line)
    else:
        lines.append("# GitHub poller settings for the utmapp-bot plugin (written by github-setup)")
    for k, v in updates.items():
        if k not in seen and v is not None:
            lines.append(f"{k}={shlex.quote(str(v))}")
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(CONFIG_FILE, 0o600)


def private(p):
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def utc(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def log(msg, echo=True):
    private(D)
    if LOG.exists() and LOG.stat().st_size > 4 << 20:
        LOG.replace(LOG.with_suffix(".log.1"))
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    if echo:
        import sys
        print(msg, file=sys.stderr, flush=True)


def _write(path, data):
    private(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


@contextmanager
def locked(name="state"):
    """One writer at a time: the poller and the commands sessions run (a
    session's `gh comment` records what it posted in the PR's file)."""
    private(D)
    with open(D / f".{name}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load():
    return _read(STATE_FILE, {})


def save(st):
    _write(STATE_FILE, st)


def pr_path(n):
    return PRS / f"{int(n)}.json"


def pr_load(n):
    return _read(pr_path(n), None)


def pr_save(rec):
    rec["updated"] = utc()
    _write(pr_path(rec["pr"]), rec)


def pr_all():
    out = []
    if PRS.exists():
        for p in sorted(PRS.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
            r = _read(p, None)
            if r:
                out.append(r)
    return out


def signal(session_id, state, **fields):
    rec = {"session_id": session_id, "state": state, "time": utc(), **fields}
    _write(SIGNALS / f"{session_id}.json", rec)
    return rec


def read_signal(session_id):
    return _read(SIGNALS / f"{session_id}.json", None)


def clear_signal(session_id):
    (SIGNALS / f"{session_id}.json").unlink(missing_ok=True)


def use(d):
    """Point the state at directory d (tests: two Macs in one process)."""
    global D, STATE_FILE, PRS, SIGNALS, SESSIONS, LOG, PAUSED
    D = Path(d)
    STATE_FILE, PRS, SIGNALS = D / "state.json", D / "prs", D / "signals"
    SESSIONS, LOG, PAUSED = D / "sessions", D / "ghbot.log", D / "PAUSED"
