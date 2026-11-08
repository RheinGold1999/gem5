/*
 * Copyright (c) 2025 AIGCIC_NOC project.
 *
 * Global trace-replay scheduler implementation (see TraceScheduler.hh).
 */

#include "mem/ruby/structures/TraceScheduler.hh"

#include <fstream>
#include <sstream>

#include "base/logging.hh"
#include "cpu/testers/garnet_synthetic_traffic/GarnetSyntheticTraffic.hh"
#include "mem/ruby/network/garnet/TraceReplay.hh"
#include "sim/sim_exit.hh"

namespace gem5
{

namespace ruby
{

TraceScheduler::TraceScheduler(const Params &p)
  : ClockedObject(p),
    m_network(p.network),
    m_repeat(p.repeat),
    m_inter_layer(p.inter_layer_cycles),
    m_plan_file(p.plan_file),
    m_event_log_path(p.event_log),
    m_startEvent([this] { windowStartEvent(m_rep + 1); },
                 name() + ".window_start")
{
    for (auto *t : p.testers) {
        m_testers.push_back(t);
    }
}

void
TraceScheduler::loadPlan(const std::string &path)
{
    std::ifstream in(path);
    if (!in) {
        fatal("TraceScheduler: cannot open plan file %s", path);
    }
    std::string line;
    long expected = -1;
    while (std::getline(in, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream iss(line);
        std::string tag;
        iss >> tag;
        if (tag == "META") {
            std::string kv;
            while (iss >> kv) {
                auto eq = kv.find('=');
                if (eq != std::string::npos && kv.substr(0, eq) == "recs") {
                    expected = std::stol(kv.substr(eq + 1));
                }
            }
            continue;
        }
        SRec rec;
        long idx = std::stol(tag);
        std::string kind_tok;
        iss >> kind_tok >> rec.owner;
        rec.kind = (kind_tok == "T") ? 0 : (kind_tok == "R") ? 1 : 2;
        // skip: seq next bytes tap cls n_ups ups[] n_abs abs[]
        int n;
        iss >> n >> n;                       // seq, next
        iss >> n;                            // bytes
        iss >> n;                            // tap
        iss >> n;                            // cls
        iss >> n;                            // n_ups
        for (int i = 0; i < n; i++) { int x; iss >> x; }
        iss >> n;                            // n_abs
        for (int i = 0; i < n; i++) { int x; iss >> x; }
        iss >> n;                            // n_after
        rec.afters.resize(n);
        for (int i = 0; i < n; i++) iss >> rec.afters[i];
        // tail annotations: lclock msg op stage rid
        long lclock; std::string msg, op, stage, rid;
        iss >> lclock >> msg >> op >> stage >> rid;
        rec.msg = msg;
        rec.op = op;
        rec.stage = stage;

        if ((long)m_recs.size() != idx) {
            fatal("TraceScheduler: plan index %ld out of order", idx);
        }
        m_recs.push_back(rec);
    }
    if (expected != (long)m_recs.size()) {
        fatal("TraceScheduler: plan recs mismatch (%ld vs %ld)",
              expected, (long)m_recs.size());
    }
    m_n_recs = (long)m_recs.size();
    // reverse dependency edges
    for (long i = 0; i < m_n_recs; i++) {
        for (int a : m_recs[i].afters) {
            m_recs[a].dependents.push_back((int)i);
        }
    }
}

void
TraceScheduler::init()
{
    ClockedObject::init();
    loadPlan(m_plan_file);
    m_pending.assign(m_n_recs, 0);
    m_done.assign(m_n_recs, false);
    // repetition 0 is armed here; the testers arm themselves in their init()
    m_rep = 0;
    m_rep_base = 0;
    for (long i = 0; i < m_n_recs; i++) {
        m_pending[i] = (int)m_recs[i].afters.size();
        m_done[i] = false;
    }
    panic_if(!m_network->traceReplay(),
             "TraceScheduler: the network has no trace plan loaded");
    m_network->traceReplay()->setScheduler(this);
    m_network->traceReplay()->windowStart(0);
    if (!m_event_log_path.empty()) {
        m_event_log.open(m_event_log_path);
        panic_if(!m_event_log.is_open(),
                 "TraceScheduler: cannot open event log %s",
                 m_event_log_path.c_str());
        m_event_log << "# tick event tid rec msg op stage router detail\n";
    }
    // stall watchdog: every 100k cycles dump the dependency frontier
    if (!m_stallEvent) {
        m_stallEvent = new EventFunctionWrapper(
            [this] { stallCheck(); }, name() + ".stall_check");
    }
    if (!m_stallEvent->scheduled()) {
        schedule(m_stallEvent, clockEdge(Cycles(100000)));
    }
    // arm the testers for repetition 0 (they loaded their record lists in
    // their own init(); later repetitions go through windowStartEvent)
    for (size_t r = 0; r < m_testers.size(); r++) {
        if (m_testers[r]) {
            m_testers[r]->setTraceScheduler(this);
            m_testers[r]->traceWindowStart(0);
        }
    }
}

void
TraceScheduler::startup()
{
    ClockedObject::startup();
    // testers arm their own rep-0 state in init(); nothing to start
}

void
TraceScheduler::arrivalDone(long tid)
{
    int rec = (int)(tid - m_rep_base);
    assert(rec >= 0 && rec < (long)m_n_recs);
    if (m_done[rec]) {
        return;
    }
    m_done[rec] = true;
    m_done_count++;
    m_arrivalsDone++;
    m_dbg_arrivals[rec]++;
    event("ARRIVE", tid, m_recs[rec].owner);

    // rule-b bookkeeping for tester-owned records
    if (m_recs[rec].kind != 2) {
        m_testers[m_recs[rec].owner]->traceSelfDone(rec);
    }

    // release dependents whose after set just cleared. The owning tester
    // is notified on EVERY edge (its per-record counter must see all of
    // them); the zero-crossing additionally drives the network side.
    for (int d : m_recs[rec].dependents) {
        m_pending[d]--;
        if (m_recs[d].kind != 2) {
            m_testers[m_recs[d].owner]->traceAfterSatisfied(d);
        }
        if (m_pending[d] == 0) {
            dispatchCleared(d);
            // a deferred re-injection leg for this record can now fire
            long tid = m_rep_base + d;
            if (m_legDeferred.erase(tid)) {
                relayLeg(tid);
            }
        }
    }

    if (m_done_count == m_n_recs) {
        m_windowsCompleted++;
        event("WINDOW_DONE", -1, -1, std::to_string(m_rep));
        if (m_rep + 1 < m_repeat) {
            if (!m_startEvent.scheduled()) {
                schedule(m_startEvent, clockEdge(m_inter_layer + Cycles(1)));
            }
        } else {
            exitSimLoop("trace replay complete");
        }
    }
}

void
TraceScheduler::dispatchCleared(int rec)
{
    // Tester-owned records were already notified on every edge (see
    // arrivalDone); the zero-crossing only drives the network side.
    if (m_recs[rec].kind == 2) {
        m_network->traceReplay()->aftersCleared(rec);
    }
}

void
TraceScheduler::tapDelivered(long tid, int bytes)
{
    m_tapBytes += bytes;
    event("TAP", tid, -1, "bytes=" + std::to_string(bytes));
}

void
TraceScheduler::relayLeg(long tid)
{
    int rec = (int)(tid - m_rep_base);
    if (m_pending[rec] > 0) {
        // the leg's own external after deps are not cleared yet: defer to
        // the zero-crossing in arrivalDone's dependent dispatch
        m_legDeferred.insert(tid);
        event("LEG_DEFER", tid, m_recs[rec].owner);
        return;
    }
    m_legInjections++;
    event("LEG_DISPATCH", tid, m_recs[rec].owner);
    m_testers[m_recs[rec].owner]->traceInjectLeg(tid);
}

void
TraceScheduler::event(const char *ev, long tid, int router,
                      const std::string &detail)
{
    if (!m_event_log.is_open()) {
        return;
    }
    long rec = (tid >= 0) ? (tid - m_rep_base) : -1;
    const SRec *r = (rec >= 0 && rec < (long)m_n_recs) ? &m_recs[rec]
                                                       : nullptr;
    m_event_log << curTick() << ' ' << ev << ' ' << tid << ' ' << rec
                << ' ' << (r ? r->msg : "-") << ' ' << (r ? r->op : "-")
                << ' ' << (r ? r->stage : "-") << ' ' << router;
    if (!detail.empty()) {
        m_event_log << ' ' << detail;
    }
    m_event_log << '\n';
    // flush per line: exitSimLoop may bypass the ofstream destructor and
    // the tail of the log is exactly what the reconciliation needs
    m_event_log.flush();
}

void
TraceScheduler::windowStartEvent(int rep)
{
    if (rep >= m_repeat) {
        return;
    }
    m_rep = rep;
    m_rep_base = (long)rep * m_n_recs;
    m_done_count = 0;
    event("WINDOW_START", -1, -1, "rep=" + std::to_string(rep));
    for (long i = 0; i < m_n_recs; i++) {
        m_pending[i] = (int)m_recs[i].afters.size();
        m_done[i] = false;
    }
    m_dbg_arrivals.clear();
    // re-arm the stall watchdog for this repetition
    if (!m_stallEvent->scheduled()) {
        schedule(m_stallEvent, clockEdge(Cycles(100000)));
    }
    m_network->traceReplay()->windowStart(m_rep_base);
    for (size_t r = 0; r < m_testers.size(); r++) {
        if (m_testers[r]) {
            m_testers[r]->traceWindowStart(m_rep_base);
        }
    }
}

void
TraceScheduler::stallCheck()
{
    if (m_done_count >= m_n_recs) {
        return;
    }
    warn("%s: trace replay stalled: %ld/%ld records done (rep %d)",
         name(), m_done_count, m_n_recs, m_rep);
    for (auto &[tid, cnt] : m_dbg_arrivals) {
        if (cnt > 1) {
            warn("  dbg: tid %ld arrivalDone fired %d times", tid, cnt);
        }
    }
    int dumped = 0;
    for (long i = 0; i < m_n_recs && dumped < 12; i++) {
        if (m_done[i]) continue;
        for (int a : m_recs[i].afters) {
            if (!m_done[a]) {
                warn("  rec %ld (kind %d owner %d) waits on rec %d "
                     "(kind %d owner %d)%s",
                     i, m_recs[i].kind, m_recs[i].owner,
                     a, m_recs[a].kind, m_recs[a].owner,
                     m_done[a] ? "" : " NOT-DONE");
                dumped++;
                break;
            }
        }
    }
    for (auto *t : m_testers) {
        if (t) t->traceDumpStall();
    }
    if (!m_stallEvent->scheduled()) {
        schedule(m_stallEvent, clockEdge(Cycles(100000)));
    }
}

void
TraceScheduler::regStats()
{
    ClockedObject::regStats();

    m_arrivalsDone
        .name(name() + ".trace_arrivals_done")
        .desc("trace replay: burst arrival completions")
        ;
    m_windowsCompleted
        .name(name() + ".trace_windows_completed")
        .desc("trace replay: fully completed window repetitions")
        ;
    m_tapBytes
        .name(name() + ".trace_tap_bytes")
        .desc("trace replay: bytes delivered virtually at taps")
        ;
    m_legInjections
        .name(name() + ".trace_leg_injections")
        .desc("trace replay: branch legs requested from testers")
        ;
}

} // namespace ruby
} // namespace gem5
