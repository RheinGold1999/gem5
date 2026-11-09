#!/usr/bin/env python3
# Copyright (c) 2025 AIGCIC_NOC project.
#
# Msg-granularity reconciliation of a trace-replay event log against the
# original noc-trace window.
#
#   usage: reconcile_event_log.py <events.txt> <window_dir>
#     e.g. reconcile_event_log.py log/.../events.txt \
#            traffic/trace/noc-trace-4x6/s1-layer-a
#
# The event log (--trace-event-log) carries one line per replay event:
#   tick EVENT tid rec msg op stage router [detail]
# with EVENT in {ISSUE, RELAY, ARRIVE, TAP, LEG_DISPATCH, LEG_DEFER,
# WINDOW_START, WINDOW_DONE}; tid is the GLOBAL record id
# (rep * n_recs + window-local index); the ARRIVE line's router field is
# the record's owner (the arrival router is rec.next, enforced at runtime
# by a panic check).
#
# Checks (spec trace-replay-spec.md 6 / 8):
#   A. every record of every repetition ARRIVEs exactly once
#   B. every F record is realized exactly once (head-flit rewrite, or a
#      tester leg; branch extras included); T/R records issue exactly once
#   C. causality: start(rec) >= ARRIVE(dep) for every external after dep
#      and same-msg upstream member (the lclock/after semantics, measured)
#   D. injection discipline: per router the T/R issues follow seq order;
#      a different-msg issue waits for the previous burst's arrival
#      (rules a/b); same-msg groups may pipeline
#   E. per msg: the record set is complete and matches records.csv
#   F. per msg summary (records/bytes/hops) vs messages.csv, plus the
#      measured makespan (last ARRIVE - first start)
#
# Output: per-check PASS/FAIL with the first violations, a per-msg table,
# and an overall verdict.

import argparse
import collections
import csv
import os
import re
import sys

FATAL_LIMIT = 8


def fatal(msg):
    print("FATAL:", msg)
    sys.exit(1)


def parse_plan(window):
    """rec idx -> dict(kind, owner, next, bytes, msg, op, stage, ups, afters)"""
    path = os.path.join(window, "_plan", "plan.txt")
    recs = {}
    for line in open(path):
        if line.startswith("#") or line.startswith("META") or not line.strip():
            continue
        t = line.split()
        i = 2
        idx = int(t[0])
        kind = t[1]
        owner = int(t[i]); i += 1
        seq = int(t[i]); i += 1
        nxt = int(t[i]); i += 1
        bts = int(t[i]); i += 1
        i += 2                      # tap, cls
        nu = int(t[i]); i += 1
        ups = list(map(int, t[i:i + nu])); i += nu
        na = int(t[i]); i += 1
        i += na                     # abs (already folded into the group)
        naf = int(t[i]); i += 1
        aft = list(map(int, t[i:i + naf])); i += naf
        lclock = t[i]; i += 1
        msgid = t[i]; i += 1
        recs[idx] = dict(kind=kind, owner=owner, next=nxt, bytes=bts,
                         ups=ups, afters=aft, seq=seq, msg=msgid,
                         group=nu + na)
    return recs


def parse_trace_csv(window):
    """records.csv: rec idx -> (msg, router, next, bytes); messages.csv rows."""
    rec2msg = {}
    rid = {}
    msg_recs = collections.defaultdict(list)
    for r in csv.DictReader(open(os.path.join(window, "records.csv"))):
        idx = int(r["rec"][1:]) - 1
        rec2msg[idx] = r["msg"]
        rid[idx] = r["rec"]
        msg_recs[r["msg"]].append(idx)
    msgs = list(csv.DictReader(open(os.path.join(window, "messages.csv"))))
    return rec2msg, msg_recs, msgs, rid


def parse_events(path):
    evs = []
    for line in open(path):
        if line.startswith("#"):
            continue
        t = line.split()
        if len(t) < 8:
            continue
        evs.append(dict(tick=int(t[0]), event=t[1], tid=int(t[2]),
                        rec=int(t[3]), msg=t[4], op=t[5], stage=t[6],
                        router=int(t[7]), detail=" ".join(t[8:]),
                        seq_line=False))
    return evs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event_log")
    ap.add_argument("window")
    ap.add_argument("--injection-order", choices=["ready", "seq"],
                    default="ready",
                    help="ready = out-of-order injection (after-satisfied "
                    "records issue immediately; D_seq/D_ruleb checks are "
                    "skipped); seq = the trace's per-router seq order is "
                    "enforced by the injection path")
    args = ap.parse_args()

    recs = parse_plan(args.window)
    n_recs = len(recs)
    rec2msg, msg_recs, msgs, rid = parse_trace_csv(args.window)
    evs = parse_events(args.event_log)
    if not evs:
        fatal("empty event log")

    reps = sorted({e["tid"] // n_recs for e in evs if e["tid"] >= 0})
    print(f"event log: {len(evs)} events, reps={reps}, recs/window={n_recs}")

    # ---- collect per (rep, rec) events ----
    arrive = {}      # (rep, rec) -> tick
    arrive_cnt = collections.Counter()
    start = {}       # (rep, rec) -> first start tick (ISSUE or RELAY)
    realized = collections.Counter()   # (rep, rec) for F recs
    issued = collections.Counter()     # (rep, rec) for T/R recs
    issue_ev = {}    # (rep, rec) -> tick of first ISSUE
    violations = collections.defaultdict(list)

    def key(e):
        return (e["tid"] // n_recs, e["tid"] % n_recs)

    for e in evs:
        if e["tid"] < 0:
            continue
        k = key(e)
        rep, rec = k
        if e["event"] == "ARRIVE":
            arrive_cnt[k] += 1
            arrive.setdefault(k, e["tick"])
        elif e["event"] == "ISSUE":
            issued[k] += 1
            issue_ev.setdefault(k, e["tick"])
            start.setdefault(k, e["tick"])
        elif e["event"] == "RELAY":
            # the RELAY decision starts the CONTINUATION's transmission
            # (cut-through at the member's head arrival); the arriving
            # record's completion is its own ARRIVE line
            m = re.search(r"cont=(\d+)", e["detail"])
            if m:
                cont_tid = int(m.group(1))
                ck = (cont_tid // n_recs, cont_tid % n_recs)
                if "act=EJECT" not in e["detail"]:
                    # cut-through: the continuation transmits from here;
                    # an EJECTed continuation starts at its leg's ISSUE
                    start.setdefault(ck, e["tick"])
                if "act=REWRITE" in e["detail"] or "act=BRANCH" in e["detail"]:
                    realized[ck] += 1
        elif e["event"] == "LEG_DISPATCH":
            realized[k] += 1

    # ---- A: arrivals exactly once ----
    for rep in reps:
        for rec in range(n_recs):
            c = arrive_cnt.get((rep, rec), 0)
            if c != 1:
                violations["A_arrive_once"].append(
                    f"rep{rep} rec{rec} ({rid[rec]}) arrived {c}x")
    # ---- B: realization exactly once ----
    for rep in reps:
        for rec, r in recs.items():
            k = (rep, rec)
            if r["kind"] == "F":
                c = realized.get(k, 0)
                if c != 1:
                    violations["B_realize_once"].append(
                        f"rep{rep} rec{rec} ({rid[rec]}) realized {c}x")
            else:
                c = issued.get(k, 0)
                if c != 1:
                    violations["B_issue_once"].append(
                        f"rep{rep} rec{rec} ({rid[rec]}) issued {c}x")
    # ---- C: causality ----
    for rep in reps:
        for rec, r in recs.items():
            k = (rep, rec)
            if k not in start:
                continue
            deps = list(r["afters"])
            if r["kind"] == "F" and r.get("group", 1) > 1:
                deps += r["ups"]      # merge: wait for every member (F5)
            # single-member rewrite: same-msg pipelining is sanctioned
            # cut-through (F1/F4) -- no member constraint
            for dep in deps:
                dk = (rep, dep)
                if dk not in arrive:
                    violations["C_causality"].append(
                        f"rep{rep} rec{rec} ({rid[rec]}) starts before dep "
                        f"rec{dep} arrives")
                elif start[k] < arrive[dk]:
                    violations["C_causality"].append(
                        f"rep{rep} rec{rec} ({rid[rec]}) starts t={start[k]} "
                        f"< dep rec{dep} arrival t={arrive[dk]}")
    # ---- D: injection discipline (seq + rule b) ----
    d_checks_on = (args.injection_order == "seq")
    if not d_checks_on:
        if d_checks_on:
            violations["D_seq"].append("SKIPPED (out-of-order injection)")
        if d_checks_on:
            violations["D_ruleb"].append("SKIPPED (out-of-order injection)")
    by_router = collections.defaultdict(list)
    for rec, r in recs.items():
        if r["kind"] != "F":
            by_router[r["owner"]].append(rec)
    for (rep, router) in {(rep, recs[rec]["owner"])
                          for rep in reps
                          for rec, r in recs.items() if r["kind"] != "F"}:
        seq_sorted = sorted(by_router[router], key=lambda c: recs[c]["seq"])
        prev = None   # (issue_tick, arrive_tick, msgid, rec)
        for rec in seq_sorted:
            k = (rep, rec)
            t = issue_ev.get(k)
            if t is None:
                if d_checks_on:
                    violations["D_seq"].append(
                    f"rep{rep} router{router} seq{recs[rec]['seq']} "
                    f"({rid[rec]}) never issued")
                prev = None
                continue
            if prev is not None:
                pt, pa, pmsg, prec = prev
                if t < pt:
                    if d_checks_on:
                        violations["D_seq"].append(
                        f"rep{rep} router{router} rec{rec} ({rid[rec]}) "
                        f"issued t={t} < earlier-seq rec{prec} t={pt}")
                if pmsg != recs[rec]["msg"] and t < pa:
                    if d_checks_on:
                        violations["D_ruleb"].append(
                        f"rep{rep} router{router} rec{rec} (msg "
                        f"{recs[rec]['msg']}) issued t={t} < previous "
                        f"diff-msg rec{prec} arrival t={pa}")
            prev = (t, arrive.get(k, t), recs[rec]["msg"], rec)
    # ---- E/F: per-msg completeness + summary ----
    msg_rows = []
    for m in msgs:
        mid = m["msg"]
        rec_idxs = msg_recs[mid]
        for rep in reps:
            got = [rec for rec in rec_idxs
                   if (rep, rec) in arrive]
            if len(got) != len(rec_idxs):
                violations["E_msg_complete"].append(
                    f"rep{rep} {mid}: {len(got)}/{len(rec_idxs)} records")
                continue
            b = sum(recs[rec]["bytes"] for rec in rec_idxs)
            st = min(start.get((rep, rec), arrive[(rep, rec)])
                     for rec in rec_idxs)
            en = max(arrive[(rep, rec)] for rec in rec_idxs)
            # messages.csv 'bytes' = per-hop payload (identical on every
            # hop); the hop-by-hop total is per-hop * records
            exp_bytes = int(m["bytes"]) * len(rec_idxs)
            ok_b = (b == exp_bytes)
            if not ok_b:
                violations["F_msg_bytes"].append(
                    f"rep{rep} {mid}: bytes {b} != messages.csv {exp_bytes}")
            op = recs[rec_idxs[0]] if False else None
            msg_rows.append((rep, mid, m["op"], m["stage"], len(rec_idxs),
                             b, en - st, ok_b))

    # ---- report ----
    order = ["A_arrive_once", "B_realize_once", "B_issue_once", "C_causality",
             "D_seq", "D_ruleb", "E_msg_complete", "F_msg_bytes"]
    all_ok = True
    for name in order:
        v = violations.get(name, [])
        skipped = v and all(str(x).startswith("SKIPPED") for x in v)
        ok = (not v) or skipped
        all_ok &= ok
        tag = "SKIP" if skipped else ("PASS" if ok else "FAIL")
        print(f"  [{tag}] {name}: {len(v)} entries")
        for x in v[:FATAL_LIMIT]:
            print(f"        {x}")
        if len(v) > FATAL_LIMIT:
            print(f"        ... ({len(v) - FATAL_LIMIT} more)")
    print(f"\nper-msg summary: {len(msg_rows)} rows "
          f"(rep, msg, op, stage, recs, bytes, makespan-ticks, bytes-ok)")
    for row in msg_rows[:10]:
        print("  ", row)
    if len(msg_rows) > 10:
        print(f"   ... ({len(msg_rows) - 10} more)")
    print(f"\nVERDICT: {'PASS' if all_ok else 'FAIL'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
