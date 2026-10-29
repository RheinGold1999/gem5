# Copyright (c) 2025 AIGCIC NoC demo
#
# SystemC/TLM master IP for the AIGCIC NoC demo. Built as an EXTRAS dir:
#   scons build/NULL_AIGCIC_NOC/gem5.opt EXTRAS=extras/aigcic_sc

from m5.objects.SystemC import SystemC_ScModule
from m5.objects.Tlm import TlmInitiatorSocket
from m5.params import *


class AigcicScMaster(SystemC_ScModule):
    type = "AigcicScMaster"
    cxx_class = "AigcicScMaster"
    cxx_header = "sc_master.hh"

    tlm = TlmInitiatorSocket(32, "TLM initiator socket")

    # The AIGCIC_NOC protocol config takes the L1 sequencer clock from
    # cpus[i].clk_domain; masters themselves are untimed SystemC actors,
    # so this is only consumed by the Ruby config. Assign a SrcClockDomain
    # in the config script.
    clk_domain = Param.SrcClockDomain(
        NULL, "clock domain passed on to the L1 sequencer by the Ruby config"
    )

    node_id = Param.UInt32(0, "index of this node (0..num_nodes-1)")
    num_dirs = Param.UInt32(
        4, "number of destination nodes (DRAM-controller agents)"
    )
    num_requests = Param.UInt32(10, "number of read requests to issue")
    dest_shift = Param.UInt32(
        7, "block-offset bits in the address map (log2 cacheline size)"
    )
    payload_size = Param.UInt32(
        128, "bytes per request; must equal the Ruby block size"
    )
    period_ns = Param.UInt32(
        100, "idle time between two requests, in SystemC nanoseconds"
    )
