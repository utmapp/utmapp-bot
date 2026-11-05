#!/bin/bash
# e2e-local.sh - run the whole Auto-Triage loop against a local games db.
#
#   scripts/gamesdb/e2e-local.sh start CHECKOUT   local site (:18080, API :18081) on
#                                                  ~/.utmapp-bot/gamesdb-local/data, and
#                                                  the e2e dispatcher polling it every 2 min
#                                                  (skips what is already up)
#   scripts/gamesdb/e2e-local.sh restart [VAR=value ...]
#                                                  restart the e2e dispatcher (extra
#                                                  settings for it and its sessions)
#   scripts/gamesdb/e2e-local.sh status | log [N] | stop
#
# Testing only; e2e-common.sh says how the e2e dispatcher is kept apart from
# production. The site is `gamesdb local start`; IGDB credentials for it
# (so it resolves store links like production) go in
# ~/.utmapp-bot/gamesdb-local/.env.
set -euo pipefail
. "$(dirname "$0")/e2e-common.sh"

case "${1:-status}" in
start)
    CHECKOUT="${2:?usage: $0 start <utm-games-db checkout>}"
    local_up || "$GDB" local start --checkout "$CHECKOUT"
    grep -q '^IGDB_CLIENT_ID=.' "$DATA/gamesdb-local/.env" 2>/dev/null ||
        echo "note: no IGDB_CLIENT_ID in $DATA/gamesdb-local/.env: new games get no store links"
    if dispatcher_alive; then echo "e2e dispatcher already running (pid $(cat "$PIDF"))"; exit 0; fi
    start_dispatcher
    echo "site http://127.0.0.1:18080"
    ;;
restart)
    shift
    local_up || { echo "ERROR: the local games db is not up: $0 start CHECKOUT" >&2; exit 1; }
    stop_dispatcher
    start_dispatcher "$@"
    ;;
status)
    local_up && echo "local API: up ($API)" || echo "local API: down"
    dispatcher_alive && echo "e2e dispatcher: pid $(cat "$PIDF")" || echo "e2e dispatcher: not running"
    e2e_gamesdb dispatch status 2>/dev/null || true
    ;;
log)
    tail -n "${2:-40}" "$E2E/dispatch/dispatch.log" 2>/dev/null || echo "no log yet"
    ;;
stop)
    stop_dispatcher
    "$GDB" local stop
    ;;
*) echo "usage: $0 start CHECKOUT | restart [VAR=value ...] | status | log [N] | stop" >&2; exit 2 ;;
esac
