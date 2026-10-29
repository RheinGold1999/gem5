#ifndef __AIGCIC_SC_MASTER_HH__
#define __AIGCIC_SC_MASTER_HH__

#include <string>
#include <tlm_utils/simple_initiator_socket.h>

#include "base/types.hh"
#include "params/AigcicScMaster.hh"
#include "sim/sim_exit.hh"
#include "systemc/ext/core/sc_module_name.hh"
#include "systemc/ext/systemc"
#include "systemc/ext/tlm"
#include "systemc/tlm_bridge/sc_mm.hh"
#include "systemc/tlm_port_wrapper.hh"

/*
 * A minimal SystemC/TLM master IP for the AIGCIC NoC demo.
 *
 * It issues a fixed number of TLM reads through the 4-phase AT
 * handshake (nb_transport_fw). This is deliberate: the in-tree
 * TlmToGem5Bridge only reaches the *timing* network through the AT
 * path (nb_transport_fw -> sendTimingReq); the blocking b_transport
 * path goes sendAtomic and would bypass the NoC entirely.
 *
 * One outstanding transaction at a time per master. The destination
 * node is encoded in the address bits right above the block offset,
 * same layout as the synthetic-traffic driver:
 *
 *     [ 0 | dest | block offset ]
 *
 * When every master has received all of its responses, the first one
 * to notice exits the simulation.
 */
SC_MODULE(AigcicScMaster)
{
  public:
    tlm_utils::simple_initiator_socket<AigcicScMaster> iSocket;
    sc_gem5::TlmInitiatorWrapper<32> wrapper;

    AigcicScMaster(sc_core::sc_module_name name,
                   const gem5::AigcicScMasterParams &p);

    gem5::Port &gem5_getPort(const std::string &if_name, int idx = -1) override;

    // backward interface: END_REQ = request accepted by the network,
    // BEGIN_RESP = data returned (complete the transaction right away).
    tlm::tlm_sync_enum nb_transport_bw(tlm::tlm_generic_payload &trans,
                                       tlm::tlm_phase &phase,
                                       sc_core::sc_time &delay);

  private:
    void run();

    // The TlmToGem5Bridge acquire()s / release()s our transactions, so
    // every generic payload must carry a memory manager.
    Gem5SystemC::MemoryManager mm;

    const uint32_t nodeId;
    const uint32_t numDirs;
    const uint32_t numRequests;
    const uint32_t destShift;
    const uint32_t payloadSize;
    const uint32_t periodNs;
    sc_core::sc_event respEvent;
};

#endif  // __AIGCIC_SC_MASTER_HH__
