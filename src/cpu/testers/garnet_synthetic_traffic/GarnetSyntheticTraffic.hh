/*
 * Copyright (c) 2016 Georgia Institute of Technology
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution;
 * neither the name of the copyright holders nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#ifndef __CPU_GARNET_SYNTHETIC_TRAFFIC_HH__
#define __CPU_GARNET_SYNTHETIC_TRAFFIC_HH__

#include <deque>
#include <set>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "base/random.hh"
#include "base/statistics.hh"
#include "mem/port.hh"
#include "params/GarnetSyntheticTraffic.hh"
#include "sim/clocked_object.hh"
#include "sim/eventq.hh"
#include "sim/sim_exit.hh"
#include "sim/sim_object.hh"
#include "sim/stats.hh"

#include <tuple>

namespace gem5
{

namespace ruby
{
class TraceScheduler;
}

enum TrafficType {BIT_COMPLEMENT_ = 0,
                  BIT_REVERSE_ = 1,
                  BIT_ROTATION_ = 2,
                  NEIGHBOR_ = 3,
                  SHUFFLE_ = 4,
                  TORNADO_ = 5,
                  TRANSPOSE_ = 6,
                  UNIFORM_RANDOM_ = 7,
                  TABLE_ = 8,
                  TRACE_ = 9,
                  NUM_TRAFFIC_PATTERNS_};

class Packet;
class GarnetSyntheticTraffic : public ClockedObject
{
  public:
    typedef GarnetSyntheticTrafficParams Params;
    GarnetSyntheticTraffic(const Params &p);

    void init() override;

    // main simulation loop (one cycle)
    void tick();

    Port &getPort(const std::string &if_name,
                  PortID idx=InvalidPortID) override;

    /**
     * Print state of address in memory system via PrintReq (for
     * debugging).
     */
    void printAddr(Addr a);

  protected:
    EventFunctionWrapper tickEvent;

    class CpuPort : public RequestPort
    {
        GarnetSyntheticTraffic *tester;

      public:

        CpuPort(const std::string &_name, GarnetSyntheticTraffic *_tester)
            : RequestPort(_name), tester(_tester)
        { }

      protected:

        virtual bool recvTimingResp(PacketPtr pkt);

        virtual void recvReqRetry();
    };

    CpuPort cachePort;

    class GarnetSyntheticTrafficSenderState : public Packet::SenderState
    {
      public:
        /** Constructor. */
        GarnetSyntheticTrafficSenderState(uint8_t *_data)
            : data(_data)
        { }

        // Hold onto data pointer
        uint8_t *data;
    };

    PacketPtr retryPkt;
    unsigned size;
    // Network burst payload in bytes stamped on each request (0 = the
    // network's default data-message size).
    unsigned burstSize;
    int id;

    std::map<std::string, TrafficType> trafficStringToEnum;

    unsigned blockSizeBits;
    Addr destShift;
    uint32_t mpuId;
    uint32_t gpuId;
    double crossDieRatio;

    Tick noResponseCycles;

    int numDestinations;
    int linesPerDest;
    Tick simCycles;
    int numPacketsMax;
    int numPacketsSent;
    int singleSender;
    int singleDest;
    int numColumns;
    int activeColumns;
    int activeRows;

    std::string trafficType; // string
    TrafficType traffic; // enum from string
    double injRate;
    double trafficReadRatio;
    int injVnet;
    int precision;

    // TABLE_ traffic (--synthetic=table --traffic-table=<file>), parsed
    // and validated in the constructor. Per source id: destinations in
    // ascending id order with their cumulative PIR thresholds and the
    // flow's transaction type (tuple: cum, dst, isWrite);
    // tableThresholds[id] (sum of the source's pirs) is that source's
    // per-cycle injection probability. --injection-rate is ignored in
    // table mode. A missing rw column in a 3-column legacy line means
    // W (writeback on replacement), matching pre-rw-column semantics.
    std::vector<double> tableThresholds;
    std::vector<std::vector<std::tuple<double, int, bool>>> tableEntries;
    void initTrafficTable(const std::string &fname);

    // ---- TRACE_ traffic (--synthetic=trace --trace-plan=<file>) ----
    // Replays one compiled trace window (see
    // tools/trace_plan/compile_trace_plan.py). This tester injects the
    // records it owns (plan kinds T/R, in seq order) as RELAY_PUTX write
    // requests once (a) the scheduler signals their external after deps
    // cleared and (b) rule (b) holds: no other burst group is in flight
    // (same-msg groups pipeline). Branch legs requested by the scheduler
    // are injected immediately from a dynamic queue.
    struct TraceSend
    {
        long global = -1;   // global record id (rep_base + rec)
        int rec = 0;        // window-local record index
        int seq = 0;        // per-router send order (rule a: issue in seq)
        int msg = 0;        // message index (rule-b same-msg exemption)
        int next = 0;       // destination router (fabric id)
        int bytes = 0;
        int pending = 0;    // external after deps not yet cleared
        int pending0 = 0;   // load-time value (re-armed per window)
        bool ready_logged = false;   // READY event logged (per window)
        bool first_try_logged = false; // FIRST_TRY event logged
        enum State { WAIT, READY, FLIGHT, DONE } state = WAIT;
    };
    bool traceMode = false;
    int traceRouterId = -1;
    std::string tracePlanFile;
    long m_trace_rep_base = 0;
    std::vector<TraceSend> m_trace_sends;  // seq order
    std::unordered_map<int, size_t> m_trace_pos;  // rec idx -> send position
    std::unordered_map<int, int> m_plan_next;     // rec idx -> next router
    std::unordered_map<int, int> m_plan_bytes;    // rec idx -> burst bytes
    std::unordered_map<int, int> m_dbg_notify;    // dbg: notify count per rec
    std::deque<long> m_trace_legs;         // dynamic branch legs (global ids)
    enum { RETRY_NONE, RETRY_SEND, RETRY_LEG } m_trace_retry_kind = RETRY_NONE;
    long m_trace_retry_tid = -1;

    void initTracePlan();
    void traceTick();
    void traceIssueHead();
    void traceIssueLeg(long tid);
    bool traceSendPkt(int rec_local, long global_tid, const char *how);
    PacketPtr makeTracePkt(int rec_local, long global_tid);

  public:
    // trace event log: the scheduler pushes itself in at init
    void setTraceScheduler(ruby::TraceScheduler *s) { m_trace_sched = s; }
    void traceLogReady(long global_tid, int rec_local);

  private:
    ruby::TraceScheduler *m_trace_sched = nullptr;

    // scheduler notifications
  public:
    void traceAfterSatisfied(int rec)
    {
        if (traceMode) {
            m_dbg_notify[rec]++;
            auto &s = m_trace_sends[m_trace_pos.at(rec)];
            s.pending--;
            if (s.pending == 0 && !s.ready_logged) {
                // deps cleared: the record becomes startable (READY);
                // any further delay to its ISSUE is rule-b/seq (T2) or
                // NoC resource wait (T4r) -- split in the attribution
                // (logged from the .cc: TraceScheduler is incomplete here)
                traceLogReady(m_trace_rep_base + rec, rec);
            }
            if (s.pending < 0) {
                warn("%s: rec %d pending went negative (%d notifies)",
                     name(), rec, m_dbg_notify[rec]);
            }
        }
    }
    void traceSelfDone(int rec)
    {
        if (!traceMode) return;
        m_trace_sends[m_trace_pos.at(rec)].state = TraceSend::DONE;
    }
    void traceWindowStart(long rep_base);
    void traceInjectLeg(long tid) { if (traceMode) m_trace_legs.push_back(tid); }
    void traceDumpStall();

    const Cycles responseLimit;

    RequestorID requestorId;

    Random::RandomPtr rng;

    void completeRequest(PacketPtr pkt);

    void generatePkt();
    void sendPkt(PacketPtr pkt);
    void initTrafficType();

    // True if a node/destination lies inside the active (col, row) region.
    bool active(int n) const;

    void doRetry();

    friend class MemCompleteEvent;
};

} // namespace gem5

#endif // __CPU_GARNET_SYNTHETIC_TRAFFIC_HH__
