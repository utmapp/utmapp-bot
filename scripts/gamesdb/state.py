"""Local state shared by the db skills and the dispatcher.

sessions  which Claude Code session works on which report, so a reporter's
          answer to a clarification goes back to the session that asked.
signals   what a dispatched session tells the dispatcher about itself:
          `blocked` (it cannot go on without a person: the dispatcher brings
          the user in over Remote Control), `unblocked` (the person
          answered; the dispatcher ends the Remote Control session and lets
          the work go on headless) and `later` (the work has to wait: the
          dispatcher resumes the session at the time it asked for).
"""

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager

from gamesdb.tunnel import STATE, Fail

SESSIONS = STATE / "sessions.json"
SIGNALS = STATE / "dispatch" / "signals"
SCOPES = ("job", "steam", "utm", "github", "claude", "server", "all")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _private_dir(p):
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


@contextmanager
def _locked(name):
    _private_dir(STATE)
    with open(STATE / f".{name}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _check_sid(session_id):
    if not re.fullmatch(r"[0-9a-f-]{36}", session_id or ""):
        raise Fail(f"not a session id: {session_id!r}")


# ---- sessions per report -----------------------------------------------------------

def sessions():
    try:
        return json.loads(SESSIONS.read_text())
    except (OSError, ValueError):
        return {}


def session_note(report_id, session_id, **extra):
    _check_sid(session_id)
    with _locked("sessions"):
        s = sessions()
        e = s.setdefault(str(report_id), {})
        e.update({"session_id": session_id, "updated": _now()}, **extra)
        _write_json(SESSIONS, s)
    return s[str(report_id)]


def session_get(report_id):
    return sessions().get(str(report_id))


# ---- signals from dispatched sessions --------------------------------------------------

def signal(session_id, state, **fields):
    _check_sid(session_id)
    _private_dir(SIGNALS)
    rec = {"session_id": session_id, "state": state, "time": _now(), **fields}
    _write_json(SIGNALS / f"{session_id}.json", rec)
    return rec


def read_signal(session_id):
    try:
        return json.loads((SIGNALS / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        return None


def clear_signal(session_id):
    (SIGNALS / f"{session_id}.json").unlink(missing_ok=True)
