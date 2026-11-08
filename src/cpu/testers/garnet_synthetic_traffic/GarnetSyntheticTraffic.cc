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

#include "cpu/testers/garnet_synthetic_traffic/GarnetSyntheticTraffic.hh"
#include "mem/ruby/structures/TraceScheduler.hh"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "base/intmath.hh"
#include "base/logging.hh"
#include "base/statistics.hh"
#include "debug/GarnetSyntheticTraffic.hh"
#include "mem/packet.hh"
#include "mem/port.hh"
#include "mem/request.hh"
#include "sim/sim_events.hh"
#include "sim/stats.hh"
#include "sim/system.hh"

#include <fstream>
#include <map>
#include <sstream>

namespace gem5
{

int TESTER_NETWORK=0;

bool
GarnetSyntheticTraffic::CpuPort::recvTimingResp(PacketPtr pkt)
{
    tester->completeRequest(pkt);
    return true;
}

void
GarnetSyntheticTraffic::CpuPort::recvReqRetry()
{
    tester->doRetry();
}

void
GarnetSyntheticTraffic::sendPkt(PacketPtr pkt)
{
    if (!cachePort.sendTimingReq(pkt)) {
        retryPkt = pkt; // RubyPort will retry sending
    }
    numPacketsSent++;
}

GarnetSyntheticTraffic::GarnetSyntheticTraffic(const Params &p)
    : ClockedObject(p),
      tickEvent([this]{ tick(); }, "GarnetSyntheticTraffic tick",
                false, Event::CPU_Tick_Pri),
      cachePort("GarnetSyntheticTraffic", this),
      retryPkt(NULL),
      size(p.memory_size),
      burstSize(p.burst_size),
      blockSizeBits(p.block_offset),
      destShift(p.dest_shift),
      mpuId(p.mpu_id),
      gpuId(p.gpu_id),
      crossDieRatio(p.cross_die_ratio),
      numDestinations(p.num_dest),
      linesPerDest(p.lines_per_dest),
      simCycles(p.sim_cycles),
      numPacketsMax(p.num_packets_max),
      numPacketsSent(0),
      singleSender(p.single_sender),
      singleDest(p.single_dest),
      numColumns(p.num_columns),
      activeColumns(p.active_columns),
      activeRows(p.active_rows),
      trafficType(p.traffic_type),
      injRate(p.inj_rate),
      trafficReadRatio(p.traffic_read_ratio),
      injVnet(p.inj_vnet),
      precision(p.precision),
      responseLimit(p.response_limit),
      requestorId(p.system->getRequestorId(this))
{
    // set up counters
    noResponseCycles = 0;
    schedule(tickEvent, 0);

    initTrafficType();
    if (trafficStringToEnum.count(trafficType) == 0) {
        fatal("Unknown Traffic Type: %s!\n", traffic);
    }
    traffic = trafficStringToEnum[trafficType];

    if (trafficReadRatio < 0.0 || trafficReadRatio > 1.0) {
        fatal("%s: --traffic-read-ratio %g outside [0, 1]\n",
              name(), trafficReadRatio);
    }
    DPRINTF(GarnetSyntheticTraffic,
            "read/write mix: reads drawn with probability %g (retired "
            "inj_vnet-based type selection; param value %d kept for "
            "compatibility)\n", trafficReadRatio, injVnet);

    // Table-driven traffic: parse and validate --traffic-table up front,
    // strictly fatal on any inconsistency, so a typo can never silently
    // distort the experiment.
    if (traffic == TABLE_) {
        if (p.traffic_table.empty()) {
            fatal("%s: traffic_type='table' requires --traffic-table=<file>\n",
                  name());
        }
        initTrafficTable(p.traffic_table);
    } else if (!p.traffic_table.empty()) {
        fatal("%s: --traffic-table is only valid with --synthetic=table\n",
              name());
    }

    // One-time validation: active-column/row masking can only re-draw a
    // uniformly-random destination, so all other patterns are rejected
    // here. NEIGHBOR_ is masking-aware (destinations stay inside the
    // active region and only active sources inject), and TABLE_ is
    // validated at parse time to reference only active nodes, so both
    // are allowed too.
    if (activeColumns > 0 || activeRows > 0) {
        assert(traffic == UNIFORM_RANDOM_ || traffic == NEIGHBOR_ ||
               traffic == TABLE_ || traffic == TRACE_);
    }

    id = TESTER_NETWORK++;
    // Initialize random generator with unique seed based on tester ID
    rng = Random::genRandom(Random::globalSeed + id);
    DPRINTF(GarnetSyntheticTraffic,"Config Created: Name = %s , and id = %d\n",
            name(), id);

    // Trace-replay mode: --synthetic=trace --trace-plan=<plan.txt>. This
    // tester injects the records it owns (fabric router id =
    // trace_router_id) per the compiled plan; the global TraceScheduler
    // drives the causal state.
    traceMode = (traffic == TRACE_);
    if (traceMode) {
        if (p.trace_plan_file.empty() || p.trace_router_id < 0) {
            fatal("%s: traffic_type='trace' requires --trace-plan and "
                  "--trace-router-id\n", name());
        }
        tracePlanFile = p.trace_plan_file;
        traceRouterId = p.trace_router_id;
        initTracePlan();
    }
}

Port &
GarnetSyntheticTraffic::getPort(const std::string &if_name, PortID idx)
{
    if (if_name == "test")
        return cachePort;
    else
        return ClockedObject::getPort(if_name, idx);
}

void
GarnetSyntheticTraffic::init()
{
    numPacketsSent = 0;
}

// ---- trace replay ----

void
GarnetSyntheticTraffic::initTracePlan()
{
    std::ifstream in(tracePlanFile);
    if (!in) {
        fatal("%s: cannot open trace plan %s", name(), tracePlanFile);
    }
    std::string line;
    while (std::getline(in, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream iss(line);
        std::string tag;
        iss >> tag;
        if (tag == "META") continue;
        int idx = std::stoi(tag);
        std::string kind_tok;
        int owner, seq, next, bytes, tap, cls, lclock, n;
        std::string msg_str, op, stage, rid;
        iss >> kind_tok >> owner >> seq >> next >> bytes >> tap >> cls;
        int rec_seq = seq;   // per-router send order
        iss >> n;                                            // n_ups
        for (int i = 0; i < n; i++) { int x; iss >> x; }     // ups
        iss >> n;                                            // n_abs
        for (int i = 0; i < n; i++) { int x; iss >> x; }     // abs
        iss >> n;                                            // n_after
        std::vector<int> afters(n);
        for (int i = 0; i < n; i++) iss >> afters[i];
        iss >> lclock >> msg_str >> op >> stage >> rid;
        (void)lclock;

        // plan cache for every record (branch legs inject from here too)
        m_plan_next[idx] = next;
        m_plan_bytes[idx] = bytes;

        if (owner != traceRouterId || kind_tok == "F") {
            continue;   // not injected by this tester
        }
        TraceSend s;
        s.rec = idx;
        s.seq = rec_seq;
        s.msg = std::stol(msg_str.substr(1));   // 'mNNNNN' -> NNNNN
        s.next = next;
        s.bytes = bytes;
        s.pending = s.pending0 = (int)afters.size();
        s.state = TraceSend::WAIT;
        m_trace_pos[idx] = m_trace_sends.size();
        m_trace_sends.push_back(s);
    }
    // rule (a): the tester issues its sends in the trace's per-router seq
    // order.  The plan file is in record-index order, which is NOT seq
    // order (seq interleaves with relay records and can disagree with the
    // lclock order), so sort by seq and rebuild the position index.
    std::sort(m_trace_sends.begin(), m_trace_sends.end(),
              [](const TraceSend &a, const TraceSend &b)
              { return a.seq < b.seq; });
    for (size_t i = 0; i < m_trace_sends.size(); i++) {
        m_trace_pos[m_trace_sends[i].rec] = i;
    }
    inform("%s: trace mode armed, %d owned records", name(),
           (int)m_trace_sends.size());
}

void
GarnetSyntheticTraffic::traceWindowStart(long rep_base)
{
    m_trace_rep_base = rep_base;
    for (auto &s : m_trace_sends) {
        s.global = rep_base + s.rec;
        s.state = TraceSend::WAIT;
        s.pending = s.pending0;
    }
    m_trace_head = 0;
    m_trace_inflight = 0;
    m_trace_inflight_msg = -1;
    m_trace_legs.clear();
    m_trace_retry_kind = RETRY_NONE;
    m_trace_retry_tid = -1;
    // a retry pkt from the previous window is stale: drop it
    if (retryPkt) {
        delete retryPkt;
        retryPkt = nullptr;
    }
}

void
GarnetSyntheticTraffic::traceTick()
{
    // dynamic branch legs first (network-driven, immediate)
    while (!m_trace_legs.empty()) {
        long tid = m_trace_legs.front();
        if (traceSendPkt((int)(tid - m_trace_rep_base), tid, "leg")) {
            m_trace_legs.pop_front();
        } else {
            return;    // held in retryPkt; recvReqRetry resumes
        }
    }

    traceIssueHead();
}

void
GarnetSyntheticTraffic::traceIssueHead()
{
    while (m_trace_head < m_trace_sends.size()) {
        TraceSend &s = m_trace_sends[m_trace_head];
        if (s.state == TraceSend::DONE || s.state == TraceSend::FLIGHT) {
            m_trace_head++;
            continue;
        }
        if (s.pending > 0) {
            return;                       // WAIT_AFTER (after deps)
        }
        if (m_trace_inflight > 0 && m_trace_inflight_msg != s.msg) {
            return;                       // rule (b): one burst group at a time
        }
        if (!traceSendPkt(s.rec, s.global, "head")) {
            return;                       // port busy, recvReqRetry resumes
        }
        s.state = TraceSend::FLIGHT;
        m_trace_inflight++;
        m_trace_inflight_msg = s.msg;
        m_trace_head++;
    }
}

PacketPtr
GarnetSyntheticTraffic::makeTracePkt(int rec_local, long global_tid)
{
    // address: the destination router's per-node window (mpu52 layout);
    // the offset encodes the record index so every relay request has a
    // unique address (the dir pops it statelessly)
    panic_if(m_plan_next.find(rec_local) == m_plan_next.end(),
             "%s: trace rec %d not in plan cache", name(), rec_local);
    int next = m_plan_next[rec_local];
    int bytes = m_plan_bytes[rec_local];
    unsigned node_x = next / numColumns;    // mesh row
    unsigned node_y = next % numColumns;    // mesh column
    Addr paddr = (Addr)((node_y << 2) | node_x) << destShift;
    paddr |= (Addr)rec_local << blockSizeBits;

    RequestPtr req = std::make_shared<Request>(paddr, 1, Request::Flags(),
                                               requestorId);
    req->setContext(id);
    // extraData: [63:32] record index + 1 (0 = not trace), [31:0] burst
    req->setExtraData(((uint64_t)(global_tid + 1) << 32) |
                      (uint64_t)(uint32_t)bytes);

    PacketPtr pkt = new Packet(req, MemCmd::WriteReq);
    pkt->dataDynamic(new uint8_t[req->getSize()]);
    pkt->senderState = NULL;
    return pkt;
}

void
GarnetSyntheticTraffic::traceDumpStall()
{
    if (!traceMode) return;
    int waiting_dep = 0, waiting_rb = 0, flight = 0, done = 0;
    for (auto &s : m_trace_sends) {
        if (s.state == TraceSend::DONE) { done++; continue; }
        if (s.state == TraceSend::FLIGHT) { flight++; continue; }
        if (s.pending > 0) waiting_dep++;
        else waiting_rb++;
    }
    inform("%s: trace stall dump: head %d/%d done %d flight %d "
           "wait-dep %d wait-ruleb %d inflight %d (msg %d)",
           name(), (int)m_trace_head, (int)m_trace_sends.size(), done,
           flight, waiting_dep, waiting_rb, m_trace_inflight,
           m_trace_inflight_msg);
    // the first two non-done records with their blockers
    int shown = 0;
    for (auto &s : m_trace_sends) {
        if (s.state == TraceSend::DONE || s.state == TraceSend::FLIGHT ||
            shown >= 2) {
            continue;
        }
        inform("  rec %d (msg %d next %d bytes %d) state %d pending %d",
               s.rec, s.msg, s.next, s.bytes, (int)s.state, s.pending);
        shown++;
    }
}

bool
GarnetSyntheticTraffic::traceSendPkt(int rec_local, long global_tid,
                                     const char *how)
{
    PacketPtr pkt = makeTracePkt(rec_local, global_tid);
    DPRINTF(GarnetSyntheticTraffic, "trace issue rec %d tid %ld\n",
            rec_local, global_tid);
    if (!cachePort.sendTimingReq(pkt)) {
        DPRINTF(GarnetSyntheticTraffic, "trace issue rec %d RETRY\n",
                rec_local);
        retryPkt = pkt;    // RubyPort will retry (recvReqRetry)
        // m_trace_sends is seq-sorted: record index != position.  The
        // failed send was either the current head (not yet advanced) or
        // the front leg (not yet popped) -- classify against the head.
        m_trace_retry_kind =
            (m_trace_head < m_trace_sends.size() &&
             m_trace_sends[m_trace_head].global == global_tid)
            ? RETRY_HEAD : RETRY_LEG;
        m_trace_retry_tid = global_tid;
        return false;
    }
    numPacketsSent++;
    if (m_trace_sched) {
        m_trace_sched->event("ISSUE", global_tid, traceRouterId, how);
    }
    return true;
}


void
GarnetSyntheticTraffic::completeRequest(PacketPtr pkt)
{
    DPRINTF(GarnetSyntheticTraffic,
            "Completed injection of %s packet for address %x\n",
            pkt->isWrite() ? "write" : "read\n",
            pkt->req->getPaddr());

    assert(pkt->isResponse());
    noResponseCycles = 0;
    delete pkt;
}


void
GarnetSyntheticTraffic::tick()
{
    // trace replay: the global scheduler owns progress and exit (idle
    // testers own no records, so the response watchdog must not fire)
    if (!traceMode && ++noResponseCycles >= responseLimit) {
        fatal("%s deadlocked at cycle %d\n", name(), curTick());
    }

    if (traceMode) {
        // trace replay: the scheduler owns the causal state and the exit;
        // this tick just drives the issue engine
        traceTick();
        if (curTick() >= simCycles) {
            exitSimLoop("Network Tester completed simCycles");
        } else {
            if (!tickEvent.scheduled())
                schedule(tickEvent, clockEdge(Cycles(1)));
        }
        return;
    }

    // make new request based on injection rate
    // (injection rate's range depends on precision)
    // - generate a random number between 0 and 10^precision
    // - send pkt if this number is < injRate*(10^precision)
    bool sendAllowedThisCycle;
    if (traffic == TABLE_) {
        // The per-pair PIR table fully governs table mode;
        // --injection-rate is ignored. generatePkt() applies the
        // per-source threshold (sum of the source's pirs).
        sendAllowedThisCycle = true;
    } else {
        double injRange = pow((double) 10, (double) precision);
        unsigned trySending = rng->random<unsigned>(0, (int) injRange);
        if (trySending < injRate*injRange)
            sendAllowedThisCycle = true;
        else
            sendAllowedThisCycle = false;
    }

    // always generatePkt unless fixedPkts or singleSender is enabled
    if (sendAllowedThisCycle) {
        bool senderEnable = true;

        if (numPacketsMax >= 0 && numPacketsSent >= numPacketsMax)
            senderEnable = false;

        if (singleSender >= 0 && id != singleSender)
            senderEnable = false;

        if (senderEnable)
            generatePkt();
    }

    // Schedule wakeup
    if (curTick() >= simCycles)
        exitSimLoop("Network Tester completed simCycles");
    else {
        if (!tickEvent.scheduled())
            schedule(tickEvent, clockEdge(Cycles(1)));
    }
}

void
GarnetSyntheticTraffic::generatePkt()
{
    int num_destinations = numDestinations;
    int radix = (int) sqrt(num_destinations);
    unsigned destination = id;
    int dest_x = -1;
    int dest_y = -1;
    int source = id;
    int src_x = id%radix;
    int src_y = id/radix;
    // Transaction type of the generated access; only table mode assigns
    // it here (from the chosen flow's rw column). Other modes draw it
    // from --traffic-read-ratio right before the request is built.
    bool flowWrite = true;

    // Condition 1: sources outside the active (row, col) region never inject.
    if (activeColumns > 0 || activeRows > 0) {
        if (!active(id))
            return;
    }

    if (traffic == TABLE_) {
        // Per-source PIR table: with probability tableThresholds[id] a
        // packet is injected this cycle; the destination (and the flow's
        // rw type) is picked from the cumulative PIR distribution (entry
        // i wins when r falls in [cum[i-1], cum[i])). singleDest, if set,
        // only overrides the destination, not the threshold gate.
        double r = rng->random<double>();
        if (r >= tableThresholds[id]) {
            return;
        }
        flowWrite = std::get<2>(tableEntries[id].back());
        destination = std::get<1>(tableEntries[id].back());
        for (const auto &entry : tableEntries[id]) {
            if (r < std::get<0>(entry)) {
                flowWrite = std::get<2>(entry);
                destination = std::get<1>(entry);
                break;
            }
        }
        if (singleDest >= 0) {
            destination = singleDest;
        }
    } else if (singleDest >= 0)
    {
        destination = singleDest;
    } else if (traffic == UNIFORM_RANDOM_) {
        // Condition 2: re-draw until the destination is inside the active region.
        do {
            destination = rng->random<unsigned>(0, num_destinations - 1);
        } while (!active(destination));
    } else if (traffic == BIT_COMPLEMENT_) {
        dest_x = radix - src_x - 1;
        dest_y = radix - src_y - 1;
        destination = dest_y*radix + dest_x;
    } else if (traffic == BIT_REVERSE_) {
        unsigned int straight = source;
        unsigned int reverse = source & 1; // LSB

        int num_bits = (int) log2(num_destinations);

        for (int i = 1; i < num_bits; i++)
        {
            reverse <<= 1;
            straight >>= 1;
            reverse |= (straight & 1); // LSB
        }
        destination = reverse;
    } else if (traffic == BIT_ROTATION_) {
        if (source%2 == 0)
            destination = source/2;
        else // (source%2 == 1)
            destination = ((source/2) + (num_destinations/2));
    } else if (traffic == NEIGHBOR_) {
        // Neighbor traffic on the physical mesh, not on the square radix
        // grid. radix = sqrt(num_destinations) assumes a square mesh and
        // breaks on non-square fabrics (e.g. 32 routers = 4 rows x 8
        // columns, where radix=5 distorts the geometry and can even
        // overflow the destination: id=31 -> dest=32). Strict
        // nearest-neighbor semantics: every node injects at the full
        // rate, each packet going to one uniformly chosen physically
        // adjacent node (E/W/N/S) that exists inside the active region
        // (the active sub-mesh when masking is on, the fabric
        // otherwise). Interior nodes choose among 4 neighbors, edge
        // nodes among 3, corner nodes among 2 -- no torus wrap, and no
        // node is silenced for sitting on an edge. All inactive sources
        // are already blocked by the active() check above.
        const int src_x = id % numColumns;
        const int src_y = id / numColumns;
        const int max_x = (activeColumns > 0) ? activeColumns - 1
                                              : numColumns - 1;
        const int max_y = (activeRows > 0)
            ? activeRows - 1 : (numDestinations / numColumns) - 1;
        int neighbors[4];
        int num_neighbors = 0;
        if (src_x < max_x)  // east
            neighbors[num_neighbors++] = src_y * numColumns + src_x + 1;
        if (src_x > 0)      // west
            neighbors[num_neighbors++] = src_y * numColumns + src_x - 1;
        if (src_y < max_y)  // south (next row)
            neighbors[num_neighbors++] = (src_y + 1) * numColumns + src_x;
        if (src_y > 0)      // north (previous row)
            neighbors[num_neighbors++] = (src_y - 1) * numColumns + src_x;
        if (num_neighbors == 0) {   // degenerate 1x1 active region
            return;
        }
        destination = neighbors[rng->random<unsigned>(0,
                                                      num_neighbors - 1)];
    } else if (traffic == SHUFFLE_) {
        if (source < num_destinations/2)
            destination = source*2;
        else
            destination = (source*2 - num_destinations + 1);
    } else if (traffic == TRANSPOSE_) {
            dest_x = src_y;
            dest_y = src_x;
            destination = dest_y*radix + dest_x;
    } else if (traffic == TORNADO_) {
        dest_x = (src_x + (int) ceil(radix/2) - 1) % radix;
        dest_y = src_y;
        destination = dest_y*radix + dest_x;
    }
    else {
        fatal("Unknown Traffic Type: %s!\n", traffic);
    }

    // The source of the packets is a cache.
    // The destination of the packets is a directory.
    //
    // dest_shift == 0 (legacy layout): the destination bits are embedded
    // in the address right after the byte-offset, matching the
    // directories' block-interleaved address mapping. With
    // lines_per_dest > 1, random line-select bits are placed right above
    // the destination bits so that packets to one destination spread over
    // many cache lines instead of all hitting the same line. These bits
    // change the line but not the home directory, as long as they stay
    // below any address-hashing bits of the directory mapping (e.g.
    // Ruby's xor_low_bit, default 20).
    //
    // dest_shift > 0 (e.g. 32 for the MPU-52 layout): the destination
    // field sits at [dest_shift, dest_shift + ceilLog2(num_dest)) and the
    // line-select bits live INSIDE the destination's local address space,
    // directly above the block offset. The address selects the owning
    // node's per-node window via range-based directory mapping, so no
    // XOR hashing is involved.
    Addr paddr;
    // Cross-die traffic: with probability cross_die_ratio, stamp a
    // remote mpu_id (0..7, != local) into bits [39:37] of the address.
    // The L1 compares the die fields against its own identity and issues
    // mismatching requests on the cross-die vnets (vnet1/vnet3). gpu_id
    // is kept local (a gpu_id mismatch works identically).
    bool remoteDie = crossDieRatio > 0 &&
        rng->random<double>() < crossDieRatio;
    if (destShift > 0) {
        // `destination` is a mesh router id (so the active-region mask
        // and the traffic patterns operate in router space, as in the
        // legacy layout). Convert it to the MPU-52 node field:
        //   node_x = mesh row (bits [33:32]), node_y = mesh column
        //   (bits [36:34])  =>  field = (col << 2) | row.
        unsigned node_x = destination / numColumns;  // mesh row
        unsigned node_y = destination % numColumns;  // mesh column
        if (remoteDie) {
            // Cross-die traffic leaves the die through the die-to-die
            // link on mesh column 0: route to the column-0 node in the
            // same row as the SOURCE (shortest in-die path to this
            // die's D2D port; no vertical detour along column 0).
            // The dir agent there proxies the remote die's memory.
            node_x = id / numColumns;   // source's mesh row
            node_y = 0;
        }
        paddr = (Addr)((node_y << 2) | node_x) << destShift;
        if (linesPerDest > 1) {
            Addr line_sel = rng->random<unsigned>(0, linesPerDest - 1);
            paddr |= line_sel << blockSizeBits;
        }
        // Die identity in the address: this die's own id by default, or
        // a remote mpu_id (never equal to the local one) for cross-die
        // requests. The L1 compares these fields against its own
        // identity to decide the request vnet.
        unsigned mpu = mpuId;
        if (remoteDie) {
            mpu = rng->random<unsigned>(0, 6);
            if (mpu >= mpuId) {
                mpu += 1;  // uniform over {0..7} \ {mpuId}
            }
        }
        paddr |= (Addr)(mpu & 0x7) << 37;
        paddr |= (Addr)(gpuId & 0x3ff) << 41;
    } else {
        paddr = destination;
        if (linesPerDest > 1) {
            Addr line_sel = rng->random<unsigned>(0, linesPerDest - 1);
            paddr |= line_sel << ceilLog2(numDestinations);
        }
        paddr <<= blockSizeBits;
        if (remoteDie) {
            unsigned remoteMpu = rng->random<unsigned>(0, 6);
            if (remoteMpu >= mpuId) {
                remoteMpu += 1;  // uniform over {0..7} \ {mpuId}
            }
            paddr |= (Addr)remoteMpu << 37;
        }
    }
    unsigned access_size = 1; // Does not affect Ruby simulation

    // Modeling different coherence msg types over different msg classes.
    //
    // GarnetSyntheticTraffic assumes the Garnet_standalone coherence protocol
    // which models three message classes/virtual networks.
    // These are: request, forward, response.
    // requests and forwards are "control" packets (typically 8 bytes),
    // while responses are "data" packets (typically 72 bytes).
    //
    // Life of a packet from the tester into the network:
    // (1) generatePkt() generates a ReadReq (read) or WriteReq (write),
    //     per --traffic-read-ratio (non-table modes) or the table's rw
    //     column (table mode)
    // (2) mem/ruby/system/RubyPort.cc converts these to RubyRequestType_LD,
    //     RubyRequestType_IFETCH, RubyRequestType_ST respectively
    // (3) mem/ruby/system/Sequencer.cc sends these to the cache controllers
    //     in the coherence protocol.
    // (4) Network_test-cache.sm tags RubyRequestType:LD,
    //     RubyRequestType:IFETCH and RubyRequestType:ST as
    //     Request, Forward, and Response events respectively;
    //     and injects them into virtual networks 0, 1 and 2 respectively.
    //     It immediately calls back the sequencer.
    // (5) The packet traverses the network (simple/garnet) and reaches its
    //     destination (Directory), and network stats are updated.
    // (6) Network_test-dir.sm simply drops the packet.
    //
    MemCmd::Command requestType;

    RequestPtr req = nullptr;
    Request::Flags flags;

    // Read vs write: in table mode the chosen flow's rw column decides;
    // in all other modes --traffic-read-ratio decides (default 0.5).
    // Under AIGCIC_NOC the network vnet is chosen by the protocol from
    // the address (internal vs cross-die), so the retired inj_vnet-based
    // type selection no longer applies. Reads fetch clean lines (the L1
    // drops them silently on replacement, no PUTX/WB_ACK); writes fetch
    // dirty lines that are written back on replacement.
    bool isRead = traffic == TABLE_
        ? !flowWrite
        : rng->random<double>() < trafficReadRatio;
    requestType = isRead ? MemCmd::ReadReq : MemCmd::WriteReq;
    req = std::make_shared<Request>(paddr, access_size, flags,
                                    requestorId);

    req->setContext(id);

    // Optional network burst size: the functional access stays one byte
    // (the protocol's block size and data payload are untouched); only
    // the on-NoC message length grows, so the NI emits ceil(burst/128)
    // flits for this request and for the matching data reply.
    if (burstSize > 0) {
        req->setExtraData(burstSize);
    }

    //No need to do functional simulation
    //We just do timing simulation of the network

    DPRINTF(GarnetSyntheticTraffic,
            "Generated packet with destination %d, embedded in address %x\n",
            destination, req->getPaddr());

    PacketPtr pkt = new Packet(req, requestType);
    pkt->dataDynamic(new uint8_t[req->getSize()]);
    pkt->senderState = NULL;

    sendPkt(pkt);
}

void
GarnetSyntheticTraffic::initTrafficType()
{
    trafficStringToEnum["bit_complement"] = BIT_COMPLEMENT_;
    trafficStringToEnum["bit_reverse"] = BIT_REVERSE_;
    trafficStringToEnum["bit_rotation"] = BIT_ROTATION_;
    trafficStringToEnum["neighbor"] = NEIGHBOR_;
    trafficStringToEnum["shuffle"] = SHUFFLE_;
    trafficStringToEnum["tornado"] = TORNADO_;
    trafficStringToEnum["transpose"] = TRANSPOSE_;
    trafficStringToEnum["uniform_random"] = UNIFORM_RANDOM_;
    trafficStringToEnum["table"] = TABLE_;
    trafficStringToEnum["trace"] = TRACE_;
}

void
GarnetSyntheticTraffic::initTrafficTable(const std::string &fname)
{
    std::ifstream in(fname);
    if (!in) {
        fatal("%s: cannot open --traffic-table file '%s'\n", name(), fname);
    }

    tableThresholds.assign(numDestinations, 0.0);
    tableEntries.assign(numDestinations, {});

    // First pass: per-source (dst -> (pir, isWrite)) maps; catches range,
    // pir-value, type and duplicate errors before anything is cumulated.
    std::vector<std::map<int, std::pair<double, bool>>> raw(numDestinations);
    size_t num_pairs = 0;

    std::string line;
    int lineno = 0;
    bool headerSeen = false;
    while (std::getline(in, line)) {
        lineno++;
        const size_t first = line.find_first_not_of(" \t\r\n");
        if (first == std::string::npos || line[first] == '#')
            continue;  // blank or comment line

        std::istringstream iss(line.substr(first));
        int src, dst;
        std::string tok;
        if (!(iss >> src >> dst >> tok)) {
            // Tolerate exactly one header line, e.g. "src dst rw pir":
            // a non-numeric line without any digit character (real data
            // lines always contain digits, so this cannot mask typos).
            if (!headerSeen &&
                line.find_first_of("0123456789") == std::string::npos) {
                headerSeen = true;
                continue;
            }
            fatal("%s: %s:%d: expected 'src_id dst_id [R|W] pir' (or "
                  "legacy 'src_id dst_id pir')\n",
                  name(), fname, lineno);
        }
        // 3rd column: transaction type (new format 'src dst rw pir') or
        // the pir itself (legacy 3-column 'src dst pir', which keeps the
        // pre-rw-column semantics: W).
        bool isWrite = true;
        bool rwGiven = false;
        double pir;
        if (tok == "R" || tok == "r" || tok == "W" || tok == "w") {
            isWrite = (tok[0] == 'W' || tok[0] == 'w');
            rwGiven = true;
        } else {
            size_t pos = 0;
            try {
                pir = std::stod(tok, &pos);
            } catch (const std::exception &) {
                fatal("%s: %s:%d: expected 'src_id dst_id [R|W] pir' (or "
                      "legacy 'src_id dst_id pir'); 3rd token '%s' is "
                      "neither R/W nor a pir\n",
                      name(), fname, lineno, tok.c_str());
            }
            if (pos != tok.size()) {
                fatal("%s: %s:%d: malformed pir '%s'\n",
                      name(), fname, lineno, tok.c_str());
            }
        }
        if (rwGiven && !(iss >> pir)) {
            fatal("%s: %s:%d: missing pir after the rw column\n",
                  name(), fname, lineno);
        }
        std::string extra;
        if (iss >> extra) {
            fatal("%s: %s:%d: trailing characters ('%s'); format is "
                  "'src_id dst_id [R|W] pir'\n",
                  name(), fname, lineno, extra.c_str());
        }

        if (src < 0 || src >= numDestinations)
            fatal("%s: %s:%d: src_id %d outside [0, %d]\n",
                  name(), fname, lineno, src, numDestinations - 1);
        if (dst < 0 || dst >= numDestinations)
            fatal("%s: %s:%d: dst_id %d outside [0, %d]\n",
                  name(), fname, lineno, dst, numDestinations - 1);
        if (pir < 0.0 || pir > 1.0)
            fatal("%s: %s:%d: pir %g outside [0, 1]\n",
                  name(), fname, lineno, pir);
        if ((activeColumns > 0 || activeRows > 0) && !active(src))
            fatal("%s: %s:%d: src_id %d lies outside the active region\n",
                  name(), fname, lineno, src);
        if ((activeColumns > 0 || activeRows > 0) && !active(dst))
            fatal("%s: %s:%d: dst_id %d lies outside the active region\n",
                  name(), fname, lineno, dst);
        if (raw[src].count(dst))
            fatal("%s: %s:%d: duplicate (src_id=%d, dst_id=%d) pair\n",
                  name(), fname, lineno, src, dst);

        raw[src][dst] = {pir, isWrite};
        num_pairs++;
    }

    if (num_pairs == 0) {
        fatal("%s: table '%s' contains no (src_id, dst_id, pir) entries\n",
              name(), fname);
    }

    // Second pass: per-source cumulative distribution + threshold check
    // (each source's pir sum is its per-cycle injection probability and
    // must stay <= 1).
    for (int s = 0; s < numDestinations; s++) {
        double cum = 0.0;
        for (const auto &kv : raw[s]) {
            cum += kv.second.first;
            tableEntries[s].emplace_back(cum, kv.first, kv.second.second);
        }
        tableThresholds[s] = cum;
        if (cum > 1.0 + 1e-9) {
            fatal("%s: source %d PIR sum %g exceeds 1\n", name(), s, cum);
        }
    }

    DPRINTF(GarnetSyntheticTraffic,
            "%s: loaded %d table pairs from %s\n",
            name(), (int) num_pairs, fname);
}

bool
GarnetSyntheticTraffic::active(int n) const
{
    if (activeColumns > 0 && n % numColumns >= activeColumns)
        return false;
    if (activeRows > 0 && n / numColumns >= activeRows)
        return false;
    return true;
}

void
GarnetSyntheticTraffic::doRetry()
{
    if (traceMode && retryPkt) {
        if (cachePort.sendTimingReq(retryPkt)) {
            numPacketsSent++;
            if (m_trace_sched) {
                m_trace_sched->event("ISSUE", m_trace_retry_tid,
                                     traceRouterId, "retry");
            }
            retryPkt = nullptr;
            if (m_trace_retry_kind == RETRY_HEAD) {
                // the held head is m_trace_head (it never advanced)
                assert(m_trace_head < m_trace_sends.size());
                auto &s = m_trace_sends[m_trace_head];
                assert(s.global == m_trace_retry_tid);
                s.state = TraceSend::FLIGHT;
                m_trace_inflight++;
                m_trace_inflight_msg = s.msg;
                m_trace_head++;
            } else if (m_trace_retry_kind == RETRY_LEG) {
                assert(!m_trace_legs.empty());
                m_trace_legs.pop_front();
            }
            m_trace_retry_kind = RETRY_NONE;
            m_trace_retry_tid = -1;
            traceTick();   // keep issuing
        }
        return;
    }
    if (cachePort.sendTimingReq(retryPkt)) {
        retryPkt = NULL;
    }
}

void
GarnetSyntheticTraffic::printAddr(Addr a)
{
    cachePort.printAddr(a);
}

} // namespace gem5
