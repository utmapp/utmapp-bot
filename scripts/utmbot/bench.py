"""3DMark runs and A/B comparisons with the benchmark-validity protocol built in.

A run is only trusted when:
  * 3DMark's own result says it is valid (Arielle.xml status codes), never
    from the score alone — a run that lost focus or ran windowed still
    produces a number;
  * the workload kept the foreground (fgforce pulls it back; a console window
    or a mode switch otherwise leaves it composed/windowed at about half the
    frame rate, and 3DMark cancels);
  * the host was awake, unlocked and UTM was not App-Napped;
  * the shader cache was warm (a cold cache makes the first graphics test
    miss 3DMark's 60 s start deadline: GT1 NO_RESPONSE, which looks exactly
    like a driver hang);
  * the first run after a boot or a build is discarded (warm-up);
  * A and B are interleaved in one session (the host drifts over hours;
    never compare against a number from another session).
"""

import json
import re
import statistics
import time
import zipfile
from pathlib import Path

from . import guest, host, remote
from .common import PLUGIN, Session, die, ensure_dir, log, probe
from .wait import Outcome, until

MARK = r"C:\Program Files\UL\3DMark\3DMarkCmd.exe"
WORKLOAD_TITLE = "3DMark Workload"


def _defpath(d):
    p = Path(d)
    if p.exists():
        return p
    p = PLUGIN / "bench" / "defs" / f"{d}.3dmdef"
    if p.exists():
        return p
    die(f"no 3DMark definition {d} (give a path, or one of: " +
        ", ".join(x.stem for x in (PLUGIN / "bench" / "defs").glob("*.3dmdef")) + ")")


def run_3dmark(vm, definition, tag="", genv=None, frames_at=None, frames_secs=20,
               timeout_min=20):
    """One 3DMark run in the console session; returns a result dict."""
    from . import capture, utm
    d = _defpath(definition)
    rd = Session(vm).run_dir()
    out = ensure_dir(rd / f"bench-{time.strftime('%H%M%S')}-{d.stem}{'-' + tag if tag else ''}")
    rc, o = remote.ssh(vm, f'if exist "{MARK}" (echo yes) else (echo no)')
    if "yes" not in o:
        die("3DMark is not installed in the guest (3DMarkCmd.exe missing): install it into the "
            "base image with provision-windows-test.sh --only content --3dmark DIR")
    problems = host.check_host(vm)
    if problems:
        log("WARNING: this run is not quotable: " + "; ".join(problems))
    utm.bring_to_front()
    fgf = probe("fgforce.exe")
    if not fgf:
        die("fgforce.exe is not built: run scripts/build-probes.sh first")
    remote.ssh(vm, "taskkill /f /im fgforce.exe >nul 2>&1 & taskkill /f /im 3DMarkCmd.exe "
                   f">nul 2>&1 & del /q {guest.GW}\\result.zip 2>nul & exit 0")
    remote.put(vm, d, f"{guest.GWS}/{d.name}")
    remote.put(vm, fgf, f"{guest.GWS}/fgforce.exe")
    # fgforce runs beside 3DMarkCmd (cmd /c, never `start x.cmd`) and only
    # re-foregrounds the workload window when it has lost focus for a while
    cmdline = (f'start "fg" /min cmd /c {guest.GW}\\fgforce.exe "{WORKLOAD_TITLE}" '
               f"{timeout_min * 60} & \"{MARK}\" --definition={guest.GWS}/{d.name} "
               f"--export={guest.GW}\\result.zip --online=off --systeminfo=off")
    t0 = time.time()
    tn, outp = guest.console_task(vm, cmdline, hidden=True, env=genv)

    def started():
        rc, o = remote.ssh(vm, 'tasklist /fi "imagename eq 3DMarkCmd.exe" /fo csv /nh')
        return "3DMarkCmd" in o, "3DMarkCmd " + ("running" if "3DMarkCmd" in o else "not yet")
    oc, obs = until(started, 120, "3DMark start", every=30, vm=vm, interval=5)
    if oc != Outcome.DONE:
        return {"verdict": "NEVER_STARTED" if oc == Outcome.TIMEOUT else oc, "dir": str(out)}
    if frames_at is not None:
        # the workload window is only visible from the console session
        marker = f"{guest.GW}\\workload-up.txt"
        remote.put(vm, PLUGIN / "scripts" / "utmbot" / "guestfiles" / "waitwindow.ps1",
                   f"{guest.GWS}/waitwindow.ps1")
        guest.console_task(vm, f"powershell -NoProfile -ExecutionPolicy Bypass -File "
                               f"{guest.GW}\\waitwindow.ps1 -Title \"{WORKLOAD_TITLE}\" "
                               f"-Out {marker} -Seconds 600")

        def workload_up():
            rc, o = remote.ssh(vm, f'type "{marker}" 2>nul & tasklist /fi "imagename eq 3DMarkCmd.exe" /fo csv /nh')
            if rc in (124, 255):  # the poll itself failed: no evidence either way
                return False, f"poll failed (ssh {rc})"
            if o.startswith(("UP", "TIMEOUT")):
                return True, o.splitlines()[0].strip()
            # 3DMark gave up (a workload that never started): stop waiting
            if "3DMarkCmd" not in o:
                return True, "3DMarkCmd exited before the workload window appeared"
            return False, "workload window not yet"
        oc, obs = until(workload_up, 600, "workload window", every=30, vm=vm, interval=2)
        if oc == Outcome.DONE and obs.startswith("UP"):
            time.sleep(frames_at)
            try:
                capture.frames(vm, frames_secs, tag=f"{out.name}-frames", save_every=5)
            except Exception as e:
                log(f"frame capture failed: {e}")
        else:
            log(f"no workload window seen ({obs}); no frame sample for this run")

    def finished():
        rc, o = remote.ssh(vm, 'tasklist /fi "imagename eq 3DMarkCmd.exe" /fo csv /nh')
        return "3DMarkCmd" not in o, f"running {int(time.time() - t0)}s"
    oc, obs = until(finished, timeout_min * 60, "3DMark run", every=60, vm=vm, interval=10)
    if oc != Outcome.DONE:
        return {"verdict": oc, "dir": str(out)}
    guest.wait_task(vm, tn, outp, 30, "3DMark task")
    remote.get(vm, outp.replace("\\", "/"), out / "console.txt", check=False)
    # the export can be caught mid-flush; cancelled runs skip --export but
    # still leave a result in Documents\3DMark
    got = False
    for _ in range(2):
        if remote.get(vm, f"{guest.GWS}/result.zip", out / "result.zip", check=False) and \
                zipfile.is_zipfile(out / "result.zip"):
            got = True
            break
        time.sleep(3)
    if not got:
        rc, o = remote.ssh(vm, 'dir /b /o-d "%USERPROFILE%\\Documents\\3DMark\\*.3dmark-result" 2>nul')
        last = o.strip().splitlines()[0].strip() if o.strip() else ""
        if last:
            rc, prof = remote.ssh(vm, "echo %USERPROFILE%")
            got = remote.get(vm, prof.strip().replace("\\", "/") + f"/Documents/3DMark/{last}",
                             out / "result.zip", check=False) and zipfile.is_zipfile(out / "result.zip")
    if not got:
        console = (out / "console.txt").read_text(errors="replace") if (out / "console.txt").exists() else ""
        if re.search(r"license key required", console, re.I):
            verdict = "NOT_ACTIVATED"
            log("3DMarkCmd needs a Professional key: provision-windows-test.sh --only content "
                "--3dmark DIR with $UTMBOT_3DMARK_KEY set")
        else:
            verdict = "NO_RESULT"
        print(f"BENCH {d.stem}: {verdict} [{out}]")
        return {"verdict": verdict, "dir": str(out)}
    with zipfile.ZipFile(out / "result.zip") as z:
        z.extractall(out)
    arielle = next(out.rglob("Arielle.xml"), None)
    if not arielle:
        print(f"BENCH {d.stem}: NO_RESULT (no Arielle.xml in the result) [{out}]")
        return {"verdict": "NO_RESULT", "dir": str(out)}
    text = arielle.read_text(errors="replace")
    bad = re.findall(r'<status code="([1-9]\d*)">([^<]*)', text)
    fps = [float(x) for x in re.findall(r'<primary_result[^>]*unit="fps">([\d.]+)', text)]
    res = {"definition": d.stem, "fps": fps, "dir": str(out),
           "host_problems": problems, "seconds": int(time.time() - t0)}
    if bad:
        # 1000: windowed/lost exclusive mode; 10000: focus/resolution flake
        res["verdict"] = "INVALID"
        res["status"] = [{"code": c, "text": t.strip()} for c, t in bad]
    else:
        res["verdict"] = "VALID"
    (out / "result.json").write_text(json.dumps(res, indent=2))
    print(f"BENCH {d.stem}: {res['verdict']} fps={' '.join(f'{x:.2f}' for x in fps)}"
          + (f" status={bad[0][0]} {bad[0][1].strip()}" if bad else "")
          + (" (NOT QUOTABLE: host)" if problems else "") + f" [{out}]")
    return res


def bench(vm, definition, n=1, warmup=False, **kw):
    results = []
    if warmup:
        log("warm-up run (discarded)")
        run_3dmark(vm, definition, tag="warmup", **kw)
    for i in range(n):
        results.append(run_3dmark(vm, definition, tag=f"r{i + 1}", **kw))
    valid = [r["fps"][0] for r in results if quotable(r)]
    if len(valid) > 1:
        print(f"BENCH SUMMARY {len(valid)}/{n} valid and quotable, GT1 mean "
              f"{statistics.mean(valid):.2f} stdev {statistics.stdev(valid):.2f}")
    return results


def quotable(r):
    """A run that counts: 3DMark says VALID and the host was fit to measure."""
    return r.get("verdict") == "VALID" and bool(r.get("fps")) and not r.get("host_problems")


def parse_arm(spec):
    """'backend=dxmt,driver=DIR,app=PATH,env=K=V;K2=V2,genv=K=V' -> dict."""
    arm = {}
    for part in filter(None, spec.split(",")):
        k, _, v = part.partition("=")
        if k in ("env", "genv"):
            arm[k] = dict(kv.split("=", 1) for kv in v.split(";") if "=" in kv)
        else:
            arm[k] = v
    return arm


def ab(vm, definition, a, b, pairs=3):
    """Interleaved A/B: per run a fresh disposable boot (+ install), one
    discarded warm-up, one measured run; ABAB… within one session."""
    import os
    from . import utm
    arms = {"A": parse_arm(a), "B": parse_arm(b)}
    for name, arm in arms.items():
        # every boot is disposable: an arm without a driver runs on none
        if not arm.get("driver"):
            die(f"arm {name} has no driver=DIR: each run is a fresh disposable boot, so "
                "both arms must name the driver package to install (the same one if the "
                "driver is not what is compared)")
    default_app = os.environ.get("UTM_APP")
    rows = []
    for i in range(pairs):
        for name in ("A", "B") if i % 2 == 0 else ("B", "A"):
            arm = arms[name]
            utm.vm_down(vm)
            # one UTM at a time: quit the previous arm's app before switching
            want = arm.get("app") or default_app
            if (want or "") != (os.environ.get("UTM_APP") or ""):
                utm.quit_app()
                if want:
                    os.environ["UTM_APP"] = want
                else:
                    os.environ.pop("UTM_APP", None)
            log(f"pair {i + 1}/{pairs} arm {name}: {arm}")
            # Debug Log off unless the arm asks: it costs time and is not what users run
            utm.vm_up(vm, backend=arm.get("backend"), env=arm.get("env"), tag=f"ab-{name}{i + 1}",
                      debug_log=arm.get("debuglog") == "on")
            guest.install(vm, arm["driver"])
            guest.display_fix(vm)
            run_3dmark(vm, definition, tag=f"{name}-warmup", genv=arm.get("genv"))
            r = run_3dmark(vm, definition, tag=f"{name}{i + 1}", genv=arm.get("genv"))
            rows.append((name, r))
    utm.vm_down(vm)
    out = {}
    for name in ("A", "B"):
        v = [r["fps"][0] for n, r in rows if n == name and quotable(r)]
        dropped = sum(1 for n, r in rows if n == name and not quotable(r))
        out[name] = v
        if dropped:
            print(f"AB {name}: {dropped} run(s) not counted (INVALID, no result, or host "
                  "NOT QUOTABLE; see each run's result.json)")
        if v:
            print(f"AB {name}: n={len(v)} mean={statistics.mean(v):.2f} "
                  f"stdev={statistics.stdev(v) if len(v) > 1 else 0:.2f} runs={v}")
        else:
            print(f"AB {name}: no valid runs")
    if len(out["A"]) > 1 and len(out["B"]) > 1:
        ma, mb = statistics.mean(out["A"]), statistics.mean(out["B"])
        noise = 2 * max(statistics.stdev(out["A"]), statistics.stdev(out["B"]))
        delta = mb - ma
        print(f"AB delta B-A = {delta:+.2f} fps ({100 * delta / ma:+.1f}%), noise band ±{noise:.2f}: "
              + ("WITHIN NOISE" if abs(delta) <= noise else ("B FASTER" if delta > 0 else "B SLOWER")))
    rd = Session(vm).run_dir()
    (rd / "ab.json").write_text(json.dumps({"a": a, "b": b, "rows": rows}, indent=2, default=str))
    return out
