# AIGCIC NoC (Garnet) Simulation & Performance-Analysis Guide

This repository is a gem5 tree instrumented for NoC (Network-on-Chip)
studies with Garnet. It ships one build configuration, the custom
`AIGCIC_NOC` Ruby protocol and a Python analysis tool that turns
`stats.txt` files into per-router bandwidth / latency matrices.

```
build/NULL_AIGCIC_NOC/gem5.opt              NoC + AIGCIC_NOC protocol build
src/mem/ruby/protocol/AIGCIC_NOC*.sm        protocol state machines (SLICC)
configs/example/aigcic_noc_synth_traffic.py synthetic-traffic driver
perf_analysis/analyze_mesh_bw.py            stats.txt -> bandwidth/latency report
log/                                        simulation output tree (stats.txt per run)
```

---

## 1. Architecture & protocol

### 1.1 Target architecture

The simulated system models a **GPU-style NoC without cache coherence**:

```
L1 (Sequencer + CacheMemory)  ---NoC (Garnet)---  DRAM Controller (agent) --- DRAM
```

* **L1 controllers** (`machine(L1Cache)`): one per mesh router. Loads,
  ifetches and stores that miss go out as GETX; dirty lines are written
  back as PUTX on replacement. No invalidations are ever received.
* **DRAM-controller agents** (`machine(Directory)`, kept for Ruby-core
  MachineType/address-mapping plumbing — semantically it *is* the DRAM
  controller): one per mesh router that owns a DRAM channel. Stateless
  request/response gateway to DRAM:
  * `GETX -> MEMORY_READ  -> DRAM -> DATA to requestor`
  * `PUTX -> MEMORY_WB    -> DRAM -> WB_ACK to requestor`
* There is **no directory coherence, no invalidations, no dirty-data
  forwarding, no DMA**. The protocol only exists to put realistic
  request/response traffic on the NoC.

### 1.2 Virtual networks (4 vnets, 1 VC each)

| vnet | purpose | messages |
|---|---|---|
| vnet0 | request, internal-die | GETX (1 flit), PUTX (data) |
| vnet1 | request, cross-die | reserved (no buffers attached yet) |
| vnet2 | response, internal-die | DATA (data), WB_ACK (1 flit) |
| vnet3 | response, cross-die | reserved (no buffers attached yet) |

Mesh traffic currently always uses vnet0/vnet2 (internal die). Cross-die
vnets are placeholders for future die-to-die traffic.

### 1.3 Flit sizing

* Link width is **1024 bits** (128 B) by default → 1 flit per 128 B.
* Control messages (GETX, WB_ACK) are 8 B → **1 flit**.
* Data messages (DATA, PUTX) are sized by payload →
  **1 flit** for the default 64 B payload.
* Burst: payloads larger than one flit occupy
  `ceil(data_length / 1024 bits)` flits. The message header is **not**
  counted (see `Network::Network` in `src/mem/ruby/network/Network.cc`).

Burst runs must raise the Ruby line size together with the payload, e.g.

```bash
--cacheline_size=1024 --data-msg-size=1024
```

(data_msg_size ≤ cacheline_size is enforced by the Ruby core).

---

## 2. Building

```bash
scons build/NULL_AIGCIC_NOC/gem5.opt -j <N>
```

`<N>` = number of parallel jobs (e.g. 64). A debug binary is available by
replacing `.opt` with `.debug`.

`build_opts/NULL_AIGCIC_NOC` contains:

```
RUBY=y
PROTOCOL="AIGCIC_NOC"
RUBY_PROTOCOL_AIGCIC_NOC=y
```

The `AIGCIC_NOC` protocol files live in `src/mem/ruby/protocol/`
(`AIGCIC_NOC.slicc`, `-msg.sm`, `-cache.sm`, `-dir.sm`) and the Python
side in `configs/ruby/AIGCIC_NOC.py`.

---

## 3. Running a simulation

All runs use `configs/example/aigcic_noc_synth_traffic.py` (a dedicated
derivative of `garnet_synth_traffic.py`). It **refuses to run on any
binary other than a `NULL_AIGCIC_NOC` build**.

### 3.1 Defaults (AIGCIC_NOC build)

The driver sets AIGCIC_NOC-specific defaults so a bare invocation gives
a **logical 4x6 mesh on a 4x8 fabric**:

| Parameter | Default | Meaning |
|---|---|---|
| `--network` | `garnet` | Garnet NoC |
| `--topology` | `Mesh_XY` | XY-routed mesh |
| `--num-cpus` / `--num-dirs` | 32 / 32 | 32 L1s + 32 DRAM controllers (fabric must stay a power of two for the address encoding) |
| `--mesh-rows` | 4 | 4x8 physical fabric |
| `--num-active-columns` / `--num-active-rows` | 6 / 4 | traffic masked to the top-left 4x6 sub-mesh |
| `--link-width-bits` | 1024 | 128 B flits |
| `--vcs-per-vnet` | 1 | one VC per vnet |
| `--inj-vnet` | 0 | inject on vnet0 (request internal-die); only vnet0 has receiving agents |
| `--xor-low-bit` | 22 | XOR-hash low bit (required for 32 dirs + default `--lines-per-dest=1024`) |

### 3.2 Bare invocation (defaults apply)

```bash
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/mesh_4x6/uniform \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=uniform_random --injectionrate=0.99 \
    --sim-cycles=50000000
```

> **Note on `--sim-cycles`**: the tester compares it against the raw
> tick counter (1 GHz clock → 1 tick = 1 ps), i.e. `--sim-cycles=50000000`
> simulates **50 000 clock cycles**, not 50 million.

### 3.3 Burst-traffic example

```bash
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/mesh_4x6/burst_1k \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=uniform_random --injectionrate=0.99 \
    --sim-cycles=50000000 \
    --cacheline_size=1024 --data-msg-size=1024 --xor-low-bit=0
```

DATA responses then occupy `ceil(1024 B / 128 B) = 8` flits. Note that
raising the line size changes the address encoding; with 1024 B lines the
default `--xor-low-bit` no longer fits, hence `--xor-low-bit=0` (no XOR
hashing) in this example.

### 3.4 Full-fabric (no masking) example

```bash
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/mesh_4x8/uniform \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=uniform_random --injectionrate=0.99 \
    --sim-cycles=50000000 \
    --num-active-columns=0 --num-active-rows=0
```

### 3.5 Key parameters

| Parameter | Meaning |
|---|---|
| `--num-cpus` / `--num-dirs` | number of L1s / DRAM-controller agents (= mesh routers each); mesh is `num_cpus` routers, `--mesh-rows` rows |
| `--mesh-rows` | rows of the mesh (columns derived) |
| `--synthetic` | traffic pattern: `uniform_random` (the only pattern compatible with active-column masking), `tornado`, `bit_complement`, `bit_reverse`, `bit_rotation`, `neighbor`, `shuffle`, `transpose` |
| `--injectionrate` | packets/cycle/node (0..1) |
| `--sim-cycles` | simulation length in **ticks** (see note above) |
| `--data-msg-size` (+ `--cacheline_size`) | data payload bytes; flits = `ceil(payload / flit_size)` |
| `--link-width-bits` | flit size in bits (default 1024) |
| `--num-active-columns` / `--num-active-rows` | restrict traffic to the top-left `rows x cols` sub-mesh; 0 = full fabric |
| `--inj-vnet` | 0 = request internal-die (default), 2 = response internal-die; 1/3 cross-die reserved |
| `--switch-arbiter` | `round_robin`, `router_age_based`, `global_age_based` |
| `--lines-per-dest`, `--xor-low-bit`, `--l1d_size`, `--l1d_assoc` | address-map knobs: spread traffic over many cache lines; the driver validates the address encoding against the XOR-hash bits |

Output layout used in this repo:

```
log/aigcic_noc/mesh_4x6/<scenario>/stats.txt
```

`garnet_synth_traffic.py` still exists unmodified for the legacy
`Garnet_standalone` / `X86_MI_example` builds, but the `AIGCIC_NOC`
protocol only works through `aigcic_noc_synth_traffic.py`.

---

## 4. Instrumented statistics (this fork)

Extra instrumentation added on top of stock gem5:

| Stats (per router / network) | Meaning |
|---|---|
| `routers<i>.flit_router_latency_{count,max,median,p75}` | flit residence inside router *i*: from being consumed off an input link (incl. Local injection) to crossing the crossbar (incl. ejection), counted as leave − enter + 1; a 1-cycle pass-through = 1 |
| `network.flit_lifetime` / `flit_lifetime_{median,p75,max}` | whole flit lifetime: source-NI message enqueue → dest-NI dequeue + dest-NI out-queue drain (per-vnet sum + p50/p75/max over delivered-flit samples) |

If you add a *new* build config, re-apply from
`src/mem/ruby/network/garnet/` (Router, CrossbarSwitch, InputUnit, flit,
GarnetNetwork, NetworkInterface).

---

## 5. Performance analysis with `perf_analysis/analyze_mesh_bw.py`

### 5.1 Single run (report to stdout)

```bash
python3 perf_analysis/analyze_mesh_bw.py log/aigcic_noc/mesh_4x6/uniform/stats.txt
```

### 5.2 Batch mode (directory scan)

```bash
python3 perf_analysis/analyze_mesh_bw.py ./log
```

Recursively scans the directory; every `stats.txt` found is analyzed and
the report is written to `perf.txt` **next to the stats.txt**:

```
[2/3] log/aigcic_noc/mesh_4x6/uniform/stats.txt
    -> log/aigcic_noc/mesh_4x6/uniform/perf.txt
batch done: 3 ok, 0 failed
```

A failing stats.txt only fails its own report (no half-written
`perf.txt` is left behind). All CLI options below also apply in batch
mode.

### 5.3 Options

| Option | Meaning |
|---|---|
| `--cycles N` | override the network-cycle count (auto: `pwrStateResidencyTicks / ruby-clock-period` from config.ini) |
| `--mesh-rows R` | override mesh rows (auto from config.ini) |
| `--flits-per-data-pkt` / `--flits-per-ctrl-pkt` | override flit-per-packet sizing (auto from config.ini message sizes) |
| `--nodes-per-router`, `--active-local-links` | override controller/port accounting |
| `--active-rows`, `--active-columns` | restrict the displayed/aggregated sub-mesh (auto from the run's `--num-active-*`) |

### 5.4 What the report contains (in order)

1. **Run header** — mesh size, network cycles, capacity model, traffic
   summary (pkts/flits, cross-checked against `packets_injected` /
   `flits_injected`).
2. **Internal-link bandwidth matrices** — per-direction (N/E/S/W)
   `rows x cols` utilization matrices (% of 1 flit/cycle), reconstructed
   hop-by-hop from the traffic matrix under XY routing.
3. **Local-port views** — per-router Local OUT/IN duty plus a per-link
   breakdown per controller type (L1 / DRAM controller).
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
