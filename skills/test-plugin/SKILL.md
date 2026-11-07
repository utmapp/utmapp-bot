---
name: test-plugin
description: End-to-end test this plugin itself — run the whole Auto-Triage loop (dispatcher, unattended sessions, blockers over Remote Control, db skills) against a local copy of the games db seeded with synthetic reports, flags, spam, deletion requests and gift/demo offers, while the VMs, snapshots, Steam account and UTM lease are the real ones; queue dispatched sessions that exercise any other skill (repro, provisioning, kernel debugging, host builds, validation, handoffs); set up and undo the scenarios that need a person (Steam signed out, an expired GitHub token, a force-push); judge each run against its pass criteria and turn every gap it shows into a general fix. Use after changing a skill, the harness or the dispatcher, or when asked to "test the plugin", "run the e2e tests", "validate the skills end to end" or "seed a test report".
argument-hint: "[scenario | skill to exercise | status]"
---

# Test the plugin end to end

Request: `$ARGUMENTS`

Unit-level checks (`py_compile`, `bin/utmbot help`, `bin/gamesdb help`,
`osacompile -o /dev/null scripts/provision/utm.applescript`,
`scripts/check-portable.sh`) catch typos. They cannot show whether a skill
leads a session to the right result. For that, a real session has to run the
skill the way production does: started by the dispatcher, unattended, on the
real VM. This skill sets that up without touching the production site.

Run it from a checkout of this repository: the scripts below live in it
(`R=<checkout>`; the commands assume `cd $R`).

## What is local and what is real

- **Local:** the games db. `scripts/gamesdb/e2e-local.sh` runs the site from
  a [utm-games-db](https://github.com/utmapp/utm-games-db) checkout on
  `127.0.0.1:18080` (internal API on `:18081`), with its data in
  `~/.utmapp-bot/gamesdb-local/`, and a second dispatcher that polls it every
  2 minutes, with its own state in `~/.utmapp-bot/gamesdb-e2e/`. Nothing it
  does reaches the production site. IGDB credentials for the local site (so new
  games get store links, as in production) go in
  `~/.utmapp-bot/gamesdb-local/.env`.
- **Real:** the test and dev VMs, their snapshots, the UTM lease, the Steam
  account (licenses claimed and gifts accepted stay on it), CrossOver, and
  GitHub (fix branches go to the bot's fork). Treat them as production:
  a test that claims a license, pushes a branch or deletes a snapshot did
  so for good.
- The e2e dispatcher and the production one share the UTM lease, so they
  never drive a VM at the same time. Pause production work for a long test
  run with `bin/gamesdb dispatch pause` (and `resume` after), or let them
  queue behind each other.

Never point `e2e-seed.py` or test sessions at the production server. Seeds
write to the local SQLite file directly, and it refuses any other database.

## Set up (once per session at the Mac)

```sh
scripts/gamesdb/e2e-local.sh start <utm-games-db checkout>   # local site + e2e dispatcher; skips what is up
scripts/gamesdb/e2e-scenario.sh status                       # dispatcher, jobs, lease, VM roles, last log lines
scripts/gamesdb/e2e-local.sh log 60                          # the e2e dispatcher's log
```

Sessions drive production's UTM, `/Applications/UTM.app`; set `UTM_APP`
before `start` to test another (a dev build), and `e2e-local.sh restart` to
change it later. A dev build may lack what production has (D3DMetal needs
the x86_64 render server).
It needs a current UTM (`docs/utm-host.md`).

The e2e dispatcher runs as production's does: a launchd job
(`app.getutm.utmapp-bot.e2e-dispatch`) under the agent launcher, so macOS
charges Local Network, UTM's container and Automation to "UTM Bot Agent",
not to your terminal. Run `db-setup` first (it builds the launcher and asks
for its permissions). A dispatcher started from a terminal would pass tests
that fail in production.

`e2e-scenario.sh status` is the one command that shows where every test
stands. Reports and Auto-Triage comments show on http://127.0.0.1:18080.
`bin/gamesdb local from-backup --tar <backup>` loads real reports from a
backup into the local database (no secrets are copied) when synthetic ones
are not enough. Pass `GAMESDB_URL=http://127.0.0.1:18081
GAMESDB_STATE=~/.utmapp-bot/gamesdb-e2e` to any `bin/gamesdb` command to aim
it at the local instance.

**The result check.** A triage that finishes records its comment with
`gamesdb final`; the dispatcher runs `/utmapp-bot:db-triage-check` and posts
it, or resumes the triage with the findings. To test the check on a real
finished triage without redoing it: copy the report into the local db
(`e2e-seed.py report` with its text, its earlier bot comments as
`author_kind='bot'` rows), fork the triage session (`claude -p "<you are a
test copy on local report N>" --resume <sid> --fork-session`), run its
`gamesdb final` in its environment (`GAMESDB_MODE=auto
GAMESDB_DISPATCH_JOB=triage:N CLAUDE_CODE_SESSION_ID=<fork>`), and give a
fresh `GAMESDB_STATE` the triage job (status running, a dead pid, a log
holding the fork's result). The dispatcher then checks it and, on a fail,
resumes the fork with the findings. Note: `e2e-seed.py` defaults to
`gamesdb-local/data/`; point `E2E_DB` at the database the local server
actually serves (`gamesdb-local/test-data/gamesdb.sqlite` when it was
started with `--data …/test-data`).

## Two ways to start a test

1. **Seed content and let the dispatcher find it.** This is the production
   path end to end: work feed, job planning, the session, its comment and
   status on the site. `scripts/gamesdb/e2e-seed.py` writes synthetic
   content (`--help` lists everything):

   ```sh
   scripts/gamesdb/e2e-seed.py games forspoken                # IGDB ids the local db knows
   scripts/gamesdb/e2e-seed.py report --game <igdb id> [--rating R] [--issue T] \
       [--backend dxmt|d3dmetal|unknown] [--utm 5.0.8] --desc "…" [--repro "…"] [--attach FILE …]
   scripts/gamesdb/e2e-seed.py reply <report> "…"             # the reporter answers a clarification
   scripts/gamesdb/e2e-seed.py flag comment <id> --times 3    # moderation work
   scripts/gamesdb/e2e-seed.py spam comment "…"
   scripts/gamesdb/e2e-seed.py deletion <report> --reason "…"
   scripts/gamesdb/e2e-seed.py request gift|demo --game <igdb id>
   scripts/gamesdb/e2e-seed.py status <report> clear          # re-queue a report
   ```

2. **Queue a session with a prompt**, for skills the dispatcher never starts
   by itself (`repro-gpu-issue`, the provisioning skills, `kernel-debug`,
   `build-utm`, `validate-stack`, `investigation-handoff`, …):

   ```sh
   scripts/gamesdb/e2e-scenario.sh queue e2e-<name> "<prompt>" [triage|moderate|…]
   ```

   The kind decides whether the job needs UTM (`triage` does). Write the
   prompt as a maintainer would ask, naming the skill and what to report,
   and state the pass criteria for yourself before it runs, not after.

Long jobs run for hours. Check back with `e2e-scenario.sh status` instead of
watching.

## Scenarios

Seeded or queued by Claude, no person needed:

| Scenario | Set-up | Passes when |
|---|---|---|
| Already fixed | a report of a bug that reproduces on the release stack and is fixed in the dev UTM or dev driver | `wont_fix`, the comment names the build that fixes it |
| Not reproduced | a report of a symptom the released driver no longer has (an old guest tools version) | `needs_clarification`, asking for exactly what is missing (here: update the guest tools) |
| A real bug to the end | a report standing in for a known open bug, worded as a player would (`e2e-scenario.sh fix-proposed setup` seeds one) | reproduced at the reporter's settings, CrossOver compared, the layer named only after attribute-to-layer, validate-stack and perf-regression ran, a branch `triage/report-<id>-…` on the bot's fork per repository and no pull request, `fix_proposed` with the links |
| Clarification answered | `e2e-seed.py reply` on a report in `needs_clarification` | the **same** session resumes, uses the answer, comments |
| No copy of the game | a report for a game the account does not own, no demo | a synthetic repro if the report pins down an API path, else db-summarize notes; either way the reporter gets a comment and the status goes back |
| Attachments with personal data | the above with attachments full of names, paths, IPs, MACs, serials, Steam IDs, e-mail addresses | nothing personal in the comment, nothing says there were attachments. Made-up logs are recognised as made-up and rightly not summarized: test the summary itself with a real UTM debug log of a game, with the account and VM names changed and a configuration that matches the report |
| Moderation | flags on a spam comment and on a legitimate report, spam and ham in the spam log, a redaction request, a vague deletion request | spam removed, griefing flags cleared, Akismet trained, the redaction drafted (or hidden when the server cannot edit), everything else left for a person and listed |
| Demo offer | `e2e-seed.py request demo` for a free demo | license claimed, `steam-app-<appid>` snapshot made, game owned on the site, request closed |
| Gift offer with no gift | `e2e-seed.py request gift` without sending one | no gift accepted, nothing marked owned, the request stays open |
| `in_progress` safety net | a queued session told to end with its report `in_progress` | resumed with the safety-net prompt twice, then marked failed |
| Scheduled return | a queued session told to run `session later --hours 0.05` | the dispatcher brings the same session back on time |
| Another skill | `queue` with a prompt for that skill | what the skill's own Report section promises, with evidence |

Scenarios that need a person (each sends exactly one push notification):

| Scenario | Set-up | The person | Passes when |
|---|---|---|---|
| Steam signed out | `e2e-scenario.sh steam-signout setup` (keeps signed-in copies as `*-e2e-good` and seeds a report) | signs in in the VM window, replies "done" | one push; other Steam work held (`--scope steam`); afterwards the session deletes the stale game snapshot, reinstalls it from the new baseline and triages. Then `steam-signout cleanup` (or `restore` if it went wrong) |
| GitHub token expired | `e2e-scenario.sh github-expired setup` | runs `github-expired fix`, replies "fixed" | the blocker has `--scope github`; the session ends with `GITHUB OK as <bot>`. Then `github-expired restore` |
| Force-push | `e2e-scenario.sh force-push setup` | answers yes or no | the session asks before pushing and does what it was told (`force-push check`) |
| Interactive vs auto | while a VM job runs, `/utmapp-bot:db-triage <id>` in a terminal | watches | the session names the job holding UTM and since when, and waits in 10-minute steps, saying so each time |
| Network loss | turn Wi-Fi off for 3 minutes during a triage | nothing | `bin/gamesdb tunnel status` goes to waiting, then connected; the session carries on, or is resumed about 15 minutes later as an outage; no push |
| A real gift | gift a game to the triage account from another account, then `e2e-seed.py request gift --game <id>` | sends the gift | accepted, license confirmed, owned on the site, request closed |
| macOS permission dialog | `tccutil reset ScreenCapture <terminal's bundle id>`, then queue a session that samples frames (`utmbot frames`) | allows the dialog, replies | one push; the work resumes |

Some things only real traffic can test: notes posted from a reporter's own
debug log, and the first dev driver that fixes something in the released one.
Note them as open rather than faking them.

## Judge the run, then fix in general terms

For each run read the session's summary (`e2e-local.sh log`), the comment
and status on the local site, the run directories it names, and what it left
in `~/.utmapp-bot/games/`, `~/.utmapp-bot/notes/` and
`~/.utmapp-bot/agent-tools/`. A pass needs the evidence the scenario names,
not the session's word for it.

When a run shows a gap, fix the plugin, not the test:

- Fix the harness, the dispatcher or the skill so that any session in that
  situation does the right thing. Never add a step, an exception or a
  wording that only matters for the test (a seeded report id, the local
  site, a scenario's name): skills and docs describe production behaviour
  only, and this skill is the one place that knows about testing.
- Re-run the scenario after the fix, and only then count it as passed.
- Commit the fix with what the run showed in the message. Test results do
  not go into the repository: keep them in
  `~/.utmapp-bot/gamesdb-e2e/results.md` (one row per run: date, scenario,
  result, what was fixed).

## Clean up

```sh
scripts/gamesdb/e2e-seed.py cleanup          # every seeded report, comment, flag, request and user
scripts/gamesdb/e2e-local.sh stop            # dispatcher and local site (running sessions go on)
```

Delete VMs, snapshots, bottles and fix branches a test created only when they
were made for the test (`e2e-*` names); a snapshot such as `steam-app-<appid>`
from a demo test is a normal cache and can stay.

## Report

Each scenario run: what it checked, pass or fail with the evidence, the
gaps found and how each was fixed (commit), and what still needs a person or
real traffic.
