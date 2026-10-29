# AIGCIC NoC SystemC/TLM demo (integration pattern A).
#
#   AigcicScMaster (SystemC IP) -> TlmToGem5Bridge -> RubySequencer
#     -> AIGCIC_NOC protocol -> Garnet NoC -> dir agent -> SimpleMemory
#
# The SC master issues a fixed number of reads (one outstanding at a
# time) through the AT handshake; the destination node is encoded in the
# address like in the synthetic-traffic driver:
#     [ 0 | dest | block offset ]
# Simulation ends when every master has all of its responses.
#
# Build (adds the SC master IP on top of the normal build):
#   scons build/NULL_AIGCIC_NOC/gem5.opt EXTRAS=extras/aigcic_sc -j <N>
#
# Run:
#   ./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_sc/demo \
#       configs/example/aigcic_sc_demo.py

import argparse
import math

import m5
from m5.defines import buildEnv
from m5.objects import *
from m5.util import addToPath, fatal

addToPath("../")

from common import Options
from ruby import Ruby

if buildEnv["PROTOCOL"] != "AIGCIC_NOC" or not buildEnv["RUBY"] or any(
    buildEnv[k] for k in buildEnv if k.startswith("USE_") and k.endswith("_ISA")
):
    fatal(
        "aigcic_sc_demo.py requires a NULL_AIGCIC_NOC build. "
        "This binary has PROTOCOL=%s, RUBY=%s." % (
            buildEnv["PROTOCOL"], buildEnv["RUBY"])
    )

parser = argparse.ArgumentParser()
Options.addNoISAOptions(parser)
Ruby.define_options(parser)

parser.add_argument(
    "--num-requests",
    type=int,
    default=10,
    help="Read requests issued by each SC master",
)
parser.add_argument(
    "--period-ns",
    type=int,
    default=100,
    help="Idle time between two requests, in nanoseconds (SystemC time)",
)
parser.add_argument(
    "--lines-per-dest",
    type=int,
    default=1,
    help="Distinct cache lines per destination (1 = one line per dir)",
)

parser.set_defaults(
    network="garnet",
    num_cpus=4,
    num_dirs=4,
    mesh_rows=2,
    topology="Mesh_XY",
    xor_low_bit=22,
    mem_type="SimpleMemory",  # ideal DRAM, as in the synthetic driver
    cacheline_size=128,
)

args = parser.parse_args()

# The demo reads one full block per request and always uses line 0 of a
# destination, so the payload must equal the block size.
args.data_msg_size = args.cacheline_size

if args.num_cpus > args.num_dirs:
    fatal("--num-cpus must not exceed --num-dirs in the demo (each L1 is "
          "also a potential destination)")

#
# Address-map sanity: same layout as the synthetic-traffic driver.
#
block_bits = int(math.log2(args.cacheline_size))
dest_bits = int(math.log2(args.num_dirs))
if 2 ** dest_bits != args.num_dirs:
    fatal("--num-dirs must be a power of two (address encoding)")
addr_bits_used = block_bits + dest_bits + int(
    math.log2(args.lines_per_dest) if args.lines_per_dest > 1 else 0
)
mem_bits = args.mem_size.bit_length() - 1 if hasattr(args.mem_size, "bit_length") else None
from m5.util.convert import toMemorySize
mem_bits = toMemorySize(args.mem_size).bit_length() - 1

if args.xor_low_bit < addr_bits_used:
    fatal(
        "--xor-low-bit=%d collides with the address encoding: with "
        "%dB lines, %d dirs and %d lines/dest the used bits are [0, %d).",
        args.xor_low_bit, args.cacheline_size, args.num_dirs,
        args.lines_per_dest, addr_bits_used,
    )

print("AIGCIC SC demo: %d masters, %d dirs, %dx%d mesh, %d requests/master, "
      "period %dns, payload %dB" % (
          args.num_cpus, args.num_dirs,
          args.num_dirs // args.mesh_rows, args.mesh_rows,
          args.num_requests, args.period_ns, args.cacheline_size))

#
# The SystemC IP replaces the synthetic-traffic testers.
#
masters = [
    AigcicScMaster(
        node_id=i,
        num_dirs=args.num_dirs,
        num_requests=args.num_requests,
        dest_shift=block_bits,
        payload_size=args.cacheline_size,
        period_ns=args.period_ns,
    )
    for i in range(args.num_cpus)
]

system = System(cpu=masters, mem_ranges=[AddrRange(args.mem_size)])
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)

# The AIGCIC_NOC config copies cpus[i].clk_domain into each L1 sequencer.
# Point the masters at the system domain like ClockedObjects would
# (creating separate orphan domains here trips the stats-group
# uniqueness check when the sequencers adopt the same object).
for m in masters:
    m.clk_domain = system.clk_domain

Ruby.create_system(args, False, system)

system.ruby.clk_domain = SrcClockDomain(
    clock=args.ruby_clock, voltage_domain=system.voltage_domain
)
system.ruby.network.data_msg_size = args.data_msg_size

#
# One TLM bridge per node: SC initiator <-> bridge.tlm (TLM),
# bridge.gem5 (gem5 RequestPort) <-> sequencer.in_ports.
#
# Do NOT pass system=system: the bridge's System param resolves via
# Parent.any from the hierarchy; passing it explicitly makes the config
# machinery adopt the System as a *child* of the bridge (cycle).
system.bridges = [TlmToGem5Bridge32() for i in range(args.num_cpus)]

i = 0
for ruby_port in system.ruby._cpu_ports:
    system.bridges[i].gem5 = ruby_port.in_ports
    masters[i].tlm = system.bridges[i].tlm
    i += 1

# SystemC_Kernel has no 'system' param in this gem5 version; attach it
# to the Root only.
kernel = SystemC_Kernel()
root = Root(full_system=False, system=system, systemc_kernel=kernel)
root.system.mem_mode = "timing"

m5.ticks.setGlobalFrequency("1ps")
m5.instantiate()
exit_event = m5.simulate(m5.MaxTick)

print("Exiting @ tick", m5.curTick(), "because", exit_event.getCause())
