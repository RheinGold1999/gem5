# Trace-Replay Performance Simulation Report (English draft)

> Configuration: baseline = `buffer-size-per-vc=4`,
> `vc-num-per-vnet=2`; max resources = `buffer-size-per-vc=16`,
> `vc-num-per-vnet=16`. Four windows, repeat=2 (attribution) / repeat=1
> (sweeps). Link- and msg-level reconciliation passes on every configured
> run (per-link flit counts match the trace exactly; 8/8 msg checks PASS).

## 1. Trace and Replay Implementation in Brief

**Trace**: all NoC traffic of one MPU during a single decode-layer
execution, recorded hop by hop (router -> adjacent router). Four windows =
{1 or 4 tokens per step} x {layer type a, layer type b}, containing
unicast / multicast trees / in-network reduction trees / allreduce.
Records carry an `after` dependency DAG (cross-message data and sync) and
a per-router `seq` issue order. Sizes: 4210/3425/10205/9420 records,
13280/11677/46381/42019 link flits.

**Replay** (hop-level in-network replay):
- Plan compiler: trace -> per-router relay plan; straight/bend
  classification; channel-dependency-graph check (packets are single-axis
  monotone, deadlock-free without VC classes) and exact links.csv
  reconciliation as mandatory gates;
- Transport: the tester injects write requests (`RELAY_PUTX`, silently
  relayed, no responses) following the after dependencies and the
  **whole-group serial injection discipline** (each router's injection
  engine has at most one message's burst group in flight; bursts within a
  group pipeline, switching groups waits until the current group fully
  lands). On head arrival a router hook rewrites the destination
  in-network for straight continuations (same branch, same packet); bends,
  merges and after-holds eject the stream and the local tester re-injects
  the continuation as a fresh leg; destination copies are virtual
  deliveries (no flits);
- Scheduling: a global TraceScheduler owns the after-dependency state and
  arrival completions, chaining window repetitions;
- Verification: per-link flit counts match links.csv exactly (zero diff on
  all 104 directed links); the 8-check msg-granularity reconciliation
  (completion uniqueness / realization uniqueness / causality / issue
  order / msg completeness / bytes) passes on every run; runtime
  assertions (burst flit count, rec.next validity, ...) zero-triggered.

## 2. MPU NoC Architecture

- **Topology**: a 4-row x 6-column mesh (logical 4x6 on a 4x8 fabric;
  inactive columns carry no traffic), 24 routers; **XY dimension-order
  routing** (row first, then column; enforced by link weights).
- **Nodes**: each router hosts one L1 controller (core side) and one
  directory controller (DRAM gateway), each with its own NI and local
  port; no cache coherence; the directory silently accepts trace traffic
  (ideal-DRAM gateway).
- **Vnets**: four vnets -- vnet0/1 request class (on-die / cross-die),
  vnet2/3 response class; all trace traffic rides **vnet0** (on-die
  requests). All four vnets are typed DATA (4-flit buffers per VC, see
  buffer-size-per-vc in Section 3).
- **Flits and links**: a flit is **128 B**; each link direction carries
  1 flit/cycle; a message's flit count = ceil(bytes / 128) (352 B -> 3
  flits, 512 B -> 4 flits).
- **Buffers and VCs (baseline)**: buffer-size-per-vc = **4 flits**,
  vc-num-per-vnet = **2**.
- **Clock**: 1 GHz (1 cycle = 1000 ticks); all latencies/makespans in this
  report are in cycles.

## 3. buffer-size-per-vc and vc-num-per-vnet Sweeps (baseline: 4 / 2)

Makespan (cycles, repeat=1):

| Configuration | s1-a | s1-b | s4-a | s4-b | vs baseline |
|---|---|---|---|---|---|
| **baseline: buffer-size-per-vc=4, vc-num-per-vnet=2** | 1911 | 1665 | 5571 | 5190 | 1.00 |
| buffer-size-per-vc=1 | 4028 | 3692 | 14293 | 13335 | 2.11-2.57x |
| buffer-size-per-vc=2 | 2454 | 2183 | 8094 | 7477 | 1.28-1.45x |
| buffer-size-per-vc=8 | 1858 | 1625 | 5465 | 5015 | 0.93-0.97x |
| buffer-size-per-vc=16 | 1858 | 1625 | 5455 | 5009 | 0.93-0.97x |
| vc-num-per-vnet=4 | 1822 | 1599 | 5416 | 4963 | 0.95-0.97x |
| vc-num-per-vnet=8 | 1821 | 1598 | 5344 | 4891 | 0.94-0.97x |
| vc-num-per-vnet=16 | 1821 | 1598 | 5342 | 4889 | 0.94-0.97x |

Reading:
1. **buffer-size-per-vc is the first-order sensitivity**: =1 (the upstream
   "request vnet" default) is 2.1-2.6x slower than =4 -- data bursts get
   serialized by the credit round trip; the DATA_VNET unification
   (4-flit buffers) removed this;
2. =4 sits at the **knee**: =8/16 gain only another 3-7%; both sweeps
   converge to the same dependency floor (approx. 1820/1600/5340/4890);
3. vc-num-per-vnet 2->4 yields 3-5% (removes output-port head-of-line
   blocking); >=8 saturates;
4. **The busiest link stays below 25% in every configuration** -- NoC
   bandwidth is not the bottleneck;
5. depth=1 reproduces the pre-change behavior (4434 vs 4422), cross-
   checking the parameter path end to end.

## 4. Makespan Decomposition: baseline vs Max Resources (vc-num-per-vnet=16, buffer-size-per-vc=16)

Method: window critical-path backtrack; four time categories (chain sum =
makespan exactly):
- **T1 structural/dependency wait (rewrite)**: an in-network rewrite
  waiting for external after data;
- **T2 group-serial wait (execution-model discipline)**: the "one burst
  group at a time" injection serialization + seq order (injected by the
  replay execution model, not a NoC capability issue);
- **T3 inherent network transit**: uncontended per-hop transit + first
  flit + tail drain (P10 baseline);
- **T4a congestion excess**: transit/convergence beyond P10 (queuing,
  contention, convergence-point serialization).

| Window | Config | W (k cyc) | T1 | T2 | T3 | T4a |
|---|---|---|---|---|---|---|
| s1-a | baseline | 1911 | 3.2% | 36.4% | 27.3% | 33.1% |
| s1-a | **max** | 1766 | 2.8% | 36.6% | 29.8% | 30.7% |
| s1-b | baseline | 1665 | 3.7% | 32.3% | 23.3% | 40.8% |
| s1-b | **max** | 1532 | 3.3% | 35.1% | 25.1% | 36.5% |
| s4-a | baseline | 5571 | -- | 46.2% | 18.4% | 35.3% |
| s4-a | **max** | 5082 | 0.1% | 43.5% | 20.1% | 36.2% |
| s4-b | baseline | 5190 | -- | 46.2% | 17.7% | 36.0% |
| s4-b | **max** | 4639 | 0.1% | 44.9% | 19.2% | 35.6% |

(T4r resource wait ~0 on the critical path; T1 = external-data waits of
in-network rewrites.)

Reading:
1. **Max resources are only 8-11% faster than the baseline** (makespan
   0.89-0.92x) -- the configuration lever is nearly exhausted at
   buffer-size-per-vc=4 / vc-num-per-vnet=2;
2. **T2 (group-serial wait) is stable at 35-46%** across configurations --
   it is the serialization of the "one burst group at a time" injection
   discipline, an execution-model property, not a NoC capability issue;
3. **T4a (congestion excess) remains 30-37% even with unbounded buffers
   and VCs** -- this is the **inherent serialization of convergence
   points**: at the A16 reduction corridor and the column-0 roots,
   multiple branches share a 1 flit/cycle output port and must queue no
   matter how deep the buffers are. It can only be removed by changing
   the communication structure (operator/scheduler level), not by NoC
   configuration;
4. T3 (inherent transit) 18-30%: pure physical cost, linear in chain
   length.

## 5. Conclusions

1. **NoC bandwidth and buffers are not the bottleneck**: busiest link
   <25%; the baseline (buffer-size-per-vc=4, vc-num-per-vnet=2) is within
   8-11% of unbounded resources; the DATA_VNET unification (4-flit
   buffers) removed the largest flow-control distortion;
2. **The makespan is dominated by the dependency critical path**: T2
   (group-serial discipline, 35-46%) plus the structural serialization at
   convergence points (residual T4a, 30-37%) are the two largest blocks;
   pure transit (T3) is only 18-30%;
3. **The improvement lever is the communication structure, not NoC
   parameters**: reduce convergence depth, break up the A16/column-0
   style critical paths, relax the one-burst-group injection discipline
   (if the real system allows) -- these live at the operator/scheduler
   level; the NoC-side buffer/VC parameters are already on their return
   plateau;
4. **Accounting caveat**: the replay has no compute model -- the T1/T2
   "dependencies" are the trace's after/lclock structure (the analysis
   model's execution schedule); real systems interleave operator compute
   into those dependency segments; T2 is injected by the replay execution
   model and must be re-calibrated against a real GPU's injection engine
   behavior before extrapolation.

## Appendix: Data and Tools

- Raw sweep data: `log/aigcic_noc/trace/{bufscan_seg,vcsscan_seg,attr_*}/`;
- Attribution tool: `tools/trace_plan/analyze_critical_path.py` (event log
  -> four-category times + critical-path chain); event logs produced by
  `--trace-event-log`;
- Design/verification spec: `traffic/trace/noc-trace-4x6/trace-replay-spec.md`;
- Reconciliation and assertions: per-link flit zero-diff and msg 8/8 PASS
  on every configuration; runtime assertions zero-triggered.
