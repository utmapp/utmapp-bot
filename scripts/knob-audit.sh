#!/bin/bash
# knob-audit.sh - which environment knobs and log strings do these trees really have?
#
# usage: knob-audit.sh <tree> [<tree> ...]               list every env knob read
#        knob-audit.sh --has NAME_OR_STRING <tree> ...    is this knob/log line present?
#
# Debug knobs come and go: a knob from an old note that the code no longer
# reads is silently ignored, and an experiment run with it measures nothing.
# Before relying on a knob or a log fingerprint, check it against the trees
# you are actually running.  Covers getenv(), Mesa's debug_get_option /
# DEBUG_GET_ONCE_* / os_get_option, DXMT's env::getEnvVar, and NPT_DEBUG-style
# flag tables ({ "token", FLAG, "description" }).
set -uo pipefail

if [ "${1:-}" = "--has" ]; then
    what="$2"; shift 2
    [ $# -gt 0 ] || { echo "usage: $0 --has STRING <tree>..." >&2; exit 2; }
    if grep -rnF --include='*.[ch]' --include='*.cpp' --include='*.hpp' --include='*.mm' --include='*.m' --include='*.cc' -- "$what" "$@" 2>/dev/null | head -5 | grep .; then
        exit 0
    fi
    echo "NOT FOUND: '$what' is in none of: $*"
    exit 1
fi
[ $# -gt 0 ] || { sed -n '2,12p' "$0" >&2; exit 2; }
for t in "$@"; do
    echo "=== $t"
    grep -rnoE --include='*.[ch]' --include='*.cpp' --include='*.hpp' --include='*.mm' --include='*.m' --include='*.cc' \
        '(getenv|secure_getenv|os_get_option|debug_get_[a-z_]*option|DEBUG_GET_ONCE_[A-Z_]+_OPTION|getEnvVar)\(\s*[a-z_]*,?\s*"[A-Za-z_][A-Za-z0-9_]*"' "$t" 2>/dev/null |
        sed -E 's/^([^:]*:[0-9]+):.*"([A-Za-z0-9_]+)".*/\2\t\1/' | sort -u -k1,1 | awk -F'\t' '{ printf "  %-36s %s\n", $1, $2 }'
    flags="$(grep -rnoE --include='*.[ch]' '\{ *"[a-z0-9_]+", *[A-Z][A-Z0-9_]+_(DEBUG|PERF)_[A-Z0-9_]+' "$t" 2>/dev/null |
             sed -E 's/.*\{ *"([a-z0-9_]+)", *([A-Z0-9_]+).*/\2=\1/' | sort -u)"
    [ -n "$flags" ] && { echo "  flag tokens:"; echo "$flags" | sed 's/^/    /'; }
done
