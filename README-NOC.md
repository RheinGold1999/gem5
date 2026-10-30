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

### 1.2 Virtual networks (4 vnets)

| vnet | purpose | messages |
|---|---|---|
| vnet0 | request, internal-die | GETX (1 flit), PUTX (data) |
| vnet1 | request, cross-die | path in place (buffers wired, idle until the address map routes cross-die traffic) |
| vnet2 | response, internal-die | DATA (data), WB_ACK (1 flit) |
| vnet3 | response, cross-die | path in place (buffers wired, idle) |

Mesh traffic currently always uses vnet0/vnet2 (internal die). The
cross-die path (vnet1/vnet3) is fully wired on both sides — L1 has
`requestFromCacheXD`/`responseToCacheXD`, the DRAM-controller agent has
`requestToDirXD`/`responseFromDirXD` and a per-TBE `CrossDie` flag that
steers the response vnet — it only needs the address-map decision to be
activated.

### 1.3 Flit sizing

* Link width is **1024 bits** (128 B) by default → 1 flit per 128 B.
* Control messages (GETX, WB_ACK) are 8 B → **1 flit**.
* Data messages (DATA, PUTX) are sized by payload →
  **1 flit** for the default 128 B payload (`--data-msg-size=128`, with
  the Ruby line size raised to match: `--cacheline_size=128`).
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
| `--addr-layout` | `mpu52` | **MPU-Die 52-bit node addressing** (see §3.6): node field [36:32] = `(col<<2)\|row`, per-node 4 GiB windows, range-based dir routing, `--lines-per-dest` = 2^25, zero-backing `SimpleMemory(null=True)`. Use `--addr-layout=legacy` for the old `[line\|dest\|offset]`+XOR layout (512 MiB interleaved space, `--lines-per-dest=1024`, `--xor-low-bit=22`) |
| `--xor-low-bit` | 22 (legacy only) | XOR-hash low bit; forced to 0 in mpu52 (range-based routing needs no hashing). Required for 32 dirs + `--lines-per-dest=1024` at 128 B lines in legacy |
| `--data-msg-size` / `--cacheline_size` | 128 / 128 | data payload = one full flit; the Ruby line size is raised together so `data_msg_size ≤ cacheline_size` holds |
| `--mem-type` | `SimpleMemory` | **ideal DRAM**: fixed 1-cycle latency, unlimited bandwidth (`configs/ruby/Ruby.py` sets this for `AIGCIC_NOC` builds), so the NoC is the only bottleneck. Override with e.g. `--mem-type=DDR4_1600_8x8` for a real memory system |

Ruby-sequencer side (set in `configs/ruby/AIGCIC_NOC.py`, not CLI):

| Parameter | Value | Meaning |
|---|---|---|
| `max_outstanding_requests` | 256 | per-L1 sequencer outstanding cap (gem5 default 16). NOTE: this is **not** the throughput bottleneck — with ≥2 VCs the wall is memory bandwidth; larger values only deepen the NI queues (higher latency, same throughput) |
| `number_of_TBEs` (L1) | 512 | one TBE per in-flight miss; must be ≥ `max_outstanding_requests` |
| `number_of_TBEs` (dir) | 16384 | the DRAM-controller agent aggregates requests from all L1s |

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

### 3.5 SystemC/TLM demo (external IP instead of synthetic traffic)

A minimal gem5-systemc demo replaces the synthetic testers with a
SystemC master IP talking TLM through the in-tree bridge:

```
AigcicScMaster (SC_MODULE, TLM initiator)
  -> TlmToGem5Bridge32 -> RubySequencer -> AIGCIC_NOC -> Garnet -> dir -> SimpleMemory
```

Build (adds the IP on top of the normal build; one-time +~1 min):

```bash
scons build/NULL_AIGCIC_NOC/gem5.opt EXTRAS=extras/aigcic_sc -j <N>
```

Run (4 nodes, 2x2 mesh, 10 reads/node, exits when every response
arrived):

```bash
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_sc/demo \
    configs/example/aigcic_sc_demo.py [--num-cpus=4 --num-dirs=4 \
    --mesh-rows=2 --num-requests=10 --period-ns=100]
```

The master uses the **4-phase AT handshake** (`nb_transport_fw`) — the
blocking `b_transport` path of `TlmToGem5Bridge` is atomic and would
bypass the NoC. Addresses follow the synthetic-traffic layout
(`dest << block_offset`, line 0), so the address-map checks and the
Garnet stats/analyzer (`analyze_mesh_bw.py`) work unchanged. Reference
data: `log/aigcic_sc/demo/` (flit lifetime MEAN ≈ 9.7 cycles at 1 hop,
1 GHz).

### 3.6 MPU-52 address layout (--addr-layout=mpu52)

For the MPU-Die node addressing, an alternative layout encodes the node
in the 52-bit address space instead of the legacy block-interleaved
channels:

```
bit:  51    50    41  40    39   37    36  34   33  32  31          7    0
     | host | gpu_id | glob | mpu_id | node_y | node_x | line_sel | offset |
       =0     =0       =0     =0        3bits   2bits                128B
```

* `node_x` [33:32] = mesh **row** (2 bits), `node_y` [36:34] = mesh
  **column** (3 bits); the node field [36:32] = `(col << 2) | row`.
* Each node owns a contiguous **4 GiB window**
  `[(field << 32), +4GiB)`; directory routing is purely range-based
  (`Network::addressToNodeID` resolves the owner dir from the
  registered ranges — no interleaving, no XOR hashing).
* `--lines-per-dest` defaults to 2^25 (= 4 GiB / 128 B: the traffic
  spans the full per-node space). Line-select bits live inside the
  node's local space right above the block offset.
* The windows are served by `SimpleMemory(null=True)` — the
  PhysMemoryPool skips backing-store allocation for null memories, so
  host RAM does **not** scale with the 32 x 4 GiB space (measured RSS
  ~= legacy runs).
* The tester converts its (router-space) destination to the node field
  internally (`dest_shift` param, default 0 = legacy). `--single-dest-id`
  stays in **router id** space in both layouts.
* **mpu52 is the default layout since the 128 B-data switch**; pass
  `--addr-layout=legacy` to reproduce the section 6 baseline datasets
  (which were measured with the legacy layout).

#### Cross-die traffic (vnet1 / vnet3)

The simulated mesh has its own die identity (`--mpu-id`, `--gpu-id`,
default 0 = bits [39:37] / [50:41]). A request whose address carries a
different `mpu_id` or `gpu_id` is **cross-die traffic**:

* the L1 issues it on **vnet1** (`requestFromCacheXD`) and the response
  comes back on **vnet3** (`responseToCacheXD`); the cache entry records
  `CrossDie` so its later writeback also uses vnet1;
* cross-die requests are sent to the **column-0 node in the source's
  row** (the die-to-die link side), whose DRAM-controller agent proxies
  the remote die's memory;
* the dir answers cross-die requests itself (one
  `to_memory_controller_latency`), never touching this die's memory
  port, and the L1 routes with the die fields replaced by the local
  identity (`localDieAddr()`), so the address map stays contiguous.

Enable traffic with `--cross-die-ratio=<0..1>` (requires mpu52):

```bash
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/mpu52_crossdie/xd0.2 \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=uniform_random --injectionrate=0.99 --sim-cycles=200000000 \
    --vcs-per-vnet=2 --cross-die-ratio=0.2
```

Results and analysis: section 6.4.

Reference mpu52 sweep (same protocol as section 6, 200 k cycles):
`log/aigcic_noc/mpu52_mesh_4x6/vc{1..4}/` — throughput 0.198 / 0.430 /
0.515 / 0.530 pkts/cyc/node (VC 1-4), busiest E-link 30% / 65% / 78% /
80%, network-part MEAN 17.9 / 16.0 / 19.0 / 24.1 cycles — matches the
legacy baseline within noise.

### 3.7 Key parameters

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
| `network.flit_network_latency_{median,p75,max}` | NETWORK part only (links + router pipelines, incl. the source-NI VC credit wait), same per-flit sample set — lets pure-NoC latency be separated from NI queueing |

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
8. **Flit latency** — grouped into three blocks (all in network cycles,
   over delivered flits):
   * **whole lifetime** (`= network + queueing`): MEAN / MEDIAN / P75 /
     MAX;
   * **network part only** (links + router pipelines): MEAN / MEDIAN /
     P75 / MAX — the pure-NoC latency, excludes NI queueing;
   * **queueing part** (src-NI injection queue + dest-NI out queue): MEAN.
   Plus the per-vnet lifetime breakdown (idle vnets print `-`).

Interpretation hints are printed inline with each section.

---

## 6. Reference results: default configuration

All runs: ideal DRAM, 4x6 active mesh on the 4x8 fabric, 1024-bit
links, 1-flit packets, `max_outstanding_requests=256`,
`--synthetic=uniform_random --injectionrate=0.99 --sim-cycles=200000000`
(= 200 000 cycles).

* **legacy layout** (now needs `--addr-layout=legacy`; measured before
  it became non-default, with the then-64 B-line default — packet and
  flit counts identical to the current 128 B default: every request and
  response is 1 flit either way):
  `log/aigcic_noc/mesh_4x6/vc{1..4}_ideal/`.
* **mpu52 layout** (the current default; measured after the switch):
  `log/aigcic_noc/mpu52_mesh_4x6/vc{1..4}/` — see section 3.6 for the
  layout. The two baselines agree within noise.

### 6.1 Throughput

| | VC=1 | VC=2 | VC=3 | VC=4 |
|---|---|---|---|---|
| delivered packets | 925 293 | 1 983 493 | 2 417 265 | 2 501 137 |
| effective injection (pkts/cyc/node) | 0.193 | 0.413 | 0.504 | **0.521** |
| Local-inject duty | 19.3% | 41.3% | 50.4% | 52.1% |
| busiest E-link | 29.2% | 62.2% | 76.1% | **79.1%** |

### 6.2 Latency (network cycles)

| | VC=1 | VC=2 | VC=3 | VC=4 |
|---|---|---|---|---|
| lifetime MEAN | 756 | 336 | 255 | 240 |
| lifetime MEDIAN | 544 | 240 | 165 | 145 |
| **network MEAN** | 19.3 | **17.2** | 19.9 | 24.8 |
| **network MEDIAN** | 15 | **14** | 15 | 18 |
| network P75 | 24 | 21 | 26 | 33 |
| queueing MEAN (NI buffers) | 737 | 319 | 235 | 215 |

### 6.3 Conclusions

1. **Pure-NoC latency is almost VC-independent**: the network-part
   MEDIAN stays at 14–18 cycles from 29% up to 79% peak link load; only
   a mild congestion tail appears at VC=4 (P75 33 cycles).
2. **All system-level latency differences live in the NI queues**: the
   whole-lifetime MEAN (756 → 240) tracks the queueing part (737 → 215),
   not the network part. At offered 0.99 ≫ capacity (~0.52/node), excess
   requests pile up in the source-NI injection queue — that is memory/
   flow-control backpressure, not NoC congestion.
3. **Throughput saturates around VC=3–4 at ~0.52 pkts/cyc/node** with
   the busiest link at 79%: VC=3→4 buys only +3.4% throughput and starts
   trading NoC latency (network MEAN 19.9 → 24.8, P75 26 → 33) for it.
4. **With a real DRAM** (`--mem-type=DDR4_1600_8x8`), the wall moves to
   the DRAM data bus (~81% read+write with the 1-read + 1-writeback mix)
   and VC=2 onward shows the same ~0.34 pkts/cyc/node regardless of VCs
   or outstanding count (`vc{1..4}` / `vc*_o{16,256,512}` datasets).
5. Practical pick for this workload: **VC=2** for latency studies,
   **VC=4** when maximum throughput matters; measure pure-NoC latency
   with the network-part stats (or lower `--injectionrate` below ~0.3 so
   the NI queues stay empty).

### 6.4 Cross-die (vnet1/vnet3) sweep

Uses `--cross-die-ratio` (section 3.6) and therefore the mpu52 layout;
VC=2, inj=0.99, 200 000 cycles, ratio = 0 / 0.1 / 0.2 / 0.3 / 0.5.
Data in `log/aigcic_noc/mpu52_crossdie/xd{0,0.1,0.2,0.3,0.5}/`.

Every cross-die request enters vnet1 and every cross-die response comes
back on vnet3, so the injected share is exactly `cross-die-ratio / 2`
per vnet (measured: 9.99% / 9.99% at `--cross-die-ratio=0.2`).

| cross-die share | 0 | 10% | 20% | 30% | 50% |
|---|---|---|---|---|---|
| packets/cyc/node | 0.430 | 0.452 | **0.475** | 0.410 | 0.262 |
| flit lifetime MEAN (cyc) | 241 | 216 | 194 | 210 | 348 |
| network part MEAN (cyc) | 16.0 | 16.1 | 17.2 | 14.6 | 12.1 |
| busiest link | 65% (E) | 68% (E/W) | **72% (E/W, cols 2-3)** | 63% (E/W) | 44% (E/W, col 1) |
| avg hops | 3.18 | 3.10 | 3.02 | 2.95 | 2.81 |
| vnet1 lifetime (cyc) | - | 23 | 75 | 132 | 183 |
| vnet3 lifetime (cyc) | - | 14 | 41 | **816** | **1116** |

Cross-die requests land on the column-0 dir of their own row (routers
0/8/16/24; the per-dir totals stay balanced at ~95k each at 20%) and
never touch the local memory port: `requestToMemory == requestToDir`
on those dirs.

Conclusions:

1. **Throughput peaks at ~20% cross-die (0.475 pkts/cyc/node, +10.5%
   over the 0% baseline)**: a cross-die request travels to column 0 of
   its own row instead of a random column, so the average path shortens
   (hops 3.18 → 3.02 at 20%, monotonically down to 2.81 at 50%) and the
   mesh carries less flit-distance for the same packet count.
2. **Beyond ~25% the funnel saturates — and the wall is the row
   corridors, not the column-0 spine**: both directions of every
   cross-die exchange (request westbound, response eastbound) traverse
   the same row links between column 0 and the source column, so the
   busiest links become the E/W corridors around columns 1-3 (72% at
   20%), while the column-0 vertical links stay at background level
   (N/S max 40%).
3. **At high share the response vnet (vnet3) is what collapses**: at
   50% share vnet3 lifetime is 1116 cycles (1108 of them NI queueing)
   while vnet1 stays at 183. The serialization point is the funnel
   dir's single response injection port, which must emit the whole
   row's cross-die responses (plus its own local ones); requests inject
   from six L1s per row and queue far less (vnet1 queueing 165 vs
   vnet3's 1108). Cross-die cost shows up as *response backpressure* at
   the remote-die proxy.
4. **Pure-NoC latency stays in the usual band**: network part MEAN 16 →
   12 cycles, MEDIAN 13 → 10 across the sweep (the completed-packet mix
   shifts toward short local flows at saturation). The whole-system
   degradation is NI/funnel queueing, not link latency.
5. Practical guidance: with one D2D port per die row, keep the
   cross-die share below ~20-25%; beyond that the row corridors into
   column 0 and the proxy's response port saturate. The sweep directly
   measures that bottleneck (the D2D link itself is still ideal — see
   the modelling note in section 3.6).

History note: the first version of the sweep (kept in
`xd*_dstrow/`) ran with the tester accidentally aiming cross-die
requests at the column-0 node of the *destination's* row, which added
a vertical detour along column 0 and made the column-0 N links the
hotspot (peak 0.456 at 20%, vnet1 exploding to 1419 cycles at 50%).
The tester was fixed to target the source's row (the documented
behaviour); the numbers above are from the fixed runs.
