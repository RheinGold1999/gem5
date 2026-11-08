/*
 * Copyright (c) 2025 AIGCIC_NOC project.
 *
 * Trace-replay engine for the garnet network.
 *
 * Consumes the plan produced by tools/trace_plan/compile_trace_plan.py
 * (one line per trace record) and implements the in-network relay semantics:
 *
 *   * Every record is one hop. A stream injected by a tester carries the
 *     record's index in RouteInfo::trace_id. At each router the input unit
 *     consults hook() with that id:
 *       - no arrival entry  -> terminal: normal routing ejects to the dir;
 *       - single continuation -> REWRITE: the head flit's destination is
 *         rewritten to the continuation's next hop and the stream identity
 *         becomes the continuation's record (cut-through at head arrival,
 *         body/tail flits follow the input VC);
 *       - held continuation (external after deps not yet cleared, or a
 *         merge waiting for members) -> HOLD: the stream buffers in its
 *         input VC without route computation; release is driven by
 *         aftersCleared() / onArrival() (see below). Credits are not
 *         returned while held, which backpressures the upstream naturally.
 *   * Merges and after-holds: the stream is EJECTED into the local dir
 *     (no buffer slots held -- no credit/path deadlock can form); the
 *     continuation's stream is re-injected by the owning tester's dynamic
 *     leg queue when every member burst has fully arrived and the
 *     continuation's external after deps are cleared. Ejected flits
 *     cross no inter-router links, so link counts stay at the trace's.
 *   * Taps: when the arriving router is itself a message destination, the
 *     copy that the core would receive is delivered virtually (event to the
 *     scheduler at burst completion) -- no local-port flits, which keeps
 *     every inter-router link count exactly at the trace's links.csv.
 *   * Branches (one arrival, k>1 continuations): the first continuation is
 *     the rewrite; the extra legs are re-injected by the owning tester as
 *     RELAY_PUTX write requests at burst completion (scheduler relayLeg).
 *
 * All state is per window repetition; repetitions never overlap (the
 * scheduler starts the next one only after every record of the previous
 * one reached arrival completion), so windowStart() resets it.
 */

#ifndef __MEM_RUBY_NETWORK_GARNET_TRACEREPLAY_HH__
#define __MEM_RUBY_NETWORK_GARNET_TRACEREPLAY_HH__

#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "base/statistics.hh"
#include "base/types.hh"
#include "mem/ruby/network/garnet/CommonTypes.hh"

namespace gem5
{

namespace ruby
{

class TraceScheduler;

namespace garnet
{

class Router;

class TraceReplay
{
  public:
    enum HookAction { HOOK_NORMAL, HOOK_REWRITE, HOOK_EJECT };

    struct HookResult
    {
        HookAction action = HOOK_NORMAL;
        int outport = -1;    // REWRITE: pre-computed output port
        int next = -1;       // REWRITE: rewritten dest router
        int cont = -1;       // continuation GLOBAL tid (new stream identity;
                             // RouteInfo::trace_id must stay global across
                             // window repetitions -- hooks map tid-rep_base)
        int cls = -1;        // REWRITE: dateline VC class (0/1)
        // REWRITE/EJECT: the routing-table destination (the dir agent at
        // 'next', or the local dir for EJECT); travels with every rewrite
        // exactly like dest_router.
        NetDest net_dest;
    };

    TraceReplay();

    void setScheduler(TraceScheduler *sched) { m_scheduler = sched; }

    bool enabled() const { return m_enabled; }

    // Parse <window>/_plan/plan.txt. fabric_cols must match the network's
    // column count (router id = row * fabric_cols + col).
    void loadPlan(const std::string &plan_path, int fabric_cols,
                  int num_routers, RubySystem *rs);

    // Called at window (repetition) start: resets merge/hold state.
    // rep_base is the global record-id offset of this repetition.
    void windowStart(long rep_base);

    // Input-unit hook, invoked once per HEAD/HEAD_TAIL flit of a trace
    // stream (trace_id >= 0) before route computation.
    HookResult hook(Router *router, int inport, int vc, int trace_id);

    // Expected flit count of the burst carrying 'tid' (spec 6: the burst
    // completion asserts the arrived flit count against the plan).
    int expectedFlits(int trace_id) const;

    // The scheduler cleared the external after deps of record 'cont' (a
    // relay continuation). May fire the re-injection leg.
    void aftersCleared(int cont);

    // Burst completion: the tail flit of record 'trace_id' was just buffered
    // at 'router_id'. Fires arrival events and merge bookkeeping.
    void onArrival(int router_id, int inport, int vc, int trace_id,
                   Router *router);

    // Global record id of a window-local index for the current repetition.
    int globalTid(int rec) const { return m_rep_base + rec; }

    // plan accessors (used by the scheduler and the testers)
    int numRecs() const { return m_recs.size(); }
    int recKind(int rec) const { return m_recs[rec].kind; }
    int recOwner(int rec) const { return m_recs[rec].owner; }
    int recNext(int rec) const { return m_recs[rec].next; }
    int recBytes(int rec) const { return m_recs[rec].bytes; }
    int recMsg(int rec) const { return m_recs[rec].msg; }
    const std::vector<int> &recAfters(int rec) const
    { return m_recs[rec].afters; }
    const std::string &recOp(int rec) const { return m_recs[rec].op; }
    const std::string &recStage(int rec) const { return m_recs[rec].stage; }

    void regStats(const std::string &prefix);

    // stats
    statistics::Scalar m_relayRewrites;
    statistics::Scalar m_relayHolds;
    statistics::Scalar m_relayDrains;
    statistics::Scalar m_relayDrainFlits;
    statistics::Scalar m_tapDelivered;
    statistics::Scalar m_tapDeliveredBytes;
    statistics::Scalar m_branchLegs;

  private:
    struct TraceRec
    {
        int kind = 0;        // 0=T(send) 1=R(root) 2=F(relay)
        int owner = 0;
        int seq = 0;
        int next = 0;
        int bytes = 0;
        int tap = 0;
        int cls = 0;
        int lclock = 0;
        int msg = 0;
        std::string op;
        std::string stage;
        std::string rid;
        NetDest term_dest;   // dir-agent destination at rec.next (terminal eject)
        std::vector<int> ups;     // listed same-msg upstream arrivals
        std::vector<int> abs;     // absorbed legs (this rec is the merge cont)
        std::vector<int> afters;  // external after deps
    };

    struct TraceCont
    {
        int rec;
        bool absorb;
    };

    // What a given arriving record triggers at its next router.
    struct ArrivalEntry
    {
        std::vector<TraceCont> conts;  // empty = terminal (normal eject)
        bool tap = false;              // arriving router is a msg dst
        int hold_id = -1;              // release group (single or merge)
    };

    // Release group = one continuation whose stream was ejected at this
    // router and will be re-injected when every member burst has fully
    // arrived AND the continuation's external after deps are cleared.
    struct HoldGroup
    {
        int cont = -1;
        int group_size = 0;          // ups + absorbed members
        int arrivals_remaining = 0;  // member bursts not yet fully arrived
        bool afters_ok = false;
        bool fired = false;          // re-injection leg scheduled
    };

    void maybeFire(int cont);
    int outportForHop(int router_id, int next_router) const;

    bool m_enabled = false;
    int m_fabric_cols = 8;
    int m_num_routers = 0;
    int m_n_rows = 0;
    int m_flit_bytes = 128;      // plan META flit_bytes (drain-flit accounting)
    long m_rep_base = 0;         // global tid base of the current repetition

    std::vector<TraceRec> m_recs;
    std::vector<ArrivalEntry> m_arrival;   // indexed by arriving record
    std::vector<HoldGroup> m_groups;       // indexed by continuation record
    std::vector<int> m_rec_group;          // rec -> release group (-1 none)
    std::vector<NetDest> m_local_dest;     // per-router local dir destination

    TraceScheduler *m_scheduler = nullptr;
};

} // namespace garnet
} // namespace ruby
} // namespace gem5

#endif // __MEM_RUBY_NETWORK_GARNET_TRACEREPLAY_HH__
