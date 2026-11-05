"""utmbot — drive a UTM test VM for Neptune / virtio-gpu work.

usage: python3 <plugin>/scripts/utmbot [--vm NAME] <command> ...
       bin/utmbot gh <command> ...   the GitHub PR poller (scripts/ghbot; `gh --help`)

UTM lease (one session drives UTM at a time; every VM command checks it):
  lock [--name NAME] status | acquire [--purpose TEXT] [--wait S] | wait [--timeout S]
       | release [--force]
                          vm up takes it and vm down/shutdown return it; a skill that
                          needs UTM across boots takes it with acquire and ends with
                          release; a lease whose session ended is taken over; it shows
                          whether auto-triage (which job) or an interactive session
                          holds it. --name: another lease (moderation)

VM (default: --vm, $UTMBOT_VM, or the only test VM the plugin knows):
  app                     the UTM.app driven, its version, vmnet entitlement, whether
                          it is current (older ones lack scripting the plugin uses)
                          and whether this terminal may script it (launches it)
  vm up [--backend default|dxmt|d3dmetal|disabled] [--env K=V ...]
        [--debug-log on|off|as-is] [--no-killwatch] [--nodebug] [--require-vmnet]
        [--tag TAG] [--writable]
                          start DISPOSABLE (verified: -snapshot), wait for SSH (new
                          boot), prepare the guest, arm the kill-watcher; a Windows
                          VM gets UTM's guest tools in its removable drive first.
                          A UTM without vmnet entitlement gets the VM's NICs
                          emulated for this boot (SSH forwarded; no kdnet).
                          --writable keeps disk changes: to install software and
                          save a snapshot, never to test a driver (`install` refuses)
  vm down [--force]       force stop, reap orphans, restore defaults, keep logs
                          (a writable boot needs `vm shutdown`, or --force)
  vm shutdown             clean guest shutdown, wait for QEMU to exit, then as vm down
  vm snap list [--json] | create NAME [--overwrite] [--set K=V] | restore NAME [--force]
        | delete NAME | label NAME --set K=V
                          UTM disk snapshots of the stopped VM (disk + UEFI vars), with
                          last-used times and whether the disk still equals one
  vm status               processes, workers (+arch), ssh, env, kill-watcher
  vm register NAME --user U [--os linux|windows] [--key PUB]
                          make an existing VM usable (e.g. a Linux guest)
  front                   bring UTM to the front (App Nap check)

Guest (Windows: console session via schtasks; Linux: detached via setsid):
  guest CMD               run a shell command over SSH (cmd.exe / sh)
  put LOCAL REMOTE | get REMOTE LOCAL [-r]
  install PKG_DIR         full signed package: trust cert, pnputil, vgpusrv, fglock
  install --released      the released driver, from the guest tools `vm up` puts in the
                          VM's removable drive (UTM's own guest tools ISO), named by its
                          INF date
  verify                  device on the 3D driver + D3D on the 0x1af4 adapter
                          (Linux: Venus device in vulkaninfo)
  display-fix             restart DWM and reset the mode from the console session
  test EXE [ARGS] [--timeout S] [--genv K=V] [--visible]
                          run in the console session and wait for exit
  launch EXE [ARGS] [--genv K=V] [--visible] | launch-out
  dump IMAGE [--full]     minidump a guest process to the run dir
  cdb IMAGE               all thread stacks of a guest process
  ods [SECONDS] | ods get collect OutputDebugString in the console session | pull it

Profiling (an in-game window; see perf-regression):
  ingame [--threshold 6] [--need 6]    wait until the guest display is animating
  etw start | etw stop [NAME]          guest ETW: context switches + ready threads with
                                       stacks, DxgKrnl/DXGI (no sampled CPU: no PMU)
  etw analyze ETL PROCESS [--dev VM]   dump with symbols on a dev VM, summarize waits,
                                       per-thread CPU, GPU queue, presents
  hostprof [SECS] [--metal] [--ctx S]  system-wide xctrace: Time Profiler for QEMU and
                                       the busiest (or matching) render worker, or
                                       Metal System Trace GPU time per process

Host:
  hostlog [-n N] [--all] [--grep RE]   error lines from this VM lifetime's logs
  crashes [-n N] [--all-time]          .ips crash reports, GPU events, worker deaths
  backend                              which DirectX backend is live (proven)
  procs                                UTM/QEMU/render-server/worker tree
  attach qemu|server|worker [ID] [-o LLDB_CMD ...]
  killwatch arm|disarm|status
  perfcap [SECS] [--sample-at T]       GPU util + per-process CPU (+ one sample)
  memsample [SECS]                     swap, pressure, GPU memory, RSS
  check-host                           conditions that invalidate measurements
  snapshot                             the evidence bundle (start every triage here)

Input (to the running VM; keys are held so games that poll per frame see them):
  input key KEY [--hold S] [--times N] | combo KEY+KEY | text TEXT | click X Y [--button B]

CrossOver on this Mac (the same game in Wine on the host, for A/B; no --vm needed):
  cx info | list | base | clone APPID | delete NAME | cleanup | backend BOTTLE [X]
     | start/run/verify/exec/stop --bottle B [--backend d3dmetal|dxmt]
     | proof | steam … | import APPID | windows | shot | frames SECS | logs | ps
                          `utmbot cx help` for the details; bottles share the GPU with
                          the VMs, so running anything in one takes the UTM lease

This Mac's VMs by role (set when a provisioning run finishes; skills ask for a role):
  machine [show] | get test_vm|dev_vm | set ROLE VM
  dev up | dev down       start / shut down the dev VM (dev_vm) under the UTM lease; builds
                          (build-probes.sh, build-drivers.sh) need it up

What agents learn and build (outside the plugin, in the data dir; only what they
verified themselves, never a reporter's text, no personal data or secrets):
  game list | path APPID | init APPID [--title T] [--fullgame APPID] | show APPID
       | log APPID --report N --result TEXT [--run DIR] [--note TEXT]
                          ~/.utmapp-bot/games/steam-<appid>/: NOTES.md, game.json,
                          tools/, saves/, runs.jsonl; read before working on a game
  notes [show|path]       ~/.utmapp-bot/notes/NOTES.md: what is not about one game (the
                          Mac and UTM, the Steam client, engines and launchers,
                          CrossOver, workarounds); show creates it on first use
  tools                   ~/.utmapp-bot/agent-tools/<name>/: general helpers agents built

Capture:
  shot [PATH]                          window screenshot
  frames SECS [--fps N] [--save-every S]   distinct fps, holds, black frames
  analyze FRAMES_CSV

Waiting:
  wait guest-proc IMAGE [--gone] [--timeout S]
  wait host-file PATH REGEX [--timeout S]
  wait guest-file PATH REGEX [--timeout S]

Kernel debugger (via the test VM's debugger dev VM):
  kd start [--break] [--sympath P] | stop | status | tail [N] | send CMD | break | go | crash
     | verbose [LEVEL]    the KMD's DbgPrint into kd.log (DebugLevel + kernel print filter)

Matrix (no --vm needed):
  matrix --windows-vm NAME [--driver PKG_DIR | --released]
         [--linux-vm NAME --linux-cmd CMD] [--band 0.07] [--save-baseline]
                          every Windows cell installs the package (or the released
                          driver from the guest tools); neither: the VM as it is

Benchmarks:
  bench DEF [-n N] [--warmup] [--genv K=V] [--frames-at S]
  ab DEF --a SPEC --b SPEC [--pairs N]
        SPEC = backend=dxmt,driver=DIR,app=PATH,env=K=V;K=V,genv=K=V,debuglog=on
        (runs count only when VALID and the host was fit to measure; Debug Log off
        unless debuglog=on)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utmbot import common  # noqa: E402
from utmbot.common import Fail, kv_list, pick_vm  # noqa: E402


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    vm_name = None
    if argv[0] == "--vm":
        vm_name, argv = argv[1], argv[2:]
    cmd, rest = argv[0], argv[1:]

    from utmbot import bench, capture, evidence, guest, host, kd, linux, remote, utm, wait

    if cmd == "ui":
        from utmbot import appui, lease
        # the host's simulators need no UTM: only commands on a VM take the lease
        if rest[:1] in (["mac"], ["build-ios"], ["lldb"]) or "--vm" in rest or \
                (rest[:1] == ["record"] and "--sim" not in rest):
            lease.require(f"`utmbot ui {rest[0] if rest else ''}`")
        return appui.main(rest)

    if cmd == "mac":
        # macOS VMs, before pick_vm: create names one that does not exist yet
        from utmbot import lease, macvm
        if rest[:1] and rest[0] not in ("screen", "-h", "--help"):
            lease.require(f"`utmbot mac {rest[0]}`")
        return macvm.main(rest)

    if cmd == "vm" and rest and rest[0] == "register":
        p = argparse.ArgumentParser(prog="utmbot vm register")
        p.add_argument("name")
        p.add_argument("--user", required=True)
        p.add_argument("--os", default="linux", choices=["linux", "windows", "macos"])
        p.add_argument("--key")
        a = p.parse_args(rest[1:])
        utm.vm_register(a.name, a.user, a.os, a.key)
        return 0

    if cmd == "matrix":
        from utmbot import matrix
        p = argparse.ArgumentParser(prog="utmbot matrix")
        p.add_argument("--windows-vm")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--driver", metavar="PKG_DIR", help="driver package to install in every Windows cell")
        g.add_argument("--released", action="store_true",
                       help="the released driver from the guest tools in every Windows cell")
        p.add_argument("--linux-vm")
        p.add_argument("--linux-cmd")
        p.add_argument("--band", type=float, default=0.07)
        p.add_argument("--save-baseline", action="store_true")
        a = p.parse_args(rest)
        res = matrix.run_matrix(a.windows_vm, a.driver, a.linux_vm, a.linux_cmd, a.band,
                                a.save_baseline, released=a.released)
        return 0 if res and all(r["ok"] for r in res.values()) else 1

    if cmd == "analyze":
        capture.analyze(rest[0])
        return 0

    if cmd == "app":
        a = utm.app_path()
        missing = utm.scripting_missing(a)
        print(f"app       {a}")
        print(f"version   {utm.app_version(a)}")
        print(f"vmnet     {'yes' if utm.app_has_vmnet(a) else 'NO: VMs run with emulated networking; no kdnet'}")
        print(f"scripting {'current' if not missing else 'MISSING ' + ', '.join(missing)}")
        if missing:
            utm.require_automation(a)
        utm.ensure_app()
        print("answers   yes (running, and this terminal may script it)")
        return 0

    if cmd == "lock":
        from utmbot import lease
        return lease.main(rest)
    if cmd == "game":
        from utmbot import memory
        return memory.main(rest)
    if cmd == "notes":
        from utmbot import memory
        return memory.notes_main(rest)
    if cmd == "cx":
        from utmbot import crossover
        return crossover.main(rest)
    if cmd == "machine":
        from utmbot import machines
        return machines.main(rest)
    if cmd == "dev":
        from utmbot import machines
        machines.dev(rest[0] if rest else "")
        return 0
    if cmd == "tools":
        from utmbot import memory
        memory.list_tools()
        return 0

    if cmd == "steam" and (not rest or rest[0] in ("resolve", "appinfo", "library", "help", "-h", "--help")):
        from utmbot import steam
        return steam.main(None, rest)

    vm = pick_vm(vm_name)

    # Commands that touch a VM, its guest or its snapshots need UTM to be
    # free or ours (scripts/utmbot/lease.py); `vm up` takes the lease itself.
    readonly = {("vm", "status"), ("vm", "register"), ("vm", "up"), ("vm", "snap:list"),
                ("procs",), ("hostlog",), ("crashes",), ("backend",), ("killwatch", "status"),
                ("check-host",), ("analyze",)}
    key = (cmd,) if cmd not in ("vm", "killwatch") else (
        cmd, (rest[0] if rest else "status") if not (rest[:1] == ["snap"]) else
        "snap:" + (rest[1] if len(rest) > 1 else "list"))
    if key not in readonly:
        from utmbot import lease
        lease.require(f"`utmbot {' '.join([cmd] + rest[:1])}`")

    if cmd == "vm":
        sub = rest[0] if rest else "status"
        if sub == "up":
            p = argparse.ArgumentParser(prog="utmbot vm up")
            p.add_argument("--backend", choices=list(utm.DIRECTX))
            p.add_argument("--env", action="append")
            p.add_argument("--debug-log", default="on", choices=["on", "off", "as-is"])
            p.add_argument("--no-killwatch", action="store_true")
            p.add_argument("--nodebug", action="store_true")
            p.add_argument("--require-vmnet", action="store_true")
            p.add_argument("--writable", action="store_true")
            p.add_argument("--tag", default="")
            a = p.parse_args(rest[1:])
            utm.vm_up(vm, backend=a.backend, env=kv_list(a.env),
                      debug_log={"on": True, "off": False, "as-is": None}[a.debug_log],
                      killwatch=not a.no_killwatch,
                      nodebug=a.nodebug, tag=a.tag, require_vmnet=a.require_vmnet,
                      writable=a.writable)
        elif sub == "down":
            p = argparse.ArgumentParser(prog="utmbot vm down")
            p.add_argument("--force", action="store_true", help="also a writable boot")
            utm.vm_down(vm, force=p.parse_args(rest[1:]).force)
        elif sub == "shutdown":
            utm.vm_shutdown(vm)
        elif sub == "snap":
            from utmbot import snap
            return snap.main(vm, rest[1:])
        elif sub == "status":
            utm.vm_status(vm)
        else:
            raise Fail(f"unknown: vm {sub}")
    elif cmd == "steam":
        from utmbot import steam
        return steam.main(vm, rest)
    elif cmd == "input":
        from utmbot import inputs
        return inputs.main(vm, rest)
    elif cmd == "front":
        utm.bring_to_front()
        host.check_host(vm, quiet=False)
    elif cmd == "guest":
        rc, out = remote.ssh(vm, " ".join(rest), timeout=600)
        print(out.rstrip())
        return rc
    elif cmd == "put":
        remote.put(vm, rest[0], rest[1], recursive=Path(rest[0]).is_dir())
    elif cmd == "get":
        p = argparse.ArgumentParser(prog="utmbot get")
        p.add_argument("remote")
        p.add_argument("local")
        p.add_argument("-r", action="store_true", help="a directory")
        a = p.parse_args(rest)
        remote.get(vm, a.remote, a.local, recursive=a.r)
    elif cmd in ("test", "launch"):
        p = argparse.ArgumentParser(prog=f"utmbot {cmd}")
        p.add_argument("exe")
        p.add_argument("--timeout", type=int, default=600)
        p.add_argument("--genv", action="append")
        p.add_argument("--visible", action="store_true")
        a, extra = p.parse_known_args(rest)
        if vm.is_windows:
            if cmd == "test":
                o, rc, _ = guest.test(vm, a.exe, extra, a.timeout, kv_list(a.genv), a.visible)
                return 0 if (o == wait.Outcome.DONE and rc == 0) else 1
            guest.launch(vm, a.exe, extra, kv_list(a.genv), a.visible)
        else:
            line = " ".join([a.exe] + extra)
            if cmd == "test":
                o, rc, _ = linux.test(vm, line, a.timeout, kv_list(a.genv))
                return 0 if (o == wait.Outcome.DONE and rc == 0) else 1
            linux.launch(vm, line, env=kv_list(a.genv))
    elif cmd == "launch-out":
        if vm.is_windows:
            guest.launch_out(vm)
        else:
            l = common.Session(vm).get("last_launch") or {}
            print(remote.ssh(vm, f"cat {l.get('out', '/dev/null')}")[1])
    elif cmd == "install":
        p = argparse.ArgumentParser(prog="utmbot install")
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("pkg_dir", nargs="?", metavar="PKG_DIR", help="a signed driver package on this Mac")
        g.add_argument("--released", action="store_true",
                       help="the released driver from the guest tools drive `vm up` attached")
        a = p.parse_args(rest)
        guest.install(vm, a.pkg_dir, released=a.released)
    elif cmd == "verify":
        return 0 if (guest.verify(vm) if vm.is_windows else linux.verify(vm)) else 1
    elif cmd == "display-fix":
        guest.display_fix(vm)
    elif cmd == "dump":
        p = argparse.ArgumentParser(prog="utmbot dump")
        p.add_argument("image")
        p.add_argument("--full", action="store_true", help="with the process's whole memory")
        a = p.parse_args(rest)
        guest.dump(vm, a.image, a.full)
    elif cmd == "cdb":
        guest.cdb_stacks(vm, rest[0])
    elif cmd == "ods":
        if rest and rest[0] == "get":
            rd = common.Session(vm).run_dir()
            remote.get(vm, f"{guest.GWS}/ods.log", rd / "ods.log")
            print(f"ODS {rd / 'ods.log'}")
        else:
            guest.ods_start(vm, int(rest[0]) if rest else 1800)
    elif cmd == "ingame":
        p = argparse.ArgumentParser(prog="utmbot ingame")
        p.add_argument("--threshold", type=float, default=6.0)
        p.add_argument("--need", type=int, default=6)
        p.add_argument("--timeout", type=int, default=180)
        a = p.parse_args(rest)
        from utmbot import profile
        profile.wait_ingame(vm, a.threshold, a.need, a.timeout)
    elif cmd == "etw":
        from utmbot import profile
        sub = rest[0] if rest else ""
        if sub == "start":
            profile.etw_start(vm)
        elif sub == "stop":
            profile.etw_stop(vm, rest[1] if len(rest) > 1 else "etw")
        elif sub == "analyze":
            p = argparse.ArgumentParser(prog="utmbot etw analyze")
            p.add_argument("etl")
            p.add_argument("process")
            p.add_argument("--dev")
            a = p.parse_args(rest[1:])
            profile.etw_analyze(vm, a.etl, a.process, a.dev)
        else:
            raise Fail("etw start | stop [NAME] | analyze ETL PROCESS [--dev VM]")
    elif cmd == "hostprof":
        p = argparse.ArgumentParser(prog="utmbot hostprof")
        p.add_argument("secs", nargs="?", type=int, default=10)
        p.add_argument("--metal", action="store_true")
        p.add_argument("--ctx")
        a = p.parse_args(rest)
        from utmbot import profile
        profile.hostprof(vm, a.secs, "Metal System Trace" if a.metal else "Time Profiler", a.ctx)
    elif cmd == "hostlog":
        p = argparse.ArgumentParser(prog="utmbot hostlog")
        p.add_argument("-n", type=int, default=60)
        p.add_argument("--all", action="store_true")
        p.add_argument("--grep")
        a = p.parse_args(rest)
        host.hostlog(vm, a.n, a.all, a.grep)
    elif cmd == "crashes":
        p = argparse.ArgumentParser(prog="utmbot crashes")
        p.add_argument("-n", type=int, default=10)
        p.add_argument("--all-time", action="store_true")
        a = p.parse_args(rest)
        host.crashes(vm, a.n, a.all_time)
    elif cmd == "backend":
        host.backend(vm)
    elif cmd == "procs":
        from utmbot import procs
        t = procs.vm_tree(vm.name)
        for k in ("utm", "helper", "qemu", "swtpm", "server", "worker"):
            for r in t["all"][k]:
                extra = f" ctx={r.get('ctx_name')} arch={r.get('arch')}" if k == "worker" else ""
                print(f"{k:7s} pid={r['pid']} ppid={r['ppid']} cpu={r['cpu']}% "
                      f"rss={r['rss_mb']}MB stat={r['stat']}{extra}")
    elif cmd == "attach":
        p = argparse.ArgumentParser(prog="utmbot attach")
        p.add_argument("which", choices=["qemu", "server", "worker"])
        p.add_argument("ident", nargs="?")
        p.add_argument("-o", action="append", default=[])
        a = p.parse_args(rest)
        host.attach(vm, a.which, a.ident, a.o)
    elif cmd == "killwatch":
        sub = rest[0] if rest else "status"
        if sub == "arm":
            host.killwatch_arm(vm)
        elif sub == "disarm":
            host.killwatch_disarm(vm)
        else:
            print(host.killwatch_state(vm))
    elif cmd == "_kdrelay":
        kd.relay(vm, rest)
    elif cmd == "_killwatch":
        host.killwatch_loop(vm, float(rest[0]) if rest else 2.0)
    elif cmd == "perfcap":
        p = argparse.ArgumentParser(prog="utmbot perfcap")
        p.add_argument("secs", nargs="?", type=int, default=60)
        p.add_argument("--sample-at", type=int)
        a = p.parse_args(rest)
        host.perfcap(vm, a.secs, a.sample_at)
    elif cmd == "memsample":
        host.memsample(vm, int(rest[0]) if rest else 0)
    elif cmd == "check-host":
        return 1 if host.check_host(vm, quiet=False) else 0
    elif cmd == "snapshot":
        evidence.snapshot(vm)
    elif cmd == "shot":
        capture.shot(vm, rest[0] if rest else None)
    elif cmd == "frames":
        p = argparse.ArgumentParser(prog="utmbot frames")
        p.add_argument("secs", type=float)
        p.add_argument("--fps", type=int, default=120)
        p.add_argument("--save-every", type=float, default=0)
        a = p.parse_args(rest)
        capture.frames(vm, a.secs, fps=a.fps, save_every=a.save_every)
    elif cmd == "wait":
        return do_wait(vm, rest)
    elif cmd == "kd":
        sub = rest[0] if rest else "status"
        if sub == "start":
            p = argparse.ArgumentParser(prog="utmbot kd start")
            p.add_argument("--break", dest="brk", action="store_true", help="break in at attach")
            p.add_argument("--sympath")
            a = p.parse_args(rest[1:])
            kd.start(vm, a.brk, a.sympath)
        elif sub == "stop":
            kd.stop(vm)
        elif sub == "status":
            print(kd.status(vm))
        elif sub == "tail":
            kd.tail(vm, int(rest[1]) if len(rest) > 1 else 40)
        elif sub == "send":
            kd.send(vm, " ".join(rest[1:]))
        elif sub == "break":
            kd.brk(vm)
        elif sub == "go":
            kd.send(vm, "g")
        elif sub == "crash":
            kd.crash(vm)
        elif sub == "verbose":
            kd.verbose(vm, int(rest[1]) if len(rest) > 1 else 4)
        else:
            raise Fail(f"unknown: kd {sub}")
    elif cmd == "bench":
        p = argparse.ArgumentParser(prog="utmbot bench")
        p.add_argument("definition")
        p.add_argument("-n", type=int, default=1)
        p.add_argument("--warmup", action="store_true")
        p.add_argument("--genv", action="append")
        p.add_argument("--frames-at", type=float)
        a = p.parse_args(rest)
        bench.bench(vm, a.definition, a.n, a.warmup, genv=kv_list(a.genv), frames_at=a.frames_at)
    elif cmd == "ab":
        p = argparse.ArgumentParser(prog="utmbot ab")
        p.add_argument("definition")
        p.add_argument("--a", required=True)
        p.add_argument("--b", required=True)
        p.add_argument("--pairs", type=int, default=3)
        a = p.parse_args(rest)
        bench.ab(vm, a.definition, a.a, a.b, a.pairs)
    else:
        raise Fail(f"unknown command {cmd!r} (utmbot help)")
    return 0


def do_wait(vm, rest):
    import re
    from utmbot import remote
    from utmbot.wait import Outcome, until
    p = argparse.ArgumentParser(prog="utmbot wait")
    p.add_argument("kind", choices=["guest-proc", "host-file", "guest-file"])
    p.add_argument("target")
    p.add_argument("regex", nargs="?")
    p.add_argument("--gone", action="store_true")
    p.add_argument("--timeout", type=int, default=600)
    a = p.parse_args(rest)
    if a.kind == "guest-proc":
        def check():
            if vm.is_windows:
                rc, o = remote.ssh(vm, f'tasklist /fi "imagename eq {a.target}" /fo csv /nh')
                present = a.target.lower() in o.lower()
            else:
                rc, o = remote.ssh(vm, f"pgrep -f {a.target!r}")
                present = rc == 0
            return present != a.gone, f"{a.target} {'present' if present else 'absent'}"
    elif a.kind == "host-file":
        rx = re.compile(a.regex or ".")

        def check():
            pth = Path(a.target)
            t = pth.read_text(errors="replace") if pth.exists() else ""
            return bool(rx.search(t)), f"{len(t)} bytes"
    else:
        rx = re.compile(a.regex or ".")

        def check():
            c = f'type "{a.target}" 2>nul' if vm.is_windows else f"cat {a.target} 2>/dev/null"
            rc, t = remote.ssh(vm, c)
            return bool(rx.search(t)), f"{len(t)} bytes"
    o, obs = until(check, a.timeout, f"wait {a.kind} {a.target}", every=30, vm=vm, interval=3)
    print(f"WAIT {o}: {obs}")
    return 0 if o == Outcome.DONE else 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
