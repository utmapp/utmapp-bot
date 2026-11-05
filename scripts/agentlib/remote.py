"""Starting and ending the sessions an agent runs, and reaching the user's
phone.

Headless sessions write their stream-json log.  Remote Control sessions need
a terminal (an agent has none), so they run under script(1) with the
terminal fed from a pipe the agent keeps.  Ending one by typing /exit lets
Claude Code close the Remote Control session properly, where a kill leaves
it listed as active on claude.ai.

A notice is one push notification to the user's phone.  PushNotification
reaches the phone only while Remote Control is connected, so a notice is a
short Remote Control session (a small model) that sends one and then touches
a file.  The agent ends the session when the file appears, or after 10
minutes.
"""

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

NOTICE_MODEL = "claude-haiku-5-5"  # a notice session only sends one notification
NOTICE_TIMEOUT = 600

# PushNotification skips a terminal it thinks someone is at, and the prompt
# just submitted counts as that: wait first (measured: after 120 s it goes
# through to the phone)
PUSH_ONCE = ("Nobody is at this terminal: first run `sleep 120`, then send the user exactly one push "
             "notification (PushNotification, status proactive, under 200 characters)")

NOTICE = ("This session exists only to notify the user; do nothing else. " + PUSH_ONCE +
          " with this message: {msg} Then run `{done}` and stop.")

children = {}     # pid -> Popen, for the sessions this process started
_remote_in = {}   # pid of a Remote Control session -> the write end of its terminal input


def alive(pid):
    if not pid:
        return False
    proc = children.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False  # gone, or the pid is another user's process now


def launch(cmd, cwd, env, log_path, remote=False):
    """Start a session in cwd: headless into log_path (stream-json), or with
    remote=True under script(1), its typescript beside log_path."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    Path(cwd).mkdir(parents=True, exist_ok=True)
    if remote:
        env = dict(env, TERM="xterm-256color")
        cmd = ["/usr/bin/script", "-q", str(log_path.with_suffix(".remote.typescript"))] + list(cmd)
        r, w = os.pipe()
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=r, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        os.close(r)
        _remote_in[proc.pid] = w
    else:
        with open(log_path, "a") as out:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, env=env, start_new_session=True)
    children[proc.pid] = proc
    return proc


def end(pid, wait_s=30):
    """End a Remote Control session: /exit typed into it, a kill only if it
    does not leave (or a headless one: a kill)."""
    w = _remote_in.pop(pid, None)
    if w is not None:
        try:
            os.write(w, b"\x1b")      # out of any half-typed input or menu
            time.sleep(0.5)
            os.write(w, b"/exit\r")
        except OSError:
            pass
        t0 = time.time()
        while time.time() - t0 < wait_s and alive(pid):
            time.sleep(1)
        try:
            os.close(w)
        except OSError:
            pass
    if pid and alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, TypeError):
            pass
    children.pop(pid, None)


# ---- notices ----------------------------------------------------------------------------

def notice_cmd(claude, prompt, session_id, name, model=None):
    return [claude, prompt, "--session-id", session_id, "--remote-control", name,
            "--permission-mode", "auto", "--model", model or NOTICE_MODEL]


def start_notice(msg, *, claude, cwd, env, state_dir, name, model=None):
    """Send msg to the user's phone.  Returns the notice's record (keep it
    in the agent's state and pass it to check_notice until it is over)."""
    sid = str(uuid.uuid4())
    done = Path(state_dir) / "notices" / f"{sid}.done"
    done.parent.mkdir(parents=True, exist_ok=True)
    prompt = NOTICE.format(msg=msg, done=f"touch {done}")
    proc = launch(notice_cmd(claude, prompt, sid, name, model), cwd, env,
                  Path(state_dir) / "notices" / f"{sid}.log", remote=True)
    return {"session_id": sid, "pid": proc.pid, "done": str(done), "started_ts": time.time(), "msg": msg}


def check_notice(n, extra_done=False):
    """None while the notice is going; 'done' or 'failed' once it is over
    (its session ended).  extra_done: another sign it was sent (an older
    notice that signalled differently)."""
    done = Path(n["done"]) if n.get("done") else None
    sent = extra_done or bool(done and done.exists())
    if not (sent or not alive(n.get("pid")) or time.time() - n.get("started_ts", 0) > NOTICE_TIMEOUT):
        return None
    end(n.get("pid"))
    if done:
        done.unlink(missing_ok=True)
    return "done" if sent else "failed"
