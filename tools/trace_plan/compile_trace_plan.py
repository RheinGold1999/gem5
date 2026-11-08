#!/usr/bin/env python3
# Copyright (c) 2025 AIGCIC_NOC project.
#
# Compile a noc-trace window (traffic/trace/noc-trace-4x6/<window>/) into the
# gem5 trace-replay plan consumed by the GarnetNetwork relay hook, the
# per-router testers and the global trace scheduler.
#
# Replay model (noc-trace-format.md + design notes):
#   * Every trace record is one hop: router -> next(adjacent).
#   * Records with a same-msg upstream arrival (upstream.next == router) are
#     RELAYS ('F'): executed in-network by the router hook (dst rewrite before
#     route computation, cut-through at head-flit arrival).
#   * Records without a same-msg upstream (send role, reduce-tree sources,
#     second source branches) are TESTER-INJECTED ('T'/'R'): one write request
#     into the NI from the owner router's local port.
#   * An arrival triggering k>1 continuations is a BRANCH (in-router flit
#     replication).  A continuation with m>1 upstreams is a MERGE (hold all
#     member streams, rewrite the last-arriving one, drain the rest; no
#     same-msg pipelining exemption at merges).
#   * tap: the arriving router itself is a message destination -> eject one
#     copy to the local dir while the stream continues.
#   * Injection discipline (tester): one burst group per router at a time;
#     same-msg recs pipeline; switching msg waits for all in-flight recs to
#     reach arrival completion (rule b).  Serialization covers send recs only
#     (rule a).
#   * Deadlock: trace paths bend (axis change / reversal) at most once per
#     branch, so 2 VC classes with "bend => class upgrade" (dateline) are
#     deadlock-free.  The class-aware channel-dependency graph is checked
#     here and must be acyclic.
#
# Output: <out>/plan.txt        (one line per record, C++-parseable)
#         <out>/plan_report.txt (validation + stats)

import argparse
import csv
import json
import os
import sys
from collections import defaultdict, Counter

FLIT_BYTES_DEFAULT = 128
FABRIC_COLS = 8  # 4x8 fabric, active 4x6; router id = row * FABRIC_COLS + col


def fatal(msg):
    print("FATAL:", msg)
    sys.exit(1)


def rc(name):
    """'r2c3' -> (2, 3)"""
    return int(name[1]), int(name[3])


def fabric_id(name):
    r, c = rc(name)
    return r * FABRIC_COLS + c


def flits(nbytes, flit_bytes):
    return max(1, -(-nbytes // flit_bytes))


class Rec:
    __slots__ = (
        "idx", "rid", "router", "router_name", "pkind", "mkind", "role",
        "seq", "next", "next_name", "bytes", "dst", "msg", "lclock",
        "stage", "op", "ups", "afters_ext", "cls", "tap", "afters_raw",
        "absorb", "absorbed_by", "group",
    )

    def __init__(self):
        self.ups = []         # rec indices: same-msg upstream arrivals (listed)
        self.afters_ext = []  # rec indices: external after deps
        self.afters_raw = []
        self.cls = 0
        self.tap = 0
        self.absorb = []      # rec indices absorbed into this merge (chained legs)
        self.absorbed_by = -1  # continuation rec index this rec is drained into
        self.group = []       # full merge group: ups + absorb (only for merges)


def load_records(window):
    path = os.path.join(window, "records.csv")
    with open(path) as f:
        rows = list(csv.DictReader(f))
    recs = []
    for i, r in enumerate(rows):
        rec = Rec()
        rec.idx = i
        rec.rid = r["rec"]
        rec.router_name = r["router"]
        rec.router = fabric_id(r["router"])
        rec.mkind = r["kind"]       # unicast / multicast / reduce / allreduce
        rec.role = r["role"]
        rec.seq = int(r["seq"])
        rec.next_name = r["next"]
        rec.next = fabric_id(r["next"])
        rec.bytes = int(r["bytes"])
        rec.dst = set(x for x in r["dst"].split(";") if x)
        rec.msg = r["msg"]
        rec.lclock = int(r["lclock"])
        rec.stage = r["stage"]
        rec.op = r["op"]
        rec.afters_raw = [t for t in r["after"].split(";") if t]
        recs.append(rec)
    return recs


def by_msg_of(recs):
    d = defaultdict(list)
    for r in recs:
        d[r.msg].append(r)
    return d


def check_ids(recs):
    for i, r in enumerate(recs):
        if r.rid != "t%06d" % (i + 1):
            fatal("rec ids not sequential at index %d: %s" % (i, r.rid))


def check_msg_consistency(recs, msgs):
    # one byte count and one dst set per message (every hop carries the same)
    for m, group in msgs.items():
        bs = set(x.bytes for x in group)
        if len(bs) != 1:
            fatal("msg %s has varying per-hop bytes: %s" % (m, bs))
        ds = set(frozenset(x.dst) for x in group)
        if len(ds) != 1:
            fatal("msg %s has varying dst sets" % m)
    for r in recs:
        for d in r.dst:
            if not (d.startswith("r") and len(d) == 4):
                fatal("bad dst token %r in %s" % (d, r.rid))


def check_adjacency(recs):
    for r in recs:
        a, b = rc(r.router_name), rc(r.next_name)
        if abs(a[0] - b[0]) + abs(a[1] - b[1]) != 1:
            fatal("rec %s: %s -> %s not adjacent" % (r.rid, r.router_name, r.next_name))


def check_lclock(recs, by_id):
    # lclock == max(after lclock) + 1 (1 if none): the trace's own definition
    for r in recs:
        want = (max(by_id[t].lclock for t in r.afters_raw) + 1) \
            if r.afters_raw else 1
        if want != r.lclock:
            fatal("rec %s lclock %d != derived %d" % (r.rid, r.lclock, want))


def classify(recs, by_id):
    """Split after refs into same-msg upstream arrivals vs external deps and
    classify: F = relay (hook), T = tester send, R = tester root injection.
    Records the analysis model labels fwd/reduce but that lack a same-msg
    upstream arrival are tester roots (verified: second source branches of
    two-level multicast trees)."""
    for r in recs:
        refs = [by_id[t] for t in r.afters_raw]
        ups = [a for a in refs if a.msg == r.msg and a.next == r.router]
        r.ups = sorted(a.idx for a in ups)
        up_set = set(r.ups)
        r.afters_ext = sorted(a.idx for a in refs if a.idx not in up_set)
        if r.ups:
            r.pkind = "F"
            for ai in r.ups:
                if by_id_r(recs, ai).lclock >= r.lclock:
                    fatal("upstream %s lclock >= relay %s lclock (deps not causal)"
                          % (by_id_r(recs, ai).rid, r.rid))
        elif r.role == "send":
            r.pkind = "T"
        else:
            r.pkind = "R"


def by_id_r(recs, idx):
    return recs[idx]


def check_seq(recs, window):
    # per-router seq contiguous; routers/*.csv agrees with records.csv
    by_router = defaultdict(list)
    for r in recs:
        by_router[r.router_name].append(r)
    for name, group in by_router.items():
        seqs = sorted(x.seq for x in group)
        if seqs != list(range(1, len(group) + 1)):
            fatal("router %s seq not contiguous: %s..%s"
                  % (name, seqs[0], seqs[-1]))
        path = os.path.join(window, "routers", name + ".csv")
        with open(path) as f:
            rows = list(csv.DictReader(f))
        rows.sort(key=lambda x: int(x["seq"]))
        mine = sorted(group, key=lambda x: x.seq)
        if len(rows) != len(mine):
            fatal("router file %s has %d rows, records.csv has %d"
                  % (name, len(rows), len(mine)))
        for a, b in zip(rows, mine):
            if a["rec"] != b.rid or int(a["seq"]) != b.seq:
                fatal("router file %s out of sync at %s" % (name, b.rid))


def detect_absorbed(recs, msgs):
    """The trace encodes some merges as dependency chains: two streams arrive
    at router Q, but the continuation X at Q lists only one as its upstream;
    the other leg is transitively reduced out of X's after list.  Physically
    that leg is ABSORBED at Q (drained in-network), not terminal-ejected.
    Detect: A with A.next == X.router, same msg, A reachable from X through
    same-msg after edges (X is in A's forward after-descendant closure),
    A not in X.ups  =>  A is absorbed by X."""
    # per-msg after-children adjacency (rec -> recs listing it in after)
    children = defaultdict(list)
    for r in recs:
        for t in r.afters_raw:
            children[t].append(r.idx)
    # legs already listed as upstreams continue via their continuation --
    # they can never be absorbed members
    in_ups = set()
    for r in recs:
        in_ups.update(r.ups)
    n_abs = 0
    for x in recs:
        if x.pkind != "F":
            continue
        # candidates: same-msg recs arriving at x.router, not listed upstreams
        cands = [a.idx for a in msgs[x.msg]
                 if a.next == x.router and a.idx != x.idx
                 and a.idx not in set(x.ups) and a.idx not in in_ups]
        for ai in cands:
            # forward BFS from candidate over same-msg after-children edges
            seen = {ai}
            stack = [ai]
            found = False
            while stack and not found:
                cur = stack.pop()
                for ci in children[recs[cur].rid]:
                    c = recs[ci]
                    if c.msg != x.msg or ci in seen:
                        continue
                    if ci == x.idx:
                        found = True
                        break
                    seen.add(ci)
                    stack.append(ci)
            if not found:
                continue
            a = recs[ai]
            if a.absorbed_by != -1:
                fatal("rec %s absorbed twice (%s and %s)"
                      % (a.rid, recs[a.absorbed_by].rid, x.rid))
            a.absorbed_by = x.idx
            x.absorb.append(ai)
            n_abs += 1
        x.absorb.sort()
        x.group = sorted(x.ups + x.absorb)
    return n_abs


def dir_of(r):
    a, b = rc(r.router_name), rc(r.next_name)
    return (b[0] - a[0], b[1] - a[1])


def assign_classes(recs):
    """cls = dateline VC class.  root: 0.  relay: max over ALL group members
    (listed upstreams + absorbed legs -- any of them may be the last-arriving
    carrier stream) of cls(member) + bend(dir(X) != dir(member)).  Bend counts
    axis change AND reversal."""
    for r in sorted(recs, key=lambda x: x.lclock):
        if r.pkind in ("T", "R"):
            r.cls = 0
            continue
        dvec = dir_of(r)
        best = None
        for mi in r.group:
            u = recs[mi]
            c = u.cls + (1 if dvec != dir_of(u) else 0)
            best = c if best is None else max(best, c)
        r.cls = best
    return max(r.cls for r in recs)


def check_cdg(recs):
    """Class-aware channel dependency graph must be acyclic.
    Nodes: (src_router, dst_router, cls) directed links.
    Edges: relay X triggered by upstream A => link(A)@cls(A) -> link(X)@cls(X).
    Sends/roots hold no channels before starting (no edges)."""
    edges = defaultdict(set)
    nodes = set()
    for r in recs:
        nodes.add((r.router, r.next, r.cls))
    for r in recs:
        if r.pkind != "F":
            continue
        for mi in r.group:  # listed upstreams AND absorbed legs
            u = recs[mi]
            edges[(u.router, u.next, u.cls)].add((r.router, r.next, r.cls))
    indeg = {n: 0 for n in nodes}
    for a, bs in edges.items():
        for b in bs:
            indeg[b] += 1
    q = [n for n in nodes if indeg[n] == 0]
    seen = 0
    while q:
        n = q.pop()
        seen += 1
        for m in edges.get(n, ()):
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    n_edges = sum(len(v) for v in edges.values())
    return len(nodes) - seen, len(nodes), n_edges


def build_arrival_map(recs):
    """arrival key (upstream rec idx) -> list of continuation rec indices."""
    cont = defaultdict(list)
    for r in recs:
        if r.pkind != "F":
            continue
        for ai in r.ups:
            cont[ai].append(r.idx)
    return cont


def check_absorption(recs):
    # absorbed legs: reduce/allreduce only, must not terminate at a msg dst,
    # must not be a listed upstream of any rec, must not carry a tap;
    # merge continuations must not sit on a dst router (dst arrivals eject)
    in_ups = set()
    for r in recs:
        in_ups.update(r.ups)
    for x in recs:
        if x.pkind != "F" or len(x.group) < 2:
            continue
        if x.router_name in x.dst:
            fatal("merge continuation %s sits on a msg dst router" % x.rid)
        if not x.absorb:
            continue
        if x.mkind not in ("reduce", "allreduce"):
            fatal("absorbed legs under %s msg %s (rec %s): only reductions "
                  "expected" % (x.mkind, x.msg, x.rid))
        for ai in x.absorb:
            a = recs[ai]
            if a.next_name in a.dst:
                fatal("absorbed leg %s terminates at a dst router (%s)"
                      % (a.rid, a.next_name))
            if a.tap:
                fatal("absorbed leg %s flagged tap" % a.rid)
            if ai in in_ups:
                fatal("absorbed leg %s is also a listed upstream elsewhere"
                      % a.rid)


def check_branch_merge(recs, cont):
    # a merge continuation (group>1) must be the ONLY continuation of each of
    # its listed upstreams; an arrival feeding a merge must trigger nothing else
    for r in recs:
        if r.pkind != "F" or len(r.group) < 2:
            continue
        for ai in r.ups:
            if cont[ai] != [r.idx]:
                fatal("merge continuation %s: upstream %s also triggers %s"
                      % (r.rid, recs[ai].rid,
                         [recs[x].rid for x in cont[ai] if x != r.idx]))
    for ai, xs in cont.items():
        merges = [x for x in xs if len(recs[x].group) > 1]
        if len(merges) > 1:
            fatal("arrival %s triggers %d merge continuations"
                  % (recs[ai].rid, len(merges)))
        if merges and len(xs) > 1:
            fatal("arrival %s triggers merge continuation %s plus others %s"
                  % (recs[ai].rid, recs[merges[0]].rid,
                     [recs[x].rid for x in xs if x not in merges]))
    fanout = Counter(len(xs) for xs in cont.values())
    merges = Counter(len(r.group) for r in recs
                     if r.pkind == "F" and len(r.group) > 1)
    return fanout, merges


def set_taps(recs):
    for r in recs:
        r.tap = 1 if (r.pkind == "F" and r.router_name in r.dst) else 0


def reconcile(window, recs, msgs):
    """Rebuild links.csv and summary.json from parsed records; diff exactly."""
    errs = []
    link = defaultdict(lambda: [0, 0])
    for r in recs:
        k = "%s>%s" % (r.router_name, r.next_name)
        link[k][0] += 1
        link[k][1] += r.bytes
    with open(os.path.join(window, "links.csv")) as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(link):
        errs.append("links.csv rows %d != rebuilt %d" % (len(rows), len(link)))
    for row in rows:
        k = row["link"]
        if k not in link:
            errs.append("links.csv extra link %s" % k)
            continue
        if int(row["records"]) != link[k][0] or int(row["bytes"]) != link[k][1]:
            errs.append("link %s: file (%s,%s) != rebuilt (%d,%d)"
                        % (k, row["records"], row["bytes"], link[k][0], link[k][1]))
    with open(os.path.join(window, "summary.json")) as f:
        s = json.load(f)

    def chk(name, got, want):
        if got != want:
            errs.append("summary.%s: %r != %r" % (name, got, want))

    chk("records", len(recs), s["records"])
    chk("messages", len(msgs), s["messages"])
    chk("records_by_kind", dict(Counter(r.mkind for r in recs)),
        s["records_by_kind"])
    chk("messages_by_kind",
        dict(Counter(g[0].mkind for g in msgs.values())),
        s["messages_by_kind"])
    chk("records_by_role", dict(Counter(r.role for r in recs)),
        s["records_by_role"])
    chk("bytes_on_links", sum(r.bytes for r in recs), s["bytes_on_links"])
    chk("lclock_max", max(r.lclock for r in recs), s["lclock_max"])
    chk("messages_by_stage_op",
        dict(Counter("%s/%s" % (g[0].stage, g[0].op) for g in msgs.values())),
        s["messages_by_stage_op"])
    bso = Counter()
    for r in recs:
        bso["%s/%s" % (r.stage, r.op)] += r.bytes
    chk("bytes_by_stage_op", dict(bso), s["bytes_by_stage_op"])
    return errs


def engine_occupancy(recs, cycles_per_wave=15, hop_latency=8, flit_bytes=FLIT_BYTES_DEFAULT):
    """Rule-(b) sanity: per-router injection-engine busy estimate with
    one-burst-group-at-a-time serialization (same-msg group pipelines)."""
    by_router = defaultdict(list)
    for r in recs:
        if r.pkind in ("T", "R"):
            by_router[r.router_name].append(r)
    lclock_max = max(r.lclock for r in recs)
    window = lclock_max * cycles_per_wave
    out = {}
    for rt, group in by_router.items():
        group.sort(key=lambda x: x.seq)
        groups = []
        for r in group:
            if groups and groups[-1][0] == r.msg:
                groups[-1][1].append(r)
            else:
                groups.append([r.msg, [r]])
        busy = sum(sum(flits(x.bytes, flit_bytes) for x in g)
                   for _, g in groups) + (len(groups) - 1) * hop_latency
        out[rt] = busy / window
    return out, window


def emit_plan(out_path, window_name, recs, msgs, flit_bytes, n_routers):
    lines = []
    lines.append("# gem5 AIGCIC_NOC trace-replay plan. Generated by "
                 "tools/trace_plan/compile_trace_plan.py -- DO NOT EDIT.")
    lines.append("# source window: %s" % window_name)
    lines.append("# idx: 0-based record index (rec 'tNNNNNN' -> idx NNNNNN-1);")
    lines.append("#      ups/afters are record indices; routers are fabric ids")
    lines.append("#      (row * fabric_cols + col).  kind: T tester-send, "
                 "R tester-root, F relay.")
    lines.append("# tap: 1 => arriving router is a msg dst: replicate one copy "
                 "to the local dir.")
    lines.append("# cls: dateline VC class (0/1); bend => upgrade.")
    lines.append("META flit_bytes=%d recs=%d msgs=%d routers=%d fabric_cols=%d window=%s"
                 % (flit_bytes, len(recs), len(msgs), n_routers, FABRIC_COLS,
                    window_name))
    lines.append("# idx kind owner seq next bytes tap cls n_ups ups[n_ups] "
                 "n_abs abs[n_abs] n_after afters[n_after] lclock msg op "
                 "stage rec")
    for r in recs:
        fields = ([str(r.idx), r.pkind, str(r.router), str(r.seq),
                   str(r.next), str(r.bytes), str(r.tap), str(r.cls),
                   str(len(r.ups))] +
                  [str(x) for x in r.ups] +
                  [str(len(r.absorb))] +
                  [str(x) for x in r.absorb] +
                  [str(len(r.afters_ext))] +
                  [str(x) for x in r.afters_ext] +
                  [str(r.lclock), r.msg, r.op, r.stage, r.rid])
        lines.append(" ".join(fields))
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Compile a noc-trace window into a gem5 trace-replay plan")
    ap.add_argument("window", help="trace window dir, e.g. "
                                   "traffic/trace/noc-trace-4x6/s1-layer-a")
    ap.add_argument("--out", default=None,
                    help="output dir (default: <window>/_plan)")
    ap.add_argument("--flit-bytes", type=int, default=FLIT_BYTES_DEFAULT)
    args = ap.parse_args()
    window = args.window
    out = args.out or os.path.join(window, "_plan")
    os.makedirs(out, exist_ok=True)

    recs = load_records(window)
    by_id = {r.rid: r for r in recs}
    if len(by_id) != len(recs):
        fatal("duplicate rec ids")
    check_ids(recs)
    msgs = by_msg_of(recs)
    check_msg_consistency(recs, msgs)
    check_adjacency(recs)
    check_lclock(recs, by_id)
    classify(recs, by_id)
    set_taps(recs)
    n_abs = detect_absorbed(recs, msgs)
    check_absorption(recs)
    check_seq(recs, window)
    max_cls = assign_classes(recs)
    if max_cls > 1:
        fatal("max dateline class %d > 1: paths with >=2 bends exist; "
              "2 VC classes insufficient" % max_cls)
    cyc, n_nodes, n_edges = check_cdg(recs)
    if cyc:
        fatal("class-aware CDG has %d cyclic nodes over %d -- deadlock risk"
              % (cyc, n_nodes))
    cont = build_arrival_map(recs)
    fanout, merges = check_branch_merge(recs, cont)
    errs = reconcile(window, recs, msgs)
    if errs:
        for e in errs:
            print("RECONCILE MISMATCH:", e)
        fatal("reconciliation failed (%d mismatches)" % len(errs))

    window_name = os.path.basename(os.path.normpath(window))
    n_routers = len(set(r.router for r in recs))
    plan_path = os.path.join(out, "plan.txt")
    emit_plan(plan_path, window_name, recs, msgs, args.flit_bytes, n_routers)

    occ, window_cycles = engine_occupancy(recs, flit_bytes=args.flit_bytes)
    occ_top = sorted(occ.items(), key=lambda kv: -kv[1])[:3]
    rep = []
    rep.append("trace-replay plan report: %s" % window_name)
    rep.append("  records=%d messages=%d flit_bytes=%d routers=%d" %
               (len(recs), len(msgs), args.flit_bytes, n_routers))
    rep.append("  plan kinds: T(send)=%d R(root)=%d F(relay)=%d" % (
        sum(1 for r in recs if r.pkind == "T"),
        sum(1 for r in recs if r.pkind == "R"),
        sum(1 for r in recs if r.pkind == "F")))
    rep.append("  dateline: max class=%d  CDG nodes=%d edges=%d cycles=0 OK" %
               (max_cls, n_nodes, n_edges))
    rep.append("  class histogram: cls0=%d cls1=%d" % (
        sum(1 for r in recs if r.cls == 0),
        sum(1 for r in recs if r.cls == 1)))
    rep.append("  branch fanout per arrival: %s" % dict(sorted(fanout.items())))
    rep.append("  merge group sizes (ups + absorbed legs): %s" %
               dict(sorted(merges.items())))
    rep.append("  absorbed legs (chained merge members, drained in-network): %d"
               % n_abs)
    rep.append("  taps (relay recs whose owner is a msg dst): %d" %
               sum(r.tap for r in recs))
    rep.append("  reconciliation: links.csv + summary.json rebuilt EXACTLY")
    rep.append("  window estimate: %d cycles (%d lclock waves x 15); worst "
               "injection-engine occupancy (rule b): %s" %
               (window_cycles, max(r.lclock for r in recs),
                ", ".join("%s=%.1f%%" % (k, v * 100) for k, v in occ_top)))
    rep.append("  plan written: %s" % plan_path)
    report = "\n".join(rep) + "\n"
    with open(os.path.join(out, "plan_report.txt"), "w") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    main()
