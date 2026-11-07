# e2e-common.sh - what e2e-local.sh and e2e-scenario.sh share (sourced, not run).
#
# Testing only. The e2e dispatcher runs in a clean environment, as launchd
# would start it, with GAMESDB_URL at the local API and its own state in
# ~/.utmapp-bot/gamesdb-e2e (cursor, sessions, signals, logs), so nothing
# mixes with production's. The Steam VM, its snapshots and the UTM lease
# are the real ones: dispatched sessions drive the real VM.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}"
E2E="$DATA/gamesdb-e2e"            # the e2e dispatcher's GAMESDB_STATE
PIDF="$E2E/dispatch.pid"
API=http://127.0.0.1:18081         # `gamesdb local start`'s default port; the site is on 18080
GDB="$REPO/bin/gamesdb"
# the UTM sessions drive: production's (/Applications/UTM.app, what db-setup
# configures) unless UTM_APP chooses another, such as a dev build to test
export UTM_APP="${UTM_APP:-/Applications/UTM.app}"
mkdir -p "$E2E"; chmod 700 "$E2E"

local_up() { curl -sf "$API/internal/healthz" >/dev/null; }
LABEL=app.getutm.utmapp-bot.e2e-dispatch
PLIST="$E2E/$LABEL.plist"
dispatcher_alive() { launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q "state = running"; }

stop_dispatcher() {
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; sleep 2
        echo "e2e dispatcher stopped (running sessions go on)"
    fi
    rm -f "$PIDF"
}

# start_dispatcher [VAR=value ...]: the extra settings go to it and its sessions.
# It runs as production's does: a launchd job under the agent launcher (UTM Bot
# Agent.app), so macOS charges its privacy-guarded actions (Local Network,
# UTM's container, Automation) to the launcher, not to this terminal.
start_dispatcher() {
    local claude launcher; claude="$(command -v claude)"
    launcher="$DATA/agents/UTM Bot Agent.app/Contents/MacOS/utmbot-agent"
    [ -x "$launcher" ] || { echo "no agent launcher: run db-setup first" >&2; return 1; }
    mkdir -p "$DATA/work"
    python3 -c "import sys, pathlib; sys.path.insert(0, sys.argv[1]); from gamesdb import setup; setup.trust_workdir(pathlib.Path(sys.argv[2]).resolve())" \
        "$REPO/scripts" "$DATA/work"
    python3 - "$PLIST" "$LABEL" "$launcher" "$REPO" "$E2E" "$DATA" "$claude" "$API" "$UTM_APP" "$@" <<'PY'
import os, plistlib, sys
plist, label, launcher, repo, e2e, data, claude, api, utm_app, *extra = sys.argv[1:]
env = {"HOME": os.environ["HOME"], "USER": os.environ.get("USER", ""), "LANG": "en_US.UTF-8",
       "PATH": f"{os.path.dirname(claude)}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
       "GAMESDB_URL": api, "GAMESDB_STATE": e2e, "GAMESDB_DISPATCH_EVERY": "120",
       "GAMESDB_PLUGIN_DIR": repo, "GAMESDB_WORKDIR": f"{data}/work", "CLAUDE_BIN": claude,
       "UTM_APP": utm_app}
env.update(kv.split("=", 1) for kv in extra)
with open(plist, "wb") as f:
    plistlib.dump({"Label": label, "ProgramArguments": [launcher, "/opt/homebrew/bin/python3",
                   f"{repo}/scripts/gamesdb/dispatch.py", "run"], "EnvironmentVariables": env,
                   "RunAtLoad": True, "KeepAlive": False, "AbandonProcessGroup": True,
                   "StandardOutPath": f"{e2e}/dispatch.out", "StandardErrorPath": f"{e2e}/dispatch.out",
                   "WorkingDirectory": data}, f)
PY
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$PLIST" || return 1
    sleep 2
    launchctl print "gui/$(id -u)/$LABEL" | awk '/^\tpid = /{print $3}' > "$PIDF"
    echo "e2e dispatcher pid $(cat "$PIDF") (launchd job $LABEL, under the agent launcher)${*:+ (with $*)}, state in $E2E"
}

# `gamesdb dispatch …` against the e2e dispatcher's state
e2e_gamesdb() { GAMESDB_URL="$API" GAMESDB_STATE="$E2E" "$GDB" "$@"; }
