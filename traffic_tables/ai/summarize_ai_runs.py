#!/usr/bin/env python3
"""Summarize the AI-workload reference runs into a markdown table.

Usage: python3 summarize_ai_runs.py [log/aigcic_noc/ai_workloads]
"""
import os
import re
import subprocess
import sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "log/aigcic_noc/ai_workloads"
SCENARIOS = [
    ("dp_ring_allreduce_24", 0.30),
    ("tp3_local_allreduce", 0.40),
    ("pp4_1f1b_pipeline", 0.40),
    ("moe_alltoall_uniform", 0.40),
    ("moe_alltoall_hot_experts", 0.20),
    ("dp_hierarchical_allreduce", 0.375),
    ("llama3_4d_composite", 0.39),
    ("pd_disagg_kv_transfer", 0.20),
]
NODE_CYCLES = 24 * 200000.0

rows = []
for name, offered in SCENARIOS:
    d = os.path.join(BASE, name)
    st = os.path.join(d, "stats.txt")
    if not os.path.exists(st):
        rows.append((name, "MISSING"))
        continue
    txt = open(st).read()

    def val(pattern):
        m = re.search(pattern, txt)
        return float(m.group(1)) if m else float("nan")

    def val0(pattern):
        # Message counters that may be entirely absent (gem5 omits
        # all-zero stats, e.g. GETX in an all-write table).
        m = re.search(pattern, txt)
        return float(m.group(1)) if m else 0.0

    # Accepted accesses: reads issue GETX, writes issue PUTX directly
    # (fire-and-forget, no fetch), so both counters must be summed.
    getx = val0(r"Directory_Controller\.GETX::total\s+(\S+)") \
        + val0(r"Directory_Controller\.GETX_XD::total\s+(\S+)")
    putx = val0(r"Directory_Controller\.PUTX::total\s+(\S+)") \
        + val0(r"Directory_Controller\.PUTX_XD::total\s+(\S+)")
    acc = getx + putx
    pkts = val(r"network\.packets_injected::total\s+(\S+)")
    ana = subprocess.run(
        ["python3", "perf_analysis/analyze_mesh_bw.py", st],
        capture_output=True, text=True).stdout
    hot = re.findall(r"(\d+\.\d+)% \(\s*\d+\.\d+ flits/cycle\)",
                     ana.split("10 busiest internal links")[1])
    netmed = re.search(r"network part MEDIAN\s*:\s*(\d+)", ana)
    med = re.search(r"flit lifetime MEDIAN\s*:\s*(\d+)", ana)
    p75 = re.search(r"flit lifetime P75\s*:\s*(\d+)", ana)
    rows.append((
        name, offered,
        acc / NODE_CYCLES, pkts / NODE_CYCLES,
        int(netmed.group(1)) if netmed else -1,
        int(med.group(1)) if med else -1,
        int(p75.group(1)) if p75 else -1,
        max(float(h) for h in hot) if hot else float("nan"),
    ))
    top = re.search(r"10 busiest internal links.*?\n  (.+)\n", ana)
    if top:
        rows[-1] = rows[-1] + (top.group(1).strip(),)

hdr = ("| scenario | offered/src | acc. access (pkts/cyc/node) | "
       "acc. msgs (pkts/cyc/node) | NoC-lat p50 | lifetime p50 | "
       "lifetime p75 | hottest link |")
print(hdr)
print("|---|---|---|---|---|---|---|---|")
for r in rows:
    if len(r) == 2:
        print("| %s | %s |" % (r[0], r[1]))
        continue
    name, offered, acc, msgs, nmed, med, p75, hot = r[:8]
    top = r[8] if len(r) > 8 else ""
    print("| %s | %.3f | %.3f | %.3f | %d | %d | %d | %.1f%% |"
          % (name, offered, acc, msgs, nmed, med, p75, hot))
    if top:
        print("|  └ hottest: `%s` |" % top)
