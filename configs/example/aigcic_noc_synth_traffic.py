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

# This script is tuned for the NULL_AIGCIC_NOC build (NULL/ISA-free, Ruby,
# PROTOCOL="AIGCIC_NOC"). Its defaults (4x6 active mesh on a 4x8 fabric,
# xor_low_bit, ...) assume that binary; refuse to run otherwise.
if buildEnv["PROTOCOL"] != "AIGCIC_NOC" or not buildEnv["RUBY"] or any(
    buildEnv[k] for k in buildEnv if k.startswith("USE_") and k.endswith("_ISA")
):
    fatal(
        "aigcic_noc_synth_traffic.py requires a NULL_AIGCIC_NOC build "
        "(PROTOCOL=AIGCIC_NOC, Ruby enabled, no ISA). "
        "This binary has PROTOCOL=%s, RUBY=%s. "
        "Build with: scons build/NULL_AIGCIC_NOC/gem5.opt"
        % (buildEnv["PROTOCOL"], buildEnv["RUBY"])
    )

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
    default=0,
    choices=[-1, 0, 1, 2, 3],
    help="Only inject in this vnet. AIGCIC_NOC vnet usage: 0 = request "
                        "internal-die (default), 1 = request cross-die "
                        "(reserved), 2 = response internal-die, 3 = response "
                        "cross-die (reserved). Only vnet 0 has a receiving "
                        "agent (the DRAM controllers); -1 injects randomly "
                        "across all vnets.",
)

parser.add_argument(
    "--addr-layout",
    choices=["legacy", "mpu52"],
    default="mpu52",
    help="Address layout. mpu52 (default): MPU-Die "
         "52-bit node addressing (see below). legacy: [line_sel | dest | "
         "offset] "
         "with XOR-hash channel selection (xor_low_bit). mpu52: MPU-Die "
         "52-bit layout; the node field [36:32] = (node_y << 2) | node_x "
         "with node_x = mesh row, node_y = mesh column; each node owns a "
         "contiguous 4GiB window routed by directory range lookup (no "
         "XOR); line-select bits sit inside the node's local space right "
         "above the block offset. Memory is backed by zero-allocation "
         "stubs, so host RAM does not scale with the 32 x 4GiB space.",
)

parser.add_argument(
    "--mpu-id",
    type=int,
    default=0,
    help="mpu_id of the simulated die (MPU-52 layout, bits [39:37])",
)
parser.add_argument(
    "--gpu-id",
    type=int,
    default=0,
    help="gpu_id of the simulated die (MPU-52 layout, bits [50:41])",
)
parser.add_argument(
    "--cross-die-ratio",
    type=float,
    default=0.0,
    help="Fraction of requests addressed to a remote die: the tester "
         "stamps a remote mpu_id (!= --mpu-id) into bits [39:37]; the L1 "
         "issues those requests on the cross-die vnets (vnet1 request / "
         "vnet3 response). mpu52 layout only.",
)

parser.add_argument(
    "--lines-per-dest",
    type=int,
    default=None,
    help="Number of distinct cache lines used per destination.\
                        Spreads traffic over many lines to reduce\
                        same-line conflicts when a real coherence\
                        protocol is used. Set to 1 for the legacy\
                        one-line-per-destination encoding. Must keep\
                        block_offset(6) + log2(num-dirs) +\
                        log2(lines-per-dest) below Ruby's xor_low_bit\
                        (default 20). Default: 1024 (legacy) or 2**25 =\
                        the full 4GiB node space (mpu52).",
)

parser.add_argument(
    "--data-msg-size",
    type=int,
    default=64,
    help="Data-message payload size in bytes for the network (default 64).\
                        Garnet sizes a packet's flit count as\
                        ceil((data_msg_size + control_msg_size) / flit_size),\
                        with control_msg_size=8 and flit_size=16 by default,\
                        so the default gives (64+8)/16 = 5 flits for a data\
                        (ST/response vnet) packet and 8/16 = 1 flit for\
                        control packets. Lower it to make data packets 1 flit\
                        (e.g. --data-msg-size 8 -> (8+8)/16 = 1 flit).",
)

parser.add_argument(
    "--num-active-columns",
    type=int,
    default=0,
    help="Only nodes in the first N columns send/receive. 0 disables. Emulates "
                        "a narrower mesh on a wider fabric, e.g. --mesh-rows 4 "
                        "--num-dirs 32 --num-active-columns 6 --num-active-rows 4 "
                        "-> a 4x6 mesh on a 4x8 fabric. Default is 6.",
)

parser.add_argument(
    "--num-active-rows",
    type=int,
    default=0,
    help="Only nodes in the first N rows send/receive. 0 disables. Default is 4.",
)

#
# Add the ruby specific and protocol specific options
#
Ruby.define_options(parser)

# AIGCIC_NOC defaults: logical 4x6 mesh (active sub-mesh) on a 4x8
# fabric. num_dirs must stay a power of two for check_address_encoding,
# so the fabric keeps 32 routers/dirs and traffic is masked to the
# top-left 4x6. Override with --num-active-columns=0 --num-active-rows=0.
# 1024-bit links -> 128B flits: control messages (8B) and default data
# messages (64B + 8B) are a single flit. For burst traffic raise the
# payload together with the line size, e.g.
#   --cacheline-size 1024 --data-msg-size 1024
# (they must be passed together: data_msg_size <= cacheline_size is
# enforced by the Ruby core). Burst flits = ceil(data_length / 128B);
# the 8B header is NOT counted (see Network::Network).
parser.set_defaults(
    network="garnet",
    topology="Mesh_XY",
    num_cpus=32,
    num_dirs=32,
    mesh_rows=4,
    num_active_columns=6,
    num_active_rows=4,
    # Ideal DRAM: fixed 1-cycle latency, unlimited bandwidth (see
    # setup_memory_controllers in configs/ruby/Ruby.py) so the NoC is
    # the only bottleneck.
    mem_type="SimpleMemory",
    link_width_bits=1024,
    vcs_per_vnet=1,
    # 128 B data payload: with 1024-bit links a DATA/PUTX packet is
    # still exactly 1 flit (ceil(128B/128B)); the Ruby line size must
    # be raised together with it (data_msg_size <= cacheline_size).
    cacheline_size=128,
    data_msg_size=128,
    # 32 dirs -> 5 dest bits above the block offset; with the default
    # --lines-per-dest=1024 (10 line-select bits) and 128 B lines the
    # address bits reach bit 22, so the XOR-hash low bit sits at 22.
    xor_low_bit=22,
)

args = parser.parse_args()


def check_address_encoding(args):
    """Validate the synthetic-traffic address encoding.

    Two layouts are supported (--addr-layout):

    legacy (default): the tester encodes the destination directory in
    the address bits right above the block offset, and --lines-per-dest
    adds random line-select bits above the destination bits:

        [ ... | line_sel | destination | block offset ]

    These random bits must stay below the directory mapping's XOR-hash
    bits (--xor-low-bit) and within the memory size, otherwise packets are
    silently routed to a different directory than the traffic pattern
    intended (uniform_random still looks uniform, but transpose/neighbor/
    single-dest etc. get distorted and per-destination stats become wrong).

    mpu52: the MPU-Die 52-bit layout. The node field [36:32] is
    (node_y << 2) | node_x with node_x = mesh row (2 bits) and node_y =
    mesh column (3 bits); each node owns a contiguous 4GiB window
    [(field << 32), +4GiB). Line-select bits live inside the node's local
    space, right above the block offset, so they must stay below bit 32.
    Directory routing is purely range-based (no XOR hashing).
    """
    block_offset_bits = int(math.log2(args.cacheline_size))
    dest_bits = max(1, math.ceil(math.log2(args.num_dirs)))
    line_bits = math.ceil(math.log2(args.lines_per_dest))

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

    if args.addr_layout == "mpu52":
        addr_bits_used = block_offset_bits + line_bits
        if addr_bits_used > 32:
            max_lines = 2 ** (32 - block_offset_bits)
            fatal(
                "--lines-per-dest=%d needs line-select bits up to bit %d, "
                "but the MPU-52 layout keeps them inside the node's local "
                "4GiB space (bits [%d, 32)). Reduce --lines-per-dest to "
                "<= %d.",
                args.lines_per_dest, addr_bits_used, block_offset_bits,
                max_lines,
            )
        if dest_bits > 5:
            fatal(
                "--num-dirs=%d does not fit the MPU-52 node field: only "
                "5 bits are available at [36:32] (node_y 3 bits x node_x "
                "2 bits = 32 nodes).",
                args.num_dirs,
            )
        if int(math.log2(toMemorySize(args.mem_size))) < 37:
            fatal(
                "--mem-size=%s is too small for the MPU-52 layout: the "
                "address space spans 32 nodes x 4GiB = 128GiB (37 bits).",
                args.mem_size,
            )
        return

    addr_bits_used = block_offset_bits + dest_bits + line_bits
    mem_bits = int(math.log2(toMemorySize(args.mem_size)))

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


if args.cross_die_ratio > 0 and args.addr_layout != "mpu52":
    fatal(
        "--cross-die-ratio requires --addr-layout=mpu52: the legacy "
        "layout's address space does not cover remote-die addresses."
    )
if args.cross_die_ratio > 0 and not (0 <= args.mpu_id <= 7):
    fatal("--mpu-id must be within the 3-bit MPU-52 field [39:37] (0..7).")

# --- --addr-layout-specific defaults (after parse, before the checks) ---
if args.addr_layout == "mpu52":
    # Full per-node 4GiB space: 4GiB / block size lines per destination.
    if args.lines_per_dest is None:
        args.lines_per_dest = (4 * 1024 * 1024 * 1024) // args.cacheline_size
    # The node field replaces the XOR-hash channel selection.
    if args.xor_low_bit != 0:
        print("addr-layout=mpu52: forcing --xor-low-bit=0 "
              "(routing is range-based, no XOR hashing)")
        args.xor_low_bit = 0
    # The space spans 32 nodes x 4GiB = 128GiB regardless of --mem-size.
    if toMemorySize(args.mem_size) < (1 << 37):
        args.mem_size = "128GiB"
else:
    if args.lines_per_dest is None:
        args.lines_per_dest = 1024

dest_shift = 32 if args.addr_layout == "mpu52" else 0
print("Address layout: %s (dest field at bit %d, lines_per_dest=%d)"
      % (args.addr_layout, dest_shift, args.lines_per_dest))

check_address_encoding(args)


cpus = [
    GarnetSyntheticTraffic(
        num_packets_max=args.num_packets_max,
        single_sender=args.single_sender_id,
        single_dest=args.single_dest_id,
        sim_cycles=args.sim_cycles,
        traffic_type=args.synthetic,
        num_columns=args.num_dirs // args.mesh_rows,
        active_columns=args.num_active_columns,
        active_rows=args.num_active_rows,
        inj_rate=args.injectionrate,
        inj_vnet=args.inj_vnet,
        precision=args.precision,
        num_dest=args.num_dirs,
        lines_per_dest=args.lines_per_dest,
        block_offset=int(math.log2(args.cacheline_size)),
        dest_shift=dest_shift,
        mpu_id=args.mpu_id,
        gpu_id=args.gpu_id,
        cross_die_ratio=args.cross_die_ratio,
    )
    for i in range(args.num_cpus)
]

# create the desired simulated system
if args.addr_layout == "mpu52":
    # One 4GiB window per node: dir at router (row, col) owns the window
    # with node field (col << 2) | row (see Ruby.py
    # setup_memory_controllers). Backed by AigcicNullMemory (no host RAM).
    num_cols = args.num_cpus // args.mesh_rows
    mem_ranges = [
        AddrRange(((col << 2) | row) << 32, size="4GiB")
        for i in range(args.num_dirs)
        for row, col in [divmod(i, num_cols)]
    ]
    system = System(cpu=cpus, mem_ranges=mem_ranges)
    if args.cross_die_ratio > 0:
        # Cross-die requests keep the foreign die fields (gpu_id [50:41],
        # is_global [40], mpu_id [39:37]) in the address, so a requestor
        # legitimately addresses a space larger than this die's physmem.
        # Declare it as "valid physical address not backed by physmem"
        # (System::isMemAddr accepts external_memory_ranges), otherwise
        # the sequencer would reject those packets as PIO addresses.
        # [0, 2^51) covers every gpu_id x mpu_id x node field of the
        # MPU-52 space (bits [50:0]; the is_host bit [51] stays out).
        system.external_memory_ranges = [AddrRange(0, size=1 << 51)]
else:
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

# Override the network data-message size. The Garnet NI sizes a packet as
# ceil((data_msg_size + control_msg_size) / flit_size) flits, so with the
# default 64 data packets are 5 flits; pass --data-msg-size 8 to make them
# 1 flit (control packets are always 1 flit).
system.ruby.network.data_msg_size = args.data_msg_size

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
