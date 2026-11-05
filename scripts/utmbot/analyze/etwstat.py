#!/usr/bin/env python3
"""etwstat.py DUMP.txt PROCESS-SUBSTRING [TOPN]

Reads `xperf -a dumper -symbols` output (CSwitch/ReadyThread with stacks,
DxgKrnl QueuePacket/DmaPacket) and reports, for the process:
  - on-CPU time per thread (from CSwitch intervals)
  - waits: total wait time grouped by the switched-in thread's stack (where
    it had blocked), collapsed to the frames that matter
  - who readied it (ReadyThread stacks of the readying thread)
  - GPU: DMA packet busy time, queue latency, presents/s
"""
import collections
import re
import sys

dump, proc = sys.argv[1], sys.argv[2]
topn = int(sys.argv[3]) if len(sys.argv) > 3 else 15

oncpu = collections.Counter()          # tid -> us
running = {}                           # cpu -> (tid, pid_name, t)
waits = collections.Counter()          # stack key -> us
wait_n = collections.Counter()
readiers = collections.Counter()       # readier stack key -> count
thread_wait = collections.Counter()    # tid -> us waited
ready_at = {}                          # tid -> ts it became ready
out_at = {}                            # tid -> ts it was switched out
ready_us = collections.Counter()       # tid -> us spent ready (runnable, no vCPU)
waits_by_tid = collections.defaultdict(collections.Counter)
first_ts = last_ts = None
dma_start = {}                         # (ctx, seq) -> ts
dma_busy = 0
queue_start = {}                       # (ctx, seq) -> ts
queue_lat = []
presents = 0
present_ts = []

pending = None   # ("cswitch"|"ready", ts, key_meta) collecting a stack
frames = []

# ETW's own stack walk and the scheduler: not the reason for the wait
SKIP = re.compile(r"^(ntoskrnl\.exe!(Etw|KeExpandKernelStack|SwapContext|Ki|Kei|KeWaitFor|KeSetEvent|KeAlertThread|"
                  r"ObWaitFor|NtWaitFor|NtSetEvent|NtAlertThread|KeDelayExecution|KeYieldExecution|ExpWaitFor)|"
                  r"ntdll\.dll!#?(Nt|Zw)|KernelBase\.dll!\$ientry|kernel32\.dll!\$ientry)")


def key_of(fr):
    """Collapse a stack (outermost last) to the 6 most informative frames."""
    keep = [f for f in fr if f and "!" in f and not SKIP.match(f)]
    if not keep:
        # Only ETW's own stack walk / scheduler frames, or no stack at all:
        # the walk never reached the caller, so the wait is unattributed.
        return "(no caller stack: the ETW stack walk did not reach it)"
    return " <- ".join(keep[:6])


def flush():
    global pending, frames
    if pending:
        kind, ts, meta = pending
        k = key_of(frames)
        if kind == "cswitch":
            waits[k] += meta[0]
            wait_n[k] += 1
            waits_by_tid[meta[1]][k] += meta[0]
        elif kind == "ready":
            readiers[k] += 1
    pending, frames = None, []


with open(dump, "r", encoding="ascii", errors="replace") as f:
    for line in f:
        if not line.startswith(" ") and not line.startswith("Microsoft"):
            continue
        parts = [p.strip() for p in line.rstrip("\r\n").split(",")]
        ev = parts[0]
        if ev == "Stack":
            if pending and len(parts) >= 6 and parts[1] != "TimeStamp":
                frames.append(parts[5])
            continue
        flush()
        if len(parts) < 3 or parts[1] == "TimeStamp":
            continue
        try:
            ts = int(parts[1])
        except ValueError:
            continue
        if ev == "CSwitch":
            first_ts = ts if first_ts is None else first_ts
            last_ts = ts
            new_name, new_tid, wait_us = parts[2], parts[3], int(parts[7] or 0)
            old_tid, cpu = parts[9], parts[16]
            if proc in parts[8]:
                out_at[old_tid] = ts
            prev = running.get(cpu)
            if prev and proc in prev[1]:
                oncpu[prev[0]] += ts - prev[2]
            running[cpu] = (new_tid, new_name, ts)
            if proc in new_name:
                r = ready_at.pop(new_tid, None)
                if r is not None:
                    ready_us[new_tid] += ts - r
                o = out_at.pop(new_tid, None)
                if o is not None:
                    off = ts - o          # off-CPU interval, ready time included
                    thread_wait[new_tid] += off
                    pending = ("cswitch", ts, (off, new_tid))
        elif ev == "ReadyThread":
            if proc in parts[4]:
                ready_at.setdefault(parts[5], ts)
                pending = ("ready", ts, None)
        elif ev.startswith("Microsoft") and len(parts) < 14:
            continue
        elif ev.endswith("DmaPacket/win:Start"):
            dma_start[(parts[9], parts[13])] = ts
        elif ev.endswith("DmaPacket/win:Stop"):
            st = dma_start.pop((parts[9], parts[12]), None)
            if st is not None:
                dma_busy += ts - st
        elif ev.endswith("QueuePacket/win:Start"):
            queue_start[(parts[9], parts[11])] = ts
            if len(parts) > 15 and parts[15] == "true":
                presents += 1
                present_ts.append(ts)
        elif ev.endswith("QueuePacket/win:Stop"):
            st = queue_start.pop((parts[9], parts[11]), None)
            if st is not None:
                queue_lat.append(ts - st)
flush()

span = (last_ts - first_ts) / 1e6 if first_ts is not None else 0
if span <= 0:
    sys.exit("no context switches in the trace: the kernel logger did not record "
             "(CSwitch events missing), so there is nothing to summarize")
print(f"trace span {span:.2f} s (CSwitch timestamps)")
print(f"\n== on-CPU time per {proc} thread (ms, % of one core)")
for tid, us in oncpu.most_common(topn):
    print(f"  tid {tid:>6}  on-CPU {us / 1000:8.1f} ms {100 * us / 1e6 / span:5.1f}%   ready (no vCPU) {ready_us[tid] / 1000:8.1f} ms   blocked {max(0, span * 1e6 - us - ready_us[tid]) / 1000:8.1f} ms")
print(f"  total {sum(oncpu.values()) / 1000:.1f} ms = {sum(oncpu.values()) / 1e6 / span:.2f} cores")
print(f"\n== where {proc} threads waited (by wait stack), top {topn}")
for k, us in waits.most_common(topn):
    print(f"  {us / 1000:9.1f} ms  n={wait_n[k]:6d}  {k[:300]}")
print(f"\n== who readied {proc} threads (readier stack), top {topn}")
for k, n in readiers.most_common(topn):
    print(f"  n={n:6d}  {k[:300]}")
for tid, _ in oncpu.most_common(3):
    print(f"\n== thread {tid}: off-CPU by wait stack (ms)")
    for k, us in waits_by_tid[tid].most_common(6):
        print(f"  {us / 1000:9.1f}  {k[:300]}")
print("\n== GPU (DxgKrnl)")
print(f"  DMA busy {dma_busy / 1e6:.2f} s over {span:.2f} s = {100 * dma_busy / 1e6 / span:.0f}% (sum over contexts)")
if queue_lat:
    queue_lat.sort()
    q = lambda p: queue_lat[int(p * (len(queue_lat) - 1))] / 1000
    print(f"  queue packet latency (submit->done) ms: p50 {q(.5):.2f} p90 {q(.9):.2f} p99 {q(.99):.2f} n={len(queue_lat)}")
if len(present_ts) > 1:
    gaps = sorted(b - a for a, b in zip(present_ts, present_ts[1:]))
    g = lambda p: gaps[int(p * (len(gaps) - 1))] / 1000
    print(f"  presents {presents} = {presents / span:.1f}/s; interval ms p50 {g(.5):.2f} p90 {g(.9):.2f} p99 {g(.99):.2f} max {gaps[-1] / 1000:.1f}")
