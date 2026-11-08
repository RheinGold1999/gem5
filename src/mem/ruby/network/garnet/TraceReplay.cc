/*
 * Copyright (c) 2025 AIGCIC_NOC project.
 *
 * Trace-replay engine implementation (see TraceReplay.hh).
 */

#include "mem/ruby/network/garnet/TraceReplay.hh"

#include "mem/ruby/common/MachineID.hh"
#include "mem/ruby/protocol/MachineType.hh"

#include <cassert>

#include "base/logging.hh"
#include "debug/RubyNetwork.hh"
#include "mem/ruby/network/garnet/InputUnit.hh"
#include "mem/ruby/network/garnet/Router.hh"
#include "mem/ruby/structures/TraceScheduler.hh"

namespace gem5
{

namespace ruby
{

namespace garnet
{

TraceReplay::TraceReplay()
{
}

void
TraceReplay::loadPlan(const std::string &plan_path, int fabric_cols,
                      int num_routers, RubySystem *rs)
{
    std::ifstream in(plan_path);
    if (!in) {
        fatal("TraceReplay: cannot open plan file %s", plan_path);
    }

    m_fabric_cols = fabric_cols;
    m_num_routers = num_routers;

    std::string line;
    bool saw_meta = false;
    long expected_recs = -1;
    while (std::getline(in, line)) {
        if (line.empty() || line[0] == '#') {
            continue;
        }
        std::istringstream iss(line);
        std::string tag;
        iss >> tag;
        if (tag == "META") {
            std::string kv;
            while (iss >> kv) {
                auto eq = kv.find('=');
                if (eq == std::string::npos) continue;
                if (kv.substr(0, eq) == "recs") {
                    expected_recs = std::stol(kv.substr(eq + 1));
                } else if (kv.substr(0, eq) == "flit_bytes") {
                    m_flit_bytes = std::stol(kv.substr(eq + 1));
                }
            }
            saw_meta = true;
            continue;
        }
        TraceRec rec;
        int idx = std::stoi(tag);
        std::string kind_tok;
        iss >> kind_tok >> rec.owner >> rec.seq >> rec.next >> rec.bytes
            >> rec.tap >> rec.cls;
        int n;
        iss >> n;                                            // n_ups
        rec.ups.resize(n);
        for (int i = 0; i < n; i++) iss >> rec.ups[i];
        iss >> n;                                            // n_abs
        rec.abs.resize(n);
        for (int i = 0; i < n; i++) iss >> rec.abs[i];
        iss >> n;                                            // n_after
        rec.afters.resize(n);
        for (int i = 0; i < n; i++) iss >> rec.afters[i];
        iss >> rec.lclock >> rec.msg >> rec.op >> rec.stage >> rec.rid;
        rec.kind = (kind_tok == "T") ? 0 : (kind_tok == "R") ? 1 : 2;
        // terminal routing destination: the dir agent at rec.next (the dir
        // controllers are created one per router, in router order)
        MachineID dir_mid;
        dir_mid.type = MachineType_Directory;
        dir_mid.num = rec.next;
        rec.term_dest = NetDest(rs);
        rec.term_dest.add(dir_mid);

        if ((int)m_recs.size() != idx) {
            fatal("TraceReplay: plan record index %d out of order", idx);
        }
        m_recs.push_back(rec);
    }
    if (!saw_meta || expected_recs != (long)m_recs.size()) {
        fatal("TraceReplay: plan %s malformed (meta=%d, recs=%d)",
              plan_path, saw_meta, expected_recs);
    }

    // arrival-key map + release groups (one group per relay continuation)
    m_arrival.assign(m_recs.size(), ArrivalEntry());
    m_rec_group.assign(m_recs.size(), -1);
    for (size_t i = 0; i < m_recs.size(); i++) {
        TraceRec &rec = m_recs[i];
        if (rec.kind != 2) {
            continue;
        }
        if (rec.ups.empty()) {
            fatal("TraceReplay: relay rec %s has no upstream", rec.rid);
        }
        int gid = (int)m_groups.size();
        HoldGroup g;
        g.cont = (int)i;
        g.group_size = (int)(rec.ups.size() + rec.abs.size());
        // single-member groups: no arrival gate (the arriving stream IS
        // the member; cut-through rewrite at head time when deps allow).
        // merge groups: gate on every member burst's arrival.
        g.arrivals_remaining = (g.group_size > 1) ? g.group_size : 0;
        g.afters_ok = rec.afters.empty();
        g.fired = false;
        m_groups.push_back(g);
        m_rec_group[i] = gid;

        for (int a : rec.ups) {
            m_arrival[a].conts.push_back({(int)i, false});
            m_arrival[a].hold_id = gid;
        }
        for (int a : rec.abs) {
            m_arrival[a].conts.push_back({(int)i, true});
            m_arrival[a].hold_id = gid;
        }
        if (rec.tap) {
            // the arriving router is a msg dst: virtual tap delivery
            m_arrival[rec.ups[0]].tap = true;
        }
    }
    // branch arrivals: every continuation shares the arrival router and
    // msg, hence the same tap flag; OR them into the member's entry
    for (const auto &rec : m_recs) {
        if (rec.kind != 2) continue;
        for (int a : rec.ups) {
            m_arrival[a].tap = m_arrival[a].tap || rec.tap;
        }
    }

    for (const auto &rec : m_recs) {
        panic_if(rec.term_dest.getSize() == 0,
                 "TraceReplay: rec %s term_dest empty (rs %p)",
                 rec.rid, (void *)rs);
    }
    // A branch continuation (k > 1 continuations of one arrival) is
    // realized by the branch rewrite at the member's HEAD arrival --
    // external after deps cannot gate it there.  The plan compiler must
    // not emit that combination.
    for (const auto &arr : m_arrival) {
        if (arr.conts.size() > 1) {
            for (auto &c : arr.conts) {
                panic_if(!m_recs[c.rec].afters.empty(),
                         "TraceReplay: branch continuation rec %s carries "
                         "external after deps (unsupported plan shape)",
                         m_recs[c.rec].rid);
            }
        }
    }
    // per-router local dir destination (for the hold-eject path)
    m_local_dest.resize(m_num_routers);
    for (int r = 0; r < m_num_routers; r++) {
        MachineID dir_mid;
        dir_mid.type = MachineType_Directory;
        dir_mid.num = r;
        m_local_dest[r] = NetDest(rs);
        m_local_dest[r].add(dir_mid);
    }

    m_enabled = true;
    inform("TraceReplay: loaded %d records (%d release groups) from %s",
           (int)m_recs.size(), (int)m_groups.size(), plan_path);
}

void
TraceReplay::windowStart(long rep_base)
{
    m_rep_base = rep_base;
    for (auto &g : m_groups) {
        g.arrivals_remaining = g.group_size;
        g.afters_ok = m_recs[g.cont].afters.empty();
        g.fired = false;
    }
}

TraceReplay::HookResult
TraceReplay::hook(Router *router, int inport, int vc, int trace_id)
{
    HookResult hr;
    int rec = (int)(trace_id - m_rep_base);
    assert(rec >= 0 && rec < (int)m_recs.size());
    if (m_recs[rec].owner == router->get_id()) {
        // The stream was just injected by this router's tester: its
        // departure identity and destination are already correct (the NI
        // stamped rec's own next hop). No rewrite here -- the record's
        // continuation is consulted when the stream ARRIVES at next.
        // F10: injected streams (heads and legs) start on dateline
        // class 0 -- an unconstrained grant could hand out the class-1
        // VC and create a class-decreasing edge the plan's CDG never
        // modeled.
        hr.cls = 0;
        DPRINTF(RubyNetwork, "trace hook router %d rec %d: source pass\n",
                router->get_id(), rec);
        return hr;  // HOOK_NORMAL
    }
    if (m_recs[rec].next != router->get_id()) {
        panic("trace hook: rec %d (next=%d) hooked at router %d",
              rec, m_recs[rec].next, router->get_id());
    }
    const ArrivalEntry &e = m_arrival[rec];
    DPRINTF(RubyNetwork, "trace hook router %d rec %d: %d conts\n",
            router->get_id(), rec, (int)e.conts.size());

    if (e.conts.empty()) {
        // terminal: normal routing ejects the stream into the local dir
        return hr;  // HOOK_NORMAL
    }

    if (e.conts.size() == 1) {
        // Single continuation.  Two cases (trace-replay-spec.md 5.1):
        //
        // * single-member group (plain relay / after-hold): cut-through
        //   REWRITE when the continuation's external deps are already
        //   cleared; otherwise EJECT into the local dir -- the continuation
        //   is then realized by one re-injection leg once this burst has
        //   fully arrived (the one-member gate is armed here) and the deps
        //   clear.  No buffer slots are held while waiting, so no credit
        //   deadlock can form.
        //
        // * multi-member group (merge, group_size > 1): a member stream is
        //   NEVER rewritten at head arrival.  The continuation is realized
        //   exactly once, by a re-injection leg, after EVERY member burst
        //   has fully arrived: the group gate was armed to group_size at
        //   load/windowStart and each member's arrival completion
        //   decrements it in onArrival().  Members are ejected to the
        //   local dir and drained; touching arrivals_remaining here would
        //   clobber the group's member count.
        const TraceRec &cont = m_recs[e.conts[0].rec];
        HoldGroup &g = m_groups[m_rec_group[e.conts[0].rec]];
        DPRINTF(RubyNetwork, "trace hook router %d rec %d single cont %d "
                "size %d rem %d afters_ok %d\n", router->get_id(), rec,
                g.cont, g.group_size, g.arrivals_remaining, g.afters_ok);
        if (g.group_size == 1 && g.afters_ok) {
            hr.action = HOOK_REWRITE;
            hr.outport = router->traceOutportForNext(cont.next);
            hr.next = cont.next;
            hr.cont = m_rep_base + g.cont;
            hr.cls = cont.cls;
            hr.net_dest = cont.term_dest;
            g.fired = true;   // realized by this rewrite; no re-inject leg
            m_relayRewrites++;
            if (m_scheduler->logging()) {
                m_scheduler->event("RELAY", m_rep_base + rec,
                                   router->get_id(),
                                   "act=REWRITE cont=" +
                                   std::to_string(hr.cont));
            }
            return hr;
        }
        if (g.group_size == 1) {
            // single member with deps pending: arm the one-member gate --
            // this burst's arrival completion is the group's only arrival
            g.arrivals_remaining = 1;
        } else {
            m_relayDrainFlits += (m_recs[rec].bytes + m_flit_bytes - 1) /
                                 m_flit_bytes;
        }
        hr.action = HOOK_EJECT;
        hr.cont = m_rep_base + g.cont;
        hr.net_dest = m_local_dest[router->get_id()];
        m_relayHolds++;
        if (m_scheduler->logging()) {
            m_scheduler->event("RELAY", m_rep_base + rec, router->get_id(),
                               "act=EJECT cont=" + std::to_string(hr.cont));
        }
        return hr;
    }

    // branch arrival (k > 1 continuations): the first continuation is the
    // cut-through rewrite; the extra legs are re-injected by their owning
    // tester when this burst completes (the arrival handler fires
    // scheduler->relayLeg for them)
    for (auto &c : e.conts) {
        m_groups[m_rec_group[c.rec]].fired = true;
    }
    const TraceRec &cont = m_recs[e.conts[0].rec];
    hr.action = HOOK_REWRITE;
    hr.outport = router->traceOutportForNext(cont.next);
    hr.next = cont.next;
    hr.cont = m_rep_base + e.conts[0].rec;
    hr.cls = cont.cls;
    hr.net_dest = cont.term_dest;
    m_relayRewrites++;
    if (m_scheduler->logging()) {
        m_scheduler->event("RELAY", m_rep_base + rec, router->get_id(),
                           "act=BRANCH cont=" + std::to_string(hr.cont));
    }
    return hr;
}

int
TraceReplay::expectedFlits(int trace_id) const
{
    int rec = (int)(trace_id - m_rep_base);
    assert(rec >= 0 && rec < (int)m_recs.size());
    return (m_recs[rec].bytes + m_flit_bytes - 1) / m_flit_bytes;
}

void
TraceReplay::onArrival(int router_id, int inport, int vc, int trace_id,
                       Router *router)
{
    int rec = (int)(trace_id - m_rep_base);
    assert(rec >= 0 && rec < (int)m_recs.size());
    if (m_recs[rec].owner == router_id) {
        // tail buffered at the SOURCE router: the burst was just injected
        // (all flits entered the source's input unit); the record's
        // delivery event fires when it reaches its next hop
        return;
    }
    // the stream identified by 'rec' must arrive at the record's own next
    // hop; a mismatch means an identity/routing bug upstream
    if (m_recs[rec].next != router_id) {
        panic("trace arrival: rec %d (next=%d) arrived at router %d",
              rec, m_recs[rec].next, router_id);
    }
    const ArrivalEntry &e = m_arrival[rec];
    DPRINTF(RubyNetwork, "trace arrival router %d rec %d\n", router_id, rec);

    m_scheduler->arrivalDone(m_rep_base + rec);

    if (e.conts.empty()) {
        return;
    }

    if (e.tap) {
        m_tapDelivered++;
        m_tapDeliveredBytes += m_recs[rec].bytes;
        m_scheduler->tapDelivered(m_rep_base + rec, m_recs[rec].bytes);
    }
    if (e.conts.size() > 1) {
        // branch: extra legs are re-injected by the owning tester
        m_branchLegs += (long)(e.conts.size() - 1);
        for (size_t i = 1; i < e.conts.size(); i++) {
            m_scheduler->relayLeg(m_rep_base + e.conts[i].rec);
        }
    }

    if (e.hold_id >= 0) {
        HoldGroup &g = m_groups[e.hold_id];
        if (g.arrivals_remaining > 0) {
            g.arrivals_remaining--;
            if (g.arrivals_remaining == 0) {
                // maybeFire takes the CONTINUATION REC index (it maps it
                // through m_rec_group); e.hold_id is the GROUP id -- the
                // two id spaces are unrelated and were previously mixed,
                // which silently gated every merge/after-hold release.
                maybeFire(g.cont);
            }
        }
    }
}

void
TraceReplay::aftersCleared(int cont)
{
    int gid = m_rec_group[cont];
    if (gid < 0) {
        return;
    }
    m_groups[gid].afters_ok = true;
    maybeFire(cont);
}

// The release of an ejected continuation: every member burst fully
// arrived at this router AND the continuation's external after deps are
// cleared -> the owning tester re-injects the continuation's stream.
void
TraceReplay::maybeFire(int cont)
{
    int gid = m_rec_group[cont];
    if (gid < 0) {
        return;
    }
    HoldGroup &g = m_groups[gid];
    DPRINTF(RubyNetwork, "trace maybeFire rec %d: rem %d afters %d "
            "fired %d\n", cont, g.arrivals_remaining, g.afters_ok, g.fired);
    if (g.fired || g.arrivals_remaining > 0 || !g.afters_ok) {
        return;
    }
    g.fired = true;
    m_relayDrains++;
    m_scheduler->relayLeg(m_rep_base + g.cont);
}

void
TraceReplay::regStats(const std::string &prefix)
{
    m_relayRewrites
        .name(prefix + ".trace_relay_rewrites")
        .desc("trace replay: head-flit destination rewrites")
        ;
    m_relayHolds
        .name(prefix + ".trace_relay_holds")
        .desc("trace replay: streams ejected at merge/after-hold points "
              "(realized via re-inject legs)")
        ;
    m_relayDrains
        .name(prefix + ".trace_relay_drains")
        .desc("trace replay: continuations realized via re-inject legs "
              "(merge/after-hold releases)")
        ;
    m_relayDrainFlits
        .name(prefix + ".trace_relay_drain_flits")
        .desc("trace replay: merge member-stream flits ejected at the "
              "merge router (dropped, not continued)")
        ;
    m_tapDelivered
        .name(prefix + ".trace_tap_delivered")
        .desc("trace replay: virtual tap deliveries (msg dst arrivals)")
        ;
    m_tapDeliveredBytes
        .name(prefix + ".trace_tap_delivered_bytes")
        .desc("trace replay: bytes delivered virtually at taps")
        ;
    m_branchLegs
        .name(prefix + ".trace_branch_legs")
        .desc("trace replay: branch legs re-injected by testers")
        ;
}

} // namespace garnet
} // namespace ruby
} // namespace gem5
