/*
 * Copyright (c) 2020 Inria
 * Copyright (c) 2016 Georgia Institute of Technology
 * Copyright (c) 2008 Princeton University
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in source and binary forms must reproduce the above
 * copyright notice, this list of conditions and the following disclaimer
 * in the documentation and/or other materials provided with the
 * distribution; neither the name of the copyright holders nor the names
 * of its contributors may be used to endorse or promote products derived
 * from this software without specific prior written permission.
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

#include "mem/ruby/network/garnet/InputUnit.hh"

#include "debug/RubyNetwork.hh"
#include "mem/ruby/network/garnet/Credit.hh"
#include "mem/ruby/network/garnet/Router.hh"
#include "mem/ruby/network/garnet/TraceReplay.hh"

namespace gem5
{

namespace ruby
{

namespace garnet
{

InputUnit::InputUnit(int id, PortDirection direction, Router *router)
  : Consumer(router), m_router(router), m_id(id), m_direction(direction),
    m_vc_per_vnet(m_router->get_vc_per_vnet())
{
    const int m_num_vcs = m_router->get_num_vcs();
    m_num_buffer_reads.resize(m_num_vcs/m_vc_per_vnet);
    m_num_buffer_writes.resize(m_num_vcs/m_vc_per_vnet);
    m_trace_vc.resize(m_num_vcs);
    for (int i = 0; i < m_num_buffer_reads.size(); i++) {
        m_num_buffer_reads[i] = 0;
        m_num_buffer_writes[i] = 0;
    }

    // Instantiating the virtual channels
    virtualChannels.reserve(m_num_vcs);
    for (int i=0; i < m_num_vcs; i++) {
        virtualChannels.emplace_back();
    }
}

/*
 * The InputUnit wakeup function reads the input flit from its input link.
 * Each flit arrives with an input VC.
 * For HEAD/HEAD_TAIL flits, performs route computation,
 * and updates route in the input VC.
 * The flit is buffered for (m_latency - 1) cycles in the input VC
 * and marked as valid for SwitchAllocation starting that cycle.
 *
 * Trace replay (see TraceReplay.hh): every flit of a trace stream carries
 * the record index of the hop it is currently traversing in
 * RouteInfo::trace_id.  The HEAD flit consults the relay hook before route
 * computation:
 *   HOOK_REWRITE: the head's destination/identity are rewritten to the
 *       continuation record and the head is routed to the pre-computed
 *       output port (cut-through relay; body/tail flits follow the VC).
 *   HOOK_EJECT:   the stream is routed to the local dir (silent pop); the
 *       continuation's stream is re-injected later by the owning tester.
 *   HOOK_NORMAL:  terminal or source pass-through, plain routing.
 * Body/tail flits are re-stamped with the input VC's current (identity,
 * destination) so a multi-flit stream stays consistent while its head is
 * already several hops ahead.  The TAIL flit fires the arrival event (the
 * record's burst fully delivered at this router).
 */

void
InputUnit::wakeup()
{
    flit *t_flit;
    if (m_in_link->isReady(curTick())) {

        t_flit = m_in_link->consumeLink();
        DPRINTF(RubyNetwork, "Router[%d] Consuming:%s Width: %d Flit:%s\n",
        m_router->get_id(), m_in_link->name(),
        m_router->getBitWidth(), *t_flit);
        assert(t_flit->m_width == m_router->getBitWidth());
        int vc = t_flit->get_vc();
        t_flit->increment_hops(); // for stats
        // Mark the cycle this flit entered this router (used for the
        // per-router flit residence latency stats).
        t_flit->set_enter_cycle(m_router->curCycle());

        // ---- trace replay hook (see TraceReplay.hh) ----
        const bool trace = m_router->traceEnabled();
        const int tid = trace ? t_flit->get_route().trace_id : -1;
        const bool is_head = (t_flit->get_type() == HEAD_) ||
                             (t_flit->get_type() == HEAD_TAIL_);

        if (is_head) {
            assert(virtualChannels[vc].get_state() == IDLE_);
            set_vc_active(vc, curTick());

            if (trace && tid >= 0) {
                TraceReplay::HookResult hr =
                    m_router->traceHook(m_id, vc, tid);
                if (hr.action == TraceReplay::HOOK_REWRITE) {
                    // cut-through relay: rewrite the destination and this
                    // stream's identity; route directly to the pre-computed
                    // output port (bypasses the routing table)
                    RouteInfo route = t_flit->get_route();
                    route.dest_router = hr.next;
                    route.trace_id = hr.cont;
                    route.net_dest = hr.net_dest;
                    t_flit->set_route(route);
                    m_trace_vc[vc] = {hr.cont, hr.cls, hr.next, 1};
                    grant_outport(vc, hr.outport);
                } else if (hr.action == TraceReplay::HOOK_EJECT) {
                    // hold point: eject the stream into the local dir (the
                    // dir pops it silently); the continuation's stream is
                    // re-injected from this router's tester when its wait
                    // completes. No buffer slots are held while waiting.
                    // NOTE: InputUnit::m_id is the PORT number, not the
                    // router id -- the router id must come from the router.
                    RouteInfo route = t_flit->get_route();
                    route.dest_router = m_router->get_id();
                    route.net_dest = hr.net_dest;
                    t_flit->set_route(route);
                    m_trace_vc[vc] = {tid, -1, m_router->get_id(), 1};
                    int outport = m_router->route_compute(
                        t_flit->get_route(), m_id, m_direction);
                    grant_outport(vc, outport);
                } else {
                    // terminal (eject here): unconstrained class -- or
                    // source pass-through: pinned to class 0 (F10) so the
                    // first hop never grabs the class-1 VC
                    m_trace_vc[vc] = {tid, hr.cls,
                                      (int)t_flit->get_route().dest_router,
                                      1};
                    // Route computation for this vc
                    int outport = m_router->route_compute(
                        t_flit->get_route(), m_id, m_direction);
                    // Update output port in VC
                    // All flits in this packet will use this output port
                    // The output port field in the flit is updated after it
                    // wins SA
                    grant_outport(vc, outport);
                }
            } else {
                m_trace_vc[vc] = {-1, -1, -1, 0};
                // Route computation for this vc
                int outport = m_router->route_compute(t_flit->get_route(),
                    m_id, m_direction);

                // Update output port in VC
                // All flits in this packet will use this output port
                // The output port field in the flit is updated after it
                // wins SA
                grant_outport(vc, outport);
            }
        } else {
            assert(virtualChannels[vc].get_state() == ACTIVE_);
            // body/tail of a trace stream always carry the input VC's
            // current identity and destination (the head may already have
            // been rewritten at this router while the body was in flight)
            if (trace && m_trace_vc[vc].rec >= 0) {
                RouteInfo route = t_flit->get_route();
                route.trace_id = m_trace_vc[vc].rec;
                route.dest_router = m_trace_vc[vc].dest;
                t_flit->set_route(route);
                m_trace_vc[vc].flits++;
            }
        }

        // Buffer the flit
        virtualChannels[vc].insertFlit(t_flit);

        int vnet = vc/m_vc_per_vnet;
        // number of writes same as reads
        // any flit that is written will be read only once
        m_num_buffer_writes[vnet]++;
        m_num_buffer_reads[vnet]++;

        Cycles pipe_stages = m_router->get_pipe_stages();
        if (pipe_stages == 1) {
            // 1-cycle router
            // Flit goes for SA directly
            t_flit->advance_stage(SA_, curTick());
        } else {
            assert(pipe_stages > 1);
            // Router delay is modeled by making flit wait in buffer for
            // (pipe_stages cycles - 1) cycles before going for SA

            Cycles wait_time = pipe_stages - Cycles(1);
            t_flit->advance_stage(SA_, m_router->clockEdge(wait_time));

            // Wakeup the router in that cycle to perform SA
            m_router->schedule_wakeup(Cycles(wait_time));
        }

        // Burst completion: the tail flit of the arriving record just
        // landed. Fires the arrival event with the identity the stream
        // carried on this hop (the record whose delivery this is).
        if (trace && tid >= 0 &&
            (t_flit->get_type() == TAIL_ ||
             t_flit->get_type() == HEAD_TAIL_)) {
            // spec 6: the delivered burst's flit count must equal the
            // plan's ceil(bytes/flit) for the record it delivered
            panic_if(m_trace_vc[vc].flits !=
                         m_router->traceExpectedFlits(tid),
                     "Router[%d]: burst of rec %d delivered %d flits, "
                     "plan expects %d", m_id, tid, m_trace_vc[vc].flits,
                     m_router->traceExpectedFlits(tid));
            m_router->traceArrival(m_id, vc, tid);
        }

        if (m_in_link->isReady(curTick())) {
            m_router->schedule_wakeup(Cycles(1));
        }
    }
}

// Send a credit back to upstream router for this VC.
// Called by SwitchAllocator when the flit in this VC wins the Switch.
void
InputUnit::increment_credit(int in_vc, bool free_signal, Tick curTime)
{
    DPRINTF(RubyNetwork, "Router[%d]: Sending a credit vc:%d free:%d to %s\n",
    m_router->get_id(), in_vc, free_signal, m_credit_link->name());
    Credit *t_credit = new Credit(in_vc, free_signal, curTime);
    creditQueue.insert(t_credit);
    m_credit_link->scheduleEventAbsolute(m_router->clockEdge(Cycles(1)));
}

bool
InputUnit::functionalRead(Packet *pkt, WriteMask &mask)
{
    bool read = false;
    for (auto& virtual_channel : virtualChannels) {
        if (virtual_channel.functionalRead(pkt, mask))
            read = true;
    }

    return read;
}

uint32_t
InputUnit::functionalWrite(Packet *pkt)
{
    uint32_t num_functional_writes = 0;
    for (auto& virtual_channel : virtualChannels) {
        num_functional_writes += virtual_channel.functionalWrite(pkt);
    }

    return num_functional_writes;
}

void
InputUnit::resetStats()
{
    for (int j = 0; j < m_num_buffer_reads.size(); j++) {
        m_num_buffer_reads[j] = 0;
        m_num_buffer_writes[j] = 0;
    }
}

} // namespace garnet
} // namespace ruby
} // namespace gem5
