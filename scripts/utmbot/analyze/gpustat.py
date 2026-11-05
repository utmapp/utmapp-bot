#!/usr/bin/env python3
"""gpustat.py METAL-GPU-INTERVALS.xml: GPU busy time per channel, and per process and
channel (depth-0 intervals), from an `xctrace export` of a Metal System Trace."""
import collections, sys, xml.etree.ElementTree as ET
ids = {}; dur = collections.Counter(); cnt = collections.Counter(); span = [None, None]
busy = collections.defaultdict(list)
def r(e):
    return ids.get(e.get("ref"), e) if e.get("ref") else e
for ev, el in ET.iterparse(sys.argv[1], events=("end",)):
    if el.get("id"): ids[el.get("id")] = el
    if el.tag != "row": continue
    c = [r(x) for x in el]
    if len(c) < 11: continue
    start, d = int(c[0].text or 0), int(c[1].text or 0)
    ch, depth = c[2].get("fmt", "?"), c[5].text
    proc = c[10].get("fmt", "?") if c[10].tag != "sentinel" else "(unattributed)"
    if depth != "0": continue
    k = (proc, ch); dur[k] += d; cnt[k] += 1; busy[ch].append((start, start + d))
    span[0] = start if span[0] is None else min(span[0], start); span[1] = max(span[1] or 0, start + d)
tot = (span[1] - span[0]) if span[0] is not None else 1
print(f"window {tot/1e9:.2f} s")
for ch, iv in busy.items():
    iv.sort(); u = 0; cs = ce = None
    for s, e in iv:
        if cs is None or s > ce:
            if cs is not None: u += ce - cs
            cs, ce = s, e
        else: ce = max(ce, e)
    u += (ce - cs) if cs is not None else 0
    print(f"  channel {ch:10s} busy {100*u/tot:5.1f}% of the window")
print("per process/channel (sum of depth-0 GPU intervals):")
for (p, ch), d in dur.most_common(20):
    print(f"  {d/1e6:9.1f} ms  {100*d/tot:5.1f}%  n={cnt[(p,ch)]:6d}  {ch:10s} {p}")
