#!/usr/bin/env python3
# Copyright (c) 2025 AIGCIC_NOC project.
#
# DRAFT: window-level critical-path attribution for trace replay.
#
#   usage: analyze_critical_path.py <events.txt> <window_dir> [--rep N]
#                                    [--top N]
#
# Event graph (nodes = events, edges = mechanical predecessors):
#   HEAD(rec)   head arrival of rec's burst at rec.next
#               (event RELAY for pass-through, HOOK for terminal/pass)
#   ARRIVE(rec) tail arrival of rec's burst at rec.next (= delivery)
#   INJ(rec)    tester injection (T/R heads and merge/bend legs)
#   LEGD(rec)   scheduler leg dispatch (after a merge/bend EJECT)
#   WS          window start (first event of the repetition)
#
# Edges:
#   LEGD -> INJ -> HEAD -> ARRIVE      a leg-realised record
#   HEAD(m) -> HEAD(rec)               in-network straight rewrite (1 hop)
#   ARRIVE(m) -> LEGD(rec)             merge/bend wait (all members)
#   ARRIVE(dep) -> START(rec)          external after-dep wait (S)
#   WS -> START(rec)                   window entry
#
# Backtrack from the last ARRIVE via the LATEST mechanical predecessor;
# chain durations sum exactly to the window makespan.  Categories:
#   S   scheduling wait (rule-b / seq / resource mixed in this draft)
#   T3  inherent (calibrated minimum per edge kind)
#   T4  excess over the minimum (congestion / re-inject overhead)

import argparse
import collections
import os
import sys


def parse_plan(window):
    recs = {}
    n = None
    for line in open(os.path.join(window, "_plan", "plan.txt")):
        if line.startswith("#") or not line.strip():
            continue
        t = line.split()
        if t[0] == "META":
            for kv in t:
                if kv.startswith("recs="):
                    n = int(kv[5:])
            continue
        if n is None:
            continue
        idx = int(t[0])
        nu = int(t[8])
        ups = list(map(int, t[9:9 + nu]))
        off = 9 + nu
        na = int(t[off]); off += 1
        off += na
        naf = int(t[off]); off += 1
        aft = list(map(int, t[off:off + naf]))
        recs[idx] = dict(kind=t[1], owner=int(t[2]), next=int(t[4]),
                         bytes=int(t[5]), ups=ups, afters=aft, rid=t[-1])
    return recs, n


def parse_events(path, rep, n_recs):
    base = rep * n_recs
    ev = collections.defaultdict(dict)
    wstart = None
    for line in open(path):
        if line.startswith("#"):
            continue
        t = line.split()
        if len(t) < 4 or not t[0].isdigit():
            continue
        tick, evn, tid = int(t[0]), t[1], int(t[2])
        if tid // n_recs != rep:
            continue
        r = tid - base
        # first of each event type, with msg + detail (HOOK's act=
        # TERMINAL/PASS distinguishes a dst head arrival from a source pass)
        ev[r].setdefault(evn, (tick, t[4] if len(t) > 4 else "?",
                               " ".join(t[8:])))
        if wstart is None or tick < wstart:
            wstart = tick                # per-rep window start
    return ev, wstart


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event_log")
    ap.add_argument("window")
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--top", type=int, default=14)
    args = ap.parse_args()

    recs, n_recs = parse_plan(args.window)
    if n_recs is None:
        sys.exit("plan parse: no META recs")
    ev, wstart = parse_events(args.event_log, args.rep, n_recs)

    # ---- per-rec event times ----
    head = {}     # rec -> head-arrival tick (RELAY or terminal HOOK)
    arrive = {}
    inj = {}      # INJ tick (ISSUE on T/R heads and legs)
    legd = {}
    ready = {}    # rec -> deps-cleared tick (T/R)
    firsttry = {} # rec -> first send-attempt tick (T/R)
    for r, e in ev.items():
        if "RELAY" in e:
            head[r] = e["RELAY"][0]       # head arrival at rec.next
        elif "HOOK" in e and "act=TERMINAL" in e["HOOK"][2]:
            head[r] = e["HOOK"][0]        # terminal head arrival at rec.next
        if "ARRIVE" in e:
            arrive[r] = e["ARRIVE"][0]
        if "ISSUE" in e:
            inj[r] = e["ISSUE"][0]
        if "LEG_DISPATCH" in e:
            legd[r] = e["LEG_DISPATCH"][0]
        if "READY" in e:
            ready[r] = e["READY"][0]
        if "FIRST_TRY" in e:
            firsttry[r] = e["FIRST_TRY"][0]

    msgof = {}
    for r, e in ev.items():
        msgof.setdefault(r, e.get("msg", "?"))

    # ---- graph ----
    preds = collections.defaultdict(list)
    nodes = set()

    def NN(t, k, r):
        n = (t, k, r)
        nodes.add(n)
        return n

    for r in sorted(ev):
        e = ev[r]
        h = head.get(r)
        a = arrive.get(r)
        i = inj.get(r)
        g = legd.get(r)
        rec = recs[r]

        n_h = NN(h, "HEAD", r) if h is not None else None
        n_a = NN(a, "ARRIVE", r) if a is not None else None
        n_i = NN(i, "INJ", r) if i is not None else None
        n_g = NN(g, "LEGD", r) if g is not None else None

        # leg chain: ARRIVE(members) -> LEGD -> INJ -> HEAD
        if n_g is not None:
            if n_i is not None:
                preds[n_i].append((g[0] if isinstance(g, tuple) else g,
                                   "LEGD", r))
            for m in rec["ups"]:
                ma = arrive.get(m)
                ma = ma[0] if isinstance(ma, tuple) else ma
                if ma is not None:
                    preds[n_g].append((ma, "ARRIVE", m))
        if n_i is not None and n_h is not None:
            preds[n_h].append((i, "INJ", r))
        # tail drain
        if n_h is not None and n_a is not None:
            preds[n_a].append((h, "HEAD", r))
        elif n_i is not None and n_a is not None:
            preds[n_a].append((i, "INJ", r))
        # in-network straight rewrite: member head -> this head
        if n_h is not None and g is None:
            for m in rec["ups"]:
                mh = head.get(m)
                if mh is not None:
                    preds[n_h].append((mh, "HEAD", m))
        # external after deps gate the ISSUE (T/R/leg) or the head arrival
        # (in-network rewrite) -- whichever happens first
        start_node = n_i if n_i is not None else n_h
        if start_node is not None:
            for dep in rec["afters"]:
                da = arrive.get(dep)
                da = da[0] if isinstance(da, tuple) else da
                if da is not None:
                    # after edge: point at the dep's own ARRIVE node so the
                    # backtracker recurses into the dep's chain; the gap
                    # (start - dep arrival) classifies as S (调度等待)
                    preds[start_node].append((da, "ARRIVE", dep))

    # window entry for nodes without predecessors
    for n in list(nodes):
        if not preds[n]:
            preds[n].append((wstart, "WS", -1))

    # ---- backtrack from the last ARRIVE ----
    last_rec, last_tick = max(arrive.items(), key=lambda kv: kv[1])
    last_tick = last_tick[0] if isinstance(last_tick, tuple) else last_tick
    W = last_tick - wstart
    chain = []
    cur = (last_tick, "ARRIVE", last_rec)
    while True:
        chain.append(cur)
        pl = preds.get(cur, [])
        if not pl:
            break
        pred = max(pl)
        if pred[0] < wstart or pred[0] > cur[0]:
            break
        cur = pred
    chain.reverse()

    # ---- calibrate P10 per (bytes bucket, edge kind) over ALL edges ----
    def bucket(f):
        if f <= 1: return "1f"
        if f <= 4: return "2-4f"
        if f <= 8: return "5-8f"
        if f <= 16: return "9-16f"
        return "17f+"
    flits = {r: max(1, -(-recs[r]["bytes"] // 128)) for r in recs}
    samples = collections.defaultdict(list)
    for n, pl in preds.items():
        cf = flits.get(n[2], 1)
        for pp in pl:
            pt, kind = pp[0], pp[1]
            d = n[0] - pt
            if d < 0:
                continue
            if d <= 0:
                continue          # degenerate same-tick edges: no baseline
            if kind == "HEAD":
                samples[(bucket(cf), "hop")].append(d)
            elif kind == "INJ":
                samples[(bucket(cf), "first")].append(d)
            elif kind == "LEGD":
                samples[(bucket(cf), "leg")].append(d)

    def p10(v):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * 0.10))]

    p10 = {k: p10(v) for k, v in samples.items()}
    mins = collections.defaultdict(lambda: 10 ** 9)
    for n, pl in preds.items():
        for pp in pl:
            pt, kind, pr = pp[0], pp[1], pp[2]
            if isinstance(n[0], tuple) or isinstance(pt, tuple):
                continue
            d = n[0] - pt
            if 0 <= d < mins[kind]:
                mins[kind] = d

    # ---- classify chain edges ----
    cat = collections.Counter()
    rows = []
    prev = (wstart, "WS", -1)
    for (tk, k, r) in chain:
        dur = tk - prev[0]
        pk, pr = prev[1], prev[2]
        # S gap at a T/R rec: split by READY/FIRST_TRY
        if pk == "ARRIVE" and pr != r and r in ready:
            rd = ready.get(r, prev[0])
            ft = firsttry.get(r, prev[0])
            t2 = max(0, ft - rd)
            t4r = max(0, tk - ft)
            cat["T2"] += t2
            cat["T4r"] += t4r
            rows.append((tk, k, r, dur, "S:T2=%d/T4r=%d" % (t2, t4r)))
            prev = (tk, k, r)
            continue
        if pk == "LEGD" and k == "INJ":
            c = "T4r"      # leg injection resource wait
        elif pk == "HEAD" and k == "HEAD":
            b = p10.get((bucket(flits.get(r, 1)), "hop"), 0)
            c = "T3" if dur <= b else "T4a"
        elif pk == "INJ" and k == "HEAD":
            b = p10.get((bucket(flits.get(r, 1)), "first"), 0)
            c = "T3" if dur <= b else "T4a"
        elif pk == "INJ" and k == "ARRIVE":
            b = p10.get((bucket(flits.get(r, 1)), "first"), 0)
            c = "T3" if dur <= b else "T4a"
        elif pk == "HEAD" and k == "ARRIVE":
            b = p10.get((bucket(flits.get(r, 1)), "tail"), 0)
            c = "T3" if dur <= b else "T4a"
        elif pk == "ARRIVE" and pr == r:
            c = "T3"      # same-rec degenerate tail edge
        elif pk == "WS":
            c = "S"
        else:
            # ARRIVE(dep) -> HEAD(rec): in-network rewrite gated by
            # external afters -- the wait is dependency-type (T1)
            c = "T1"
        cat[c] += dur
        rows.append((tk, k, r, dur, c))
        prev = (tk, k, r)
    W2 = sum(cat.values())

    rmsg = {}
    for line in open(os.path.join(args.window, "records.csv")):
        rr = line.split(",")
        if rr[0].startswith("t"):
            rmsg[int(rr[0][1:]) - 1] = rr[3] if len(rr) > 3 else "?"

    print(f"window makespan W = {W} cyc  (chain {len(chain)} events, sum {W2})")
    print("P10 baselines: " +
          "  ".join(f"{k[0]}|{k[1]}={v}" for k, v in sorted(p10.items()))
          + "  cyc")
    print("\n== 关键路径四类时间 ==")
    labels = {"T1": "结构空窗/依赖等待(改写)", "T2": "执行模型纪律",
              "T3": "网络固有", "T4a": "拥塞超额", "T4b": "重注入超额",
              "T4r": "资源等待", "S": "调度等待(未拆)"}
    for c in ["T1", "T2", "T3", "T4a", "T4r", "S"]:
        if cat[c]:
            print(f"  {c:<3} {labels[c]:<18} {cat[c]:>8} cyc  {cat[c]/W*100:5.1f}%")
    print("\n== 关键路径链(前 %d 段)==\n" % args.top)
    for (tk, k, r, dur, c) in rows[:args.top]:
        print(f"  t={tk:>9} {k:<7} rec={r:<5} dur={dur:>6} {c:<3} "
              f"[{rmsg.get(r,'-')}]")
    if len(rows) > args.top:
        print(f"  ... ({len(rows)-args.top} more)")
    print("\n== top gaps ==")
    for (tk, k, r, dur, c) in sorted(rows, key=lambda x: -x[3])[:args.top]:
        print(f"  dur={dur:>6} {c:<3} at {k} rec={r} [{rmsg.get(r,'-')}] t={tk}")


if __name__ == "__main__":
    main()
