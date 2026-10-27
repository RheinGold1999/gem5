# NoC (Garnet) Simulation & Performance-Analysis Guide

This repository is a gem5 tree instrumented for NoC (Network-on-Chip)
studies with Garnet. It ships two build configurations and a Python
analysis tool that turns `stats.txt` files into per-router bandwidth /
latency matrices.

```
build/Garnet_standalone/gem5.opt   pure NoC exploration build
build/X86_MI_example/gem5.opt      NoC + real coherence-protocol build
perf_analysis/analyze_mesh_bw.py   stats.txt -> bandwidth/latency report
log/                               simulation output tree (stats.txt per run)
```

---

## 1. The two build configurations

Both builds use Ruby + Garnet (`RUBY=y`) and are driven by
`configs/example/garnet_synth_traffic.py` (synthetic traffic testers).
They differ in the Ruby protocol, i.e. in *what kind of traffic* flows
through the network.

### 1.1 `Garnet_standalone` — pure network exploration

```
build_opts/Garnet_standalone:   RUBY=y  PROTOCOL="Garnet_standalone"
```

* **Scenario**: studying the network itself (routing, arbitration,
  link/router bandwidth, router pipeline latency) with fully controlled
  synthetic traffic, without any coherence-protocol noise.
* **Traffic model**: only the L1-side testers (`GarnetSyntheticTraffic`)
  inject; the directory controllers *drop everything they receive and
  never answer*. Destinations are directories only. Every packet is a
  one-way injection.
* **Consequences for the analysis**:
  * of the 2 Local (external) ports per router, exactly ONE per direction
    carries traffic: ext-OUT = the L1's link (injection), ext-IN = the
    Dir's link (delivery);
  * router outbound capacity = `#mesh_dirs + 1` (corner 3 / edge 4 /
    interior 5 flit/cycle), not `+2`.
* Typical use: latency/bandwidth sweeps over injection rate, traffic
  patterns (uniform, tornado, transpose, ...), arbitration policies.

### 1.2 `X86_MI_example` — network with a real coherence protocol

```
build_opts/X86_MI_example:      RUBY=y  PROTOCOL="MI_example"  (+ X86 ISA)
```

* **Scenario**: NoC studies with *realistic* traffic: the MI_example
  coherence protocol generates GETX/PUTX requests, DATA responses and
  forwarded snoops, so the network carries request/response/control
  traffic in BOTH directions, with destination sets and message sizes
  dictated by the protocol.
* **Traffic model**: both the L1 controllers AND the directory
  controllers send and receive over the network.
* **Consequences for the analysis**:
  * BOTH Local ports per router per direction carry traffic; the
    per-router Local view is the average over both links;
  * router outbound capacity = `#mesh_dirs + 2`;
  * data messages are 1 flit when run with `--data-msg-size 8`
    (see below), so every packet is a single flit.

### 1.3 Instrumented statistics (this fork)

Both builds carry extra instrumentation added on top of stock gem5:

| Stats (per router / network) | Meaning |
|---|---|
| `routers<i>.flit_router_latency_{count,max,median,p75}` | flit residence inside router *i*: from being consumed off an input link (incl. Local injection) to crossing the crossbar (incl. ejection), counted as leave − enter + 1; a 1-cycle pass-through = 1 |
| `network.flit_lifetime` / `flit_lifetime_{median,p75,max}` | whole flit lifetime: source-NI message enqueue → dest-NI dequeue + dest-NI out-queue drain (per-vnet sum + p50/p75/max over delivered-flit samples) |

If you add a *new* build config, re-apply from
`src/mem/ruby/network/garnet/` (Router, CrossbarSwitch, InputUnit, flit,
GarnetNetwork, NetworkInterface).

---

## 2. Building

```bash
# pure NoC exploration build
scons build/Garnet_standalone/gem5.opt -j <N>

# coherence-protocol build (MI_example, X86)
scons build/X86_MI_example/gem5.opt -j <N>
```

`<N>` = number of parallel jobs (e.g. 64). A debug binary is available by
replacing `.opt` with `.debug`.

---

## 3. Running a simulation

All NoC runs use `configs/example/garnet_synth_traffic.py`. Output goes
to the directory given by `-d`/`--outdir` (default `m5out/`), which
receives `stats.txt`, `config.ini` and `command.txt`.

### 3.1 Garnet_standalone example

```bash
./build/Garnet_standalone/gem5.opt -d log/garnet_standalone/mesh_4x6/round_robin \
    configs/example/garnet_synth_traffic.py \
    --network=garnet --topology=Mesh_XY \
    --num-cpus=32 --num-dirs=32 --mesh-rows=4 \
    --synthetic=uniform_random --injectionrate=0.99 \
    --sim-cycles=50000000 \
    --data-msg-size=8 \
    --num-active-columns=6 --num-active-rows=4 \
    --switch-arbiter round_robin
```

### 3.2 X86_MI_example example

```bash
./build/X86_MI_example/gem5.opt -d log/mi_example/mesh_4x6/global_age_based \
    configs/example/garnet_synth_traffic.py \
    --network=garnet --topology=Mesh_XY \
    --num-cpus=32 --num-dirs=32 --mesh-rows=4 \
    --synthetic=uniform_random --injectionrate=0.99 \
    --sim-cycles=50000000 \
    --data-msg-size=8 --lines-per-dest=1024 --l1d_size=1MiB --l1d_assoc=2 \
    --xor-low-bit=22 \
    --num-active-columns=6 --num-active-rows=4 \
    --switch-arbiter global_age_based
```

### 3.3 Key parameters

| Parameter | Meaning |
|---|---|
| `--num-cpus` / `--num-dirs` | number of L1 testers / directories (= mesh routers each); mesh is `num_cpus` routers, `--mesh-rows` rows |
| `--mesh-rows` | rows of the mesh (columns derived) |
| `--synthetic` | traffic pattern: `uniform_random`, `tornado`, `bit_complement`, `bit_reverse`, `bit_rotation`, `neighbor`, `shuffle`, `transpose` |
| `--injectionrate` | packets/cycle/node (0..1) |
| `--sim-cycles` | simulation length in cycles |
| `--data-msg-size` | data payload bytes; `(size+8)/16` flits per data packet — 8 → 1 flit, 64 (default) → 5 flits |
| `--num-active-columns` / `--num-active-rows` | restrict traffic to the top-left `rows x cols` sub-mesh (e.g. a 4x6 mesh on a 4x8 fabric); 0 = full mesh |
| `--switch-arbiter` | switch-arbitration policy: `round_robin`, `router_age_based` (oldest-first by enqueue time at this router), `global_age_based` (oldest-first by source-NI enqueue time) |
| `--lines-per-dest`, `--xor-low-bit`, `--l1d_size`, `--l1d_assoc` | MI_example address-map knobs: spread traffic over many cache lines to avoid same-line conflicts; the config validates that the address encoding stays inside the XOR-hash bits |
| `--inj-vnet`, `--num-packets-max`, `--single-sender-id`, `--single-dest-id` | finer injection control |

Output layout used in this repo:

```
log/
├── garnet_standalone/mesh_4x6/<arbiter>/stats.txt
└── mi_example/mesh_4x6/<arbiter>/stats.txt
```

---

## 4. Performance analysis with `perf_analysis/analyze_mesh_bw.py`

### 4.1 Single run (report to stdout)

```bash
python3 perf_analysis/analyze_mesh_bw.py log/mi_example/mesh_4x6/global_age_based/stats.txt
```

### 4.2 Batch mode (directory scan)

```bash
python3 perf_analysis/analyze_mesh_bw.py ./log
```

Recursively scans the directory; every `stats.txt` found is analyzed and
the report is written to `perf.txt` **next to the stats.txt**:

```
[2/3] log/mi_example/mesh_4x6/global_age_based/stats.txt
    -> log/mi_example/mesh_4x6/global_age_based/perf.txt
batch done: 3 ok, 0 failed
```

A failing stats.txt only fails its own report (no half-written
`perf.txt` is left behind). All CLI options below also apply in batch
mode.

### 4.3 Options

| Option | Meaning |
|---|---|
| `--cycles N` | override the network-cycle count (auto: `pwrStateResidencyTicks / ruby-clock-period` from config.ini) |
| `--mesh-rows R` | override mesh rows (auto from config.ini) |
| `--flits-per-data-pkt` / `--flits-per-ctrl-pkt` | override flit-per-packet sizing (auto from config.ini message sizes) |
| `--nodes-per-router`, `--active-local-links` | override controller/port accounting |
| `--active-rows`, `--active-columns` | restrict the displayed/aggregated sub-mesh (auto from the run's `--num-active-*`) |

### 4.4 What the report contains (in order)

1. **Run header** — mesh size, network cycles, capacity model, traffic
   summary (pkts/flits, cross-checked against `packets_injected` /
   `flits_injected`).
2. **Internal-link bandwidth matrices** — per-direction (N/E/S/W)
   `rows x cols` utilization matrices (% of 1 flit/cycle), reconstructed
   hop-by-hop from the traffic matrix under XY routing.
3. **Local-port views** — per-router Local OUT/IN duty plus a per-link
   breakdown per controller type (L1 / Dir).
4. **Router totals** — top-5 busiest routers, outbound flits/cycle vs.
   outport capacity.
5. **Contention** — per-router input/output arbiter activity ratio
   (>1.1 = output contention) and crossbar throughput.
6. **Per-router flit residence latency matrices** — MEDIAN (p50), P75
   and MAX of the enter→leave latency of every flit through each router
   (network cycles). Hot spots show up as high p75/max with median ≈ 1.
7. **10 busiest internal links**.
8. **Flit whole-lifetime latency** — network-wide averages split into
   network part and queueing part, per-vnet breakdown, plus the
   lifetime MEDIAN / P75 / MAX distribution (all in network cycles).

Interpretation hints are printed inline with each section.