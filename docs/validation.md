# Validating a change: what it could break

For every change a skill builds, reviews or recommends that alters runtime
behavior: its own fixes (`answer-utm-issue`, `land-stack-change`), the
pull requests it reviews (`review-utm-pr`), the fixes an Auto-Triage
proposes (`db-triage`, checked by `db-triage-check`), and every change to
the GPU stack (`triage-gpu-issue`, `repro-gpu-issue`, `attribute-to-layer`:
the guest driver, virglrenderer, the backend shims, QEMU, UTM's display;
section 6). Text-only changes (docs, translations, comments) and pure
layout changes are covered by the screenshot rules instead.

Section 5 applies to all of that work, whatever the change: what to do
with a problem found on the way that the change did not introduce.

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
- Started by the games-db dispatcher: `gamesdb session blocked --reason
  "<the setup needed>"`.

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

## 5. Existing issues found along the way

While reviewing a pull request, fixing an issue, triaging a report or
working on the GPU stack, you will find problems the change at hand did
not introduce: a bug next to the changed code, a race in a path the test
plan cycles, a teardown that leaves state behind. Do not drop one because
it is "pre-existing" or "out of scope". Do not fold it into the current
change either: the current task stays as it was planned.

First look for earlier work on it: search the repository's issues and pull
requests, open and closed (`gh search issues`, `gh search prs`, `gh pr list
--state all --search`, with the function, file or symptom as terms), and
the upstream's when the code comes from one.
- **An open issue or pull request** already covers it: do not start a
  second one. Link it in the current pull request or comment, and add what
  you found to it (the test, the evidence) when that is new.
- **A closed pull request** for it that was not merged: the maintainers
  have waived it as something to be concerned about. Do not reopen or
  redo it, and do not follow it up; at most mention it with the link where
  it matters to the current change.
- **A merged fix** that does not cover what you see: the follow-up below
  links it and says what it missed.

Then decide whether it **reproduces with a test**: a probe, a unit or UI
test, or a scripted sequence (with a widened gap or a debugger breakpoint
where the race needs one, section 3) that fails on this Mac today and that
a fix would turn into a pass.

- **Reproducible:** write the test and keep its failing output with the
  evidence. Finish the current task first: its review, its pull request,
  its comment. Then fix the existing issue as a **new pull request** of
  its own, through the same process as any change: this document's test
  plan, `land-stack-change` (section 7 says where it is published), and
  for UTM itself `utm-review` and `utm-submit`. Its body links the work
  where it was found; the original pull request or comment links it back
  once it exists. When the current session cannot get to it (a poller or
  dispatcher session at its end), write it down in the handoff or the
  notes (`$UB notes`) with the test, so the next session starts there.
- **Not reproducible with a test:** surface it, with no follow-up now. Say
  in the original pull request body, review comment or Auto-Triage comment
  what it is, where (`file:line` or the component), what was seen, and why
  it could not be turned into a test.

Either way it is not a blocker for the current change unless the change
makes it worse, which makes it part of the change (a regression, section
1). A follow-up pull request for an existing issue in a repository the
org does not own is a fix branch, as `land-stack-change` section 7 says.

## 6. The GPU stack

For a change to the guest driver (KMD, UMD), virglrenderer, the backend
shims, QEMU or UTM's display path, the test plan of section 1 means:

- **Where it runs:** both backends (D3DMetal, DXMT), Windows and, when the
  path is shared, Linux guests; ARM64 and x64 (ARM64EC) applications, and
  x86 (WOW64) for the D3D driver; Vulkan (Venus) as well as D3D when the
  KMD or a shared path changes. `/utmapp-bot:validate-stack` is the matrix;
  `/utmapp-bot:perf-regression` the performance side.
- **What it produces:** pixel-exact probes (`/utmapp-bot:write-gpu-probe`)
  as well as benchmark scores; a score says nothing about wrong pixels.
- **Lifecycle (section 3):** for the KMD, the cycles are the device's own:
  `pnputil /disable-device` and `/enable-device`, a live upgrade from the
  released driver (and from the previous release) to the change, and back;
  for the host side, VM start and stop, and a guest reboot. A change in
  teardown or start is not tested until it has gone through these.
- **Widen the gaps with the debugger:** a kernel-debugger breakpoint at the
  ordering point (`/utmapp-bot:kernel-debug`, e.g. on the dxgkrnl DDI that
  starts or stops the adapter) holds the race open for as long as needed
  and lets the state be read there (registers, PCI config space). A guest
  with one vCPU removes scheduling luck from races that need a thread and
  an interrupt on the same CPU. Restore the VM's configuration afterwards.
- **Released drivers:** a live upgrade involves the driver being replaced,
  which this change cannot fix. Test from the drivers users actually run
  (`utmbot install --released`, the previous release), not only from a
  development build.

