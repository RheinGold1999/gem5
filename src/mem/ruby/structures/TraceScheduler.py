# Copyright (c) 2025 AIGCIC_NOC project.
#
# Global trace-replay scheduler: owns the causal state (per-record external
# after-dependency counts and arrival completion) of a trace replay run and
# chains window repetitions. See tools/trace_plan/compile_trace_plan.py for
# the plan format and TraceReplay.hh for the network-side semantics.

from m5.objects.ClockedObject import ClockedObject
from m5.objects.GarnetNetwork import GarnetNetwork
from m5.objects.GarnetSyntheticTraffic import GarnetSyntheticTraffic
from m5.params import *


class TraceScheduler(ClockedObject):
    type = "TraceScheduler"
    cxx_header = "mem/ruby/structures/TraceScheduler.hh"
    cxx_class = "gem5::ruby::TraceScheduler"

    plan_file = Param.String(
        "",
        "Path to the compiled trace plan (plan.txt) produced by "
        "tools/trace_plan/compile_trace_plan.py",
    )
    network = Param.GarnetNetwork(
        NULL, "The garnet network whose TraceReplay this scheduler drives"
    )
    testers = VectorParam.GarnetSyntheticTraffic(
        [],
        "The per-router testers, indexed by fabric router id "
        "(router id = row * fabric_cols + col)",
    )
    repeat = Param.Int(
        1, "How many times to replay the window (repetitions never overlap)"
    )
    inter_layer_cycles = Param.Int(
        0,
        "Idle gap (cycles) inserted between window repetitions, modelling "
        "inter-layer compute time (0 = back-to-back)",
    )
    event_log = Param.String(
        "",
        "Optional per-record event log (ISSUE/ARRIVE/RELAY/TAP/LEG/... per "
        "line) for msg-granularity reconciliation against the original "
        "trace (tools/trace_plan/reconcile_event_log.py)",
    )
    # clk_domain / voltage_domain are inherited from ClockedObject; the
    # driver points them at the system domain (a re-declaration here would
    # shadow the base param and leave the Clocked ctor with a null domain)
