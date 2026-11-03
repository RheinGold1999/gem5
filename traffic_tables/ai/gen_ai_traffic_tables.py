#!/usr/bin/env python3
"""Generate AI-workload traffic tables for `--synthetic=table`.

Each scenario distills the *steady-state network traffic* of a real,
published AI system into per-(src,dst) packet injection rates (PIR,
packets/cycle). One "packet" is one AIGCIC_NOC request message
(128 B payload by default => 1 data flit + 1 head flit on a 1024-bit
link), so a source whose PIRs sum to R offers R*128 B/cycle of payload.

Mesh mapping (driver defaults): logical 4x6 active mesh on a 4x8 fabric,
router ids row-major with numColumns = 8:

      col:  0   1   2   3   4   5
    row 0:  0   1   2   3   4   5
    row 1:  8   9  10  11  12  13
    row 2: 16  17  18  19  20  21
    row 3: 24  25  26  27  28  29

Ids 6,7,14,15,22,23 (columns 6-7 of the fabric) are inactive and must
never appear in a table.

Every generated table is validated the same way the gem5 tester
validates it (active ids, no self-loops, no duplicate pairs, pir in
[0,1], per-source pir sum <= 1) and a load summary is printed.

Usage:  python3 gen_ai_traffic_tables.py [output_dir]
"""
import os
import sys
from collections import defaultdict

FABRIC_COLS = 8
ROWS, COLS = 4, 6  # active region


def nid(row, col):
    assert 0 <= row < ROWS and 0 <= col < COLS
    return row * FABRIC_COLS + col


ACTIVE = sorted(nid(r, c) for r in range(ROWS) for c in range(COLS))

# ---------------------------------------------------------------------------
# Scenario definitions.  Each returns (flows, comment_lines) where a flow is
# (src_id, dst_id, pir).
# ---------------------------------------------------------------------------


def ring_edges(order):
    """Directed ring edges over `order` (closed)."""
    n = len(order)
    return [(order[i], order[(i + 1) % n]) for i in range(n)]


def snake_ring():
    """Single snake ring over all 24 active nodes.

    Ring edges are 1 hop except the wrap 24 -> 0 (3 hops up column 0).
    """
    order = []
    for r in range(ROWS):
        row = [nid(r, c) for c in range(COLS)]
        order += row if r % 2 == 0 else row[::-1]
    assert len(order) == ROWS * COLS
    return order


def sc_dp_ring_allreduce():
    """Data-parallel ring all-reduce, one global ring of 24 GPUs.

    Real system: Baidu ring all-reduce / NCCL ring (Patarasuk & Yuan 2009;
    Baidu 2017) as used in plain data-parallel SGD (ResNet, GPT pretraining).
    Each step every worker streams one S/N gradient chunk to its ring
    successor; reduce-scatter + all-gather = 2*(N-1) steps, same direction.
    Steady state: one flow per source (its ring successor), equal PIR.
    """
    r = 0.30
    flows = [(s, d, r) for s, d in ring_edges(snake_ring())]
    note = [
        "DP ring all-reduce, single global ring of 24 nodes (snake order).",
        "Anchor: Baidu ring all-reduce / NCCL ring; data-parallel SGD.",
        "Per-pair PIR = %.2f pkts/cyc (1 gradient chunk per step)." % r,
    ]
    return flows, note


def sc_tp3_local_allreduce():
    """Tensor-parallel all-reduce inside 8 local groups of 3.

    Real system: Megatron-LM (Shoeybi et al., 2019): tensor-parallel
    transformer runs 2 all-reduces per layer (forward + backward); the TP
    group is sized to one NVLink domain.  Here a TP group = 3 adjacent
    tiles of a row segment (8 groups), i.e. TP traffic that spills onto the
    die mesh.  Ring all-reduce within each group => 1 flow per source.
    """
    r = 0.40
    flows = []
    for row in range(ROWS):
        for half in (0, 1):
            g = [nid(row, 3 * half + c) for c in range(3)]
            flows += [(s, d, r) for s, d in ring_edges(g)]
    note = [
        "TP=3 tensor-parallel ring all-reduce, 8 groups (row segments).",
        "Anchor: Megatron-LM 2 all-reduces per layer within the TP group.",
        "Per-pair PIR = %.2f pkts/cyc; all traffic stays inside row"
        " segments, vertical links idle." % r,
    ]
    return flows, note


def sc_pp4_1f1b_pipeline():
    """Pipeline-parallel activations/gradients, PP=4 stages x DP=6.

    Real system: Megatron-LM 1F1B (PipeDream-Flush) / GPipe: in steady
    state each stage boundary carries forward activations (stage i ->
    i+1) and backward activation-gradients (stage i+1 -> i) for a stream
    of microbatches; the two directions have similar message sizes.
    Mapping: stage = mesh row (4 stages), column = DP replica (6
    independent microbatch pipelines).
    """
    f = b = 0.20
    flows = []
    for c in range(COLS):
        for row in range(ROWS - 1):
            flows.append((nid(row, c), nid(row + 1, c), f))  # activations
            flows.append((nid(row + 1, c), nid(row, c), b))  # gradients
    note = [
        "PP=4 pipeline (stage = mesh row) x DP=6 (column = replica), 1F1B.",
        "Anchor: Megatron-LM 1F1B / PipeDream-Flush; fwd act %.2f, bwd grad"
        " %.2f pkts/cyc." % (f, b),
        "Pure vertical column traffic; horizontal links idle.",
    ]
    return flows, note


def _moe_alltoall(flows, note):
    return flows, note


def sc_moe_alltoall_uniform():
    """MoE / DLRM all-to-all, balanced.

    Real systems: Switch Transformer & GShard expert-parallel all-to-all
    (token dispatch) with balanced routing; Meta DLRM's all-to-all
    embedding shuffle.  Every EP rank splits its token batch over all
    other ranks: 23 uniform flows per source.
    """
    r_total = 0.40
    p = round(r_total / (len(ACTIVE) - 1), 6)
    flows = []
    for s in ACTIVE:
        for d in ACTIVE:
            if d != s:
                flows.append((s, d, p))
    note = [
        "MoE expert-parallel all-to-all (dispatch), balanced routing.",
        "Anchor: Switch Transformer / GShard all-to-all; Meta DLRM",
        "embedding all-to-all. %d uniform flows per source, PIR %.6f"
        " each (sum %.4f)." % (len(ACTIVE) - 1, p, p * (len(ACTIVE) - 1)),
        "Combine phase doubles the volume: rerun with --table-scale=2.",
    ]
    return flows, note


def sc_moe_alltoall_hot_experts():
    """MoE all-to-all with hot experts (measured load imbalance).

    Real systems: DeepSeek-V3 / Mixtral-style MoE: a small fraction of
    experts absorbs a disproportionate share of tokens (the reason
    DeepSeek-V3 carries expert-bias load balancing).  Model: each source
    spreads 75% of its dispatch rate uniformly over all 23 other ranks
    and concentrates 25% on 4 "hot" expert ranks => hot ranks receive
    ~2.2x the mean rank rate (0.44 vs 0.20; 2.9x the non-hot ranks'
    0.15).
    """
    r_total = 0.20
    hot_frac = 0.25
    hot = [nid(0, 2), nid(1, 5), nid(2, 2), nid(3, 3)]
    base = round(r_total * (1 - hot_frac) / (len(ACTIVE) - 1), 6)
    extra = round(r_total * hot_frac / len(hot), 6)
    flows = []
    for s in ACTIVE:
        for d in ACTIVE:
            if d == s:
                continue
            flows.append((s, d, round(base + (extra if d in hot else 0), 6)))
    note = [
        "MoE all-to-all with hot experts (dispatch), total %.2f pkts/cyc"
        " per source." % r_total,
        "Anchor: DeepSeek-V3 / Mixtral expert load imbalance.",
        "Hot expert ranks: %s receive base %.6f + extra %.6f; others"
        " %.6f." % (hot, base, extra, base),
    ]
    return flows, note


def sc_dp_hierarchical_allreduce():
    """Two-level hierarchical all-reduce (intra-row + leader ring).

    Real systems: NCCL hierarchical all-reduce, BlueConnect (Cai et al.,
    2019), Horovod hierarchical: first reduce inside the fast local
    domain, then all-reduce the domain aggregates across domains.
    Mapping: level 1 = ring within each row of 6; level 2 = ring over the
    4 column-0 leaders.  Local leg carries ~4x the global leg.
    """
    r_loc, r_glob = 0.30, 0.075
    flows = []
    for row in range(ROWS):
        rowring = [nid(row, c) for c in range(COLS)]
        flows += [(s, d, r_loc) for s, d in ring_edges(rowring)]
    leaders = [nid(r, 0) for r in range(ROWS)]
    flows += [(s, d, r_glob) for s, d in ring_edges(leaders)]
    note = [
        "Two-level hierarchical DP all-reduce.",
        "Anchor: NCCL hierarchical / BlueConnect / Horovod.",
        "Local ring (row of 6) PIR %.3f; leader ring (column 0) PIR"
        " %.3f." % (r_loc, r_glob),
    ]
    return flows, note


def sc_llama3_4d_composite():
    """Composite 4D-parallel training snapshot (TP + PP + DP).

    Real system: LLaMA 3 pretraining ("The Llama 3 Herd of Models",
    2024): TP x CP x PP x DP; per transformer layer TP runs 2 all-reduces
    (most frequent), each PP boundary streams activations/gradients per
    microbatch, and DP all-reduces once per optimizer step (lowest rate).
    Scaled snapshot on 24 tiles: TP=3 (row segments), PP=4 (rows),
    DP=2 (row pairs 0-1 vs 2-3), CP=1.
    """
    r_tp, r_pp, r_dp = 0.25, 0.06, 0.02
    flows = []
    # TP: ring all-reduce inside each row segment of 3 (2 ARs per layer).
    for row in range(ROWS):
        for half in (0, 1):
            g = [nid(row, 3 * half + c) for c in range(3)]
            flows += [(s, d, r_tp) for s, d in ring_edges(g)]
    # PP: activations down, gradients up, between adjacent rows.
    for c in range(COLS):
        for row in range(ROWS - 1):
            flows.append((nid(row, c), nid(row + 1, c), r_pp))
            flows.append((nid(row + 1, c), nid(row, c), r_pp))
    # DP: pairwise gradient exchange between the 2 replica groups.
    for c in range(COLS):
        for row in (0, 1):
            flows.append((nid(row, c), nid(row + 2, c), r_dp))
            flows.append((nid(row + 2, c), nid(row, c), r_dp))
    note = [
        "4D-parallel training composite: TP=3 + PP=4 + DP=2 on 24 tiles.",
        "Anchor: LLaMA 3 pretraining parallelism mix (TP x CP x PP x DP).",
        "Rates: TP all-reduce %.2f (2/layer), PP act/grad %.2f each way,"
        " DP all-reduce %.2f (per step)." % (r_tp, r_pp, r_dp),
    ]
    return flows, note


def sc_pd_disagg_kv_transfer():
    """Disaggregated prefill/decode inference: KV-cache transfer.

    Real systems: Mooncake (Moonshot/Kimi KVCache-centric disaggregation),
    DistServe / vLLM split-prefill-decode: long prompts are prefilled on a
    prefill pool, the KV cache is shipped across the fabric to the decode
    instance that continues the request; decode's own NoC traffic (small
    tokens, results leaving via NICs) is comparatively negligible.
    Mapping: rows 0-1 = prefill pool, rows 2-3 = decode pool; each
    prefill node spreads its KV stream uniformly over the 12 decode
    nodes (Mooncake-style pool routing, session-affinity averaged out).
    """
    r_kv = 0.20
    prefill = [nid(r, c) for r in (0, 1) for c in range(COLS)]
    decode = [nid(r, c) for r in (2, 3) for c in range(COLS)]
    p = round(r_kv / len(decode), 6)
    flows = [(s, d, p) for s in prefill for d in decode]
    note = [
        "Disaggregated inference: prefill(rows 0-1) -> KV-cache ->"
        " decode(rows 2-3).",
        "Anchor: Mooncake (Kimi), DistServe split prefill/decode.",
        "Each prefill node: %d flows x %.6f (sum %.3f).  At full"
        " acceptance each row1->row2 corridor link carries %.2f flits/cyc"
        " southbound (GETX %.2f + PUTX %.2f = 4x R_KV); the Data/WB_ACK"
        " responses return northbound, so both directions are loaded." %
        (len(decode), p, p * len(decode), 4 * r_kv, 2 * r_kv, 2 * r_kv),
    ]
    return flows, note


SCENARIOS = {
    "dp_ring_allreduce_24.tbl": sc_dp_ring_allreduce,
    "tp3_local_allreduce.tbl": sc_tp3_local_allreduce,
    "pp4_1f1b_pipeline.tbl": sc_pp4_1f1b_pipeline,
    "moe_alltoall_uniform.tbl": sc_moe_alltoall_uniform,
    "moe_alltoall_hot_experts.tbl": sc_moe_alltoall_hot_experts,
    "dp_hierarchical_allreduce.tbl": sc_dp_hierarchical_allreduce,
    "llama3_4d_composite.tbl": sc_llama3_4d_composite,
    "pd_disagg_kv_transfer.tbl": sc_pd_disagg_kv_transfer,
}

# Transaction type per table, written as the 4th 'rw' column of every
# flow line.  "W" = write: the fetched line is written back on
# replacement (GETX + Data + PUTX + WB_ACK = 4 msgs/access) — matches
# the pre-rw-column baselines in log/aigcic_noc/ai_workloads/.  "R" =
# read: fetch-only (GETX + Data = 2 msgs/access; the clean line is
# dropped silently on replacement).  To model a read-dominated phase
# (e.g. decode-side traffic), switch that scenario's entry to "R" or
# extend this to a per-flow mapping inside its sc_* generator.
RW_TYPES = {fname: "W" for fname in SCENARIOS}


def validate(flows, fname):
    seen = set()
    src_sum = defaultdict(float)
    dst_sum = defaultdict(float)
    for s, d, p in flows:
        for n in (s, d):
            if n not in ACTIVE:
                raise ValueError("%s: id %d inactive" % (fname, n))
        if s == d:
            raise ValueError("%s: self-loop %d" % (fname, s))
        if (s, d) in seen:
            raise ValueError("%s: duplicate (%d,%d)" % (fname, s, d))
        if not 0.0 <= p <= 1.0:
            raise ValueError("%s: pir %g outside [0,1]" % (fname, p))
        seen.add((s, d))
        src_sum[s] += p
        dst_sum[d] += p
    for s, tot in src_sum.items():
        if tot > 1.0 + 1e-9:
            raise ValueError("%s: src %d pir sum %g > 1" % (fname, s, tot))
    return src_sum, dst_sum


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.abspath(__file__))
    os.makedirs(outdir, exist_ok=True)
    print("active mesh: %d nodes, ids %s" % (len(ACTIVE), ACTIVE))
    for fname, gen in sorted(SCENARIOS.items()):
        flows, note = gen()
        src_sum, dst_sum = validate(flows, fname)
        path = os.path.join(outdir, fname)
        with open(path, "w") as f:
            f.write("# %s\n" % fname)
            for line in note:
                f.write("# %s\n" % line)
            f.write("# rw column (3rd): R = read (fetch-only, GETX+Data); "
                    "W = write (fire-and-forget, PUTX+WB_ACK)\n")
            f.write("# src dst rw pir\n")
            rw = RW_TYPES[fname]
            for s, d, p in flows:
                f.write("%d %d %s %.6f\n" % (s, d, rw, p))
        print("%-32s %4d flows | src load max %.3f avg %.3f | dst recv"
              " max %.3f avg %.3f" % (
                  fname, len(flows), max(src_sum.values()),
                  sum(src_sum.values()) / len(src_sum),
                  max(dst_sum.values()),
                  sum(dst_sum.values()) / len(dst_sum)))


if __name__ == "__main__":
    main()
