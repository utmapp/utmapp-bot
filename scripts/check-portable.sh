#!/bin/bash
# check-portable.sh - keep machine-specific details out of the plugin.
#
# The plugin runs on other machines with none of the author's context: no
# absolute home paths, user names, private IPs, signing identities or team IDs
# may appear in it.  Run before every commit; exits 1 on findings.
#   allowed: the kdnet network 192.168.240.0/24, QEMU user networking's
#            10.0.2.0/24, documentation addresses,
#            127.0.0.1, ~/ paths, /Applications, /Library, /tmp, /var/db.
set -uo pipefail
cd "$(dirname "$0")/.."
files="$(git ls-files 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)"
[ -n "$files" ] || files="$(find . -type f -not -path './.git/*')"
rc=0
check() {
    local what="$1" re="$2" hits
    hits="$(printf '%s\n' "$files" | grep -v '^scripts/check-portable.sh$' | tr '\n' '\0' |
            xargs -0 grep -nIE -- "$re" 2>/dev/null | grep -vE "${3:-^$}")"
    if [ -n "$hits" ]; then
        echo "== $what"
        echo "$hits" | head -20
        rc=1
    fi
}
check "absolute home paths" '/Users/[A-Za-z0-9._-]+|/home/[a-z][a-z0-9_-]+/'
check "scratch/temp paths of one machine" '/private/tmp/claude|/var/folders/[a-z0-9_]+/'
o='(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])'
check "private IPv4 addresses" "(^|[^0-9.])(10\\.$o\\.$o\\.$o|172\\.(1[6-9]|2[0-9]|3[01])\\.$o\\.$o|192\\.168\\.$o\\.$o)([^0-9.]|\$)" \
      '10\.0\.2\.|192\.168\.240\.|192\.168\.x|192\.168\.0\.0'
check "Apple team IDs / signing identities" '\b[A-Z0-9]{10}\.com\.|Developer ID Application: [A-Z]'
check "email addresses" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|llc|org|net|io)\b' 'noreply@|@example\.'
check "workspace-only names" 'nept\.py|/tmp/nept|Neptune-QEMU|run_user_windows'
# the games database server is configured locally and named nowhere in the
# plugin: its host, read from the local config, must not appear
gdb_env="${UTMAPP_BOT_DATA:-$HOME/.utmapp-bot}/gamesdb.env"
gdb_host="$( { [ -f "$gdb_env" ] && sed -n 's/^[[:space:]]*\(export[[:space:]]*\)\{0,1\}GAMESDB_SSH=//p' "$gdb_env"; printf '%s\n' "${GAMESDB_SSH:-}"; } |
             tr -d "\"'" | sed 's/.*@//; s/:.*//' | grep -v '^$' | head -1)"
if [ -n "$gdb_host" ]; then
    check "the games database server" "$(printf '%s' "$gdb_host" | sed 's/[.[\*^$]/\\&/g')"
fi
# nor is its site: the games database can run on any domain (GAMESDB_SITE,
# set by db-setup --site), so neither the configured one nor the original
# one is written into the plugin
gdb_site="$( { [ -f "$gdb_env" ] && sed -n 's/^[[:space:]]*\(export[[:space:]]*\)\{0,1\}GAMESDB_SITE=//p' "$gdb_env"; printf '%s\n' "${GAMESDB_SITE:-}"; } |
             tr -d "\"'" | sed 's|^[a-z]*://||; s|[/:].*||' | grep -v '^$' | head -1)"
check "the games database site" "db\.getutm\.app${gdb_site:+|$(printf '%s' "$gdb_site" | sed 's/[.[\*^$]/\\&/g')}"
[ $rc -eq 0 ] && echo "check-portable: clean"
exit $rc
