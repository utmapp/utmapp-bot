"""Seeing the VM: screenshots, frame sampling, frame-pacing analysis.

`shot` is UTM's `capture screen` (the guest framebuffer).  `frames` samples
the VM window with ScreenCaptureKit on purpose: it sees what the host
composites, at the rate it does.  CrossOver games have only their window
(shot_window, frames_window).

Capture traps this encodes:
  * One still proves little (`screencapture -l` also stalls while a window
    is rendering and can return a stale frame).  Judge "frozen" or "black"
    from `frames` (many samples), never from one shot.
  * Recording a screen region (`screencapture -v -R`) captures the lock screen
    on an unattended Mac; window capture (ScreenCaptureKit) does not.
  * ScreenCaptureKit delivers repeats when the window re-presents the same
    image, so "frames delivered" overstates the frame rate; count frames whose
    pixels changed.
  * A sleeping display stops the window drawing; wake it before capturing.
  * Capture timing matters: a 3DMark graphics test lasts ~30 s, so capture
    while the workload runs, not minutes after it ended.
"""

import csv
import statistics
import subprocess
import time
from pathlib import Path

from .common import DATA, PLUGIN, Session, die, ensure_dir, log, run


def tool(name):
    src = PLUGIN / "scripts" / "utmbot" / "swift" / f"{name}.swift"
    out = ensure_dir(DATA / "bin") / name
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        log(f"compiling {name}")
        run(["swiftc", "-O", "-o", str(out), str(src)], check=True, timeout=600)
    return str(out)


def window_id(vm, owner="UTM"):
    rc, out = run([tool("winlist"), owner])
    rows = [l.split("\t") for l in out.splitlines() if l.count("\t") >= 3]
    exact = [r for r in rows if r[3] == vm.name or r[3].startswith(vm.name + " ")]
    if not exact and rows and all(not r[3] for r in rows):
        need_screen_recording()
    if not exact:
        die(f"no on-screen UTM window titled '{vm.name}' (is the VM window open and not "
            "minimized?)")
    # largest matching window (UTM also has small helper windows)
    exact.sort(key=lambda r: -int(r[1].split(",")[2]) * int(r[1].split(",")[3]))
    return exact[0][0]


def need_screen_recording():
    """Ask for Screen Recording (only an explicit request makes macOS list the
    terminal in System Settings), then stop with what to do."""
    rc, out = run([tool("screenaccess")], timeout=60)
    die("the terminal has no Screen Recording permission"
        + (" (macOS was just asked; look for its dialog)" if "requested" in out else "")
        + ": turn it on for the terminal app in System Settings > Privacy & Security > "
          "Screen & System Audio Recording, then quit and reopen the terminal")


def wake_display():
    subprocess.run(["caffeinate", "-u", "-t", "3"], capture_output=True)


def shot(vm, path=None):
    """One screenshot of the guest display, by UTM's `capture screen`: it
    reads the guest framebuffer itself, so no window, no Screen Recording
    permission and nothing on top of the VM in the picture."""
    from . import utm
    rd = Session(vm).run_dir()
    path = Path(path) if path else rd / f"shot-{time.strftime('%H%M%S')}.png"
    utm.osa("capture-screen", vm.name, str(path.resolve()))
    if not path.exists():
        die("capture screen wrote no file")
    print(f"SHOT {path}")
    return path


def shot_window(wid, path):
    """A screenshot of one on-screen window (a Wine game's: `cx shot`)."""
    wake_display()
    rc, out = run(["screencapture", "-x", "-o", f"-l{wid}", str(path)], timeout=60)
    if rc != 0 or not path.exists():
        die(f"screencapture failed: {out.strip()}")
    print(f"SHOT {path}")
    return path


def frames(vm, secs, tag="frames", fps=120, save_every=0.0):
    out = Session(vm).run_dir() / f"{tag}-{time.strftime('%H%M%S')}"
    return frames_window(window_id(vm), secs, out, fps, save_every)


def frames_window(wid, secs, out, fps=120, save_every=0.0):
    """Sample one window's frames (frames.swift) and analyze them."""
    wake_display()
    out = ensure_dir(out)
    cmd = [tool("frames"), str(wid), str(secs), str(out), "--fps", str(fps)]
    if save_every:
        cmd += ["--save-every", str(save_every)]
    log(f"sampling window {wid} for {secs}s -> {out}")
    rc, o = run(cmd, timeout=secs + 60)
    if rc != 0:
        die(f"frame capture failed: {o.strip()} (Screen Recording permission?)")
    return analyze(out / "frames.csv", secs=secs)


def analyze(csv_path, hold_ms=100.0, black_luma=8.0, secs=None):
    rows = []
    with open(csv_path) as fh:
        for r in csv.DictReader(fh):
            rows.append((float(r["t_ms"]), r["hash"], float(r["luma"])))
    if not rows:
        print(f"FRAMES {csv_path}: no samples (window hidden or not composited?)")
        return {}
    # ScreenCaptureKit delivers frames only when the window changes, so a
    # static window yields few samples: the capture length is the duration
    dur = secs or max((rows[-1][0] - rows[0][0]) / 1000, 0.001)
    changes = [rows[0][0]] + [t for (t, h, _), (_, ph, _) in zip(rows[1:], rows) if h != ph]
    gaps = [b - a for a, b in zip(changes, changes[1:])]
    holds = [g for g in gaps if g > hold_ms]
    black = sum(1 for _, _, l in rows if l < black_luma)
    res = {
        "seconds": round(dur, 1),
        "delivered_fps": round(len(rows) / dur, 1) if dur else 0,
        "distinct_fps": round((len(changes) - 1) / dur, 1) if dur else 0,
        "holds_over_ms": hold_ms,
        "holds": len(holds),
        "longest_hold_ms": round(max(gaps), 0) if gaps else 0,
        "median_frame_ms": round(statistics.median(gaps), 1) if gaps else 0,
        "black_fraction": round(black / len(rows), 3),
        "mean_luma": round(statistics.mean(l for _, _, l in rows), 1),
    }
    print(f"FRAMES distinct={res['distinct_fps']} fps (delivered {res['delivered_fps']}), "
          f"holds>{hold_ms:.0f}ms={res['holds']} longest={res['longest_hold_ms']}ms, "
          f"black={res['black_fraction'] * 100:.1f}%, mean luma {res['mean_luma']} "
          f"over {res['seconds']}s [{csv_path}]")
    if res["distinct_fps"] == 0:
        print("FRAMES: the window never changed — frozen (or a static screen); "
              "check the guest clock/cursor before calling it a hang")
    tint = tint_events(csv_path)
    if tint:
        res["tint"] = tint
    return res


def tint_events(csv_path, shift=0.12, min_luma=12.0):
    """Colour-tint events: distinct, lit frames whose R/G/B balance jumps away
    from the capture's usual balance (whole-screen colour flashes, swapped or
    dropped channels, a wrong texture format).  Needs the r,g,b columns
    frames.swift writes.  A frame is tinted when one channel's share
    r/(r+g+b) is more than SHIFT from that channel's median over the
    capture; consecutive tinted frames are one event.  A scene that really
    changes colour (a cutscene, a red menu) shows up too: look at the saved
    frames before calling it a bug."""
    rows, prev = [], None
    with open(csv_path) as fh:
        rd = csv.DictReader(fh)
        if not {"r", "g", "b"} <= set(rd.fieldnames or []):
            return None
        for r in rd:
            if r["hash"] == prev:
                continue
            prev = r["hash"]
            c = (float(r["r"]), float(r["g"]), float(r["b"]))
            if float(r["luma"]) < min_luma or sum(c) <= 0:
                continue
            rows.append((float(r["t_ms"]), tuple(v / sum(c) for v in c)))
    if len(rows) < 5:
        return None
    med = [statistics.median(s[i] for _, s in rows) for i in range(3)]
    events, cur = [], None
    for t, sh in rows:
        dev = [sh[i] - med[i] for i in range(3)]
        if max(abs(d) for d in dev) > shift:
            hi = "".join("RGB"[i] for i in range(3) if dev[i] > shift / 2) or "?"
            lo = "".join("RGB"[i] for i in range(3) if dev[i] < -shift / 2)
            kind = f"+{hi}" + (f"/-{lo}" if lo else "")
            if cur is None:
                cur = {"start_s": round(t / 1000, 2), "end_s": round(t / 1000, 2), "frames": 0, "kinds": {}}
                events.append(cur)
            cur["end_s"] = round(t / 1000, 2)
            cur["frames"] += 1
            cur["kinds"][kind] = cur["kinds"].get(kind, 0) + 1
        else:
            cur = None
    tinted = sum(e["frames"] for e in events)
    out = {"lit_frames": len(rows), "tinted_frames": tinted, "events": events[:50],
           "usual_shares": [round(m, 3) for m in med]}
    print(f"TINT {tinted}/{len(rows)} lit distinct frames tinted in {len(events)} event(s); "
          f"usual R:G:B shares {':'.join(f'{m:.2f}' for m in med)}")
    for e in events[:10]:
        print(f"TINT   t={e['start_s']}s {e['frames']} frame(s) to {e['end_s']}s {e['kinds']}")
    return out
