---
name: db-steam-accept
description: Accept Steam gifts sent to the games-db triage account and mark those games owned on the games database — boot the Steam VM from steam-baseline (disposable), list pending gifts through the client's own API, accept the one for the given game (or every pending one), confirm the license, then record it with /internal/triage/owned and close the matching gift offer. Use when someone gifted a game for Auto-Triage, or when asked to "accept the gift", "accept gifts" or "mark <game> as owned".
argument-hint: "[game name | steam app id] [--auto]"
---

# Accept a Steam gift

Arguments: `$ARGUMENTS` (empty: every pending gift)

```sh
export UTMBOT_VM="$("${CLAUDE_PLUGIN_ROOT}/bin/utmbot" machine get test_vm)"   # fails: see docs, "The Steam VM"
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
GDB="${CLAUDE_PLUGIN_ROOT}/bin/gamesdb"
```

`${CLAUDE_PLUGIN_ROOT}/docs/gamesdb.md` covers the tunnel, the Steam VM and
its snapshots. Accepting a gift changes the account, not the disk, so the
boot is disposable and nothing is snapshotted.

**Mode.** Interactive unless the dispatcher started this session
(`GAMESDB_MODE=auto`, AUTO MODE in the system prompt, or `--auto`). Where
this skill says *ask*, auto mode decides itself when that is safe or
reversible, and otherwise leaves the item as it is and lists it in the
summary. `docs/gamesdb.md`, "Interactive and auto mode", has the rules,
including the one exception: a blocker only a person can clear.

It uses UTM across several boots, so it holds the UTM lease from start to
end. Interactive: `$UB lock status` first, and while auto-triage holds UTM,
wait as `docs/gamesdb.md` ("UTM is one at a time") says.

```sh
$UB lock acquire --purpose "db-steam-accept" --wait 21600   # first, before any vm command
$UB lock release                                   # last, on every path out
```

## 1. Which game

If a game was given, resolve it to an app id (`$UB steam resolve <name>`).
When the name is ambiguous, accept only a pending gift whose app matches
one of the candidates; if none does, ask (auto: leave the gift pending).
Look for the matching offer on the site:

```sh
$GDB triage-requests          # open gift/demo offers: game, storefront, notes
```

## 2. Boot and look

```sh
$UB vm snap restore steam-baseline
$UB vm up --tag accept-gift
$UB steam start --wait 300            # "STEAM UP signed in as …"; signed OUT: db-steam-load, "Signed out during other work"
$UB steam gifts                       # pending gifts: index, app ids, text, controls
```

`steam gifts` reads the store's gifts page
(`https://store.steampowered.com/gifts/`) inside the client, where the
account is signed in. It waits for the page to settle: it never answers
from the transient "Verifying login information…" page, and it reloads on
a Steam server error. A gift shows up a few minutes after it was sent. If
it is not there yet, check again once or twice over ~10 minutes, then stop:
it may not have been sent yet, or it went to another account.

The gift's sender is a person: do not repeat their name or account in
anything that leaves this session.

## 3. Accept

```sh
$UB steam accept-gift --appid <appid>   # or --index I from `steam gifts`; --all without a game
$UB steam owned --appid <appid>         # must say owned: true
```

`accept-gift` triggers the gift block's own **Accept** control on that page
(and a follow-up "Add to library" confirmation if one appears), so it does
not depend on the page's internal function names. Steam changes that page
now and then. If `accept-gift` says it found no gift block or no Accept
control while `steam gifts` shows `none: false`, read the page
(`$UB steam targets`, then `$UB steam js 'document.body.innerText' --target
Gifts` while it is open) and accept through the control the page shows,
with `$UB steam js … --target Gifts`. Record what the page looks like now in
`$UB notes`, and say in your summary that `_GIFTS_JS` in
`scripts/utmbot/steam.py` needs it, for a maintainer. `steam owned` is the
proof either way.

A gift for a game the account already owns is better declined, so the
sender is refunded. Do not accept it; ask whether to decline it (the gift's
own Decline control on the gifts page). Auto: leave it pending.

## 4. Record it on the site

For each newly owned app:

```sh
$GDB owned-add steam <appid>
```

Whenever the site's list may be behind the account (games bought or
claimed elsewhere), push the whole library instead. Adding entries is
idempotent:

```sh
$UB steam owned --entries > /tmp/owned.json && $GDB owned-add --json /tmp/owned.json
```

The response's `matched` count says whether the site knows the game
(through its IGDB store links). A game nobody has reported yet is kept on
the list and matched when its first report arrives. Then close the offer
that brought it, if there is one:

```sh
$GDB triage-request <request id> done
```

A 409 ("already resolved") means another triage machine closed it first:
fine, nothing to do. (Each machine has its own Steam account, so the gift
on this one is still worth accepting if it was sent here.)

A gift that has not arrived after ~10 minutes leaves the request open. The
dispatcher looks at open gift requests again once a day. Say so in your
summary.

```sh
$UB vm down
$UB lock release
```

## Report

The game(s) accepted (title, app id), the owned-list response, and the
offer(s) closed. Mention that installing them is
`/utmapp-bot:db-steam-install <appid>`, or happens on the first
`/utmapp-bot:db-triage` for one of their reports.
