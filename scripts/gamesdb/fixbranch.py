"""Publish a validated fix on GitHub: a branch on the bot's own fork, and,
for a repository the org owns, a pull request from that branch.

Where a fix goes (skills/land-stack-change, "Where it is published"):
- utmapp/UTM: a pull request, opened only by UTM's own utm-review and
  utm-submit. Never by this command (`--pr` refuses it).
- any other repository the org owns (utmapp/virglrenderer, utmapp/dxmt,
  utmapp/d3dmetal-native, utmapp/MoltenVK, utmapp/qemu, the guest driver's
  utmapp/mesa and utmapp/viogpu-wddm, ...): `--pr` opens
  a pull request against it from the branch on the bot's fork.
- a repository held by anyone else (a third-party upstream): a branch
  only. A person reviews it, consolidates
  it and submits it. `--pr` refuses these.

  gamesdb fix-branch REPO_DIR --branch NAME [--ref HEAD] [--upstream OWNER/NAME] [--dry-run]
  gamesdb fix-branch REPO_DIR --branch NAME --pr --title T --body-file F [--base BRANCH]
  gamesdb fix-branch REPO_DIR --branch NAME --update
  gamesdb fix-branch --whoami

- The bot account is whoever `gh` is logged in as. No login is an error;
  in a dispatched session that is a blocker (`--scope github`).
- The upstream is REPO_DIR's `origin`, or --upstream when the checkout's
  origin is something else (a mirror, the bot's fork). An old name GitHub
  redirects (a transferred or renamed repository) resolves to the current
  one. The fork is the bot's existing fork in the upstream's network, under
  whatever name it has, or is made
  under the bot account if it does not exist yet (`gh repo fork
  --clone=false`). Nothing is ever pushed to the upstream itself.
- The push uses HTTPS with gh's token, not SSH: this Mac's SSH key may
  belong to another account.
- New branches only. A branch that already exists on the fork is refused,
  so nothing is overwritten. The one exception is --update: the branch of
  this account's own open pull request, after a reviewer asked for a
  change, pushed with --force-with-lease against the head GitHub has.
- --base: the branch the pull request targets. Default: the upstream's
  default branch. Use the branch UTM's pinned commit is on when that is
  another one.

Prints `FIX-BRANCH <url>`, `COMPARE <url>` (the base vs the fix), `HEAD
<sha>`, and with --pr `PR <url>`.
"""

import json
import os
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


def org():
    return os.environ.get("GHBOT_ORG") or "utmapp"


def canonical(owner, name):
    """The repository's current OWNER/NAME: GitHub redirects a transferred or
    renamed one (osy/virtio-win-mesa is now utmapp/mesa), and a checkout's
    origin may still use the old name."""
    p = gh("api", f"repos/{owner}/{name}", "--jq", ".full_name", check=False)
    full = p.stdout.strip()
    if p.returncode == 0 and re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", full):
        return tuple(full.split("/"))
    return owner, name


def find_fork(owner, name, login):
    """The bot's fork of OWNER/NAME, as its repository JSON, or None. GitHub
    gives an account one fork per network and keeps its name when the parent
    is renamed or transferred, so it is not always LOGIN/NAME
    (UTMapp-bot/virtio-win-mesa is the fork of utmapp/mesa)."""
    want = f"{owner}/{name}".lower()
    p = gh("api", f"repos/{login}/{name}", check=False)
    if p.returncode == 0:
        repo = json.loads(p.stdout)
        if (repo.get("parent") or {}).get("full_name", "").lower() == want:
            return repo
    p = gh("api", "--paginate", f"repos/{owner}/{name}/forks?per_page=100",
           "--jq", f'.[] | select(.owner.login | ascii_downcase == "{login.lower()}") | .full_name', check=False)
    found = p.stdout.split()
    if p.returncode == 0 and found:
        return json.loads(gh("api", f"repos/{found[0]}").stdout)
    return None


def open_prs(owner, name, login, branch):
    p = gh("api", "--method", "GET", f"repos/{owner}/{name}/pulls", "-f", "state=open", "-f", f"head={login}:{branch}")
    return json.loads(p.stdout or "[]")


def run(repo, branch, ref="HEAD", dry_run=False, pr=False, title=None, body_file=None, base=None, update=False,
        upstream=None):
    if not re.fullmatch(r"[A-Za-z0-9._/-]{3,100}", branch) or branch.startswith(("-", "/")) or ".." in branch:
        die(f"not a usable branch name: {branch!r} (e.g. triage/report-123-short-name)")
    login = whoami()
    if upstream:
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", upstream):
            die(f"--upstream is OWNER/NAME, not {upstream!r}")
        owner, name = upstream.split("/")
    else:
        owner, name = upstream_of(repo)
    owner, name = canonical(owner, name)
    if pr:
        if owner.lower() != org().lower():
            # the checkout may be of an upstream the org has its own repository of
            if gh("api", f"repos/{org()}/{name}", check=False).returncode == 0:
                die(f"this checkout's origin is {owner}/{name}, but the org has {org()}/{name}: if that is where "
                    f"UTM takes it from (patches/sources), open the pull request there with --upstream "
                    f"{org()}/{name}")
            die(f"{owner}/{name} is not the {org()} org's repository, and the org has none of that name: a fix "
                "there is published as a branch only (no --pr); a person reviews and submits it")
        if name.lower() == "utm":
            die(f"a pull request to {owner}/{name} is opened only by UTM's own utm-review and utm-submit")
        if not title or not body_file:
            die("--pr needs --title and --body-file")
        body = open(body_file).read()
        if not body.strip():
            die(f"{body_file} is empty")
    sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if owner.lower() == login.lower():
        die(f"{owner}/{name} is the bot's own repository; a fix goes to a fork of the upstream")
    # the fork: ours already, or made now
    fork = find_fork(owner, name, login)
    if not fork and gh("api", f"repos/{login}/{name}", check=False).returncode == 0:
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
                fork = find_fork(owner, name, login)
                if fork:
                    break
                time.sleep(5)
            else:
                die(f"GitHub did not finish forking {owner}/{name} to {login}/{name} within 5 minutes")
            note(f"forked {owner}/{name} to {login}/{name}")
    fname = fork["name"] if fork else name
    url = f"https://github.com/{login}/{fname}.git"
    cred = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    there = ""
    if fork:
        p = subprocess.run(["git", *cred, "ls-remote", "--heads", url, f"refs/heads/{branch}"],
                           capture_output=True, text=True)
        if p.returncode:
            die(f"cannot read {url}: {p.stderr.strip()[-300:]}")
        there = p.stdout.split()[0] if p.stdout.strip() else ""
    lease = []
    if update:
        # only the branch of this account's own open pull request is ever rewritten
        mine = open_prs(owner, name, login, branch) if there else []
        if not mine:
            die(f"--update: {login}:{branch} has no open pull request on {owner}/{name}; "
                "publish a new branch instead (never overwrite)")
        lease = [f"--force-with-lease=refs/heads/{branch}:{there}"]
    elif there:
        die(f"{login}/{fname} already has a branch {branch!r}; publish under a new name (never overwrite), "
            "or --update when it is the branch of this account's open pull request")
    if dry_run:
        print(f"WOULD PUSH {sha[:12]} to {login}/{fname}:{branch}")
    else:
        p = subprocess.run(["git", *cred, "-C", repo, "push", *lease, url, f"{sha}:refs/heads/{branch}"],
                           capture_output=True, text=True)
        if p.returncode:
            die(f"push to {login}/{fname}:{branch} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    base = base or json.loads(gh("api", f"repos/{owner}/{name}").stdout).get("default_branch", "main")
    print(f"FIX-BRANCH https://github.com/{login}/{fname}/tree/{branch}")
    print(f"COMPARE https://github.com/{owner}/{name}/compare/{base}...{login}:{fname}:{branch}")
    print(f"HEAD {sha}")
    if update:
        print(f"PR {open_prs(owner, name, login, branch)[0]['html_url']}" if not dry_run else "WOULD UPDATE the PR")
    elif pr:
        if dry_run:
            print(f"WOULD OPEN a pull request on {owner}/{name}: {login}:{branch} -> {base}: {title}")
            return 0
        p = gh("pr", "create", "--repo", f"{owner}/{name}", "--base", base, "--head", f"{login}:{branch}",
               "--title", title, "--body-file", body_file, check=False)
        if p.returncode:
            die(f"the branch is pushed, but the pull request on {owner}/{name} was not opened: "
                f"{(p.stderr or p.stdout).strip()[-400:]}")
        print(f"PR {p.stdout.strip().splitlines()[-1]}")
    return 0


def main(a):
    if a.whoami:
        print(whoami())
        return 0
    if not a.repo or not a.branch:
        raise Fail("fix-branch REPO_DIR --branch NAME [--ref REF] [--upstream OWNER/NAME] [--dry-run] "
                   "[--pr --title T --body-file F [--base B] | --update]")
    if a.pr and a.update:
        raise Fail("--pr opens a pull request, --update pushes to an open one's branch: one of them")
    return run(a.repo, a.branch, a.ref, a.dry_run, a.pr, a.title, a.body_file, a.base, a.update, a.upstream)
