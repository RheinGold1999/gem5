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
#
# Author: Tushar Krishna

import argparse
import math
import os
import sys

import m5
from m5.defines import buildEnv
from m5.objects import *
from m5.util import addToPath, fatal, warn
from m5.util.convert import toMemorySize

addToPath("../")

from common import Options
from ruby import Ruby

# Get paths we might need.  It's expected this file is in m5/configs/example.
config_path = os.path.dirname(os.path.abspath(__file__))
config_root = os.path.dirname(config_path)
m5_root = os.path.dirname(config_root)

parser = argparse.ArgumentParser()
Options.addNoISAOptions(parser)

parser.add_argument(
    "--synthetic",
    default="uniform_random",
    choices=[
        "uniform_random",
        "tornado",
        "bit_complement",
        "bit_reverse",
        "bit_rotation",
        "neighbor",
        "shuffle",
        "transpose",
    ],
)

parser.add_argument(
    "-i",
    "--injectionrate",
    type=float,
    default=0.1,
    metavar="I",
    help="Injection rate in packets per cycle per node. \
                        Takes decimal value between 0 to 1 (eg. 0.225). \
                        Number of digits after 0 depends upon --precision.",
)

parser.add_argument(
    "--precision",
    type=int,
    default=3,
    help="Number of digits of precision after decimal point\
                        for injection rate",
)

parser.add_argument(
    "--sim-cycles", type=int, default=1000, help="Number of simulation cycles"
)

parser.add_argument(
    "--num-packets-max",
    type=int,
    default=-1,
    help="Stop injecting after --num-packets-max.\
                        Set to -1 to disable.",
)

parser.add_argument(
    "--single-sender-id",
    type=int,
    default=-1,
    help="Only inject from this sender.\
                        Set to -1 to disable.",
)

parser.add_argument(
    "--single-dest-id",
    type=int,
    default=-1,
    help="Only send to this destination.\
                        Set to -1 to disable.",
)

parser.add_argument(
    "--inj-vnet",
    type=int,
    default=-1,
    choices=[-1, 0, 1, 2],
    help="Only inject in this vnet (0, 1 or 2).\
                        0 and 1 are 1-flit, 2 is 5-flit.\
                        Set to -1 to inject randomly in all vnets.",
)

parser.add_argument(
    "--lines-per-dest",
    type=int,
    default=1024,
    help="Number of distinct cache lines used per destination.\
                        Spreads traffic over many lines to reduce\
                        same-line conflicts when a real coherence\
                        protocol is used. Set to 1 for the legacy\
                        one-line-per-destination encoding. Must keep\
                        block_offset(6) + log2(num-dirs) +\
                        log2(lines-per-dest) below Ruby's xor_low_bit\
                        (default 20).",
)

#
# Add the ruby specific and protocol specific options
#
Ruby.define_options(parser)

args = parser.parse_args()


def check_address_encoding(args):
    """Validate the synthetic-traffic address encoding.

    The tester encodes the destination directory in the address bits right
    above the block offset, and --lines-per-dest adds random line-select
    bits above the destination bits:

        [ ... | line_sel | destination | block offset ]

    These random bits must stay below the directory mapping's XOR-hash
    bits (--xor-low-bit) and within the memory size, otherwise packets are
    silently routed to a different directory than the traffic pattern
    intended (uniform_random still looks uniform, but transpose/neighbor/
    single-dest etc. get distorted and per-destination stats become wrong).
    """
    block_offset_bits = int(math.log2(args.cacheline_size))
    dest_bits = max(1, math.ceil(math.log2(args.num_dirs)))
    line_bits = math.ceil(math.log2(args.lines_per_dest))
    addr_bits_used = block_offset_bits + dest_bits + line_bits
    mem_bits = int(math.log2(toMemorySize(args.mem_size)))

    if args.lines_per_dest < 1:
        fatal(
            "--lines-per-dest=%d is invalid: must be >= 1 "
            "(1 = legacy one-line-per-destination encoding).",
            args.lines_per_dest,
        )

    if 2 ** int(math.log2(args.num_dirs)) != args.num_dirs:
        fatal(
            "--num-dirs=%d is not a power of two: the destination cannot "
            "be encoded in dedicated address bits, so packets would not "
            "map one-to-one onto directories. Choose a power of two.",
            args.num_dirs,
        )

    if args.xor_low_bit > 0 and addr_bits_used > args.xor_low_bit:
        max_lines = 2 ** (args.xor_low_bit - block_offset_bits - dest_bits)
        fatal(
            "--lines-per-dest=%d needs address bits [0, %d), but the "
            "directory mapping XOR-hashes bits [%d, %d) into the "
            "destination-select bits (dir = addr[%d:%d] ^ addr[%d:%d]). "
            "The random line-select bits would spill into the hash bits "
            "and packets would be silently routed to the wrong directory. "
            "Fix one of:\n"
            "  1. reduce --lines-per-dest to <= %d;\n"
            "  2. raise --xor-low-bit to >= %d (must stay <= %d for "
            "--mem-size=%s);\n"
            "  3. set --xor-low-bit=0 to disable XOR hashing entirely.",
            args.lines_per_dest,
            addr_bits_used,
            args.xor_low_bit,
            args.xor_low_bit + dest_bits,
            block_offset_bits + dest_bits - 1,
            block_offset_bits,
            args.xor_low_bit + dest_bits - 1,
            args.xor_low_bit,
            max_lines,
            addr_bits_used,
            mem_bits - dest_bits,
            args.mem_size,
        )

    if addr_bits_used > mem_bits:
        max_lines = 2 ** (mem_bits - block_offset_bits - dest_bits)
        fatal(
            "--lines-per-dest=%d needs address bits [0, %d), which "
            "exceeds --mem-size=%s (%d address bits): generated addresses "
            "would fall outside memory. Fix one of:\n"
            "  1. reduce --lines-per-dest to <= %d;\n"
            "  2. increase --mem-size to >= %s.",
            args.lines_per_dest,
            addr_bits_used,
            args.mem_size,
            mem_bits,
            max_lines,
            f"{2 ** addr_bits_used // 2 ** 20}MB",
        )

    working_set = args.num_dirs * args.lines_per_dest * args.cacheline_size
    l1_size = toMemorySize(args.l1d_size)
    if l1_size < working_set:
        warn(
            "L1 size %s is smaller than the synthetic-traffic working set "
            "(%d dirs x %d lines x %dB = %dKiB): capacity/set-conflict "
            "replacements will add PUTX writeback traffic to the network. "
            "For replacement-free traffic use --l1d_size=%dKiB (with "
            "--l1d_assoc>=2 the dense address encoding then maps exactly "
            "assoc lines to every set).",
            args.l1d_size,
            args.num_dirs,
            args.lines_per_dest,
            args.cacheline_size,
            working_set // 1024,
            working_set // 1024,
        )


check_address_encoding(args)

cpus = [
    GarnetSyntheticTraffic(
        num_packets_max=args.num_packets_max,
        single_sender=args.single_sender_id,
        single_dest=args.single_dest_id,
        sim_cycles=args.sim_cycles,
        traffic_type=args.synthetic,
        inj_rate=args.injectionrate,
        inj_vnet=args.inj_vnet,
        precision=args.precision,
        num_dest=args.num_dirs,
        lines_per_dest=args.lines_per_dest,
        block_offset=int(math.log2(args.cacheline_size)),
    )
    for i in range(args.num_cpus)
]

# create the desired simulated system
system = System(cpu=cpus, mem_ranges=[AddrRange(args.mem_size)])


# Create a top-level voltage domain and clock domain
system.voltage_domain = VoltageDomain(voltage=args.sys_voltage)

system.clk_domain = SrcClockDomain(
    clock=args.sys_clock, voltage_domain=system.voltage_domain
)

Ruby.create_system(args, False, system)

# Create a seperate clock domain for Ruby
system.ruby.clk_domain = SrcClockDomain(
    clock=args.ruby_clock, voltage_domain=system.voltage_domain
)

i = 0
for ruby_port in system.ruby._cpu_ports:
    #
    # Tie the cpu test ports to the ruby cpu port
    #
    cpus[i].test = ruby_port.in_ports
    i += 1

# -----------------------
# run simulation
# -----------------------

root = Root(full_system=False, system=system)
root.system.mem_mode = "timing"

# Not much point in this being higher than the L1 latency
m5.ticks.setGlobalFrequency("1ps")

# instantiate configuration
m5.instantiate()

# simulate until program terminates
exit_event = m5.simulate(args.abs_max_tick)

print("Exiting @ tick", m5.curTick(), "because", exit_event.getCause())
