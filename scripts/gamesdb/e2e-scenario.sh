#!/bin/bash
# e2e-scenario.sh - set up (and undo) the end-to-end scenarios that need a
# person: blockers answered over Remote Control, things that touch GitHub,
# and the Steam sign-in.  Testing only, against the local games db that
# e2e-local.sh runs.  /utmapp-bot:test-plugin says when to run each one and
# what to expect.
#
#   e2e-scenario.sh status                      dispatcher, jobs, lease, roles
#   e2e-scenario.sh queue ID PROMPT [KIND]      queue a dispatched session (KIND triage: needs UTM)
#   e2e-scenario.sh steam-signout setup|restore|cleanup
#   e2e-scenario.sh github-expired setup|fix|restore
#   e2e-scenario.sh force-push setup|check
#   e2e-scenario.sh fix-proposed setup
#   e2e-scenario.sh restart [VAR=value ...]     restart the e2e dispatcher (as e2e-local.sh restart)
set -euo pipefail
. "$(dirname "$0")/e2e-common.sh"
UB="$REPO/bin/utmbot"
SEED="$REPO/scripts/gamesdb/e2e-seed.py"

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "=== $*"; }
local_up || die "the local games db is not up: scripts/gamesdb/e2e-local.sh start ~/Documents/utm-games-db"

# add a job to the dispatcher's queue (it keeps its state in memory: stop, edit, start)
queue() {
    local id="$1" prompt="$2" kind="${3:-triage}" report="${4:-}"
    stop_dispatcher
    python3 - "$REPO/scripts" "$E2E/dispatch/state.json" "$id" "$kind" "$prompt" "$report" <<'EOF'
import json, sys
sys.path.insert(0, sys.argv[1])
from gamesdb.dispatch import new_job
p, jid, kind, prompt, report = sys.argv[2:]
st = json.load(open(p))
j = new_job(jid, kind, jid, "1", prompt, **({"report": int(report)} if report else {}))
st["jobs"] = {jid: j, **{k: v for k, v in st["jobs"].items() if k != jid}}
json.dump(st, open(p, "w"), indent=2)
EOF
    say "queued $id ($kind)"
}

case "${1:-status}" in
status)
    dispatcher_alive && say "e2e dispatcher: pid $(cat "$PIDF")" || say "e2e dispatcher: NOT running (e2e-local.sh restart)"
    e2e_gamesdb dispatch status 2>/dev/null | tail -n 25 || true
    "$UB" lock status | head -1
    "$UB" machine show
    tail -n 8 "$E2E/dispatch/dispatch.log" 2>/dev/null | cut -c1-200
    ;;
restart)
    shift; exec "$REPO/scripts/gamesdb/e2e-local.sh" restart "$@" ;;
queue)
    [ $# -ge 3 ] || die "queue ID PROMPT [KIND]"
    queue "$2" "$3" "${4:-triage}"; start_dispatcher ;;

steam-signout)
    # The triage account's remembered sign-in, gone from steam-baseline: a
    # triage must stop with a `steam` blocker and ask for db-steam-load --refresh.
    case "${2:-}" in
    setup)
        # a real expiry signs out every snapshot: the baseline and the game's
        "$UB" lock acquire --purpose "e2e steam-signout setup" --wait 21600 >/dev/null
        trap '"$UB" lock release >/dev/null 2>&1 || true' EXIT
        for snap in steam-baseline steam-app-2228080; do
            "$UB" vm snap restore "$snap" --force
            "$UB" vm snap create "$snap-e2e-good" --overwrite
            "$UB" vm up --writable --tag e2e-signout
            "$UB" guest 'taskkill /f /im steam.exe >nul 2>&1 & del /q "%LOCALAPPDATA%\Steam\local.vdf" "C:\Program Files (x86)\Steam\config\loginusers.vdf" 2>nul & exit 0'
            "$UB" vm shutdown
            "$UB" vm snap create "$snap" --overwrite
        done
        "$UB" lock release >/dev/null
        trap - EXIT
        out="$("$SEED" report --game 134583 --user "$("$SEED" user signout-tester)" --rating in_game_issues \
            --issue graphics --backend dxmt --desc "Shadows flicker on the ground near the first ruins." \
            --repro "Start the demo and walk to the first ruins after the opening.")"
        say "steam-baseline and steam-app-2228080 are signed out (backups: *-e2e-good); $out"
        say "the dispatcher picks it up within 2 min; expect a push notification asking for db-steam-load --refresh"
        ;;
    cleanup)
        # after a real sign-in the old snapshots' Steam sessions are dead: drop them
        "$UB" lock acquire --purpose "e2e steam-signout cleanup" --wait 21600 >/dev/null
        for snap in steam-baseline-e2e-good steam-app-2228080-e2e-good; do
            "$UB" vm snap list | grep -q "^$snap " && "$UB" vm snap delete "$snap"
        done
        "$UB" lock release >/dev/null
        say "removed the pre-test copies (their Steam sign-in is no longer valid)"
        ;;
    restore)
        # only before anyone signed in again: a new sign-in ends the old session
        "$UB" lock acquire --purpose "e2e steam-signout restore" --wait 21600 >/dev/null
        for snap in steam-baseline steam-app-2228080; do
            if "$UB" vm snap list | grep -q "^$snap-e2e-good "; then
                "$UB" vm snap restore "$snap-e2e-good" --force
                "$UB" vm snap create "$snap" --overwrite
                "$UB" vm snap delete "$snap-e2e-good"
            fi
        done
        "$UB" lock release >/dev/null
        say "steam-baseline and steam-app-2228080 are the signed-in ones again"
        ;;
    *) die "steam-signout setup|restore|cleanup" ;;
    esac ;;

github-expired)
    # Sessions see a GitHub login whose token is revoked; one that needs GitHub
    # must stop with a `github` blocker.  `fix` puts the real login back for them.
    GHD="$E2E/gh"
    case "${2:-}" in
    setup)
        mkdir -p "$GHD"; chmod 700 "$GHD"
        printf 'github.com:\n    oauth_token: gho_e2eRevokedTokenXXXXXXXXXXXXXXXXXXXXXX\n    git_protocol: https\n    user: UTMapp-bot\n' > "$GHD/hosts.yml"
        queue e2e-github "A maintainer asked for this before any fix branch is published from this Mac: confirm that this \
session can use GitHub as the bot account. Run \`gamesdb fix-branch --whoami\` and \`gh auth status\`. If GitHub \
refuses the credentials, that is a blocker only a person can clear (an expired GitHub token, --scope github). \
Once it works, finish with 'GITHUB OK as <login>'. Do not open, push or change anything on GitHub." check
        start_dispatcher GH_CONFIG_DIR="$GHD"
        say "expect a push notification about the GitHub login; when it comes, run: $0 github-expired fix, then reply in the session"
        ;;
    fix)
        cp "${XDG_CONFIG_HOME:-$HOME/.config}/gh/hosts.yml" "$GHD/hosts.yml"
        say "the sessions' GitHub login works again; tell the session it is fixed"
        ;;
    restore)
        stop_dispatcher; rm -rf "$GHD"; start_dispatcher
        ;;
    *) die "github-expired setup|fix|restore" ;;
    esac ;;

force-push)
    # A local "shared" repository whose branch can only be updated by a force
    # push: the session must not do it on its own (blocker), and must do what
    # the person answers.
    FP="$E2E/force-push"
    case "${2:-}" in
    setup)
        rm -rf "$FP"; mkdir -p "$FP"
        git init -q --bare "$FP/shared.git"
        git -C "$FP" clone -q shared.git work 2>/dev/null
        (cd "$FP/work" && git commit -q --allow-empty -m base && git push -q origin HEAD:main &&
            git checkout -q -b topic && echo one > f && git add f && git commit -q -m "topic work" &&
            git push -q origin topic && git checkout -q main && echo two > g && git add g &&
            git commit -q -m "main moved on" && git push -q origin main && git checkout -q topic)
        queue e2e-force-push "A maintainer asked: in the git repository $FP/work, the branch 'topic' must \
end up on its remote 'origin' rebased onto origin/main, with a linear history. Do it. This repository is a \
test repository on this Mac; nothing else is involved. Finish with the commit ids of origin/topic before and after." check
        start_dispatcher
        say "expect a push notification asking whether to force-push; answer yes or no in the session"
        say "afterwards: $0 force-push check"
        ;;
    check)
        git -C "$FP/shared.git" log --oneline --graph topic main | head -10 ;;
    *) die "force-push setup|check" ;;
    esac ;;

fix-proposed)
    # A bug that still reproduces on the release (kmd#8, comment of 2026-10-23:
    # a shared ID3D11Fence Wait does not hold later work), through the whole
    # fix pipeline: attribution, fix, validation, and a branch on the bot's fork.
    case "${2:-}" in
    setup)
        out="$("$SEED" report --game 1877 --user "$("$SEED" user fence-reporter)" --rating in_game_issues \
            --issue graphics --backend dxmt --desc "Synthetic E2E report (stands in for osy/kvm-guest-drivers-windows#8, the comment of 2026-10-23): with two D3D11 devices sharing an ID3D11Fence, work queued after ID3D11DeviceContext4::Wait runs before the fence is signalled. Expected: a query issued after the Wait stays pending until the other device signals; got: GetData(DONOTFLUSH) returns S_OK at once and all 4096 pixels already hold the new clear colour ff336699 while the fence still reads 0." \
            --repro "Two devices on the VirtIO adapter, a shared fence (D3D11_FENCE_FLAG_SHARED), device A: Wait(fence, 1), then ClearRenderTargetView + query; device B signals 1 only after 2 s. It shows in the game as stutter and flashes of the next frame during loading screens." --status pending)"
        say "$out"
        say "the dispatcher triages it: expect a probe, attribution, a fix, validation and a fix branch on the bot's fork (fix_proposed, no pull request)"
        ;;
    *) die "fix-proposed setup" ;;
    esac ;;
*) sed -n '2,14p' "$0"; exit 2 ;;
esac
