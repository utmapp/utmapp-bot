"""Set up this Mac to pick up GitHub reviews for the bot account (github-setup).

usage: utmbot gh setup [--repo OWNER/NAME] [--org ORG] [--model M] [--paused]
                       | --check | --uninstall

Needs `gh` signed in as the bot account (a token with the `repo` or
`notifications` scope) and Claude Code logged in; nothing from the games
database.  Idempotent: run it again after updating the plugin.  It copies
scripts/ghbot and scripts/agentlib to ~/.utmapp-bot/agents/lib/ and loads the
LaunchAgent app.getutm.utmapp-bot.github, which runs `ghbot run`.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from agentlib import install as I  # noqa: E402
from ghbot import state as S  # noqa: E402
from ghbot.gh import GitHub, GitHubError  # noqa: E402

AGENT = "github"


def checks(cfg, strict=True):
    """The account, its token's scopes, org membership. Returns the login."""
    gh = GitHub()
    try:
        r = gh.request("GET", "/user")
    except GitHubError as e:
        msg = f"gh: {e} (sign in as the bot account: `gh auth login`)"
        if strict:
            raise SystemExit(f"ERROR: {msg}")
        I.warn(msg)
        return None
    login = r.data["login"]
    scopes = {s.strip() for s in (r.headers.get("X-OAuth-Scopes") or "").split(",") if s.strip()}
    if scopes and not scopes & {"repo", "notifications"}:
        I.warn(f"the token's scopes ({', '.join(sorted(scopes))}) cannot read notifications: "
               "`gh auth refresh -s notifications`")
    I.say(f"GitHub: signed in as {login}" + (f" (scopes {', '.join(sorted(scopes))})" if scopes else ""))
    org = cfg["GHBOT_ORG"]
    if gh.status("GET", f"/orgs/{org}/members/{login}") != 204:
        I.warn(f"{login} is not a member of {org}: members who keep their membership private look "
               "like outsiders to it, and are ignored")
    try:
        gh.get(f"/repos/{cfg['GHBOT_REPO']}")
    except GitHubError as e:
        I.warn(f"cannot read {cfg['GHBOT_REPO']}: {e}")
    return login


def install(a):
    cfg = S.config()
    upd = {}
    for k, v in (("GHBOT_REPO", a.repo), ("GHBOT_ORG", a.org), ("GHBOT_MODEL", a.model)):
        if v is not None:
            upd[k] = v
    claude = cfg.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(claude).exists():
        raise SystemExit("ERROR: Claude Code (`claude`) not found; install it and log in first")
    upd.setdefault("CLAUDE_BIN", claude)
    upd["GHBOT_PLUGIN_DIR"] = str(HERE.parents[1])
    S.write_config(upd)
    cfg = S.config()
    checks(cfg)

    for d in (S.D, S.D / "logs", S.D / "work", I.DATA / "locks"):
        S.private(d)
    with S.locked("main"):
        st = S.load()
        if not st.get("installed_at"):
            # activity from before this Mac was set up is not work for it
            st["installed_at"] = S.utc()
            I.say(f"picking up GitHub activity from {st['installed_at']} on")
        S.save(st)

    changed = I.install_package(HERE, "ghbot") | I.install_agentlib()
    if changed:
        I.say(f"installed the poller's scripts in {I.LIB}")
    py = shutil.which("python3") or sys.executable
    env = {"PATH": I.agent_path(cfg.get("CLAUDE_BIN"), py), "HOME": str(Path.home()),
           "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for k in ("UTMAPP_BOT_DATA", "UTM_APP"):
        if cfg.get(k):
            env[k] = cfg[k]
    if a.paused:
        S.PAUSED.write_text("installed paused by github-setup --paused\n")
    launcher, how = I.install_launcher()
    I.say(f"agent launcher {launcher.parents[2].name}: {how}")
    d = I.plist(AGENT, [launcher, py, str(I.LIB / "ghbot"), "run"], env, S.D / "logs", keepalive=True)
    d["WorkingDirectory"] = str(S.D)
    d["AbandonProcessGroup"] = True  # a restart of the agent must not take running sessions down
    I.say(f"agent {I.label(AGENT)}: {I.load_agent(AGENT, d, restart=changed or how == 'built')}")
    from agentlib.permissions import request_all
    request_all(launcher, py, HERE.parents[1], cfg.get("UTM_APP") or "/Applications/UTM.app",
                I.DATA, I.say, I.warn)

    _, work = __import__("ghbot.jobs", fromlist=["cfg_paths"]).cfg_paths(cfg)
    I.trust_workdir(Path(work).resolve())
    I.power_check()
    I.say("what a poll would pick up now (dry run)")
    r = subprocess.run([py, str(I.LIB / "ghbot"), "poll", "--dry-run"], capture_output=True, text=True,
                       timeout=600, env=dict(env))
    for line in (r.stdout + r.stderr).strip().splitlines()[-12:]:
        print("    " + line[:200])
    I.say("done; `utmbot gh status` shows the state, `utmbot gh setup --check` checks it")
    return 0


def check():
    cfg = S.config()
    checks(cfg, strict=False)
    v = I.LIB / "ghbot" / ".version"
    print(f"scripts   {I.LIB / 'ghbot'} version {v.read_text().strip() if v.exists() else 'NOT installed'}")
    r = subprocess.run(["launchctl", "print", f"gui/{I.UID}/{I.label(AGENT)}"], capture_output=True, text=True)
    state = next((l.split("=", 1)[1].strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), None)
    print(f"agent     {I.label(AGENT)}: {state or 'NOT loaded'}")
    print(f"launcher  {I.launcher_check() or 'installed and signed'}")
    st = S.load()
    print(f"poll      last {st.get('last_poll')}, failures {st.get('poll_failures', 0)}")
    I.power_check()
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="utmbot gh setup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo"); ap.add_argument("--org"); ap.add_argument("--model")
    ap.add_argument("--paused", action="store_true")
    ap.add_argument("--check", action="store_true"); ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    if a.uninstall:
        I.unload_agent(AGENT)
        I.say("GitHub poller agent removed; settings and state kept "
              f"({S.CONFIG_FILE}, {S.D}). Running sessions go on. Owned PRs keep their 👀: "
              "`utmbot gh release PR` frees one for other Macs")
        return 0
    return install(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
