---
name: investigation-handoff
description: Write or resume a handoff for a GPU-stack investigation that spans sessions — a fixed template of confirmed findings with evidence, exact repro steps, environment and versions, saved diagnostic patches, eliminated hypotheses as a do-not-redo list, and the single next step; on resume, re-verify every claim against the current trees before acting. Use when asked to "write a handoff", "save where we are", "continue from the handoff", or when a long investigation is about to stop.
argument-hint: "write <topic> | resume <handoff file>"
---

# Investigation handoff

Request: `$ARGUMENTS`

A handoff is what the next session (or person) starts from. It is only as
good as its evidence: a wrong confident claim in a handoff costs more than
no handoff.

## Write

Location: `~/.utmapp-bot/handoffs/<yyyy-mm-dd>-<topic>.md`, or the repo the
user names. Write a **new** file for a new phase, rather than appending to a
stale one. Use `template.md` in this skill's directory.

Rules:

- **Only confirmed findings,** each with its evidence: a log line, a probe
  verdict, a measurement with run directory, a commit. Mark anything
  unproven as a hypothesis, with what would prove or kill it.
- **No story of failed attempts.** Wrong paths go into "Eliminated" as one
  line each, with the evidence that killed them, so nobody redoes them. If a
  finding was retracted, say so there, plainly.
- **Exact repro:** the commands (`utmbot …`), VM, UTM build, backend,
  D3DMetal version, driver package and the expected vs. actual output.
- **State of every tree:** repo, branch, commit, and uncommitted edits.
  Name the edits, and say whether they are diagnostics (with the patch path)
  or part of the fix. Uncommitted edits that outlive a session are a classic
  source of "impossible" symptoms later.
- **One next step,** concrete enough to start without thinking: what to
  run, and what result decides what.
- Drop outdated content. Sections and numbers that no longer hold are worse
  than none.

## Resume

1. Read the handoff completely.
2. **Re-verify before acting.** `git status`/`log` every tree it names, and
   compare with what it says. Check that the UTM build and driver package
   still exist and are the ones described (`verify-app.sh --marker`). Rerun
   the repro and confirm you see the documented symptom.
3. Anything that no longer matches the handoff is a finding. Note it before
   continuing.
4. Treat build-time or duration estimates in old notes as unreliable;
   measure them again.
5. Continue from "Next step". Do not reopen anything under "Eliminated"
   without new evidence.
