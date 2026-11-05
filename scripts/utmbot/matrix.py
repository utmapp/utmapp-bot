"""The validation matrix: every backend x workload cell, rendered AND fast enough.

Windows cells: [D3DMetal, DXMT] x [Time Spy GT1 (D3D12: D3DMetal only),
Fire Strike GT1].  Each cell gets a fresh disposable boot (one benchmark
family per boot: a second family after a fullscreen run is not comparable),
the driver installed, a discarded warm-up, then a measured run with a frame
sample taken mid-workload.  A cell passes when 3DMark calls the run valid,
the sample is not black and its frames change, and the score is within the
baseline band.

Linux cells run a caller-supplied command per backend that prints a line
`FPS <number>`, with frames sampled while it runs.

Baselines are per machine (~/.utmapp-bot/baselines.json) and never committed:
scores depend on the Mac, macOS, the display and D3DMetal.
"""

import json
import re
import time

from . import bench, capture, guest, linux, utm
from .common import DATA, VM, Session, log

WIN_CELLS = [("d3dmetal", "ts_gt1"), ("d3dmetal", "fs_gt1"), ("dxmt", "fs_gt1")]


def _baselines():
    p = DATA / "baselines.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _judge(cell, fps, frames, base, band):
    notes = []
    ok = True
    if frames:
        if frames.get("black_fraction", 0) > 0.2:
            ok = False
            notes.append(f"black {frames['black_fraction'] * 100:.0f}%")
        if frames.get("distinct_fps", 0) < 1:
            ok = False
            notes.append("frames never changed")
    else:
        notes.append("no frame sample")
    if fps is None:
        return False, notes + ["no score"]
    b = base.get(cell)
    if b:
        lo = b * (1 - band)
        if fps < lo:
            ok = False
            notes.append(f"{fps:.1f} < baseline {b:.1f} -{band * 100:.0f}%")
        else:
            notes.append(f"baseline {b:.1f} ({100 * (fps - b) / b:+.1f}%)")
    else:
        notes.append("no baseline")
    return ok, notes


def run_matrix(win_vm=None, driver=None, linux_vm=None, linux_cmd=None, band=0.07,
               save_baseline=False, frames_at=12, released=False):
    """driver: a package dir to install in every Windows cell; released: the
    released driver from the guest tools instead; neither: the VM as it is."""
    results = {}
    base = _baselines()
    if win_vm:
        vm = VM(win_vm)
        for backend, d in WIN_CELLS:
            cell = f"windows/{backend}/{d}"
            log(f"cell {cell}")
            utm.vm_down(vm)
            utm.vm_up(vm, backend=backend, tag=cell.replace("/", "-"), debug_log=None)
            ident = "as installed"
            if driver or released:
                ident = guest.install(vm, driver, released=released)
                guest.display_fix(vm)
            bench.run_3dmark(vm, d, tag="warmup")
            r = bench.run_3dmark(vm, d, tag="measured", frames_at=frames_at)
            fr = _latest_frames(vm)
            fps = r["fps"][0] if r.get("verdict") == "VALID" and r.get("fps") else None
            ok, notes = _judge(cell, fps, fr, base, band)
            if r.get("verdict") != "VALID":
                ok = False
                notes.insert(0, str(r.get("verdict")) + (f" {r['status']}" if r.get("status") else ""))
            results[cell] = {"ok": ok, "fps": fps, "frames": fr, "notes": notes, "dir": r.get("dir"),
                             "driver": ident}
        utm.vm_down(vm)
    if linux_vm and linux_cmd:
        vm = VM(linux_vm)
        for backend in ("d3dmetal", "dxmt"):
            cell = f"linux/{backend}"
            log(f"cell {cell}")
            utm.vm_down(vm)
            utm.vm_up(vm, backend=backend, tag=cell.replace("/", "-"), debug_log=None)
            name = f"m{int(time.time()) % 100000}"
            linux.launch(vm, linux_cmd, name=name)
            time.sleep(frames_at + 20)
            fr = capture.frames(vm, 15, tag=f"{backend}-frames")
            o, rc, text = linux.test(vm, f"while pgrep -f {linux_cmd.split()[0]!r} >/dev/null; "
                                         f"do sleep 5; done; grep -h '^FPS' ~/utmbot/{name}.log | "
                                         "tail -1", timeout_s=1800)
            m = re.search(r"^FPS ([\d.]+)", text, re.M)
            fps = float(m[1]) if m else None
            ok, notes = _judge(cell, fps, fr, base, band)
            results[cell] = {"ok": ok, "fps": fps, "frames": fr, "notes": notes}
        utm.vm_down(vm)

    out = DATA / "runs" / f"matrix-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2))
    print("\nMATRIX")
    for cell, r in results.items():
        fps = f"{r['fps']:.2f}" if r["fps"] is not None else "-"
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {cell:28s} {fps:>8s}  {'; '.join(r['notes'])}")
    unseen = [c for c, r in results.items() if r["ok"] and "no frame sample" in r["notes"]]
    verdict = "PASS" if results and all(r["ok"] for r in results.values()) else "FAIL"
    if verdict == "PASS" and unseen:
        # scores alone do not show that anything was drawn
        verdict = "PASS (scores only: rendering not checked)"
        log("no frame samples, so black or frozen output would have passed; give the "
            "terminal Screen Recording permission for `utmbot frames`")
    print(f"MATRIX {verdict} [{out}]")
    if save_baseline:
        prov = base.setdefault("_provenance", {})
        app = utm.ensure_app()
        for cell, r in results.items():
            if r["ok"] or (r["fps"] and "no baseline" in r["notes"]):
                base[cell] = r["fps"]
                # what produced the number: a band against another VM, app or
                # D3DMetal says nothing
                prov[cell] = {"vm": win_vm if cell.startswith("windows/") else linux_vm,
                              "app": f"{app} {utm.app_version(app)}",
                              "driver": r.get("driver", "as installed"),
                              "date": time.strftime("%Y-%m-%d")}
        (DATA / "baselines.json").write_text(json.dumps(base, indent=2))
        print(f"baselines saved to {DATA / 'baselines.json'}")
    return results


def _latest_frames(vm):
    rd = Session(vm).run_dir()
    csvs = sorted(rd.glob("*frames*/frames.csv"))
    return capture.analyze(csvs[-1]) if csvs else None
