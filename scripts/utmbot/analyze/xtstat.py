#!/usr/bin/env python3
"""xtstat.py TIME-PROFILE.xml [TOPN] [--thread SUBSTR]

Aggregates an `xctrace export` of the time-profile table (Time Profiler):
self time by leaf function, inclusive time by function, per-thread totals,
and per-binary self time.  Resolves xctrace's id/ref element sharing.
Frames without a symbol are shown as binary+offset when the binary is known.
"""
import collections
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
topn = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 25
tfilter = sys.argv[sys.argv.index("--thread") + 1] if "--thread" in sys.argv else None
pfilter = sys.argv[sys.argv.index("--pid") + 1] if "--pid" in sys.argv else None

ids = {}
self_t = collections.Counter()
incl_t = collections.Counter()
thread_t = collections.Counter()
bin_t = collections.Counter()
stacks = collections.Counter()
total = 0


def resolve(el):
    r = el.get("ref")
    return ids.get(r, el) if r else el


def frame_name(fr):
    fr = resolve(fr)
    name = fr.get("name") or ""
    b = fr.find("binary")
    bname = resolve(b).get("name") if b is not None else "?"
    if not name or name.startswith("0x"):
        addr = fr.get("addr", "")
        load = resolve(b).get("load-addr") if b is not None else None
        try:
            off = int(addr, 16) - int(load, 16) if load else None
        except ValueError:
            off = None
        name = f"{bname}+0x{off:x}" if off is not None else f"{bname}!{addr}"
    return name, bname


for ev, el in ET.iterparse(path, events=("end",)):
    if el.get("id"):
        ids[el.get("id")] = el
    if el.tag != "row":
        continue
    w = el.find("weight")
    weight = int(resolve(w).text) if w is not None and resolve(w).text else 1
    th = el.find("thread")
    tname = ""
    if th is not None:
        th = resolve(th)
        tname = th.get("fmt") or ""
    if tfilter and tfilter not in tname:
        continue
    if pfilter:
        pr = el.find("process")
        pfmt = resolve(pr).get("fmt", "") if pr is not None else ""
        if f"({pfilter})" not in pfmt:
            continue
    bt = el.find("tagged-backtrace")
    if bt is None:
        bt = el.find("backtrace")
    if bt is None:
        continue
    bt = resolve(bt)
    inner = bt.find("backtrace")
    if inner is not None:
        bt = resolve(inner)
    frames = [frame_name(f) for f in bt.iter("frame")]
    if not frames:
        continue
    total += weight
    thread_t[tname] += weight
    self_t[frames[0][0]] += weight
    bin_t[frames[0][1]] += weight
    for n in set(f[0] for f in frames):
        incl_t[n] += weight
    stacks[" <- ".join(f[0] for f in frames[:8])] += weight

ms = lambda w: w / 1e6   # weights are ns
print(f"samples total {ms(total):.0f} ms")
print("\n== threads")
for t, w in thread_t.most_common(12):
    print(f"  {ms(w):8.0f} ms {100 * w / total:5.1f}%  {t[:110]}")
print("\n== self time by binary")
for b, w in bin_t.most_common(12):
    print(f"  {ms(w):8.0f} ms {100 * w / total:5.1f}%  {b}")
print(f"\n== self time by function (top {topn})")
for f, w in self_t.most_common(topn):
    print(f"  {ms(w):8.0f} ms {100 * w / total:5.1f}%  {f[:150]}")
print(f"\n== inclusive time by function (top {topn})")
for f, w in incl_t.most_common(topn):
    print(f"  {ms(w):8.0f} ms {100 * w / total:5.1f}%  {f[:150]}")
print("\n== hottest stacks (leaf first, 8 frames)")
for s, w in stacks.most_common(12):
    print(f"  {ms(w):8.0f} ms {100 * w / total:5.1f}%  {s[:400]}")
