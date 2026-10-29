# Copyright (c) 2016 Georgia Institute of Technology
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from m5.objects.ClockedObject import ClockedObject
from m5.params import *
from m5.proxy import *


class GarnetSyntheticTraffic(ClockedObject):
    type = "GarnetSyntheticTraffic"
    cxx_header = (
        "cpu/testers/garnet_synthetic_traffic/GarnetSyntheticTraffic.hh"
    )
    cxx_class = "gem5::GarnetSyntheticTraffic"

    block_offset = Param.Int(6, "block offset in bits")
    mpu_id = Param.UInt32(
        0, "mpu_id of the die this tester lives in (address die-field check)"
    )
    gpu_id = Param.UInt32(
        0, "gpu_id of the die this tester lives in"
    )
    cross_die_ratio = Param.Float(
        0.0,
        "Fraction of requests addressed to a remote die: a remote mpu_id "
        "(different from mpu_id, 0..7) is stamped into bits [39:37] of the "
        "generated address, which makes the L1 issue them on the cross-die "
        "vnets (vnet1/vnet3)",
    )
    dest_shift = Param.UInt64(
        0,
        "Bit position of the destination field in the generated address. "
        "0 (default) = legacy layout: [line_sel | dest | block_offset]. "
        "A non-zero shift (e.g. 32 for the MPU-52 layout) places the dest "
        "field at [dest_shift, dest_shift+ceilLog2(num_dest)) and the "
        "line-select bits inside the destination's local space, right "
        "above the block offset.",
    )
    num_dest = Param.Int(1, "Number of Destinations")
    lines_per_dest = Param.Int(
        1,
        "Number of distinct cache lines used per destination. The random "
        "line-select bits are placed right above the destination bits, so "
        "block_offset + ceilLog2(num_dest) + ceilLog2(lines_per_dest) must "
        "stay below any address-hashing bits of the directory mapping "
        "(e.g. Ruby's xor_low_bit, default 20) and within memory size.",
    )
    memory_size = Param.Int(65536, "memory size")
    sim_cycles = Param.Int(1000, "Number of simulation cycles")
    num_packets_max = Param.Int(
        -1,
        "Max number of packets to send. \
                        Default is to keep sending till simulation ends",
    )
    single_sender = Param.Int(
        -1,
        "Send only from this node. \
                                   By default every node sends",
    )
    single_dest = Param.Int(
        -1,
        "Send only to this dest. \
                                 Default depends on traffic_type",
    )
    num_columns = Param.Int(
        1,
        "Mesh columns (num_dirs / mesh_rows), e.g. 8 for a 4x8 fabric. "
        "Used to resolve a node id to (col, row) for active-column/row masking.",
    )
    active_columns = Param.Int(
        0,
        "Only col < active_columns send/receive. 0 = no column limit.",
    )
    active_rows = Param.Int(
        0,
        "Only row < active_rows send/receive. 0 = no row limit. "
        "Together with active_columns these mask a rectangular region, e.g. "
        "emulate a 4x6 mesh on a 4x8 fabric: active_columns=6, "
        "active_rows=4.",
    )
    traffic_type = Param.String("uniform_random", "Traffic type")
    inj_rate = Param.Float(0.1, "Packet injection rate")
    inj_vnet = Param.Int(
        -1,
        "Vnet to inject in. \
                              0 and 1 are 1-flit, 2 is 5-flit. \
                                Default is to inject in all three vnets",
    )
    precision = Param.Int(
        3,
        "Number of digits of precision \
                              after decimal point",
    )
    response_limit = Param.Cycles(
        5000000,
        "Cycles before exiting \
                                            due to lack of progress",
    )
    test = RequestPort("Port to the memory system to test")
    system = Param.System(Parent.any, "System we belong to")
