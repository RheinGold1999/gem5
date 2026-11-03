# AI workload traffic tables（真实 AI 系统通信模式 → `src dst pir` 表）

本目录把已发表的**真实 AI 系统的片上/片间通信模式**提炼成
`--synthetic=table --traffic-table=<file>` 使用的流量表（每行
`src_id dst_id pir`）。每个表对应一个明确的工作负载场景：**谁、
以什么频率、向谁发包**，并注明出处的论文/系统。

- 生成脚本：`gen_ai_traffic_tables.py`（表由它确定性生成，数值可复现、
  可改参数重新生成；表内注释即场景摘要）。
- 参考运行结果：`log/aigcic_noc/ai_workloads/<场景名>/`
  （默认表、`--vcs-per-vnet=2`、200k cycles、饱和区，见 §3 基线表）。

---

## 1. 映射约定

**网格与路由器编号**：驱动默认在 4×8 fabric 上仿真 4×6 活动 mesh
（行优先编号，`numColumns=8`；第 6/7 列的 id 6,7,14,15,22,23 不可用）：

```
       col0  col1 col2 col3 col4 col5
row0:    0     1    2    3    4    5
row1:    8     9   10   11   12   13
row2:   16    17   18   19   20   21
row3:   24    25   26   27   28   29
```

**PIR 语义**（`src dst pir` 第三列）：
- pir = 该 (src,dst) 流的**每周期注入概率**（packet injection rate）。
  测试器每周期对每个源投一次均匀随机数 r：`r < 该源所有 pir 之和`
  则注入一个包，目的地址按各流 pir 的累计分布抽取；**每源 pir 和 ≤ 1**。
- 一个“包” = 一次合成访存（默认 128 B payload），分读（R）/写（W）
  两类。两种类型的网络开销**相同（各 2 条消息）**，但方向相反：
  - **R = 读**：GETX 请求（控制 flit）+ 目录 Data 响应（数据 flit）；
    取进的行落在 clean 态，逐出时**静默丢弃，零网络流量**。
  - **W = 写**：PUTX 请求（**数据 flit，写本身随消息直送目录**，
    fire-and-forget，不取数、不占缓存行）+ 目录 WB_ACK（控制 flit）。
  - 非 table 模式的读/写概率由 `--traffic-read-ratio`（默认 0.5）决定；
    table 模式由每行第 3 列的 `rw`（`src dst rw pir`；旧 3 列格式
    `src dst pir` 仍接受，视为 `W`）决定。
  - 表中 PIR 是 **offer 的访存速率**；网络消息速率 ≈ 被接受速率 × 2
    （R、W 皆然），且**每访存恰好在请求方向 1 个 flit、应答方向
    1 个 flit**——数据 flit 对 R 走应答向、对 W 走请求向。
- 测试器不管网络是否拥塞，每周期都按表 offer（发不出去的包在
  测试器/Sequencer 里重试，不丢），所以饱和后接受速率由网络容量决定，
  但**每个包的目的地仍严格按表分布**——拥塞只改变“发多少”，
  不改变“发给谁”的形状。
- **pir ≠ 链路带宽**：pir 是访存速率。满接受时，任何一条 pir=p 的流
  （R 或 W）在**请求路径**的每条有向边上贡献 p flit/cyc（R 的 GETX /
  W 的 PUTX），在**应答路径**（反方向）每条边上再贡献 p（R 的 Data /
  W 的 WB_ACK）；数据 flit 对 R 在应答向、对 W 在请求向。
  环形拓扑的环绕流应答还会扫过整行/整列。下文各场景
  “预期 NoC 行为”只给访存速率与拓扑形状，链路 flit 占用的实测值
  一律以 §3 为准。

**扫负载**：`--table-scale=<k>`（本仓库新增驱动选项）把所有 pir 乘 k
（自动校验 pir≤1、每源和≤1，缩放后的表写入
`<outdir>/traffic_table_scaled.tbl` 存档）。这就是 table 模式下的
`--injectionrate` 扫描等价物。饱和点因场景而异，建议按 0.05–0.3 步进扫。

**时钟**：默认 1 GHz，即 PIR=0.30 ⇔ 每节点每周期 offer 0.30 个访存
⇔ 128B×0.30 ≈ 38 GB/s 的请求 payload（响应另计，放大系数见上）。

---

## 2. 八个场景

### 2.1 `dp_ring_allreduce_24.tbl` — 数据并行训练：全局环 All-Reduce

- **真实系统**：百度 Ring All-Reduce（Patarasuk & Yuan 2009，百度 2017
  博客推广）/ NCCL ring 算法，是数据并行 SGD（ResNet、GPT 预训练等）
  的梯度同步标准做法。
- **模式**：N 个 worker 连成一个环；每步所有 worker 把一个 S/N 大小的
  梯度分块发给环上的下一个节点；reduce-scatter + all-gather 共
  2(N-1) 步，方向相同。稳态下**每个源只有一个目的地址**（环邻居）。
- **映射**：24 个节点按"蛇形"串成一个全局环（相邻 id 之间全是 1 跳，
  只有收尾 24→0 是沿第 0 列向上的 3 跳）。每对 pir = 0.30。
- **预期 NoC 行为**：每条环边承载 1 条 0.30 的访存流（flit 占用按
  §1 折算）；唯一的热点不均来自 24→0 环绕——它的 GETX+PUTX 与
  8→16 的应答 Data+ACK 在 col0 纵向边界叠加（实测该边界 ~80%，
  为全网最热，见 §3）；和 uniform_random 的区别是
  每源只有一个固定目的，没有跳距分散。

### 2.2 `tp3_local_allreduce.tbl` — 张量并行（Megatron）：局部 All-Reduce

- **真实系统**：Megatron-LM（Shoeybi et al. 2019）：每个 transformer 层
  前向/后向各做一次 TP all-reduce（频率最高的集合通信）；TP 组
  通常限制在一个 NVLink 域内。
- **映射**：TP 组 = 一行内相邻 3 个 tile（共 8 组），组内做 3 节点环
  all-reduce。每对 pir = 0.40。
- **预期 NoC 行为**：全部流量集中在行段内部的横向链路（每流访存
  0.40），**所有纵向链路空闲**——“局部化”通信的极端对照场景。
  注意 flit 占用是 pir 的 ~2 倍：每条组内有向边同时承载本边流的
  GETX+PUTX 和环绕流（2→0）应答的过路（实测 ~0.8 flit/cyc，见 §3）。
  它对应 TP 组溢出到 die 内 mesh 的情况。

### 2.3 `pp4_1f1b_pipeline.tbl` — 流水线并行（1F1B）：激活/梯度双向流

- **真实系统**：Megatron-LM 的 1F1B（PipeDream-Flush）调度 / GPipe：
  稳态下每个 stage 边界同时有前向激活（stage i → i+1）和反向激活梯度
  （stage i+1 → i）随 microbatch 流动，两个方向消息量同量级。
- **映射**：流水线 stage = 网格行（PP=4），列 = DP 副本（6 条独立的
  microbatch 流水线）。每列相邻行之间：下行 0.20 + 上行 0.20。
- **预期 NoC 行为**：纯纵向列流量——每个行界上有下行访存流 0.20
  （GETX+PUTX 南下）和上行访存流 0.20（Data+WB_ACK 北上），分别占
  边界的两个方向（满接受时每方向 ~0.8 flit/cyc；VC2 实测 ~0.4，
  受 VC 限流，见 §3）；横向链路空闲。
  中间行（row1/row2）的源注入率 0.40，两端行 0.20。
  与 2.2 正好互补：一个只有横向、一个只有纵向。

### 2.4 `moe_alltoall_uniform.tbl` — MoE/推荐模型：均衡 All-to-All

- **真实系统**：Switch Transformer（Fedus et al. 2021）/ GShard 的
  expert-parallel all-to-all（token dispatch），路由均衡时；以及
  Meta DLRM（Gupta et al. 2019）embedding 分片后的均匀 all-to-all。
- **模式**：每个 EP rank 把 token batch 均匀切给其余全部 23 个 rank
  → 每源 23 条等 pir 流（0.40/23 ≈ 0.0174），每节点接收 ≈0.40。
- **预期 NoC 行为**：所有节点收发均匀、跳距分散（平均 ~2 跳以上），
  是"最平滑"的参照场景（稳态边际分布与 uniform_random 相同，
  但表模式把每对流的比例钉死）。combine 阶段体积翻倍：
  用 `--table-scale=2` 复现。

### 2.5 `moe_alltoall_hot_experts.tbl` — MoE：热点专家（负载不均）

- **真实系统**：DeepSeek-V3 / Mixtral 实测的专家负载不均——少数专家
  吸收超额 token（这正是 DeepSeek-V3 引入 expert-bias 无辅助损失
  负载均衡的原因）。
- **模式**：每源把 75% 的 dispatch 速率均匀摊给 23 个 rank，
  25% 集中到 4 个“热专家”rank（id 2, 13, 18, 27）。
  热点接收 ≈0.44，约为均值（0.20）的 2.2 倍、非热点节点（0.15）
  的 2.9 倍。
- **预期 NoC 行为**：4 个热点目录成为漏斗（和 §6.4 cross-die 漏斗
  同签名）：热点局部队列暴涨、全网接受率被拖低；对照 2.4 可以
  量化"专家不均"的带宽代价。combine 翻倍（`--table-scale=2`）会让
  热点彻底饱和。

### 2.6 `dp_hierarchical_allreduce.tbl` — 两级层次化 All-Reduce

- **真实系统**：NCCL hierarchical all-reduce、BlueConnect（Cai et al.
  2019）、Horovod 分层归约：先在快域内归约，再把域结果做全局归约。
- **映射**：一级 = 每行 6 节点行内环（pir 0.30）；二级 = 4 个 col0
  leader 组成纵向环（pir 0.075，局部腿：全局腿 ≈ 4:1）。
- **预期 NoC 行为**：行内每边 1 条 0.30 访存流，且环绕流应答扫过
  整行（实测行内横向 63-70%，见 §3）；col0 纵向仅 leader 环 0.075
  （含 24→0 环绕），其余纵向链路空闲。对照 2.1 可研究“层次化 vs
  单环”的链路占用差异。

### 2.7 `llama3_4d_composite.tbl` — LLaMA-3 式 4D 并行训练合成快照

- **真实系统**：LLaMA 3 预训练（"The Llama 3 Herd of Models", 2024）
  的 4D 并行：TP × CP × PP × DP。频率层次：TP all-reduce 每层 2 次
  （最高频）≫ PP 激活收发（每个 microbatch 过一次边界）≫
  DP all-reduce（每个 optimizer step 一次，最低频）。
- **映射**：24 tile 上缩放为 TP=3（行内 3 tile 组）+ PP=4（行）
  + DP=2（row0-1 与 row2-3 互为副本，成对交换）+ CP=1。
  每源速率：TP 环 0.25 + PP 下行 0.06/上行 0.06 + DP 0.02；
  每源总和 ≤ 0.39。
- **预期 NoC 行为**：横向（TP）+ 纵向（PP/DP）混合负载，
  最接近真实训练稳态的"背景流量"；适合作为叠加其他事件的底噪场景。

### 2.8 `pd_disagg_kv_transfer.tbl` — 推理：Prefill/Decode 分离的 KV 迁移

- **真实系统**：Mooncake（月之暗面 Kimi 的 KVCache 中心化分离架构，
  Qin et al. 2024）、DistServe/vLLM 的 prefill-decode 分离：长 prompt
  在 prefill 池算完后，**KV cache 整块跨网络搬到接续解码的 decode 实例**；
  decode 侧自身流量（小 token、走网卡的结果）相对可忽略。
- **映射**：row0-1 = prefill 池（12 节点），row2-3 = decode 池（12 节点）；
  每个 prefill 节点把 KV 流按池均匀散给 12 个 decode 节点
  （Mooncake 式池路由，会话亲和性在稳态下被平均掉）。
  每对 pir = 0.20/12 ≈ 0.0167，prefill 源总速率 0.20，decode 全收。
- **预期 NoC 行为**：**单向重负载流**（请求南下、应答北上——两个
  方向都有流量，但内容相反）。XY 路由下所有以 row2-3 为目的的流都在
  目的列穿越 row1→row2 边界：南向每条走廊承载该列 2 个 decode 目的的
  GETX+PUTX = 4×R_KV = 0.80 flit/cyc（满接受；GETX、PUTX 各 0.40），
  北向承载对应的 Data+WB_ACK（同样 0.80）——默认速率下走廊就贴住
  单链路容量（VC=4 满接受实测 80.0%，见 §3）。按 1 flit/cyc 容量
  折算，带宽饱和点在 R_KV ≈ 0.25（`--table-scale≈1.25`）；
  横向链路完全空闲。

---

## 3. 参考运行基线（默认表、VC=2、200k cycles、饱和区）

目录 `log/aigcic_noc/ai_workloads/<场景>/`，分析器
`perf_analysis/analyze_mesh_bw.py`。注意：默认表在 VC=2 下大多处于
或接近饱和（8 张表全 `W`，fire-and-forget 写 = 每访存 2 条消息），
接受吞吐是容量限制的值——这正是 §6 基线（inj=0.99）的方法学；要
研究饱和前行为，用 `--table-scale` 往下扫。**本基线采样于
fire-and-forget 写语义生效之后**（写 = PUTX+WB_ACK，不取数）；
更早一版基线（写 = 取数+写回，4 条/访存，接受率约为本表的一半）
已被本版取代，属历史数据。

<!-- AI_BASELINE_TABLE:START -->

| 场景 | offer/源 | 接受访存 (pkts/cyc/node) | 接受消息 (pkts/cyc/node) | 纯NoC延迟 p50 | 全寿命 p50/p75 | 最热内部链路 |
|---|---|---|---|---|---|---|
| dp_ring_allreduce_24 | 0.300 | 0.300（offer 全收） | 0.600 | 5 cyc | 8 / 9 | 59.9%（col0 环绕向） |
| tp3_local_allreduce | 0.400 | 0.384 | 0.767 | 5 cyc | 14 / 608 | 79.9%（组内横向；组间横向全空） |
| pp4_1f1b_pipeline | 0.400 | 0.295 | 0.590 | 5 cyc | 105 / 481 | 39.7%（纵向；瓶颈 = VC credit loop） |
| moe_alltoall_uniform | 0.400 | 0.218 | 0.435 | 14 cyc | 31 / 160 | 68.4% |
| moe_alltoall_hot_experts | 0.200 | 0.163 | 0.325 | 11 cyc | 16 / 31 | 58.2%；热专家目录漏斗拖低全网 |
| dp_hierarchical_allreduce | 0.375 | 0.312 | 0.624 | 5 cyc | 9 / 15 | 60.3%（行内横向） |
| llama3_4d_composite | 0.390 | 0.354 | 0.708 | 6 cyc | 20 / 288 | 50.1%（横纵混合） |
| pd_disagg_kv_transfer | 0.200 | 0.100（offer 全收） | 0.200 | 16 cyc | 27 / 63 | 40.3%（走廊南向 PUTX；北向 WB_ACK 对称） |

观察（fire-and-forget 写语义、`--vcs-per-vnet=2`、200k cycles、饱和区；
每访存恒为 2 条消息：请求向 1 + 应答向 1）：

1. **局部化与短跳模式受益最大**：dp_ring 的唯一热点（col0 环绕）
   负载减半后不再饱和，offer 全收（0.300）；tp3（1 跳为主）0.384，
   仅受组内横向 80% 链路限制；多跳均匀 all-to-all 0.218 访存 =
   0.435 消息——与旧语义下消息侧饱和值（~0.41-0.44）一致，
   "跳距决定吞吐"在消息轴上依然成立。
2. **专家不均有代价**：moe_hot 只 offer 了 0.20（均匀版的一半），
   接受率却更低（0.163 vs 0.218）——热目录漏斗依旧
   （与 §6.4 cross-die 漏斗同签名）。
3. **pp4 与 pd_disagg 的瓶颈仍不是链路带宽而是 VC credit loop**
   （pp4 纵向链路占用仅 39.7% 但排队深：全寿命 p50 105 vs 纯 NoC
   p50 5；提高 `--vcs-per-vnet` 可释放，机制与旧语义下相同）。
   pd_disagg 在新语义下走廊每方向只承 2×R_KV = 0.40（南向 PUTX、
   北向 WB_ACK 对称），offer 全收（0.100 = 0.200/2）；走廊带宽
   饱和点相应外移到 R_KV ≈ 0.5（`--table-scale≈2.5`）。
4. 延迟分层清晰：纯 NoC 中位 5-16 cyc；写完成于 WB_ACK，全寿命
   p50 较取数+写回时代大幅下降（如 pp4 235→105、pd 175→27）。
   tp3 的 p75=608 是饱和区请求排队的尾部。

<!-- AI_BASELINE_TABLE:END -->

## 4. 运行示例

```bash
# 单场景（默认表，VC=2，200k cycles）
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/ai_workloads/tp3_local_allreduce \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=table --traffic-table=traffic_tables/ai/tp3_local_allreduce.tbl \
    --vcs-per-vnet=2 --sim-cycles=200000000

# 扫负载（--injectionrate 在 table 模式无效，用 --table-scale）
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/ai_workloads/moe_hot_x0.5 \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=table --traffic-table=traffic_tables/ai/moe_alltoall_hot_experts.tbl \
    --table-scale=0.5 --vcs-per-vnet=2 --sim-cycles=200000000

python3 perf_analysis/analyze_mesh_bw.py log/aigcic_noc/ai_workloads/tp3_local_allreduce/stats.txt
```

## 5. 改参数 / 加场景

所有表由 `gen_ai_traffic_tables.py` 生成；改 `SCENARIOS` 里的速率、
热点集合、池划分后重跑 `python3 gen_ai_traffic_tables.py` 即可
（脚本按测试器同样的规则校验：活动 id、无自环、无重复对、
pir∈[0,1]、每源和 ≤1，并打印每源/每接收负载摘要）。
