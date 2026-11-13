---
name: github-setup
description: Set up this Mac, once, to pick up GitHub work for the bot account on utmapp/UTM — a LaunchAgent that polls the account's notifications every 2 minutes, claims a pull request or issue with a 👀 reaction when an org member mentions the bot (or requests its review), runs /utmapp-bot:review-utm-pr (a PR) or /utmapp-bot:answer-utm-issue (an issue) in an unattended Claude Code session, and resumes that same session with follow-up comments (questions, "review again", "build the fix"); several Macs on one account divide the work through that reaction. Needs only `gh` and Claude Code, not the games database. Re-running it updates the installed scripts. Use when asked to "set up GitHub reviews", "install the PR poller", "let the bot pick up mentions", or to check, pause, release or remove it.
argument-hint: "[--paused] [--check] [--uninstall]"
---

# Set up the GitHub poller on this Mac

Arguments: `$ARGUMENTS`

```sh
UB="${CLAUDE_PLUGIN_ROOT}/bin/utmbot"
$UB gh setup $ARGUMENTS          # idempotent; --check, --uninstall, --paused
```

## Before the first run

- **`gh` signed in as the bot account** (`gh auth status`). Its token needs
  the `repo` or `notifications` scope: the notifications API does not take
  fine-grained tokens. The account should be a member of the org
  (`utmapp`): only then can it see members who keep their membership
  private. Setup checks both and warns.
- **Claude Code** installed and logged in (`claude`).
- **What reviews need:** the macOS VMs, Xcode VMs and the fork clone that
  `/utmapp-bot:review-utm-pr` uses (`provision-macos-vm`, `install-xcode-vm`).
  The poller runs without them, but reviews then fail and say why.
- **Awake and unlocked**, as for any unattended VM work: `sudo pmset -a
  disablesleep 1`, screen lock and screen saver off. Setup warns.

Installing the agent makes something that keeps running after this session,
so Claude Code asks permission for the command. If auto mode refuses it,
give the user the command to run (`! <command>`); do not work around it.

## What it does

The agent (`app.getutm.utmapp-bot.github`) runs `ghbot run`. How it works:
`scripts/ghbot/poller.py` (who may call the bot, claims) and
`scripts/ghbot/jobs.py` (sessions). In short:

- **Triggers:**
  - on a pull request: an org member mentions `@<bot>`, or requests the
    bot's review. That starts `review-utm-pr <n> --auto` in a new session.
  - on an issue: an org member mentions `@<bot>`. That starts
    `answer-utm-issue <n> --auto`, which reproduces the issue, finds the
    cause and replies. A small fix it builds and opens as a PR from the
    bot's fork. For a large one it posts the cause and a fix outline.
  - on the account's own PR (a fix it opened) or issue: any org member's
    comment, mention or not. It is never reviewed; the comment is answered
    (and a requested change made on the PR's branch), in the session of the
    issue that opened the PR when this Mac has it, else in a new one.
  Either trigger claims the PR or issue.
- **Follow-ups:** later comments go to that same session. They come from
  org members, and from the PR's or issue's author once a member has
  started the conversation. Only an org member's comment can get a fix
  built and opened as a PR: `utmbot gh pr` refuses any other round. The session replies only where it is addressed. "Review
  again" runs the review again on the new head.
- **Ignored, with no reply:** everyone else, bot accounts, review
  requests on the account's own PRs, and its own comments.
- **Several Macs on one account:** the first Mac to add 👀 to the PR owns
  it (GitHub answers 201). A Mac that gets 200 (the account's 👀 is
  already there) never touches the PR. Deleting the 👀 releases it.
- **Two Macs racing:** if both win the first claim, both work.
  `utmbot gh comment|review` refuse a round the account already answered
  (DUPLICATE), so the slower Mac discards its work.
- **Limits:** one new review at a time and at most 2 sessions. No daily
  cap by default; `GHBOT_REVIEWS_PER_DAY=N` in `~/.utmapp-bot/github.env`
  sets one (a guard against runaway cost).
- **Releasing:** a PR that is closed and quiet for 7 days is released
  automatically.

## Day to day

```sh
$UB gh status [--scan]      # PRs this Mac knows; --scan: open PRs carrying the account's 👀
$UB gh poll --dry-run       # what would be picked up now (claims nothing)
$UB gh pause | resume       # stop/start new sessions (running ones go on)
$UB gh retry PR             # after clearing a blocker (or a failed session)
$UB gh release PR           # free a PR for other Macs (also one whose Mac is gone)
```

The agent runs under the `UTM Bot Agent` launcher app, and setup asks for
its macOS permissions (Local Network, controlling UTM, UTM's data, Screen
Recording) while the user is at the Mac, as db-setup does; the two setups
share the launcher and its permissions.

A blocked session (a macOS permission dialog, an expired token), a session
that failed three times, and the Claude Code login nearing expiry each send
one push notification to the user's phone (a short Remote Control session,
the same code the games-db dispatcher uses), and show in `gh status`. Clear
the cause, then `gh retry PR`.

## Report

Report:
- the account and its scopes;
- the agent's state;
- where pickup starts from: only activity after the first setup is work;
- what a dry-run poll would pick up;
- every WARNING line, above all sleep and lock.
