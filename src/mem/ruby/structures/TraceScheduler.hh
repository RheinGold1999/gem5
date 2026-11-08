/*
 * Copyright (c) 2025 AIGCIC_NOC project.
 *
 * Global trace-replay scheduler (see the design notes in
 * tools/trace_plan/compile_trace_plan.py and TraceReplay.hh).
 *
 * Owns the causal state of the replay: per-record external after-dependency
 * counts and arrival completion. The network's TraceReplay reports arrival
 * completions here; the scheduler dispatches
 *   - to the owning tester:  after-satisfied (starts WAIT_AFTER -> READY),
 *                            self-done (rule-b in-flight bookkeeping),
 *                            branch-leg injection requests;
 *   - to the network:        after-cleared of relay continuations (may
 *                            release held streams).
 *
 * Window repetitions are chained: repetition k+1 starts only after every
 * record of repetition k reached arrival completion, plus an optional
 * inter-layer gap.
 */

#ifndef __MEM_RUBY_STRUCTURES_TRACESCHEDULER_HH__
#define __MEM_RUBY_STRUCTURES_TRACESCHEDULER_HH__

#include <fstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "mem/ruby/network/garnet/GarnetNetwork.hh"
#include "params/TraceScheduler.hh"
#include "sim/clocked_object.hh"

namespace gem5
{

class GarnetSyntheticTraffic;

namespace ruby
{

namespace garnet
{

class TraceReplay;

} // namespace garnet

class TraceScheduler : public ClockedObject
{
  public:
    typedef TraceSchedulerParams Params;
    TraceScheduler(const Params &p);

    void init() override;
    void startup() override;
    void regStats() override;

    // ---- from TraceReplay (synchronous, inside a router wakeup) ----

    // The burst of record 'tid' fully arrived at its next hop.
    void arrivalDone(long tid);
    // Virtual tap delivery at a message-destination router (stats only).
    void tapDelivered(long tid, int bytes);
    // A branch arrival requests the injection of extra leg 'tid' by the
    // leg's owning tester.
    void relayLeg(long tid);

    // ---- optional per-record event log (--trace-event-log) ----
    // One line per replay event: tick event tid rec msg op stage router
    // [detail].  Powers the msg-granularity reconciliation against the
    // original trace (tools/trace_plan/reconcile_event_log.py).  Inactive
    // unless an event-log path was given.
    void event(const char *ev, long tid, int router,
               const std::string &detail = "");
    bool logging() const { return m_event_log.is_open(); }

  private:
    void windowStartEvent(int rep);
    void dispatchCleared(int rec);

    struct SRec
    {
        int kind = 0;               // 0=T 1=R 2=F
        int owner = 0;
        std::vector<int> afters;    // window-local after deps
        std::vector<int> dependents; // reverse edges
        std::string msg, op, stage; // trace annotations (event log)
    };

    void loadPlan(const std::string &path);

    // plan
    std::vector<SRec> m_recs;
    long m_n_recs = 0;

    // causal state (window-local)
    std::vector<int> m_pending;
    std::vector<bool> m_done;
    std::unordered_map<long, int> m_dbg_arrivals; // dbg: arrival count per tid
    std::unordered_set<long> m_legDeferred;       // legs waiting on afters
    long m_done_count = 0;
    int m_rep = -1;
    long m_rep_base = 0;

    // config
    garnet::GarnetNetwork *m_network = nullptr;
    std::vector<GarnetSyntheticTraffic *> m_testers;  // by fabric router id
    int m_repeat = 1;
    Cycles m_inter_layer;
    std::string m_plan_file;

    // optional per-record event log
    std::string m_event_log_path;
    std::ofstream m_event_log;

    EventFunctionWrapper m_startEvent;
    EventFunctionWrapper *m_stallEvent = nullptr;

    // stall watchdog: dump the unsatisfied dependency frontier periodically
    void stallCheck();

    // stats
    statistics::Scalar m_arrivalsDone;
    statistics::Scalar m_windowsCompleted;
    statistics::Scalar m_tapBytes;
    statistics::Scalar m_legInjections;
};

} // namespace ruby
} // namespace gem5

#endif // __MEM_RUBY_STRUCTURES_TRACESCHEDULER_HH__
