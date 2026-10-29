#include "sc_master.hh"

#include <cstring>

namespace {
// Cooperative SystemC scheduling makes these plain counters safe.
uint32_t completedRequests = 0;
uint32_t totalRequests = 0;
}  // namespace

using namespace sc_core;
using namespace tlm;
using namespace gem5;

AigcicScMaster::AigcicScMaster(sc_module_name name,
                               const AigcicScMasterParams &p)
    : sc_module(name),
      iSocket("iSocket"),
      wrapper(iSocket, std::string(name) + ".tlm", InvalidPortID),
      nodeId(p.node_id),
      numDirs(p.num_dirs),
      numRequests(p.num_requests),
      destShift(p.dest_shift),
      payloadSize(p.payload_size),
      periodNs(p.period_ns)
{
    iSocket.register_nb_transport_bw(this, &AigcicScMaster::nb_transport_bw);
    totalRequests += numRequests;
    SC_HAS_PROCESS(AigcicScMaster);
    SC_THREAD(run);
}

Port &
AigcicScMaster::gem5_getPort(const std::string &if_name, int idx)
{
    return wrapper;
}

tlm_sync_enum
AigcicScMaster::nb_transport_bw(tlm_generic_payload &trans, tlm_phase &phase,
                                sc_time &delay)
{
    if (phase == END_REQ) {
        // Request accepted into the NoC; keep waiting for BEGIN_RESP.
        return TLM_ACCEPTED;
    }

    panic_if(phase != BEGIN_RESP,
             "AigcicScMaster %s: unexpected TLM phase\n", name());

    panic_if(trans.get_response_status() != TLM_OK_RESPONSE,
             "AigcicScMaster %s: request to 0x%llx failed (%s)\n",
             name(), trans.get_address(),
             trans.get_response_string().c_str());

    if (++completedRequests == totalRequests) {
        exitSimLoop("AIGCIC SC demo complete");
    }
    respEvent.notify(delay);

    // Completing on BEGIN_RESP: no END_RESP phase needed.
    return TLM_COMPLETED;
}

void
AigcicScMaster::run()
{
    // Let instantiation/connections settle.
    wait(SC_ZERO_TIME);

    for (uint32_t i = 0; i < numRequests; i++) {
        // Round-robin over the destination nodes; dest index in the
        // bits right above the block offset, line 0.
        const uint32_t dest = (nodeId + 1 + i) % numDirs;
        const Addr addr = (Addr)dest << destShift;

        // Created with the memory manager: the bridge acquire()/release()s
        // the payload and the manager recycles it; never delete it here.
        tlm_generic_payload *trans = new tlm_generic_payload(&mm);
        unsigned char *data = new unsigned char[payloadSize];
        memset(data, 0, payloadSize);

        trans->set_command(TLM_READ_COMMAND);
        trans->set_address(addr);
        trans->set_data_ptr(data);
        trans->set_data_length(payloadSize);
        trans->set_streaming_width(payloadSize);
        trans->set_byte_enable_ptr(nullptr);
        trans->set_dmi_allowed(false);
        trans->set_response_status(TLM_INCOMPLETE_RESPONSE);

        tlm_phase phase = BEGIN_REQ;
        sc_time delay = SC_ZERO_TIME;
        const tlm_sync_enum status =
            iSocket->nb_transport_fw(*trans, phase, delay);
        panic_if(status != TLM_ACCEPTED,
                 "AigcicScMaster %s: nb_transport_fw returned %d\n",
                 name(), (int)status);

        wait(respEvent);

        delete[] data;

        wait(sc_time((double)periodNs, SC_NS));
    }
}

AigcicScMaster *
gem5::AigcicScMasterParams::create() const
{
    return new AigcicScMaster(name.c_str(), *this);
}
