# Validating a change: what it could break

For every change a skill builds, reviews or recommends that alters runtime
behavior: its own fixes (`answer-utm-issue`, `land-stack-change`) and the
pull requests it reviews (`review-utm-pr`). Text-only changes (docs,
translations, comments) and pure layout changes are covered by the
screenshot rules instead.

The point is to test what a change could break, not only what it claims to
improve. A change that is measured only on its benefit can ship a
regression in everything around it. Keep the work in proportion to the
change: a few lines of plan for a small fix, one run per entry, and reuse
measurements already made.

## 1. The test plan

Before testing, write the plan down (in the evidence notes, and later in
the PR body or review comment):

- **The benefit:** the configuration the change is for, and the result it
  should have there.
- **What the changed code path takes part in, beyond that.** Derive it
  from the diff, not from the description:
  - when the code runs: startup, teardown, resize, moves between
    screens or devices, background, hidden or paused states, error paths;
  - what it produces: the correctness of the output (pixels, sizes, data,
    state), not only its speed;
  - what it branches on: hardware (displays, GPUs, input and USB devices),
    OS versions (the older supported ones included), settings.

Test each entry, or mark it untested and say why. The PR body or the
review comment lists the plan's coverage: each entry with its result, or
"untested" with the reason and the risk.

## 2. Untested entries: when they block

An entry that cannot be tested on this Mac blocks only when it is:

- **critical to the feature:** the configuration the change exists for.
  For example, a change for multiple displays when no second display is
  attached, or a fix for a device that is not here; or
- **very likely to affect another part:** the change sits on a path other
  features go through, and the untested entry is where they would break.

A blocking entry means: do not approve, do not recommend keeping the
change, and do not submit a pull request for it. Ask a person for the
setup instead, and say at the top of the comment what is missing.
- Interactive: tell the user, with a push notification.
- Started by the GitHub poller: `utmbot gh session blocked --reason "<the
  setup needed>"`.

Every other untested entry is listed with its risk and does not block.

## 3. Lifecycle and ordering

When the change touches initialization, teardown, timers, callbacks,
notifications, threads, or the ownership of a shared resource (who creates
it, who uses it, in what order):

- **Cycle only what the change touches.** A cycle is the shortest
  sequence that runs the changed code once: if the change is in window
  setup, open and close the VM window; if it is in VM start, start the VM
  and stop it once its display is up. Don't wait for a guest to boot, run
  a benchmark or replay a whole test suite when the changed code has
  already run by then.
- **Repeat in proportion to how long a cycle takes.** Check every cycle
  for a crash (a new crash report, the process gone) and for the result
  still working.
  - Quick cycles (seconds): 20 or more.
  - Cycles of a minute or two: 5 to 10.
  - Cycles that cannot be shortened (a full VM benchmark, about 20
    minutes): 1 to 3. Rely more on the next point.
- **Widen the gaps.** For each ordering gap the change creates or relies on
  (the time between two steps in which another callback, timer or thread
  can run), add a temporary delay there (100 ms is a good start; never
  committed). Run the cycles again, on the old code and on the new. A
  widened gap turns a rare race into one that fires every time, so a few
  cycles with the delay are worth more than many without it.

A race that does not fire in a handful of runs is untested, not absent.

## 4. Risks the analysis names

When an analysis names a failure path in the change ("if Y happens, this
falls back to X"), whether the analysis is yours, a code review's or a
reporter's, the PR body or the review comment says what happens there:
- why it cannot occur, from the code, or
- what the user sees when it does.

A test of that path is recommended. When there is none, say so next to
it.
