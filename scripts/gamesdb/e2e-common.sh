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
# the UTM sessions drive: chosen as the harness chooses (UTM_APP, the last
# build-utm.sh build, /Applications/UTM.app)
export UTM_APP="${UTM_APP:-$(bash "$REPO/scripts/lib/utm.sh" app)}"
mkdir -p "$E2E"; chmod 700 "$E2E"

local_up() { curl -sf "$API/internal/healthz" >/dev/null; }
dispatcher_alive() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

stop_dispatcher() {
    if dispatcher_alive; then kill "$(cat "$PIDF")"; sleep 2; echo "e2e dispatcher stopped (running sessions go on)"; fi
    rm -f "$PIDF"
}

# start_dispatcher [VAR=value ...]: the extra settings go to it and its sessions
start_dispatcher() {
    local claude; claude="$(command -v claude)"
    mkdir -p "$DATA/work"
    python3 -c "import sys, pathlib; sys.path.insert(0, sys.argv[1]); from gamesdb import setup; setup.trust_workdir(pathlib.Path(sys.argv[2]).resolve())" \
        "$REPO/scripts" "$DATA/work"
    env -i HOME="$HOME" USER="$USER" LANG=en_US.UTF-8 \
        PATH="$(dirname "$claude"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        GAMESDB_URL="$API" GAMESDB_STATE="$E2E" GAMESDB_DISPATCH_EVERY=120 \
        GAMESDB_PLUGIN_DIR="$REPO" GAMESDB_WORKDIR="$DATA/work" CLAUDE_BIN="$claude" \
        UTM_APP="$UTM_APP" "$@" \
        nohup python3 "$REPO/scripts/gamesdb/dispatch.py" run >> "$E2E/dispatch.out" 2>&1 &
    echo $! > "$PIDF"
    echo "e2e dispatcher pid $!${*:+ (with $*)}, state in $E2E"
}

# `gamesdb dispatch …` against the e2e dispatcher's state
e2e_gamesdb() { GAMESDB_URL="$API" GAMESDB_STATE="$E2E" "$GDB" "$@"; }
