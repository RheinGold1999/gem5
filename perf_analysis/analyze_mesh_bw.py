#!/usr/bin/env python3
"""Derive per-router-direction bandwidth utilization from a Garnet run's stats.txt.

Usage:
  python3 analyze_mesh_bw.py <run_dir/stats.txt> [--cycles N] [--mesh-rows R]

Background
----------
stats.txt contains (a) the per-node-pair packet counters added to
GarnetNetwork,

    system.ruby.network.data_traffic_distribution.n<src>.n<dst>
        packets on the DATA vnet  (5 flits each here: 1 header + 4 data)
    system.ruby.network.ctrl_traffic_distribution.n<src>.n<dst>
        packets on the CTRL vnets (request/snp, 1 flit each)

and (b) measured link-activity totals

    flits_injected / packets_injected            (per vnet subtotals + ::total)
    int_link_utilization      = sum of flits actually transmitted on all
                                internal links (all directions)
    ext_in_link_utilization / ext_out_link_utilization   (router->NI / NI->router)

This script reconstructs, hop by hop, the flit load of EVERY internal link
under the exact routing used by the run (Mesh_XY: X/col phase first with
weight 1, then Y/row phase with weight 2; a 5-flit wormhole crosses each hop
5 times). Direction convention of configs/topologies/Mesh_XY.py:

    N : router (r,c) -> (r+1,c)      S : (r,c) -> (r-1,c)
    E : router (r,c) -> (r,c+1)      W : (r,c) -> (r,c-1)
    router id = r * cols + c         (8x8 mesh here)

Link capacity in this gem5 build: 1 flit / cycle / link direction (all vnets
share the physical link, see NetworkLink::wakeup). Utilization per link =
flits / (cycles * 1 flit/cycle). The run's cycle count defaults to
10,000 (10,000,000 ticks at 1 GHz, from pwrStateResidencyTicks and clock=1000
in config.ini); pass --cycles to override.

Two things to keep straight:

  * The traffic-distribution keys are ROUTER ids (route.src_router /
    route.dest_router in GarnetNetwork::update_traffic_distribution), so every
    grid below - including "Local OUT/IN" - is per ROUTER, and each cell
    aggregates all controllers hanging off that router.
  * A router has one INDEPENDENT "Local" output port per attached controller
    (GarnetNetwork::makeExtOutLink -> Router::addOutPort("Local", ...) once per
    ext link), each 1 flit/cycle. With Mesh_XY, num_cpus == num_routers, so
    nodes-per-router = (num_cpus + num_dirs) / num_cpus = 2 for the 8x8 runs.
    Hence the PHYSICAL switch capacity is #mesh_dirs + nodes_per_router (6
    flit/cycle for an interior router), NOT 5. Pass --nodes-per-router if the
    run differs.
  * Physically present != actually used, though. In Garnet_standalone the only
    traffic generators are the L1 controllers (the dir machine drops whatever it
    receives and never answers), and destinations are directories only
    (num_dest == num_dirs, dest index encoded in the address bits). So of a
    router's 2 ext links per direction exactly ONE carries traffic, and it is a
    different one per direction: ext-OUT = the L1's link (injection, skewed by
    path length), ext-IN = the Dir's link (delivery, flat because dirs receive a
    uniform share). The other one is idle. That is exactly the "one extra flit" of
    the old 5/6 confusion: the router really has 6 physical outports, but one of
    them (the Dir's local-out) can never be loaded by this traffic, so the capacity
    that matters is #mesh_dirs + active_local = #mesh_dirs + 1 (5 flit/cycle
    interior). The Local OUT/IN grids are normalized by --active-local-links (default
    1) for the same reason. For a real protocol, where dirs emit snoops/data and
    L1s receive, use --active-local-links=<nodes-per-router>.
"""
import argparse
import contextlib
import io
import os
import re
import sys
from collections import defaultdict


def parse_stats(path):
    rx = re.compile(
        r"^system\.ruby\.network\.(data|ctrl)_traffic_distribution\.n(\d+)\.n(\d+)\s+(\d+)")
    data, ctrl = {}, {}
    totals = {}
    residency_ticks = None
    # per-controller MessageBuffer counters, keyed by (controller, buffer).
    # Unlike the router-keyed traffic matrix, m_msg_count is per CONTROLLER, so
    # it is the exact traffic of each individual ext link (the controller's
    # "Local" port). Used for the per-link Local OUT/IN view.
    bufs = {}
    brx = re.compile(r"^system\.ruby\.(\w+)\.(\w+)\.m_msg_count\s+(\d+)\s")
    # per-router switch-allocator counters, keyed by router id (r*cols+c)
    rstat = defaultdict(dict)
    rrx = re.compile(
        r"^system\.ruby\.network\.routers(\d+)\."
        r"(sw_input_arbiter_activity|sw_output_arbiter_activity|crossbar_activity|"
        r"flit_router_latency_count|"
        r"flit_router_latency_max|flit_router_latency_median|"
        r"flit_router_latency_p75)"
        r"\s+([\d.]+)\s")
    # flit whole-lifetime averages from GarnetNetwork (single scalar, or a
    # per-vnet list for the *_vnet_latency / *_vqueue_latency variants).
    # Values are in TICKS; converted to network cycles by the caller.
    flx = re.compile(
        r"^system\.ruby\.network\.(average_flit_\w+|"
        r"flit_lifetime_median|flit_lifetime_p75|flit_lifetime_max|"
        r"flit_network_latency_median|flit_network_latency_p75|"
        r"flit_network_latency_max)\s+(.*)$")
    numrx = re.compile(r"[\d.]+(?:[eE][+-]?\d+)?")
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            m = rx.match(line)
            if m:
                (data if m.group(1) == "data" else ctrl)[
                    (int(m.group(2)), int(m.group(3)))
                ] = int(m.group(4))
                continue
            mb = brx.match(line)
            if mb:
                bufs[(mb.group(1), mb.group(2))] = int(mb.group(3))
                continue
            mr = rrx.match(line)
            if mr:
                # median/p75 come out as decimals (e.g. 1.500000); keep the
                # integer stats as ints, the quantile ones as floats.
                val = mr.group(3)
                rstat[int(mr.group(1))][mr.group(2)] = \
                    int(val) if val.isdigit() else float(val)
                continue
            mf = flx.match(line)
            if mf:
                vals = parse_stat_vector(mf.group(2))
                if vals:
                    totals[mf.group(1)] = \
                        vals if len(vals) > 1 else vals[0]
                continue
            if residency_ticks is None and "pwrStateResidencyTicks" in line:
                residency_ticks = int(line.split()[1])
            mt = re.match(r"^system\.ruby\.network\.(flits_injected|flits_received|packets_injected|"
                          r"int_link_utilization|ext_in_link_utilization|"
                          r"ext_out_link_utilization)(::total)?\s+(\d+)\s", line)
            if mt:
                totals[f"{mt.group(1)}::total" if mt.group(2) else mt.group(1)] = \
                    int(mt.group(3))
    return data, ctrl, totals, residency_ticks, rstat, bufs


def parse_stat_vector(col):
    """Parse a gem5 one-line vector stat column, keeping positions.

    gem5 prints vectors as '|v0 |v1 |nan |v3 (Unspecified)'; the idle
    vnets print 'nan', which a plain number scan would drop and shift
    every later label by one. Returns floats with None for nan.
    """
    num = re.compile(r"[-+]?\d*\.?\d+(?:[eE][+-]?\d+)?")
    if "|" not in col:
        m = num.search(col)
        return [float(m.group())] if m else []
    out = []
    for tok in col.split("|")[1:]:
        tok = tok.strip().split()[0] if tok.strip() else ""
        if tok.lower().lstrip("+-") == "nan":
            out.append(None)                   # idle vnet, no samples
            continue
        try:
            out.append(float(tok))
        except ValueError:
            out.append(None)                   # unparseable
    return out


def read_config(path):
    """Pull the mesh geometry out of config.ini so the caller doesn't have to
    repeat --mesh-rows / --nodes-per-router / --active-local-links.

      num_rows      network.num_rows        (= the --mesh-rows option)
      num_routers   count of [system.ruby.network.routers*]
      num_ext_links count of [system.ruby.network.ext_links*]
      num_injectors count of type=GarnetSyntheticTraffic testers
                   (standalone: only those controllers ever inject -> that many
                   ext links per direction actually carry traffic)
      net_clock    clock= of [system.ruby.clk_domain], i.e. the period in TICKS
                   that the Garnet network runs on (= --ruby-clock; 2GHz -> 500).
                   THIS is the cycle base for link/router utilization, NOT the
                   system cpu clock. (config.ini prints the clock value as the
                   period in ticks at the 1 ps global frequency: 1GHz -> 1000,
                   2GHz -> 500.)
      clock        first clock= seen (system.clk_domain) - fallback only
      active_cols / active_rows  active_columns=/active_rows= of the
                   GarnetSyntheticTraffic testers (garnet_synth_traffic.py's
                   --num-active-columns/--num-active-rows). All testers share
                   the same values; None = no masking (0 in config.ini maps to
                   None). Lets the script auto-derive the active sub-mesh.
      protocol     controller type family, inferred from the type= line of the
                   Ruby controllers in config.ini:
                     "Garnet_standalone" -> only L1s inject and only Dirs are
                        destinations, so exactly ONE of the two Local ports per
                        direction carries traffic (active_local = 1)
                     "MI_example"        -> BOTH the L1 and the Dir controllers
                        send AND receive over the network (GETX/PUTX requests,
                        DATA/WB_ACK responses, forwards), so BOTH Local ports per
                        direction carry traffic (active_local = nodes_per_router)
                   None if no controller type is found.
      ext_map      {controller_name: router_id} built from the ext_links
                   sections (ext_node=system.ruby.l1_cntrl0,
                   int_node=system.ruby.network.routers00). Gives the exact
                   controller <-> router binding needed for the per-link view.
      buf_dir      {(controller, buffer): "OUT"/"IN"} read from the MessageBuffer
                   port wiring in config.ini: a buffer with
                   out_port=system.ruby.network.in_port is controller->network
                   (OUT); in_port=system.ruby.network.out_port is
                   network->controller (IN). This is AUTHORITATIVE and beats the
                   name heuristic, which is wrong for the DMA controller (its
                   buffers are named from the directory's view: requestToDir is
                   the DMA's OUT, responseFromDir is its IN). Only present when
                   gem5 serialised the ports (MI_example does, some configs do
                   not) -- the per-link view falls back to the name rule then.
      data_msg_size / control_msg_size / ni_flit_size
                   [system.ruby.network] sizing params. Together they fix the
                   flit count gem5 actually puts on the wire (this fork:
                   Network.cc sets m_data_msg_size = data_msg_size WITHOUT
                   the header, i.e. burst flits = ceil(payload / flit_size),
                   see NetworkInterface.cc). So:
                     data pkt flits = ceil(data_msg_size / ni_flit_size)
                     ctrl pkt flits = ceil(control_msg_size / ni_flit_size)
                   e.g. 64B or 128B data at 1024-bit links -> 1 flit.
                   None if not found.

    Returns a dict with these keys; count keys stay 0 if absent.
    """
    cfg = {"clock": None, "net_clock": None, "num_rows": None,
           "num_routers": 0, "num_ext_links": 0, "num_injectors": 0,
           "active_cols": None, "active_rows": None, "protocol": None,
           "ext_map": {}, "buf_dir": {},
           "data_msg_size": None, "control_msg_size": None,
           "ni_flit_size": None}
    if not os.path.exists(path):
        return cfg
    in_network = False
    in_ruby_clk = False
    in_tester = False          # inside a [system.cpu*] GarnetSyntheticTraffic section
    cur_ext = None             # controller name of the ext_links section being read
    cur_buf = None             # (controller, buffer) of the MessageBuffer section
    with open(path) as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("["):
                in_network = (line == "[system.ruby.network]")
                in_ruby_clk = (line == "[system.ruby.clk_domain]")
                m = re.match(r"^\[system\.ruby\.network\.(routers|ext_links)"
                            r"[0-9]*\]$", line)
                if m:
                    cfg["num_routers" if m.group(1) == "routers"
                        else "num_ext_links"] += 1
                cur_ext = None
                # MessageBuffer section: [system.ruby.<ctrl>.<buffer>]
                mb = re.match(r"^\[system\.ruby\.(\w+)\.(\w+)\]$", line)
                cur_buf = (mb.group(1), mb.group(2)) if mb else None
                in_tester = re.match(r"^\[system\.cpu[0-9]+\]$", line) is not None
                continue
            # MessageBuffer port wiring -> authoritative OUT/IN direction
            if cur_buf is not None:
                if re.match(r"^\s*out_port=system\.ruby\.network\.in_port", line):
                    cfg["buf_dir"][cur_buf] = "OUT"
                elif re.match(r"^\s*in_port=system\.ruby\.network\.out_port", line):
                    cfg["buf_dir"][cur_buf] = "IN"
            # inside an ext_links section: capture the node<->router binding
            if re.match(r"^\s*ext_node=system\.ruby\.(\w+)", line):
                cur_ext = re.match(r"^\s*ext_node=system\.ruby\.(\w+)",
                                   line).group(1)
                continue
            if cur_ext is not None:
                m = re.match(r"^\s*int_node=system\.ruby\.network\.routers(\d+)",
                             line)
                if m:
                    cfg["ext_map"][cur_ext] = int(m.group(1))
                    cur_ext = None
                continue
            if re.match(r"^\s*type=GarnetSyntheticTraffic\b", line):
                cfg["num_injectors"] += 1
                in_tester = False          # type= line counts the section once
            # Protocol family: from the Ruby controller class names in
            # config.ini, e.g. type=MI_example_Directory_Controller or
            # type=Garnet_standalone_L1Cache_Controller. This decides how many
            # Local ports per router actually carry traffic (see docstring).
            m = re.match(r"^\s*type=(Garnet_standalone|MI_example)_\w*Controller\b",
                         line)
            if m and cfg["protocol"] is None:
                cfg["protocol"] = m.group(1)
            if re.match(r"^\s*clock=(\d+)", line):
                c = int(re.match(r"^\s*clock=(\d+)", line).group(1))
                if in_ruby_clk and cfg["net_clock"] is None:
                    cfg["net_clock"] = c          # network cycle period (ticks)
                if cfg["clock"] is None:
                    cfg["clock"] = c
            if in_network:
                m = re.match(r"^\s*num_rows=(\d+)", line)
                if m:
                    cfg["num_rows"] = int(m.group(1))
                # packet sizing -> flit count (only inside [system.ruby.network])
                m = re.match(r"^\s*data_msg_size=(\d+)", line)
                if m and cfg["data_msg_size"] is None:
                    cfg["data_msg_size"] = int(m.group(1))
                m = re.match(r"^\s*control_msg_size=(\d+)", line)
                if m and cfg["control_msg_size"] is None:
                    cfg["control_msg_size"] = int(m.group(1))
                m = re.match(r"^\s*ni_flit_size=(\d+)", line)
                if m and cfg["ni_flit_size"] is None:
                    cfg["ni_flit_size"] = int(m.group(1))
            # active sub-mesh mask of the synthetic testers (all share the same
            # values; 0 = no limit on that axis)
            m = re.match(r"^\s*active_columns=(\d+)", line)
            if m and cfg["active_cols"] is None:
                cfg["active_cols"] = int(m.group(1)) or None
            m = re.match(r"^\s*active_rows=(\d+)", line)
            if m and cfg["active_rows"] is None:
                cfg["active_rows"] = int(m.group(1)) or None
    return cfg


def route_flits(pairs, flits_per_pkt, rows, cols):
    """Add flit load to every internal link for one (src,dst)->pkt matrix."""
    lf = defaultdict(int)
    for (s, d), pkts in pairs.items():
        f = pkts * flits_per_pkt
        sr, sc = divmod(s, cols)
        dr, dc = divmod(d, cols)
        # X phase: along source row sr, from col sc toward dc
        step = 1 if dc > sc else -1
        for c in range(sc, dc, step):          # c = departing column
            lf[("E" if step > 0 else "W", sr, c)] += f
        # Y phase: along destination column dc, from row sr toward dr
        step = 1 if dr > sr else -1
        for r in range(sr, dr, step):          # r = departing row
            lf[("N" if step > 0 else "S", dc, r)] += f
    return lf


def print_grid(title, grid, rows, cols, width=9):
    print(title)
    print("        " + " ".join(f"{c:>{width}}" for c in range(cols)))
    for r in range(rows):
        print(f"  {r}     " + " ".join(
            f"{'-':>{width}}" if grid[(r, c)] is None
            else f"{grid[(r, c)]:{width}.2f}%"
            for c in range(cols)))
    # avg/max/min over the DISPLAYED region only (a grid dict may carry extra
    # keys for routers outside the active sub-mesh -> must not dilute the stats)
    vals = [grid[(r, c)] for r in range(rows) for c in range(cols)
            if (r, c) in grid and grid[(r, c)] is not None]
    print(f"        avg {sum(vals) / len(vals):6.2f}%   max {max(vals):6.2f}%   "
          f"min {min(vals):6.2f}%\n")


def print_value_grid(title, grid, rows, cols, fmt="{:7.2f}", unit=""):
    """Generic (non-percent) grid; grid[(r,c)] may be None -> '-'."""
    print(title)
    print("        " + " ".join(f"{c:>7}" for c in range(cols)))
    for r in range(rows):
        print(f"  {r}     " + " ".join(
            "      -  " if grid[(r, c)] is None else fmt.format(grid[(r, c)])
            for c in range(cols)))
    vals = [grid[(r, c)] for r in range(rows) for c in range(cols)
            if (r, c) in grid and grid[(r, c)] is not None]
    print(f"        avg {sum(vals)/len(vals):7.2f}{unit}  "
          f"max {max(vals):7.2f}{unit}  min {min(vals):7.2f}{unit}\n")


def contention_grid(rstat, rows, cols, phys_cols, cycles, capacity_note):
    """Per-router arbitration-contention view.

    rows/cols = the region DISPLAYED (may be an active sub-mesh); phys_cols is
    the FULL mesh column count used to turn (r,c) into a physical router id,
    because rstat is keyed by the real router id (r * phys_cols + c). Passing
    the active cols here would mislabel routers (diagonal zeros).

    ratio = sw_input_arbiter_activity / sw_output_arbiter_activity
        ~1.0 : inputs are granted as fast as they request -> the router is
               NOT output-contention-bound; any low link load there is IDLE
               (under-supplied by routing), not backpressured.
        >1.1 : many input VCs wanted to switch but lost arbitration ->
               output contention / congestion at this router.
    Also shows crossbar_activity/cycle = flits the router actually switched.
    """
    ratio, xbar = {}, {}
    for r in range(rows):
        for c in range(cols):
            rid = r * phys_cols + c
            ia = rstat.get(rid, {}).get("sw_input_arbiter_activity", 0)
            oa = rstat.get(rid, {}).get("sw_output_arbiter_activity", 0)
            cb = rstat.get(rid, {}).get("crossbar_activity", 0)
            ratio[(r, c)] = (ia / oa) if oa else None
            xbar[(r, c)] = cb / cycles
    print_value_grid(
        "OUTPUT CONTENTION per router = input_arb/output_arb activity "
        "(~1=idle/under-supplied, >1.1=contended):",
        ratio, rows, cols, fmt="{:7.2f}")
    print_value_grid(
        "Router switch throughput = crossbar_activity / cycle "
        f"(flits routed per cycle, of {capacity_note}):",
        xbar, rows, cols, fmt="{:7.2f}")


def router_latency_grids(rstat, rows, cols, phys_cols):
    """Per-router flit residence latency (enter router -> leave router).

    Source stats (per router, added to garnet Router):
      flit_router_latency_count   flits that left this router
      flit_router_latency_max     max residence cycle of any flit
      flit_router_latency_median  median (p50) residence cycle, linear-
                                  interpolation empirical quantile
      flit_router_latency_p75     75% percentile residence cycle

    A flit enters at the cycle it is consumed off an input link (incl. the
    Local port from the NI) and leaves the cycle it traverses the crossbar
    (incl. ejection over the Local port), counted as leave - enter + 1, so a
    1-cycle pass-through = 1. Latency is in NETWORK cycles (system.ruby
    clock domain). Flits still inside the router at the end of the run are
    not counted (they never left).

    rows/cols = displayed region (active sub-mesh); phys_cols is the FULL
    mesh column count, since rstat is keyed by physical router id.
    Returns (max, median, p75) grids; None where the router saw no
    flits.
    """
    mx, med, p75 = {}, {}, {}
    for r in range(rows):
        for c in range(cols):
            rid = r * phys_cols + c
            rs = rstat.get(rid, {})
            cnt = rs.get("flit_router_latency_count", 0)
            if cnt:
                mx[(r, c)] = float(rs.get("flit_router_latency_max", 0))
                med[(r, c)] = float(rs.get("flit_router_latency_median", 0))
                p75[(r, c)] = float(rs.get("flit_router_latency_p75", 0))
            else:
                for g in (mx, med, p75):
                    g[(r, c)] = None
    return mx, med, p75


def read_command(path):
    """Return the gem5 invocation that produced this run, from command.txt in
    the same directory as stats.txt (gem5 writes it next to config.ini).

    command.txt holds a header line or two (e.g. "git: <hash>") followed by the
    actual command line, so pick the line that looks like the invocation (one
    that names a gem5 binary). Returns None if the file is missing.
    """
    if not os.path.exists(path):
        return None
    lines = [l.strip() for l in open(path) if l.strip()]
    for line in lines:
        if re.search(r"\bgem5\.(opt|debug|fast)\b", line):
            return line
    return lines[-1] if lines else None


def main():
    ap = argparse.ArgumentParser(
        description="If <stats> is a FILE, analyze that single stats.txt. "
                    "If it is a DIRECTORY, recursively scan it for every "
                    "stats.txt and analyze each one, writing the output to "
                    "perf.txt next to the stats.txt.")
    ap.add_argument("stats", help="stats.txt file, or a directory to batch-scan")
    ap.add_argument("--cycles", type=int, default=0)
    ap.add_argument("--mesh-rows", type=int, default=None,
                    help="mesh rows; auto-read from config.ini (network.num_rows) "
                         "if omitted. cols is derived from the router count.")
    ap.add_argument("--flits-per-data-pkt", type=int, default=None,
                    help="flits per DATA-vnet packet; auto from config.ini as "
                         "ceil(data_msg_size/ni_flit_size) "
                         "if omitted (e.g. 1 for the --data-msg-size=8 runs, 5 "
                         "for the default 64B payload).")
    ap.add_argument("--flits-per-ctrl-pkt", type=int, default=None,
                    help="flits per CTRL-vnet packet; auto from config.ini as "
                         "ceil(control_msg_size/ni_flit_size) if omitted "
                         "(1 for the default control_msg_size=8, ni_flit_size=16).")
    ap.add_argument("--nodes-per-router", type=int, default=None,
                    help="controllers (hence external links / Local outports) "
                         "attached to each router; auto from config.ini "
                         "(#ext_links / #routers).")
    ap.add_argument("--active-local-links", type=int, default=None,
                    help="per router, how many of its ext links actually carry "
                         "traffic in ONE direction. Auto for Garnet_standalone = #"
                         "injectors/#routers (1 for these 8x8 runs, since only "
                         "the L1 testers inject and dirs are destinations). A real "
                         "protocol (dirs emit snoops/data) needs "
                         "= --nodes-per-router.")
    ap.add_argument("--active-rows", type=int, default=0,
                  help="only rows [0, active-rows) belong to the active sub-mesh "
                       "shown/aggregated; 0 = all rows. Use with runs launched "
                       "via garnet_synth_traffic.py --num-active-rows.")
    ap.add_argument("--active-columns", type=int, default=0,
                  help="only columns [0, active-columns) belong to the active "
                       "sub-mesh shown/aggregated; 0 = all columns. Use with "
                       "garnet_synth_traffic.py --num-active-columns.")
    args = ap.parse_args()

    if os.path.isdir(args.stats):
        batch_run(args)
        return
    run_analysis(args.stats, args)


def batch_run(args):
    """Directory mode: recursively find every stats.txt under the given dir
    and analyze it, saving the report to perf.txt in the same directory.
    Each run is isolated: a bad stats.txt only fails its own report.
    """
    root_dir = args.stats
    stats_list = sorted(
        os.path.join(root, "stats.txt")
        for root, _, files in os.walk(root_dir) if "stats.txt" in files)
    if not stats_list:
        sys.exit(f"no stats.txt found anywhere under {root_dir}")
    print(f"batch mode: {len(stats_list)} run(s) found under {root_dir}")
    ok = fail = 0
    for i, sp in enumerate(stats_list, 1):
        out_path = os.path.join(os.path.dirname(sp), "perf.txt")
        print(f"[{i}/{len(stats_list)}] {sp}")
        # Capture the report in memory first; only write perf.txt if the
        # analysis completed, so a half-written report never masquerades
        # as a valid one.
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                run_analysis(sp, args)
        except SystemExit as e:
            print(f"    FAILED: {e}")
            fail += 1
            continue
        except Exception as e:                       # noqa: BLE001
            print(f"    FAILED: {e.__class__.__name__}: {e}")
            fail += 1
            continue
        with open(out_path, "w") as f:
            f.write(buf.getvalue())
        print(f"    -> {out_path}")
        ok += 1
    print(f"batch done: {ok} ok, {fail} failed")


def run_analysis(stats_path, args):

    # Print the gem5 invocation that produced this run FIRST, from command.txt
    # in the same directory as stats.txt (gem5 writes it next to config.ini).
    cmd = read_command(os.path.join(os.path.dirname(stats_path), "command.txt"))
    if cmd:
        print(f"# {cmd}")

    data, ctrl, totals, residency, rstat, bufs = parse_stats(stats_path)
    max_id = max([max(s, d) for (s, d) in data] +
                 [max(s, d) for (s, d) in ctrl])
    # data/ctrl_traffic_distribution is keyed by ROUTER id, not controller/node
    # id (GarnetNetwork::update_traffic_distribution uses route.src_router /
    # route.dest_router), so this is the router count and the grids below are
    # per router, not per node.
    n_routers = max_id + 1

    # --- geometry: prefer CLI, else config.ini, else fall back -----------------
    cfg = read_config(os.path.join(os.path.dirname(stats_path), "config.ini"))
    if args.mesh_rows:
        rows = args.mesh_rows
    elif cfg["num_rows"]:
        rows = cfg["num_rows"]
    else:
        rows = int(n_routers ** 0.5)
        if rows * rows != n_routers:
            sys.exit(f"cannot infer mesh rows from {n_routers} routers and "
                    "config.ini has no num_rows; pass --mesh-rows")
    cols = n_routers // rows
    assert cols * rows == n_routers, "mesh rows x cols != router count"

    # Active sub-mesh = the top-left a_rows x a_cols region (matches the
    # garnet_synth_traffic.py --num-active-rows/--num-active-columns masking:
    # node id = col + row*cols, active iff id%cols < a_cols and id//cols < a_rows).
    # 0 means "no limit on that axis" (full mesh -> backward compatible).
    a_rows = args.active_rows or cfg["active_rows"] or rows
    a_cols = args.active_columns or cfg["active_cols"] or cols
    if (a_rows, a_cols) != (rows, cols):
        src = "CLI" if (args.active_rows or args.active_columns) else "config.ini"
        print(f"  (auto: active sub-mesh {a_rows}x{a_cols} from {src})")
    if not (1 <= a_rows <= rows and 1 <= a_cols <= cols):
        sys.exit(f"--active-rows/--active-columns must be within the physical "
                f"{rows}x{cols} mesh; got {a_rows}x{a_cols} (0 = full).")
    n_data, n_ctrl = sum(data.values()), sum(ctrl.values())

    # --- packet -> flit sizing (auto from config.ini) -----------------------
    # NOTE (this fork): Network.cc sets m_data_msg_size = data_msg_size
    # WITHOUT the header, so a DATA packet occupies
    # ceil(data_msg_size / ni_flit_size) flits and a CTRL packet
    # ceil(control_msg_size / ni_flit_size) (stock gem5 would add the
    # header to the data size). Derive both from config.ini so a
    # --data-msg-size=8 run (1 flit/pkt), a 64B run (1 flit) and a 128B
    # run (1 flit) are all handled without hand-passing
    # --flits-per-data-pkt.
    fpp_data = args.flits_per_data_pkt
    fpp_ctrl = args.flits_per_ctrl_pkt
    sizing_src = []
    if fpp_data is None or fpp_ctrl is None:
        d, cm, fs = (cfg["data_msg_size"], cfg["control_msg_size"],
                     cfg["ni_flit_size"])
        if d is not None and cm is not None and fs:
            auto_data = -(-d // fs)             # ceil, integer-only
            auto_ctrl = -(-cm // fs)
            if fpp_data is None:
                fpp_data = auto_data
                sizing_src.append(f"data={auto_data} flits ceil({d}/{fs})")
            if fpp_ctrl is None:
                fpp_ctrl = auto_ctrl
                sizing_src.append(f"ctrl={auto_ctrl} flits ceil({cm}/{fs})")
    if fpp_data is None:
        fpp_data = 1                           # fallback: no config.ini sizing
    if fpp_ctrl is None:
        fpp_ctrl = 1
    if sizing_src:
        print(f"  (auto: {cfg.get('protocol') or 'packet'} sizing -> "
              + "; ".join(sizing_src) + ")")

    if args.nodes_per_router:
        local_ports = args.nodes_per_router
    elif cfg["num_ext_links"] and cfg["num_routers"]:
        local_ports = cfg["num_ext_links"] // cfg["num_routers"]
    else:
        local_ports = 1  # degenerate; caller should say
    if args.active_local_links is not None:
        active_local = args.active_local_links
        active_local_src = "CLI"
    elif cfg["protocol"] == "Garnet_standalone":
        # Garnet_standalone: the dir machine drops every request it receives and
        # never answers, and the only traffic generators are the L1 testers. So
        # of a router's Local ports per direction exactly ONE carries traffic:
        # ext-OUT = the L1's (injection), ext-IN = the Dir's (delivery).
        active_local = max(1, round(cfg["num_injectors"] / cfg["num_routers"])) \
            if cfg["num_injectors"] and cfg["num_routers"] else 1
        active_local_src = "Garnet_standalone"
    elif cfg["protocol"] == "MI_example":
        # MI_example: a REAL coherence protocol -- both controllers talk over the
        # network in both directions, so BOTH Local ports of every router carry
        # traffic: the L1 sends GETX/PUTX requests + DATA responses and receives
        # forwards + DATA; the Dir receives requests and sends DATA / WB_ACK /
        # WB_NACK / forwards. Hence active_local = nodes_per_router (2 here).
        active_local = local_ports
        active_local_src = "MI_example"
    elif cfg["num_injectors"] and cfg["num_routers"]:
        active_local = max(1, round(cfg["num_injectors"] / cfg["num_routers"]))
        active_local_src = "#injectors/#routers (assumed standalone-like)"
    else:
        active_local = local_ports   # no synthetic testers -> assume all inject
        active_local_src = "nodes-per-router (all ports active)"
    # Short tag used in the human-readable banners below, so the printed text
    # matches the protocol actually in the run (was hard-coded Garnet_standalone).
    # If the user forced --active-local-links, respect that over the protocol
    # default so the note never contradicts the effective active_local.
    if args.active_local_links is not None:
        local_note = (f"forced by --active-local-links={active_local} "
                      f"(protocol default overridden)")
    elif active_local == 1 and cfg["protocol"] == "Garnet_standalone":
        local_note = ("only the L1 injects and only the Dir receives, so exactly "
                      "one of the two Local ports per direction carries traffic")
    elif cfg["protocol"] == "MI_example" and active_local == local_ports:
        local_note = ("MI_example is a real protocol: both the L1 and the Dir "
                      "send and receive, so BOTH Local ports per direction "
                      "carry traffic")
    else:
        local_note = f"{active_local} of {local_ports} Local ports carry traffic"

    # --- network cycles in this run ----------------------------------------
    # The Garnet network is clocked by system.ruby.clk_domain (net_clock), NOT
    # the system cpu clock. config.ini's clock= is the PERIOD in ticks (1 ps
    # global freq: 2GHz -> 500, 1GHz -> 1000), so network cycles =
    # residency_ticks // net_clock. (Using the system clock here undercounts the
    # network's cycles and inflates every utilization -- the source of the
    # impossible >100% link loads on a 2GHz-ruby / 1GHz-system run.)
    net_period = cfg.get("net_clock") or cfg["clock"]
    if not args.cycles:
        if residency and net_period:
            args.cycles = residency // net_period  # ticks // (ticks/cycle)
        else:
            args.cycles = 10000
    cycles = args.cycles

    print(f"=== Run: {os.path.basename(os.path.dirname(stats_path))} ===")
    print(f"mesh {rows}x{cols} ({n_routers} routers), network cycles = {cycles} "
          f"(capacity: 1 flit/cycle per link direction)")
    if not args.cycles:
        print(f"  (cycles = {residency} ticks // {net_period} ticks/cycle = network "
              f"clock {10**12 // net_period / 1e9:.0f} GHz "
              f"[system.ruby.clk_domain]; system cpu clock "
              f"{10**12 // cfg['clock'] / 1e9:.0f} GHz is NOT the network clock)")
    if a_rows != rows or a_cols != cols:
        print(f"  ACTIVE sub-mesh {a_rows}x{a_cols} "
              f"({a_rows * a_cols} routers shown/aggregated; the other "
              f"{n_routers - a_rows * a_cols} routers carry no traffic):")
    det = []
    if not args.mesh_rows and cfg["num_rows"]:
        det.append(f"num_rows={rows} from config.ini")
    if not args.nodes_per_router and cfg["num_ext_links"] and cfg["num_routers"]:
        det.append(f"{local_ports} controller(s)/router from "
                  f"{cfg['num_ext_links']} ext links")
    if (args.active_local_links is None and cfg["num_injectors"]
            and cfg["num_routers"] and cfg["protocol"] == "Garnet_standalone"):
        det.append(f"{active_local} injector(s)/router (Garnet_standalone)")
    if args.active_local_links is None and cfg["protocol"] == "MI_example":
        det.append(f"{active_local} active Local port(s)/direction "
                  f"(MI_example: both L1 and Dir send+receive)")
    if det:
        print("  (auto: " + "; ".join(det) + ")")
    print(f"  {local_ports} independent \"Local\" ports per direction "
          f"(1 flit/cycle each); of those only {active_local} per direction "
          f"actually carry traffic in this traffic model.")
    print(f"  ({local_note}.)")
    print(f"  router outbound capacity = #mesh_dirs + {active_local} (only the "
          f"outports this traffic can load) = corner {2 + active_local}/edge "
          f"{3 + active_local}/interior {4 + active_local} flit/cycle\n")
    est_flits = n_data * fpp_data + n_ctrl * fpp_ctrl
    print(f"traffic: data vnet {n_data} pkts x {fpp_data} flits + "
          f"ctrl vnets {n_ctrl} pkts x {fpp_ctrl} flit = {n_data + n_ctrl} pkts, "
          f"{est_flits} flits est")
    print(f"  vs stats: packets_injected {totals.get('packets_injected::total')}, "
          f"flits_injected {totals.get('flits_injected::total')}\n")

    # --- internal link flit load -------------------------------------------
    lf = route_flits(data, fpp_data, rows, cols)
    for k, v in route_flits(ctrl, fpp_ctrl, rows, cols).items():
        lf[k] += v                     # merge, do NOT overwrite
    est_int = sum(lf.values())
    meas_int = totals.get("int_link_utilization")
    n_links = 2 * a_rows * (a_cols - 1) + 2 * a_cols * (a_rows - 1)
    in_flight_int = est_int - meas_int
    print(f"internal links: {n_links} (directions)")
    print(f"  est flits on int links (from traffic matrix, XY routing): {est_int}")
    print(f"  measured int_link_utilization:                            {meas_int}")
    print(f"  difference (flits still in flight / dropped at end):      {in_flight_int}"
          f"  ({100 * in_flight_int / est_int:.2f}%)\n")

    grids = {}
    exist = {"E": lambda r, c: c + 1 < a_cols,
             "W": lambda r, c: c >= 1,
             "N": lambda r, c: r + 1 < a_rows,
             "S": lambda r, c: r >= 1}
    dest = {"N": "(r+1,c)", "S": "(r-1,c)", "E": "(r,c+1)", "W": "(r,c-1)"}
    for direction in "NESW":
        g = {}
        for r in range(a_rows):
            for c in range(a_cols):
                if not exist[direction](r, c):
                    g[(r, c)] = None          # no link in this direction
                    continue
                fl = lf.get((direction, r, c), 0) if direction in "EW" \
                    else lf.get((direction, c, r), 0)   # N/S key is (col, row)
                g[(r, c)] = 100.0 * fl / cycles
        grids[direction] = g
        print_grid(f"--- {direction} links: router (r,c) -> {dest[direction]}, "
                   f"% of 1 flit/cycle ---",
                   g, a_rows, a_cols)

    # --- local (external) direction, per ROUTER ------------------------------
    # The traffic-distribution keys are router ids, so out_flits[r] / in_flits[r]
    # aggregate ALL controllers attached to that router. Physically each of those
    # controllers has its own "Local" port (GarnetNetwork::makeExtOutLink ->
    # Router::addOutPort("Local", ...) once per ext link) => local_ports ports
    # per direction. How many of them actually carry traffic depends on the
    # protocol:
    #   Garnet_standalone: only active_local=1 of them per direction, and it is a
    #     DIFFERENT controller each way:
    #       ext-OUT: only the L1 controllers have testers (the dir machine drops
    #                everything it receives and never responds)  -> 1 link
    #       ext-IN : destinations are directories only (num_dest == num_dirs, the
    #                dest index is encoded in the address)        -> 1 link
    #     So per-router sum == duty of that router's single active link, and the
    #     grid must be normalized by active_local(=1), not by local_ports.
    #   MI_example: active_local = local_ports (2 here). Both the L1 and the Dir
    #     controller send AND receive, so the per-router sum is spread over BOTH
    #     Local ports per direction and the grid is the AVERAGE duty of those two
    #     links (normalized by active_local=2). NOTE this is a per-router average:
    #     the two links are NOT equally loaded (L1 carries the requests+data,
    #     Dir carries the forwarded snoops+responses), so the true per-link duty
    #     of each individual Local port differs -- this script reports their mean.
    out_flits, in_flits = defaultdict(int), defaultdict(int)
    for (s, d), pkts in data.items():
        out_flits[s] += pkts * fpp_data
        in_flits[d] += pkts * fpp_data
    for (s, d), pkts in ctrl.items():
        out_flits[s] += pkts * fpp_ctrl
        in_flits[d] += pkts * fpp_ctrl
    # raw flits/cycle per router (used for the router total below)
    lo_fpc = {divmod(i, cols): out_flits[i] / cycles for i in range(rows * cols)}
    li_fpc = {divmod(i, cols): in_flits[i] / cycles for i in range(rows * cols)}
    # grid = duty of the active local link(s) of that router
    lo = {k: 100.0 * v / active_local for k, v in lo_fpc.items()}
    li = {k: 100.0 * v / active_local for k, v in li_fpc.items()}
    lo_what = ("here the L1's" if active_local == 1
               else f"the average over {active_local} links")
    li_what = ("here the Dir's" if active_local == 1
               else f"the average over {active_local} links")
    print_grid(f"--- Local OUT (NI -> router) = duty of the injecting link(s) "
               f"({active_local} of {local_ports} ext links per router are used; "
               f"{lo_what}), % of {active_local} flit/cycle ---",
               lo, a_rows, a_cols)
    print_grid(f"--- Local IN (router -> NI) = duty of the delivering link(s) "
               f"({active_local} of {local_ports} ext links per router are used; "
               f"{li_what}), % of {active_local} flit/cycle ---",
               li, a_rows, a_cols)
    est_ext_out, est_ext_in = sum(out_flits.values()), sum(in_flits.values())
    n_active_routers = a_rows * a_cols
    n_ext_links = n_routers * local_ports
    n_act_links = n_active_routers * active_local
    print(f"  est ext-out {est_ext_out} vs measured {totals.get('ext_out_link_utilization')} "
          f"| est ext-in {est_ext_in} vs measured {totals.get('ext_in_link_utilization')}")
    print(f"  local side: {n_act_links} ACTIVE ext links x {cycles} cycles = "
          f"{n_act_links * cycles} flit capacity -> "
          f"{100.0 * est_ext_out / (n_act_links * cycles):.2f}% avg duty on the "
          f"injecting link(s), {100.0 * est_ext_in / (n_act_links * cycles):.2f}% "
          f"on the delivering link(s)")
    print(f"  (the other {n_ext_links - n_act_links} ext links sit idle: "
          f"{100.0 * est_ext_out / (n_ext_links * cycles):.2f}% if you insist on "
          f"dividing by all {n_ext_links})\n")

    # --- per-link Local OUT/IN (DEFAULT VIEW) --------------------------------
    # The per-router grids above average the router's Local ports together. This
    # view goes one level deeper: each controller has its OWN Local port
    # (GarnetNetwork::makeExtOutLink -> Router::addOutPort("Local", ...) per ext
    # link), and the per-controller MessageBuffer counters give that individual
    # link's exact traffic. Buffer direction comes from the SLICC naming:
    #   *FromCache / *FromDir  = controller -> network  (network="To")  -> OUT
    #   *ToCache   / *ToDir    = network -> controller  (network="From") -> IN
    # Data-vnet buffers (name contains "response") carry --flits-per-data-pkt
    # flits per message; the rest are 1-flit control messages. This is validated
    # against the router-keyed traffic matrix (sum of data buffers == data pkts,
    # sum of ctrl buffers == ctrl pkts).
    #
    # Garnet_standalone: the Dir machine has NO "FromDir" (out) buffers and the
    # L1 has NO "ToCache" (in) buffers -- the dir only receives and the L1 only
    # injects -- so the Dir-OUT and L1-IN links are structurally EMPTY. They are
    # still printed (as 0.00%) so it is explicit that those directions carry no
    # traffic. MI_example loads all four.
    linkrx = re.compile(r"^(\w+?)(From|To)(Cache|Dir|Dma)$")
    buf_dir = cfg.get("buf_dir") or {}
    # per (controller, direction) -> flits
    link_flits = defaultdict(int)
    for (ctrl, buf), cnt in bufs.items():
        # direction: prefer the authoritative port wiring from config.ini; fall
        # back to the SLICC name rule (From*=OUT, To*=IN) only when unavailable.
        direction = buf_dir.get((ctrl, buf))
        if direction is None:
            m = linkrx.match(buf)
            if not m:
                continue                   # mandatoryQueue / *Memory / ... skip
            direction = "OUT" if m.group(2) == "From" else "IN"
        else:
            # still require it to be a network-facing Local buffer
            if not linkrx.match(buf):
                continue
        flits = cnt * (fpp_data if "response" in buf else fpp_ctrl)
        link_flits[(ctrl, direction)] += flits
    ext_map = cfg["ext_map"]
    if ext_map and bufs:
        # group controllers by (kind, direction) and place on the router grid
        for kind, disp in (("l1_cntrl", "L1"), ("dir_cntrl", "Dir"),
                           ("dma_cntrl", "DMA")):
            for direction in ("OUT", "IN"):
                g = {}
                seen = False
                for r in range(a_rows):
                    for c in range(a_cols):
                        rid = r * cols + c
                        fl = 0
                        for ctrl, rn in ext_map.items():
                            if rn == rid and ctrl.startswith(kind):
                                fl += link_flits.get((ctrl, direction), 0)
                                seen = True
                        g[(r, c)] = 100.0 * fl / cycles
                if not seen:
                    # no such controller type in this run (e.g. no DMA) -> skip
                    continue
                verb = ("controller -> router" if direction == "OUT"
                        else "router -> controller")
                print_grid(f"--- Local {direction} PER LINK ({disp}: {verb}) = "
                           f"that controller's own Local port, % of 1 flit/cycle "
                           f"(0.00% = this direction carries no traffic) ---",
                           g, a_rows, a_cols)
        # cross-check the per-link totals against the measured ext-link totals
        lk_out = sum(v for (c, d), v in link_flits.items() if d == "OUT")
        lk_in = sum(v for (c, d), v in link_flits.items() if d == "IN")
        print(f"  per-link totals: OUT {lk_out} flits vs ext_out_link_utilization "
              f"{totals.get('ext_out_link_utilization')} | IN {lk_in} flits vs "
              f"ext_in_link_utilization {totals.get('ext_in_link_utilization')}\n")
    else:
        print("  (per-link view skipped: config.ini has no ext_links map or no "
              "per-controller buffer stats)\n")

    # --- busiest routers (outports that EACH actually carry flits) ----------------
    # Capacity denominator = #mesh_dirs + active_local, i.e. only the outports
    # this traffic model can ever load.
    #   Garnet_standalone: the extra local port IS a physical outport (the router
    #     has 6) but is never handed a flit (only the L1 controller injects), so
    #     counting it would overstate the loadable capacity -> active_local=1.
    #   MI_example: BOTH local ports are loaded (L1 and Dir both send), so both
    #     belong in the denominator -> active_local = local_ports = 2.
    if args.active_local_links is not None:
        cap_extra = (f"forced by --active-local-links={active_local}; "
                     f"{active_local} Local port(s) counted)")
    elif active_local == 1:
        cap_extra = ("the other Local port never carries a flit here - only the "
                     "L1 injects)")
    else:
        cap_extra = (f"all {active_local} Local ports carry flits here - both the "
                     f"L1 and the Dir send)")
    cap_note = (f"#mesh_dirs + {active_local} local "
                f"(corner {2 + active_local} / edge {3 + active_local} / "
                f"interior {4 + active_local} flit/cycle; {cap_extra}")
    print(f"Router total outbound (N+S+E+W+local-out), capacity = {cap_note}:")
    rsum, rcap = {}, {}
    for r in range(a_rows):
        for c in range(a_cols):
            tot = lo_fpc[(r, c)]
            for d in "NESW":
                v = grids[d][(r, c)]
                if v is not None:
                    tot += v / 100.0           # grid is %, convert to flits/cycle
            rsum[(r, c)] = tot
            rcap[(r, c)] = sum(1 for d in "NESW" if exist[d](r, c)) + active_local
    top_r = sorted(rsum.items(), key=lambda kv: -kv[1])[:5]
    for (r, c), v in top_r:
        print(f"  router ({r},{c}) id {r * cols + c}: {v:.3f} flits/cycle "
              f"({100 * v / rcap[(r, c)]:.1f}% of {rcap[(r, c)]} outports)")
    avgr = sum(rsum.values()) / len(rsum)
    avgcap = sum(rcap.values()) / len(rcap)
    print(f"  average router: {avgr:.3f} flits/cycle "
          f"({100 * avgr / avgcap:.1f}% of {avgcap:.2f} outports avg)\n")

    # --- per-router contention (idle vs backpressured) -----------------------
    if rstat:
        contention_grid(rstat, a_rows, a_cols, cols, cycles, cap_note)
        # --- per-router flit residence latency (enter -> leave) -------------
        if any("flit_router_latency_count" in rs for rs in rstat.values()):
            l_max, l_med, l_p75 = router_latency_grids(
                rstat, a_rows, a_cols, cols)
            print_value_grid(
                "Router flit residence latency MEDIAN (p50) per router "
                "(enter -> leave, +1 crossbar exit; network cycles):",
                l_med, a_rows, a_cols, fmt="{:7.2f}", unit=" cyc")
            print_value_grid(
                "Router flit residence latency P75 (75% percentile) per "
                "router (network cycles):",
                l_p75, a_rows, a_cols, fmt="{:7.2f}", unit=" cyc")
            print_value_grid(
                "Router flit residence latency MAX per router "
                "(network cycles):",
                l_max, a_rows, a_cols, fmt="{:7.2f}", unit=" cyc")
            n_flit = sum(rs.get("flit_router_latency_count", 0)
                         for rs in rstat.values())
            print(f"  ({n_flit} router-enter->leave events over all routers, "
                  f"hop-weighted; flits still in flight at run end are not "
                  f"counted. median=p50 flit residence, p75 = the value the "
                  f"slowest 25% of flits exceed (congestion tail); median~1 "
                  f"with high p75/max = queuing concentrated in a few "
                  f"flits.)\n")
        else:
            print("  (per-router flit latency matrices skipped: stats.txt has "
                  "no routers*.flit_router_latency_* stats; rebuild gem5 with "
                  "the instrumented Router/CrossbarSwitch/InputUnit)\n")
        print("  NOTE: Local OUT grid above = injection (NI->router, source);")
        print("        Local IN grid = delivery (router->NI, sink). Both are")
        print("        per ROUTER.")
        if active_local == 1:
            print("        (Garnet_standalone) the two directions use two DIFFERENT")
            print("        ext links: OUT = the L1's, IN = the Dir's.")
        else:
            print(f"        (MI_example) each direction spreads over {active_local} "
                  f"Local links")
            print("        (the L1's and the Dir's); the grid is their average duty.")
        print("        Low OUT + normal IN at a router = injection-starved net")
        print("        sink (its traffic can't clear the congested core), NOT a")
        print("        backpressured output. High contention ratio = the router")
        print("        where output contention/backpressure actually lives.\n")

    # --- busiest links -------------------------------------------------------
    print(f"--- 10 busiest internal links ({cycles} cycles, capacity {cycles} flits) ---")
    top = sorted(lf.items(), key=lambda kv: -kv[1])[:10]
    # keys: E/W = (row, dep_col); N/S = (col, dep_row)
    dsrc = {"E": lambda a, b: (a, b), "W": lambda a, b: (a, b),
            "N": lambda a, b: (b, a), "S": lambda a, b: (b, a)}
    ddst = {"E": lambda a, b: (a, b + 1), "W": lambda a, b: (a, b - 1),
            "N": lambda a, b: (b + 1, a), "S": lambda a, b: (b - 1, a)}
    for (dirn, a, b), fl in top:
        src, dst = dsrc[dirn](a, b), ddst[dirn](a, b)
        print(f"  {dirn}  {src} -> {dst}: {fl:8d} flits = {100.0 * fl / cycles:6.2f}% "
              f"({fl / cycles:6.3f} flits/cycle)")

    # --- flit whole-lifetime latency (network-wide, GarnetNetwork) ----------
    # Source: NetworkInterface::incrementStats records, at the DEST NI, the
    # full life of each delivered flit: source-NI message enqueue ->
    # dest-NI dequeue + drain of the dest-NI out queue (ticks).
    # average_flit_latency = network part (links + routers) + queueing part
    # (src-NI injection queue + dest-NI out queue). Converted here to
    # NETWORK cycles (divide by the ruby clock period). flit_lifetime_
    # {median,p75,max} come from the per-flit lifetime samples kept by the
    # instrumented NI (rebuild gem5 with them for the distribution).
    if "average_flit_latency" in totals and net_period:
        def _cyc(t):
            return t / net_period

        fl_tot = totals["average_flit_latency"]
        fl_net = totals.get("average_flit_network_latency")
        fl_que = totals.get("average_flit_queueing_latency")
        print(f"\n--- flit latency (src NI enqueue -> dest NI dequeue + "
              f"drain; network-wide over delivered flits) ---")
        print(f"  [whole lifetime = network + queueing]")
        print(f"  flit lifetime MEAN    : {_cyc(fl_tot):8.2f} cyc "
              f"({fl_tot:.0f} ticks)")
        lt_med = totals.get("flit_lifetime_median")
        lt_p75 = totals.get("flit_lifetime_p75")
        lt_max = totals.get("flit_lifetime_max")
        if lt_med is not None:
            print(f"  flit lifetime MEDIAN  : {_cyc(lt_med):8.2f} cyc")
        if lt_p75 is not None:
            print(f"  flit lifetime P75     : {_cyc(lt_p75):8.2f} cyc")
        if lt_max is not None:
            print(f"  flit lifetime MAX     : {_cyc(lt_max):8.2f} cyc")
        print(f"  [network part only = links + router pipelines]")
        if fl_net is not None:
            print(f"  network part MEAN     : {_cyc(fl_net):8.2f} cyc")
        nt_med = totals.get("flit_network_latency_median")
        nt_p75 = totals.get("flit_network_latency_p75")
        nt_max = totals.get("flit_network_latency_max")
        if nt_med is not None:
            print(f"  network part MEDIAN   : {_cyc(nt_med):8.2f} cyc")
        if nt_p75 is not None:
            print(f"  network part P75      : {_cyc(nt_p75):8.2f} cyc")
        if nt_max is not None:
            print(f"  network part MAX      : {_cyc(nt_max):8.2f} cyc")
        print(f"  [queueing part = src-NI injection queue + dest-NI out queue]")
        if fl_que is not None:
            print(f"  queueing part MEAN    : {_cyc(fl_que):8.2f} cyc")
        # NOTE: garnet's average_flit_vnet_latency is m_flit_network_latency
        # / m_flits_received per vnet, i.e. the NETWORK part per vnet (the
        # stat name is misleading); average_flit_vqueue_latency is the
        # queueing part. Per-vnet lifetime = network + queueing. Idle
        # vnets have no samples (nan) and print as '-'.
        vnet_net = totals.get("average_flit_vnet_latency")
        vnet_que = totals.get("average_flit_vqueue_latency")
        if isinstance(vnet_net, list) and isinstance(vnet_que, list):
            def _fmt(vals):
                return " | ".join(
                    "-" if v is None else f"vnet{i} {_cyc(v):.2f}"
                    for i, v in enumerate(vals)) + " cyc"
            vnet_lt = [None if (n is None or q is None) else n + q
                       for n, q in zip(vnet_net, vnet_que)]
            print("  per-vnet lifetime     : " + _fmt(vnet_lt))
            print("  per-vnet network part : " + _fmt(vnet_net))
            print("  per-vnet queueing part: " + _fmt(vnet_que))
        n_recv = totals.get("flits_received::total")
        if n_recv:
            dist = ("; lifetime median/p75/max over those "
                    f"{n_recv} delivered-flit samples"
                    if lt_med is not None else
                    "; no lifetime distribution in this stats.txt (rebuild "
                    "gem5 with the instrumented NetworkInterface)")
            print(f"  ({n_recv} flits delivered over all vnets; flits still in "
                  f"flight at run end are NOT in these averages{dist}; the "
                  f"per-router residence matrices above cover only the "
                  f"router-internal part of the lifetime.)")


if __name__ == "__main__":
    main()
