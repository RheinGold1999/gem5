# AGENTS.md — Working notes for AI agents on this repo

Read this before making changes. It records **why** this repo exists, the
**frozen design decisions**, and the **conventions/lessons** that are easy
to get wrong.

## 1. Purpose of this repo

The research subject is the **NoC (Garnet), not the CPU or coherence**.
Everything outside the network is intentionally ideal or minimal:

* The custom Ruby protocol `AIGCIC_NOC` exists **only to put realistic
  request/response traffic on the NoC**. It models a GPU-style system:
  L1 caches + DRAM controllers, **no cache coherence** — no
  invalidations, no dirty-data forwarding, no directory state, no DMA.
* The DRAM side is an **ideal slave** by default (`SimpleMemory`,
  1-cycle latency, unlimited bandwidth, set in
  `configs/ruby/Ruby.py:setup_memory_controllers` gated on
  `PROTOCOL == "AIGCIC_NOC"`) so that **the NoC is the only bottleneck**.
* Do not "fix" the protocol to be coherence-correct, and do not add
  features serving CPU simulation — that is out of scope.

## 2. Frozen design decisions (do not change casually)

* **4 vnets, all active**: vnet0/1 = request internal/cross-die,
  vnet2/3 = response internal/cross-die. Cross-die traffic is decided by
  the address: the mesh has its own die identity (`--mpu-id`/`--gpu-id`,
  default 0), and an address whose `mpu_id` [39:37] or `gpu_id` [50:41]
  differs is cross-die. The L1 puts those requests on vnet1
  (`isCrossDie()` in the .sm), they go to the **column-0 node of the
  source's row** (die-to-die link side), and the dir proxies the remote
  memory and answers on vnet3. Enable traffic with
  `--cross-die-ratio=<0..1>` (mpu52 only; legacy fatal-errors).
* **Deadlock rule**: a vnet may only depend on vnets with index >= its own
  (requests vnet0/1 → responses vnet2/3). Any new message flow must keep
  this ordering.
* **Flit sizing**: links are 1024 bits (128 B). `Network::Network` in
  `src/mem/ruby/network/Network.cc` was modified (`m_data_msg_size =
  payload`, header **not** counted) so burst flits =
  `ceil(data_length / 128 B)`. This is global — other protocols (e.g.
  `X86_MI_example`) get the same semantics if rebuilt.
* **data size default 128 B** with `cacheline_size=128` (they must move
  together: `data_msg_size <= cacheline_size` is fatal-checked).
* **Logical 4x6 mesh on a 4x8 fabric**: `num_dirs` must stay a power of
  two (address-encoding check), so a 4x6 mesh is emulated by masking
  traffic (`--num-active-columns=6 --num-active-rows=4`) on 32 routers.
* **Address layout**: default `--addr-layout=mpu52` (MPU-Die 52-bit
  node addressing, per-node 4GiB windows, range-based dir routing,
  zero-backing memory). The legacy XOR-interleaved layout stays
  available via `--addr-layout=legacy`.
* **Sequencer**: `max_outstanding_requests=256` (gem5 default 16), L1
  `number_of_TBEs=512`, dir `number_of_TBEs=16384`. NOTE: the
  outstanding cap is *not* the throughput bottleneck — raising it only
  deepens NI queues (same throughput, worse latency). With ideal DRAM
  and ≥2 VCs the NoC itself is the wall; with real DRAM it is the DRAM
  bus (~81% R+W).
* The legacy `garnet_synth_traffic.py` stays untouched; AIGCIC_NOC has
  its own driver (see below).

## 3. Key files

| File | Role |
|---|---|
| `src/mem/ruby/protocol/AIGCIC_NOC*.sm`, `.slicc` | protocol state machines (L1Cache + Directory-as-DRAM-gateway; no DMA machine) |
| `configs/ruby/AIGCIC_NOC.py` | controller creation, sequencer/TBE params, 4-vnet wiring |
| `configs/example/aigcic_noc_synth_traffic.py` | the only valid driver for AIGCIC_NOC; sets all defaults; **aborts on any non-`NULL_AIGCIC_NOC` build** |
| `configs/ruby/Ruby.py` | `setup_memory_controllers` (ideal-DRAM hook), `create_directories` (dir TBE size) — shared with legacy protocols, gate AIGCIC changes by protocol |
| `src/mem/ruby/network/garnet/` | instrumented stats (router residence, flit lifetime, network-part quantiles) |
| `src/mem/ruby/network/Network.cc` | flit-sizing rule (see above) |
| `perf_analysis/analyze_mesh_bw.py` | `stats.txt` → per-router bandwidth/latency report |
| `extras/aigcic_sc/` | SystemC/TLM demo master IP (`sc_master.hh/cc`, `AigcicScMaster.py`); build with `EXTRAS=extras/aigcic_sc` |
| `configs/example/aigcic_sc_demo.py` | Pattern-A SystemC demo: SC master → `TlmToGem5Bridge` → RubySequencer → NoC |
| `configs/example/aigcic_noc_synth_traffic.py --addr-layout` | **`mpu52` (default)**: MPU-Die 52-bit layout, per-node 4GiB windows, `SimpleMemory(null=True)` = zero backing, `--lines-per-dest=2^25`. `legacy` = old XOR-interleaved 512MiB space (needed to reproduce `log/aigcic_noc/mesh_4x6/vc*_ideal`); mpu52 reference sweep in `log/aigcic_noc/mpu52_mesh_4x6/` |
| `README-NOC.md` | user-facing guide + §6 reference results (VC sweep baseline) |
| `log/aigcic_noc/mesh_4x6/` | legacy-layout result tree (`vc{1..4}_ideal/` = §6 baseline) |
| `log/aigcic_noc/mpu52_mesh_4x6/`, `mpu52_crossdie/` | current-default (mpu52) sweeps: VC sweep and cross-die-share sweep |

## 4. Commands

```bash
# build (required after any .sm / C++ change; Python config changes need NO rebuild)
scons build/NULL_AIGCIC_NOC/gem5.opt -j <N>

# run (defaults: ideal DRAM, 4x6 active mesh, 128 B data, SimpleMemory)
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/mesh_4x6/<scenario> \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=uniform_random --injectionrate=0.99 --sim-cycles=200000000

# analyze (single or batch; writes perf.txt next to stats.txt)
python3 perf_analysis/analyze_mesh_bw.py log/aigcic_noc/mesh_4x6/<scenario>/stats.txt
```

**`--sim-cycles` is compared against the raw tick counter** (1 GHz →
1 tick = 1 ps): `--sim-cycles=200000000` = 200 000 clock cycles, not 200
million.

## 5. Verification rules (learned the hard way)

* Smoke-test with `aigcic_noc_synth_traffic.py` (synthetic traffic).
  **Never** use `configs/example/ruby_random_test.py` — it verifies data
  correctness and is fundamentally incompatible with the no-coherence
  protocol (panic on stale reads is expected, not a bug).
* SLICC gotcha: a transition can be triggered from several `in_port`s,
  and a pop action is bound to one specific buffer. Sharing one event
  across buffers pops the wrong queue — duplicate events/actions per
  buffer (see the `*_XD` events in `AIGCIC_NOC-cache.sm` / `-dir.sm`).
* TBE tables assert on overflow (`m_map.size() < m_number_of_TBEs`).
  Whenever sequencer outstanding or request concurrency grows, raise the
  matching `number_of_TBEs`.
* After changing latency/bandwidth/queue assumptions, re-check the
  bottleneck location before drawing conclusions — it has moved three
  times already (VC credit loop → sequencer flow control → DRAM bus →
  the NoC itself with ideal DRAM). Use the network-part stats (not the
  NI-queueing-inclusive lifetime) to attribute latency to the NoC.
* The analyzer prints `MEAN` (not "total" — it is an average), splits
  lifetime into network part vs NI-queueing part, and handles idle-vnet
  `nan` entries as `-`.
* SystemC/TLM co-simulation gotchas (see `configs/example/aigcic_sc_demo.py`):
  * The in-tree `TlmToGem5Bridge` only reaches the *timing* NoC through
    the 4-phase AT path (`nb_transport_fw`); the blocking `b_transport`
    goes `sendAtomic` and **bypasses the NoC entirely**.
  * Transactions handed to the bridge must carry a memory manager
    (`tlm_generic_payload(&mm)`) — the bridge `acquire()`/`release()`s
    them and the vendored GP asserts `m_mm != 0`.
  * Never pass a SimObject as a param value while it is still an orphan
    (e.g. `TlmToGem5Bridge(system=system)`): the config machinery
    adopts it as a *child* of the holder, creating a parent cycle and a
    `RecursionError` in `SimObject.path`. Let `Parent.any` resolve it.
  * A custom (non-`ClockedObject`) node must still expose `clk_domain`:
    `AIGCIC_NOC.py` copies `cpus[i].clk_domain` into the L1 sequencer.
    Point it at `system.clk_domain` (a separate orphan domain trips the
    stats-group uniqueness check when sequencers share it).
  * Don't forget `network="garnet"` in `set_defaults` — the generic
    Options default is a simple network, which has no Garnet stats.
* Address-layout gotchas (mpu52):
  * The tester's `active()`/traffic patterns operate in **router id**
    space; the tester converts router→node-field internally
    (`dest_shift`). Never compare a field value against router-space
    masks — a field-space `active()` check silently skews the
    destination geometry (avg hops 3.18 → 3.62, -35% throughput).
  * `RubyDirectoryMemory` allocates an entry table sized by its
    addr_ranges; for AIGCIC_NOC (which never touches it) it is pinned
    to a one-block range in `create_directories`, else a 4GiB window
    costs 32M pointers per dir.
  * `SimpleMemory(null=True)` gets NO backing store from the
    PhysMemoryPool — the zero-RAM way to serve huge simulated windows.
  * The analyzer's flit sizing is `ceil(data_msg_size / flit_size)`
    (header NOT counted — matches the modified Network.cc).
* Cross-die gotchas:
  * `AddrRange::isSubset` **panics on interleaved ranges**, so a dir
    window cannot be an interleaved range covering all die ids — keep
    contiguous 4GiB windows and adjust the *address* instead.
  * `RubyPort` rejects any request address outside `System::isMemAddr`
    as a PIO address. Cross-die addresses (foreign die fields) live
    outside this die's physmem, so the driver declares
    `system.external_memory_ranges` (`System::isMemAddr` also accepts
    those) whenever `--cross-die-ratio > 0`.
  * The dir must NOT forward a cross-die address to its memory port
    (the port's window only covers this die). It answers from the
    request queue instead, which also means no TBE is allocated for
    cross-die accesses (they are stateless on the dir side).
  * Route cross-die requests with `localDieAddr(addr)` (die fields
    replaced by this die's identity), not `addr % 2^37`: the windows
    sit at the local die identity, so masking everything breaks
    `--mpu-id != 0`.
  * The tester must stamp the *local* die identity into ordinary
    addresses, otherwise every request looks cross-die when
    `--mpu-id != 0`.

## 6. Reference baseline (do not lose)

`README-NOC.md` §6 holds the current-default VC sweep (ideal DRAM,
inj=0.99, 200 k cycles): throughput 0.193/0.413/0.504/0.521 pkts/cyc/node
for VC=1/2/3/4, pure-NoC latency median 14–18 cycles across the whole
range, NI queueing explaining all system-latency differences. Any change
to defaults (link width, payload, outstanding, DRAM model, mesh) must be
compared against it, and §6 updated if the baseline moves.

## 7. Planned next steps

* Address map deciding internal- vs cross-die at the request side
  (L1 picks vnet0 vs vnet1; dir TBE `CrossDie` already steers the
  response vnet) — this activates vnet1/vnet3.
* Possibly an exposure of `max_outstanding_requests` as a driver CLI
  option (currently hardcoded 256 in `configs/ruby/AIGCIC_NOC.py`).
