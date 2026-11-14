---
name: land-stack-change
description: Turn an investigation's working tree into clean, reviewable commits across the repos of the VirtIO GPU stack (guest driver, KMD, virglrenderer, backend shims, QEMU, UTM) — separate diagnostics from the fix, check it against the API documentation (MSDN for DirectX APIs and DDIs, the Vulkan spec), fix the related paths that share the bug, run /code-review at high effort before anything is published and validate again after it, split it into logical commits that each carry their own evidence, follow each repo's own attribution and message rules, and publish each repo's fix where it belongs (a pull request to UTM through UTM's own skills, a pull request to any other repository the org owns, a fix branch for anyone else's; a draft UTM pull request when UTM pins the dependency by commit), leaving nothing pushed that was not asked for. Every skill that changes code in the UTM graphics stack lands it through here (db-triage, triage-gpu-issue, attribute-to-layer, repro-gpu-issue). Use when asked to "commit this", "clean up the branch", "prepare a PR", "squash", or "land the fix".
argument-hint: "[repo paths]"
---

# Land a change

Repos: `$ARGUMENTS` (default: every tree touched in this session)

## 1. Inventory

For every tree involved, `git status`, `git diff --stat`, and `git log
@{u}..` when there is an upstream. Investigations leave edits in several
trees; find all of them, including ones from earlier sessions. List the
files per tree and classify each hunk:

- **fix**: the change that solves the problem;
- **diagnostic**: logging, traces, counters, env knobs, asserts added to
  investigate, timing probes;
- **unrelated**: formatting, drive-by edits, editor noise.

## 2. Take the diagnostics out

Diagnostics never go into commits. That includes knobs that turn behavior
on and off: features are always on, and a knob is a diagnostic.

Save them as patches so the next investigation can reapply them:

```sh
mkdir -p ~/.utmapp-bot/patches/<repo>
git diff -- <files or hunks> > ~/.utmapp-bot/patches/<repo>/<date>-<topic>.patch
```

Revert them in the tree (`git checkout -p` hunk by hunk). Then rebuild and
rerun the proof, to be sure the fix does not depend on a diagnostic.

## 3. Split into logical commits

- One commit per logical change. A commit must build and pass on its own.
- **Each commit proves its own effect.** Give it the probe verdict
  (fail before, pass after), the benchmark delta, or the conformance-test
  diff that shows *that commit's* improvement, measured on the commit in
  sequence. A change in a bundle that shows no measurable effect is dropped,
  not kept "just in case".
- Do not touch code you did not need to change. Match the surrounding style
  and comment density. Comments say why the code is the way it is now, not
  the history of the investigation.
- **No version or ABI bumps for contracts that have not shipped.** Edit the
  unreleased version in place. A protocol or ABI change ships in lockstep
  across its consumers (guest driver ↔ host protocol; virglrenderer ↔
  backend shims). Rebuild them all, with no compatibility path for unshipped
  versions.

## 4. Review before publishing

Every change to the UTM graphics stack goes through this before it is
pushed or published, as a fix branch or as a pull request to any
repository (section 7), whichever skill made it.

1. **Check it against the documentation.** For anything that touches the
   DirectX stack (a D3D/DXGI API or a WDDM/D3D DDI, user-mode or
   kernel-mode), look up each function, structure and flag the change uses
   or implements on Microsoft Learn (MSDN) online: the remarks, the
   return values, and the notes on edge cases (zero sizes, NULL or optional
   arguments, flags that change meaning together, which callbacks the
   runtime allows from where). Those notes are often nowhere in the headers
   and are the most useful part. For Vulkan, check the Vulkan specification:
   the command's valid usage rules and the extension's text. Fix what the
   change gets wrong against them.
2. **Variant analysis: fix the related paths too.** A bug in one function
   is often in its siblings: the other API versions (D3D11 and D3D12),
   the create and destroy halves, the shared and non-shared paths, the
   other formats or resource kinds handled by the same code, and code that
   was copied from it. Search for them and fix the ones that have the same
   bug. A synthetic test of those paths (`/utmapp-bot:write-gpu-probe`) is
   recommended, not required.
3. **`/code-review high`** on the change (the working tree, or the branch
   against its base). High is the default effort; never max unless the
   user asks. Weigh what it finds in this order: **performance, robustness,
   correctness.** Fast code that handles 99% of cases and never crashes
   beats covering the last 1% with a lot of extra code that can regress
   other paths. Two things are never traded away: the code must not crash
   (the guest, the host renderer, QEMU or UTM), and it must not open a
   security hole (the guest is untrusted input to the host). Apply what is
   worth it, and say in the report what you left and why.
4. **Validate again.** The review and the variant fixes changed the code:
   rebuild, rerun the proof (probe fail before, pass after), the reporter's
   path when there is one, and the validation the change already had
   (`/utmapp-bot:validate-stack`, `/utmapp-bot:perf-regression`).
5. **Review again only if it changed a lot.** Significant changes from
   steps 1 to 3 (new logic, other functions, another repository) go
   through steps 3 and 4 once more; small ones (a check, a rename, a
   comment) do not.
6. **Adjacent fixes are their own commits.** A related path fixed in step
   2 is usually a separate logical change: commit it separately (step 3),
   with its own evidence.

## 5. Messages and attribution

- Read the repo's own rules before writing: `CONTRIBUTING.md`, `AGENTS.md`,
  `CLAUDE.md`, recent `git log`. Title format: e.g. `component: summary`.
  Body: the problem, the cause and the fix, plus the evidence from step 3.
- **Attribution trailer:** use the one the repo asks for. Some repos
  require `Assisted-by: AGENT:MODEL` and forbid `Co-Authored-By`; others use
  `Co-Authored-By`. Never add session links.
- **Amending or fixing up an existing commit:** keep its trailers as they
  are; do not add a new attribution to someone's existing commit.
- Commit messages and PR text describe the technical change. Leave out
  business context the repo does not need.
- Write the message to a file and use `git commit -F <file>`; shell quoting
  mangles multi-line messages.

## 6. History

- Linear history: rebase, never merge, onto the target branch.
  `git rebase -i` is not available to the agent, so build the sequence with
  `git commit --fixup` plus `GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash`,
  or by cherry-picking onto a fresh branch.
- **Do not push unless asked.** Then use `--force-with-lease`, never
  `--force`.
- Report every commit hash and which ones are unpushed.

## 7. Where it is published

One rule for every skill that publishes a fix (db-triage,
answer-utm-issue, triage-gpu-issue). The owner of the repository the fix
belongs in decides, and you look it up instead of assuming: the `*_REPO`
and `*_SRC` lines of UTM's `patches/sources` say where UTM takes each
dependency from, and `gh api repos/<owner>/<name>` says whether it exists.

| The fix is in | Publish it as |
|---|---|
| utmapp/UTM | a pull request, only through UTM's own skills: `.agents/skills/utm-review`, then `.agents/skills/utm-submit` (in auto mode, accept every change the review recommends, and validate again). Never by hand |
| any other repository the org owns (`utmapp/<name>`: virglrenderer, dxmt, d3dmetal-native, MoltenVK, qemu, …) | a pull request against it, from a branch on the bot's own fork of it: `gamesdb fix-branch … --pr` |
| a repository someone else holds (osy's: the guest driver, the KMD, Mesa, Vulkan-Loader; a third-party upstream) | a fix branch on the bot's fork, no pull request: `gamesdb fix-branch` without `--pr`. A person reviews it, consolidates it and submits it |

A repository the org owns is not "someone else's project": opening the
pull request there is the expected result, not something to leave for a
person. Never push to the org's repository itself, only to the bot's fork.

**A pull request to the org's repository:**

- The bar is the same as for UTM: section 4 (documentation, related paths,
  `/code-review high`, validation again), `/utmapp-bot:validate-stack` and
  `/utmapp-bot:perf-regression` where the change can affect them, all
  before it is opened. Nothing is opened to "get feedback".
- That repository's own rules for commits and attribution (section 5), not
  UTM's.
- The base is the branch UTM's pinned commit is on (`git branch -r
  --contains <pin>` in a clone of it). That is usually the default branch,
  which `fix-branch` uses unless `--base` names another.
- The body: the problem, the cause, the fix, and the evidence (probe fail
  before and pass after, the matrix, the perf A/B), and the UTM issue or
  games-db report it comes from. The bot is only notified of comments on
  utmapp/UTM, so the body asks reviewers to write on the UTM issue or
  pull request it links when they want an answer or a change from the bot. No session links, and attribution only as
  that repository asks.

```sh
GDB="${CLAUDE_PLUGIN_ROOT}/bin/gamesdb"    # works without the games-db tunnel
$GDB fix-branch <checkout> --branch <name> --pr --title "<component: summary>" --body-file <file> [--base <branch>]
```

It prints `FIX-BRANCH`, `COMPARE`, `HEAD <sha>` and `PR <url>`. It
refuses a repository the org does not own, and UTM itself. When the
checkout's `origin` is not the org's repository (a mirror, the bot's
fork), add `--upstream utmapp/<name>`. A reviewer's change to an open pull
request goes to the same branch: amend the commit it belongs to, run
section 4 again, then `$GDB fix-branch <checkout> --branch <name>
--update`.

**When UTM pins that repository by commit** (a `<NAME>_COMMIT` line in
`patches/sources`), the fix reaches users only when UTM's pin moves, so it
takes two pull requests:

1. The org repository's pull request first (above). Its `HEAD <sha>` is
   the fork commit.
2. Prove UTM can fetch that commit the way its build does, before
   anything else is opened:
   `git clone --filter=tree:0 --no-checkout <the *_REPO url> <tmp> && git -C <tmp> checkout <sha>`.
   The validation must have run on a UTM built with exactly that commit
   (`/utmapp-bot:build-utm`).
3. A UTM pull request that moves the pin to that commit, through
   utm-review and utm-submit as always, **opened as a draft**: add
   `--draft` to utm-submit's `gh pr create`. Its body links the org
   repository's pull request, and says the pin points at that pull
   request's head and that this one must not be merged before it.
4. Later a maintainer writes on the UTM pull request that the other one
   is merged. Then:
   - confirm it with `gh pr view <url> --json state,mergedAt,mergeCommit,baseRefName`,
     not from the comment;
   - find the fix on the org's branch (the merge commit, or the commits
     as rebased or squashed there) and pin that commit;
   - if its tree differs from what you tested (`git diff <fork commit>
     <merged commit>` is not empty), build and validate again;
   - rewrite the pin commit in place (utm-submit's update path: no "update
     pin" commit on top, `--force-with-lease`), update the body, mark the
     pull request ready (`gh pr ready <n> -R utmapp/UTM`), and reply with
     what changed.
   Until a maintainer says so, the UTM pull request stays a draft.

**When UTM takes it as a release tarball plus a patch** (a `*_SRC` line,
and a file in `patches/`): the change goes into that patch file in a UTM
pull request, as UTM's CONTRIBUTING.md says for dependency projects. That
pull request works on its own, so it is not a draft. When the org owns the
repository too, open the same change there as well, so the next release
tarball carries it, and link the two.

**A repository someone else holds** gets no pull request and no draft UTM
pull request: publish the branch, and say in the reply or the comment
which pin a maintainer has to move once it is submitted.

## 8. Report

Per repo: branch, the new commits (hash and title), the evidence each one
carries, the documentation checked, the related paths found (fixed or
not, and why), what `/code-review` found and what was applied, the
validation after it, patches saved for diagnostics, and anything left
uncommitted on purpose, and each pull request or fix branch published
(section 7), with the reason for any repository that got only a branch.
