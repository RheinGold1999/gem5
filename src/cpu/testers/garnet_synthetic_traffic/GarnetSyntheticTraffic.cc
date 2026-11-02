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

#include <cmath>
#include <iomanip>
#include <set>
#include <string>
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
               traffic == TABLE_);
    }

    id = TESTER_NETWORK++;
    // Initialize random generator with unique seed based on tester ID
    rng = Random::genRandom(Random::globalSeed + id);
    DPRINTF(GarnetSyntheticTraffic,"Config Created: Name = %s , and id = %d\n",
            name(), id);
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
    if (++noResponseCycles >= responseLimit) {
        fatal("%s deadlocked at cycle %d\n", name(), curTick());
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

    // Condition 1: sources outside the active (row, col) region never inject.
    if (activeColumns > 0 || activeRows > 0) {
        if (!active(id))
            return;
    }

    if (traffic == TABLE_) {
        // Per-source PIR table: with probability tableThresholds[id] a
        // packet is injected this cycle; the destination is picked from
        // the cumulative PIR distribution (entry i wins when r falls in
        // [cum[i-1], cum[i])). singleDest, if set, only overrides the
        // destination, not the threshold gate.
        double r = rng->random<double>();
        if (r >= tableThresholds[id]) {
            return;
        }
        if (singleDest >= 0) {
            destination = singleDest;
        } else {
            destination = tableEntries[id].back().second;
            for (const auto &entry : tableEntries[id]) {
                if (r < entry.first) {
                    destination = entry.second;
                    break;
                }
            }
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
    // (1) This function generatePkt() generates packets of one of the
    //     following 3 types (randomly) : ReadReq, INST_FETCH, WriteReq
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

    // Inject in specific Vnet
    // Vnet 0 and 1 are for control packets (1-flit)
    // Vnet 2 is for data packets (5-flit)
    int injReqType = injVnet;

    if (injReqType < 0 || injReqType > 2)
    {
        // randomly inject in any vnet
        injReqType = rng->random(0, 2);
    }

    if (injReqType == 0) {
        // generate packet for virtual network 0
        requestType = MemCmd::ReadReq;
        req = std::make_shared<Request>(paddr, access_size, flags,
                                        requestorId);
    } else if (injReqType == 1) {
        // generate packet for virtual network 1
        requestType = MemCmd::ReadReq;
        flags.set(Request::INST_FETCH);
        req = std::make_shared<Request>(
            0x0, access_size, flags, requestorId, 0x0, 0);
        req->setPaddr(paddr);
    } else {  // if (injReqType == 2)
        // generate packet for virtual network 2
        requestType = MemCmd::WriteReq;
        req = std::make_shared<Request>(paddr, access_size, flags,
                                        requestorId);
    }

    req->setContext(id);

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

    // First pass: per-source (dst -> pir) maps; catches range, pir-value
    // and duplicate errors before anything is cumulated.
    std::vector<std::map<int, double>> raw(numDestinations);
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
        double pir;
        if (!(iss >> src >> dst >> pir)) {
            // Tolerate exactly one header line, e.g. "src_id dst_id pir":
            // a non-numeric line without any digit character (real data
            // lines always contain digits, so this cannot mask typos).
            if (!headerSeen &&
                line.find_first_of("0123456789") == std::string::npos) {
                headerSeen = true;
                continue;
            }
            fatal("%s: %s:%d: expected 'src_id dst_id pir'\n",
                  name(), fname, lineno);
        }
        std::string extra;
        if (iss >> extra) {
            fatal("%s: %s:%d: trailing characters after pir ('%s')\n",
                  name(), fname, lineno, extra);
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

        raw[src][dst] = pir;
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
            cum += kv.second;
            tableEntries[s].emplace_back(cum, kv.first);
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
