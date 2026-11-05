"""Where the games database's internal API is, and what every gamesdb
program shares: the settings, the state directory, errors (Fail, die, note,
cli) and the agents' logs.

The server's internal API is a Unix socket on the server. This Mac reaches it
through one SSH forward, kept up by the tunnel agent that db-setup installs
(scripts/gamesdb/tunneld.py, a LaunchAgent): 127.0.0.1:<port> here is the
socket there, for as long as the user is logged in.  Commands only use that
endpoint; they never open SSH themselves, and they fail at once when it does
not answer.

Settings (environment first, then ~/.utmapp-bot/gamesdb.env):
  GAMESDB_SSH          the forwarding-only account on the server, _triage@host
  GAMESDB_TUNNEL_PORT  local port of the forward (default 8081)
  GAMESDB_SOCKET       the socket on the server (default
                       /usr/local/gamesdb/run/internal.sock)
  GAMESDB_URL          an API base URL used instead of the tunnel (a local
                       instance for testing)
  GAMESDB_SITE         the site's public base URL (https://…), for links to
                       its pages; set by db-setup --site.  No site is built in
  GAMESDB_HOLDER       this Mac's name as a triage machine (default: its
                       LocalHostName): which machine holds a report's
                       triage lease, sent with every API call
"""

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("UTMAPP_BOT_DATA", "~/.utmapp-bot")).expanduser()
CONFIG_FILE = DATA / "gamesdb.env"
# GAMESDB_STATE keeps a test run's dispatcher, sessions and logs apart
STATE = Path(os.environ["GAMESDB_STATE"]).expanduser() if os.environ.get("GAMESDB_STATE") else DATA / "gamesdb"
STATUS_FILE = STATE / "tunnel.status.json"
DEFAULT_SOCKET = "/usr/local/gamesdb/run/internal.sock"
AGENT_LABEL = "app.getutm.utmapp-bot.tunnel"


class Fail(Exception):
    pass


def die(msg):
    raise Fail(msg)


def note(msg):
    print(f"gamesdb: {msg}", file=sys.stderr, flush=True)


def cli(main):
    """Run main(argv) as a program: a Fail is one ERROR line and exit 1."""
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


def append_log(path, msg, echo=False, max_bytes=2 << 20):
    """One timestamped line in an agent's log (rotated to .log.1 past
    max_bytes); echo also puts the message on stderr."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        path.replace(path.with_suffix(".log.1"))
    with open(path, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    if echo:
        print(msg, file=sys.stderr, flush=True)


def config():
    """GAMESDB_* settings: the environment wins over the config file."""
    vals = {}
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
        # the environment wins: every GAMESDB_* setting, and any other
        # setting the file has (CLAUDE_BIN, UTM_APP, …)
        if k.startswith("GAMESDB_") or k in vals:
            vals[k] = v
    return vals


def holder(cfg=None):
    """This Mac's name among the triage machines (see GAMESDB_HOLDER)."""
    h = (cfg or config()).get("GAMESDB_HOLDER", "").strip()
    if h:
        return h
    try:
        h = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                           timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        h = ""
    return h or os.uname().nodename.split(".")[0]


def state_dir():
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    return STATE


def local_port(cfg=None):
    return int((cfg or config()).get("GAMESDB_TUNNEL_PORT") or 8081)


def healthy(base, timeout=5):
    try:
        with urllib.request.urlopen(base + "/internal/healthz", timeout=timeout) as r:
            return json.load(r).get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def read_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return None


def base_url():
    """The API base URL; fails when nothing answers there."""
    cfg = config()
    if cfg.get("GAMESDB_URL"):
        url = cfg["GAMESDB_URL"].rstrip("/")
        if not healthy(url):
            die(f"GAMESDB_URL={url} does not answer /internal/healthz")
        return url, f"GAMESDB_URL {url}"
    port = local_port(cfg)
    url = f"http://127.0.0.1:{port}"
    if healthy(url):
        return url, f"server via the tunnel on 127.0.0.1:{port}"
    st = read_status()
    why = ""
    if st:
        why = f" The tunnel agent says: {st.get('state')} since {st.get('since')}" + (
            f" ({st.get('last_error')})" if st.get("last_error") else "") + "."
    elif not cfg.get("GAMESDB_SSH"):
        why = " This Mac is not set up for it: run /utmapp-bot:db-setup once."
    else:
        why = " The tunnel agent has written no status: is it installed (`gamesdb tunnel status`)?"
    die(f"the games database is unreachable at {url}.{why} Commands do not open the "
        "connection themselves; the tunnel agent reconnects on its own when the server or "
        "the network comes back.")


def status():
    cfg = config()
    if cfg.get("GAMESDB_URL"):
        url = cfg["GAMESDB_URL"].rstrip("/")
        print(f"target   GAMESDB_URL {url} (tunnel not used)")
        print(f"healthy  {'yes' if healthy(url) else 'NO'}")
        return
    port = local_port(cfg)
    st = read_status() or {}
    p = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                       capture_output=True, text=True)
    agent = "loaded" if p.returncode == 0 else "NOT loaded (run /utmapp-bot:db-setup)"
    print(f"agent    {AGENT_LABEL}: {agent}")
    print(f"server   {'configured' if cfg.get('GAMESDB_SSH') else 'NOT configured'} "
          f"(GAMESDB_SSH in {CONFIG_FILE})")
    print(f"tunnel   127.0.0.1:{port} -> {cfg.get('GAMESDB_SOCKET') or DEFAULT_SOCKET}")
    if st:
        print(f"state    {st.get('state')} since {st.get('since')}"
              + (f"; last error: {st.get('last_error')}" if st.get("last_error") else "")
              + (f"; next try {st.get('next_try')}" if st.get("next_try") else ""))
    print(f"healthy  {'yes' if healthy(f'http://127.0.0.1:{port}') else 'NO'}")


def site():
    """The games database's public base URL (GAMESDB_SITE)."""
    url = (config().get("GAMESDB_SITE") or "").rstrip("/")
    if not url:
        die(f"no GAMESDB_SITE in {CONFIG_FILE}: run db-setup with --site https://<the site>")
    return url
