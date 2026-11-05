"""Set this Mac up as a games-db triage machine (db-setup).  Safe to re-run:
a second run updates the installed scripts, settings and agents.

usage: python3 setup.py --server _triage@HOST [options]
       python3 setup.py --check        what is installed and whether it works
       python3 setup.py --uninstall    remove the agents (keeps data and backups)

Installs, for the logged-in user (LaunchAgents in ~/Library/LaunchAgents):
  app.getutm.utmapp-bot.tunnel    keeps the SSH forward to the internal API up
                                  (tunneld.py; KeepAlive)
  app.getutm.utmapp-bot.backup    `backup run` every hour: a validated backup
                                  every 12 h, retention 2/7/4/12
  app.getutm.utmapp-bot.dispatch  polls the work feed every 15 min and runs the
                                  db skills in Claude Code sessions (KeepAlive)

The agents run a copy of scripts/gamesdb in ~/.utmapp-bot/agents/ (the
plugin directory is replaced on updates; a re-run refreshes the copy).

Options:
  --server USER@HOST    the forwarding-only account on the server (_triage@host)
  --site URL            the site's public base URL (https://…), for links to its pages
  --holder NAME         this Mac's name among the triage machines, on the server's
                        triage leases (default: kept, else its LocalHostName); unique
                        per Mac
  --port N              local port of the forward (default 8081)
  --socket PATH         the internal API socket on the server
  --plugin-dir DIR      the plugin dispatched sessions load (default: this one)
  --workdir DIR         the sessions' working directory (default ~/.utmapp-bot/work;
                        never the plugin, which sessions must not edit)
  --model NAME          model for dispatched sessions (default: Claude Code's)
  --utm-app PATH        the UTM.app sessions drive (sets UTM_APP)
  --backup-dir DIR      where backups go (default ~/.utmapp-bot/gamesdb/backups)
  --no-dispatch         install the tunnel and backups only
  --paused              install the dispatcher paused (`gamesdb dispatch resume` starts it)
  --skip-checks         do not run the first backup and the dispatcher dry run
"""

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from gamesdb import tunnel  # noqa: E402
from gamesdb.tunnel import die  # noqa: E402
from agentlib.install import power_check, trust_workdir  # noqa: E402

DATA = tunnel.DATA
AGENTS = DATA / "agents"
LIB = AGENTS / "lib"
LA = Path.home() / "Library" / "LaunchAgents"
LOGS = tunnel.STATE / "logs"
LABEL = "app.getutm.utmapp-bot"
UID = os.getuid()
# the package the agents run (not the e2e test scripts); keep it in step with
# the directory: a module missing here is missing from the agents' copy
CODE = ["__init__.py", "__main__.py", "api.py", "attachments.py", "backup.py", "dispatch.py",
        "fixbranch.py", "redact.py", "setup.py", "state.py", "tunnel.py", "tunneld.py",
        "attachment.sb", "videoframes.swift"]


def say(msg):
    print(f"==> {msg}", flush=True)


def warn(msg):
    print(f"WARNING: {msg}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---- configuration -------------------------------------------------------------------

def write_config(updates):
    lines, seen = [], set()
    if tunnel.CONFIG_FILE.exists():
        for line in tunnel.CONFIG_FILE.read_text().splitlines():
            k = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if k in updates:
                seen.add(k)
                if updates[k] is None:
                    continue
                line = f"{k}={updates[k]}"
            lines.append(line)
    else:
        lines.append("# games-db settings for the utmapp-bot plugin (written by db-setup; not in any repository)")
    for k, v in updates.items():
        if k not in seen and v is not None:
            lines.append(f"{k}={v}")
    DATA.mkdir(parents=True, exist_ok=True)
    tunnel.CONFIG_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(tunnel.CONFIG_FILE, 0o600)


def host_of(dest):
    return dest.split("@", 1)[-1]


def ensure_host_key(dest):
    host = host_of(dest)
    if run(["ssh-keygen", "-F", host]).returncode == 0:
        return
    say(f"first connection to {host}: recording its host key")
    keys = run(["ssh-keyscan", "-T", "15", "-t", "ed25519", host]).stdout.strip()
    if not keys:
        die(f"could not read {host}'s host key (is it reachable on port 22?)")
    fp = run(["ssh-keygen", "-lf", "-"], input=keys).stdout.strip()
    with open(Path.home() / ".ssh" / "known_hosts", "a") as f:
        f.write(keys + "\n")
    print(f"    {fp}\n    (compare with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server)")


# ---- code and agents ------------------------------------------------------------------

def install_code():
    """Copy the scripts the agents run; returns True when they changed."""
    src = HERE
    dst = LIB / "gamesdb"
    h = hashlib.sha256()
    for name in CODE:
        h.update(name.encode() + (src / name).read_bytes())
    digest = h.hexdigest()[:16]
    stamp = dst / ".version"
    if stamp.exists() and stamp.read_text().strip() == digest:
        return False
    new = LIB / f"gamesdb.new-{os.getpid()}"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    for name in CODE:
        shutil.copy2(src / name, new / name)
    (new / ".version").write_text(digest + "\n")
    old = LIB / "gamesdb.old"
    shutil.rmtree(old, ignore_errors=True)
    if dst.exists():
        dst.replace(old)
    new.replace(dst)
    shutil.rmtree(old, ignore_errors=True)
    # the agents call `gamesdb …` too (sessions, blockers)
    (AGENTS / "bin").mkdir(parents=True, exist_ok=True)
    w = AGENTS / "bin" / "gamesdb"
    w.write_text(f'#!/bin/sh\nexec python3 "{LIB}/gamesdb" "$@"\n')
    w.chmod(0o755)
    say(f"installed the agents' scripts in {dst} (version {digest})")
    return True


def install_all_code():
    """The games-db package and the shared agentlib it imports."""
    from agentlib.install import install_agentlib
    changed = install_code()
    if install_agentlib():
        say(f"installed the shared agent code in {LIB / 'agentlib'}")
        changed = True
    return changed


def agent_env(cfg):
    path = ":".join(dict.fromkeys(
        [str(Path(cfg["CLAUDE_BIN"]).parent) if cfg.get("CLAUDE_BIN") else "",
         str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin",
         str(Path.home() / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]).keys()).strip(":")
    env = {"PATH": path, "HOME": str(Path.home()), "LANG": "en_US.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("UTMAPP_BOT_DATA"):
        env["UTMAPP_BOT_DATA"] = str(DATA)
    if cfg.get("UTM_APP"):
        env["UTM_APP"] = cfg["UTM_APP"]
    return env


def plist_for(name, args, env, keepalive=False, interval=None, nice=0):
    p = {"Label": f"{LABEL}.{name}", "ProgramArguments": args, "RunAtLoad": True,
         "EnvironmentVariables": env, "WorkingDirectory": str(DATA),
         "StandardOutPath": str(LOGS / f"{name}.agent.log"),
         "StandardErrorPath": str(LOGS / f"{name}.agent.log"),
         "ProcessType": "Background" if not keepalive else "Standard"}
    if keepalive:
        p["KeepAlive"] = True
        p["ThrottleInterval"] = 30
    if interval:
        p["StartInterval"] = interval
    if nice:
        p["Nice"] = nice
        p["LowPriorityIO"] = True
    if name == "dispatch":
        # sessions are children of the agent; a restart of the agent must
        # not take running sessions down with it
        p["AbandonProcessGroup"] = True
    return p


def load_agent(name, plist, code_changed):
    LA.mkdir(parents=True, exist_ok=True)
    path = LA / f"{LABEL}.{name}.plist"
    data = plistlib.dumps(plist)
    changed = not path.exists() or path.read_bytes() != data
    loaded = run(["launchctl", "print", f"gui/{UID}/{LABEL}.{name}"]).returncode == 0
    if changed:
        path.write_bytes(data)
    if changed and loaded:
        run(["launchctl", "bootout", f"gui/{UID}/{LABEL}.{name}"])
        time.sleep(1)
        loaded = False
    if not loaded:
        r = run(["launchctl", "bootstrap", f"gui/{UID}", str(path)])
        if r.returncode != 0:
            die(f"launchctl bootstrap {path.name}: {r.stderr.strip()}")
        say(f"{name} agent loaded")
    elif code_changed and plist.get("KeepAlive"):
        run(["launchctl", "kickstart", "-k", f"gui/{UID}/{LABEL}.{name}"])
        say(f"{name} agent restarted with the new scripts")
    else:
        say(f"{name} agent unchanged")


def unload_agent(name):
    path = LA / f"{LABEL}.{name}.plist"
    run(["launchctl", "bootout", f"gui/{UID}/{LABEL}.{name}"])
    path.unlink(missing_ok=True)


def _kept_workdir(cfg, plugin):
    """The configured workdir, unless it is the plugin (an older setup allowed
    that; sessions would edit and commit the plugin): then the default."""
    w = cfg.get("GAMESDB_WORKDIR")
    if w:
        r = Path(w).expanduser().resolve()
        if r == plugin or plugin in r.parents:
            say(f"GAMESDB_WORKDIR was the plugin ({w}); sessions now work in {DATA / 'work'}")
            return str(DATA / "work")
        return w
    return str(DATA / "work")


def stop_old_tunnels():
    """Tunnels from before db-setup: the old on-demand ControlMaster, and any
    tunneld started by hand."""
    ctl = Path(f"/tmp/gamesdb-{UID}/tunnel.sock")
    if ctl.exists():
        run(["ssh", "-S", str(ctl), "-O", "exit", "x"])
        say("stopped the old on-demand tunnel")
    me = str(LIB / "gamesdb" / "tunneld.py")
    for line in run(["pgrep", "-fl", "tunneld.py run"]).stdout.splitlines():
        pid, _, cmd = line.partition(" ")
        if me not in cmd:
            os.kill(int(pid), 15)
            say(f"stopped a tunnel agent started by hand (pid {pid})")


# ---- checks ---------------------------------------------------------------------------------

def wait_healthy(port, secs=90):
    url = f"http://127.0.0.1:{port}"
    t0 = time.time()
    while time.time() - t0 < secs:
        if tunnel.healthy(url):
            return True
        time.sleep(3)
    return False


def sleep_settings():
    out = run(["pmset", "-g"]).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["sleep"] and len(parts) > 1 and parts[1] != "0":
            return (f"the Mac sleeps after {parts[1]} min idle (pmset sleep). The tunnel reconnects "
                    "after wake, but a triage mid-run does not survive it: System Settings > "
                    "Energy > prevent automatic sleeping, or `sudo pmset -c sleep 0`.")
    return None


def utm_check(cfg, plugin):
    """The UTM sessions drive must be current: the harness refuses an older one
    (its scripting lacks what the plugin uses), so every VM job would fail.
    Returns a warning or ''."""
    app = cfg.get("UTM_APP") or "/Applications/UTM.app"
    r = run(["bash", str(plugin / "scripts" / "lib" / "utm.sh"), "scripting-missing", app])
    missing = ", ".join(l.strip() for l in r.stdout.splitlines() if l.strip())
    if r.returncode != 0 or missing:
        return (f"{app} is too old for this plugin (its scripting lacks {missing or r.stderr.strip()}): "
                "every VM job would fail. Update UTM, or rerun with --utm-app <a current UTM>")
    return ""


def check(cfg):
    ok = True
    port = tunnel.local_port(cfg)
    for name in ("tunnel", "backup", "dispatch"):
        r = run(["launchctl", "print", f"gui/{UID}/{LABEL}.{name}"])
        state = "loaded" if r.returncode == 0 else "NOT loaded"
        pid = next((l.split("=")[1].strip() for l in r.stdout.splitlines() if l.strip().startswith("pid =")), "-")
        print(f"agent {name:9s} {state} (pid {pid})")
        ok &= r.returncode == 0 or (name == "dispatch" and not (LA / f"{LABEL}.dispatch.plist").exists())
    healthy = tunnel.healthy(f"http://127.0.0.1:{port}")
    print(f"api       {'answers' if healthy else 'DOES NOT answer'} on 127.0.0.1:{port}")
    st = tunnel.read_status() or {}
    print(f"tunnel    {st.get('state')} since {st.get('since')}" + (f" ({st.get('last_error')})" if st.get("last_error") else ""))
    bdir = Path(cfg.get("GAMESDB_BACKUP_DIR") or tunnel.STATE / "backups").expanduser()
    try:
        b = json.loads((bdir / "status.json").read_text())
        print(f"backup    last ok {b.get('last_success_at')}; last error: {b.get('last_error')}")
    except (OSError, ValueError):
        print("backup    none yet")
    v = LIB / "gamesdb" / ".version"
    print(f"scripts   {LIB / 'gamesdb'} version {v.read_text().strip() if v.exists() else 'NOT installed'}")
    w = sleep_settings()
    if w:
        print(f"sleep     {w}")
    u = utm_check(cfg, Path(cfg.get("GAMESDB_PLUGIN_DIR") or HERE.parents[1]))
    print(f"utm       {u or (cfg.get('UTM_APP') or '/Applications/UTM.app') + ': current'}")
    return ok and healthy and not u


# ---- main ---------------------------------------------------------------------------------

def main(argv):
    p = argparse.ArgumentParser(prog="db-setup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server")
    p.add_argument("--site")
    p.add_argument("--holder")
    p.add_argument("--port", type=int)
    p.add_argument("--socket")
    p.add_argument("--plugin-dir")
    p.add_argument("--workdir")
    p.add_argument("--model")
    p.add_argument("--utm-app")
    p.add_argument("--backup-dir")
    p.add_argument("--no-dispatch", action="store_true")
    p.add_argument("--paused", action="store_true")
    p.add_argument("--skip-checks", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    a = p.parse_args(argv)

    cfg = tunnel.config()
    if a.check:
        return 0 if check(cfg) else 1
    if a.uninstall:
        for name in ("dispatch", "backup", "tunnel"):
            unload_agent(name)
        say("agents removed; settings, state and backups are kept in " + str(DATA))
        return 0

    # ---- settings
    server = a.server or cfg.get("GAMESDB_SSH")
    if not server:
        die("--server _triage@HOST is needed the first time (the server's administrator adds "
             "this Mac with deploy/macos/add-operator.sh and this Mac's public key)")
    if "@" not in server:
        server = f"_triage@{server}"
    plugin = Path(a.plugin_dir or cfg.get("GAMESDB_PLUGIN_DIR") or HERE.parents[1]).expanduser().resolve()
    if not (plugin / "skills" / "db-triage" / "SKILL.md").exists():
        die(f"{plugin} is not the utmapp-bot plugin (no skills/db-triage)")
    claude = shutil.which("claude") or cfg.get("CLAUDE_BIN")
    if not a.no_dispatch and not claude:
        die("the `claude` command is not on PATH; install Claude Code first")
    keys = [k for k in ("id_ed25519", "id_ecdsa", "id_rsa") if (Path.home() / ".ssh" / k).exists()]
    if not keys:
        die("no SSH key in ~/.ssh: create one (ssh-keygen -t ed25519) and have the server's "
             "administrator add its .pub with deploy/macos/add-operator.sh")
    updates = {"GAMESDB_SSH": server,
               "GAMESDB_SITE": a.site.rstrip("/") if a.site else cfg.get("GAMESDB_SITE"),
               "GAMESDB_HOLDER": a.holder or cfg.get("GAMESDB_HOLDER") or tunnel.holder(cfg),
               "GAMESDB_TUNNEL_PORT": str(a.port) if a.port else cfg.get("GAMESDB_TUNNEL_PORT"),
               "GAMESDB_SOCKET": a.socket or cfg.get("GAMESDB_SOCKET"),
               "GAMESDB_PLUGIN_DIR": str(plugin),
               "GAMESDB_WORKDIR": str(Path(a.workdir).expanduser()) if a.workdir else _kept_workdir(cfg, plugin),
               "CLAUDE_BIN": claude,
               "GAMESDB_MODEL": a.model or cfg.get("GAMESDB_MODEL"),
               "UTM_APP": a.utm_app or cfg.get("UTM_APP"),
               "GAMESDB_BACKUP_DIR": a.backup_dir or cfg.get("GAMESDB_BACKUP_DIR")}
    write_config(updates)
    cfg = tunnel.config()
    work = Path(cfg["GAMESDB_WORKDIR"]).expanduser()
    if work.resolve() == plugin or plugin in work.resolve().parents:
        die(f"--workdir must not be the plugin ({plugin}): sessions would edit it")
    work.mkdir(parents=True, exist_ok=True)
    trust_workdir(work.resolve())
    say(f"settings in {tunnel.CONFIG_FILE}")
    power_check()
    u = utm_check(cfg, plugin)
    if u:
        warn(u)
    for d in (LOGS, DATA / "locks", tunnel.STATE / "dispatch"):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)

    # ---- the connection, before anything depends on it
    ensure_host_key(server)
    stop_old_tunnels()
    code_changed = install_all_code()
    # a stable path (Homebrew's versioned interpreter moves on upgrades)
    py = shutil.which("python3") or sys.executable
    env = agent_env(cfg)
    lib = LIB / "gamesdb"
    load_agent("tunnel", plist_for("tunnel", [py, str(lib / "tunneld.py"), "run"], env, keepalive=True),
               code_changed)
    port = tunnel.local_port(cfg)
    say("waiting for the internal API through the tunnel")
    if not wait_healthy(port):
        st = tunnel.read_status() or {}
        pub = (Path.home() / ".ssh" / f"{keys[0]}.pub").read_text().strip()
        die(f"the API does not answer on 127.0.0.1:{port}: {st.get('state')} "
             f"({st.get('last_error')}).\nIf it says Permission denied, the server's administrator "
             f"has to run:\n  sudo ./deploy/macos/add-operator.sh \"{pub}\"")
    say(f"the internal API answers on 127.0.0.1:{port}")

    # ---- backups
    load_agent("backup", plist_for("backup", [py, str(lib / "backup.py"), "run"], env,
                                   interval=3600, nice=10), code_changed)

    # ---- dispatcher
    if a.no_dispatch:
        unload_agent("dispatch")
        say("dispatcher not installed (--no-dispatch)")
    else:
        paused = tunnel.STATE / "dispatch" / "PAUSED"
        if a.paused:
            paused.write_text("installed paused by db-setup --paused\n")
        load_agent("dispatch", plist_for("dispatch", [py, str(lib / "dispatch.py"), "run"], env,
                                         keepalive=True), code_changed)

    # ---- first runs
    if not a.skip_checks:
        say("first backup (skipped when a valid one is under 12 h old)")
        r = run([py, str(lib / "backup.py"), "run"], timeout=7200, env=dict(os.environ, **env))
        last = (r.stdout.strip() or r.stderr.strip()).splitlines()[-1:]
        print("    " + (last[0] if last else ""))
        if r.returncode not in (0,):
            warn("the first backup did not complete; the agent retries every hour "
                 "(`gamesdb backup list`)")
        if not a.no_dispatch:
            say("what the dispatcher would start now (dry run)")
            r = run([py, str(lib / "dispatch.py"), "tick", "--dry-run"], timeout=600,
                    env=dict(os.environ, **env))
            for line in (r.stdout + r.stderr).strip().splitlines()[-12:]:
                print("    " + line[:200])
            say("macOS permission dialogs (UTM automation, Screen Recording) appear the first "
                "time a dispatched session needs them; such a session reports itself blocked and "
                "reaches you over Remote Control with a push notification")
    w = sleep_settings()
    if w:
        warn(w)
    say("done; `python3 " + str(lib / "setup.py") + " --check` (or db-setup --check) shows the state")
    return 0


if __name__ == "__main__":
    tunnel.cli(main)
