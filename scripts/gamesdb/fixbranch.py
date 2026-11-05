"""Publish a validated fix as a branch on the bot's own GitHub fork.

This opens no pull request. A fix goes to a new branch on the fork that the
bot's GitHub account has of the upstream repository, and the triage result
links that branch. A person reviews it, consolidates it with other changes
and submits the pull request. (A fix in UTM itself may instead be submitted
by UTM's own utm-review and utm-submit skills; never a PR to other repos.)

  gamesdb fix-branch REPO_DIR --branch NAME [--ref HEAD] [--dry-run]
  gamesdb fix-branch --whoami

- The bot account is whoever `gh` is logged in as. No login is an error;
  in a dispatched session that is a blocker (`--scope github`).
- The upstream is REPO_DIR's `origin`. The fork is made under the bot
  account if it does not exist yet (`gh repo fork --clone=false`).
- The push uses HTTPS with gh's token, not SSH: this Mac's SSH key may
  belong to another account.
- New branches only. A branch that already exists on the fork is refused,
  so nothing is ever overwritten or force-pushed. Use a new name (…-v2).

Prints `FIX-BRANCH <url>` and `COMPARE <url>` (upstream default branch vs the
fix, for the reviewer).
"""

import json
import re
import subprocess
import time

from gamesdb.tunnel import Fail, die, note


def gh(*args, check=True):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and p.returncode:
        die(f"gh {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def whoami():
    p = gh("api", "user", "--jq", ".login", check=False)
    login = p.stdout.strip()
    if p.returncode or not login:
        die("GitHub: `gh` is not logged in, or its token no longer works "
            f"({(p.stderr or p.stdout).strip()[-200:]}). Log in with `gh auth login` as the bot "
            "account. In a dispatched session this is a blocker: `gamesdb session blocked --scope github`.")
    return login


def git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and p.returncode:
        die(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout.strip()


def upstream_of(repo):
    url = git(repo, "remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        die(f"{repo}: origin ({url}) is not a GitHub repository")
    return m[1], m[2]


def run(repo, branch, ref="HEAD", dry_run=False):
    if not re.fullmatch(r"[A-Za-z0-9._/-]{3,100}", branch) or branch.startswith(("-", "/")) or ".." in branch:
        die(f"not a usable branch name: {branch!r} (e.g. triage/report-123-short-name)")
    login = whoami()
    owner, name = upstream_of(repo)
    sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if owner.lower() == login.lower():
        die(f"{owner}/{name} is the bot's own repository; a fix goes to a fork of the upstream")
    # the fork: ours already, or made now
    p = gh("api", f"repos/{login}/{name}", check=False)
    fork = json.loads(p.stdout) if p.returncode == 0 else None
    if fork and (fork.get("parent") or {}).get("full_name", "").lower() != f"{owner}/{name}".lower():
        die(f"{login}/{name} exists but is not a fork of {owner}/{name}")
    if not fork:
        if dry_run:
            note(f"would fork {owner}/{name} to {login}/{name}")
        else:
            # --clone=false and no --remote: with a repository argument gh
            # rejects --remote (it only applies to forking the current checkout),
            # and stdin is not a terminal, so nothing prompts
            gh("repo", "fork", f"{owner}/{name}", "--clone=false")
            for _ in range(60):  # forking is asynchronous
                if gh("api", f"repos/{login}/{name}", check=False).returncode == 0:
                    break
                time.sleep(5)
            else:
                die(f"GitHub did not finish forking {owner}/{name} to {login}/{name} within 5 minutes")
            note(f"forked {owner}/{name} to {login}/{name}")
    url = f"https://github.com/{login}/{name}.git"
    cred = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    if fork:
        p = subprocess.run(["git", *cred, "ls-remote", "--heads", url, branch], capture_output=True, text=True)
        if p.returncode:
            die(f"cannot read {url}: {p.stderr.strip()[-300:]}")
        if p.stdout.strip():
            die(f"{login}/{name} already has a branch {branch!r}; publish under a new name (never overwrite)")
    if dry_run:
        print(f"WOULD PUSH {sha[:12]} to {login}/{name}:{branch}")
    else:
        p = subprocess.run(["git", *cred, "-C", repo, "push", url, f"{sha}:refs/heads/{branch}"],
                           capture_output=True, text=True)
        if p.returncode:
            die(f"push to {login}/{name}:{branch} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    base = json.loads(gh("api", f"repos/{owner}/{name}").stdout).get("default_branch", "main")
    print(f"FIX-BRANCH https://github.com/{login}/{name}/tree/{branch}")
    print(f"COMPARE https://github.com/{owner}/{name}/compare/{base}...{login}:{name}:{branch}")
    return 0


def main(a):
    if a.whoami:
        print(whoami())
        return 0
    if not a.repo or not a.branch:
        raise Fail("fix-branch REPO_DIR --branch NAME [--ref REF] [--dry-run]")
    return run(a.repo, a.branch, a.ref, a.dry_run)
