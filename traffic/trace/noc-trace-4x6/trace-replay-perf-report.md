# trace replay 性能仿真报告(中文 draft)

> 配置:分段模式分支,`buffer-size-per-vc=4`、`vc-num-per-vnet=2` 为 baseline;
> 最大资源 = `buffer-size-per-vc=16, vc-num-per-vnet=16`。
> 四窗口 repeat=2(attribution)repeat=1(sweep)。
> 所有链路/msg 对账在全部配置下通过(链路逐条零差异、msg 八项 PASS)。

## 1. trace 与回放实现简介

**trace**:一颗 MPU 在一层 decode 里的全部 NoC 发包,逐跳记录(router→相邻
router)。四个窗口 = {每步 1 或 4 个 token} × {a 型层, b 型层},含
unicast / 多播树 / 在网归约树 / allreduce;记录间有 `after` 依赖 DAG
(跨消息数据与同步)与每 router 的 `seq` 发送次序;`lclock` 为依赖深度。
规模:记录 4210/3425/10205/9420,链路 flit 13280/11677/46381/42019。

**回放实现**(hop-level in-network replay):
- plan 编译器:trace → 逐 router 的中继计划;直行/弯分类;信道依赖图
  (分段模式:单轴单调包,无类即无死锁)与 links.csv 精确对账为强制门;
- 传输:tester 按 after 依赖与**整组串行注入纪律**(每个 router 的注入引擎
  同一时刻只发一个消息的 burst 组;组内流水并行,换组须等当前组全部落地)
  注入写请求(`RELAY_PUTX`,静默中继,
  无响应);包头到达路由器后由钩子**网内改写**直行延续(同分支同包),
  弯/汇聚/分支处 EJECT + 由本 router tester 重注入延续 leg;
  目的副本为虚拟投递(不计 flit);
- 调度:TraceScheduler 持有 after 因果状态与到达完成,链式窗口重复;
- 验证:逐链路 flit 与 links.csv 逐条零差异;msg 粒度八项对账
  (完成唯一性/实现唯一性/因果/发送次序/msg 完备性/字节)全 PASS;
  运行时断言(burst flit 数、rec.next 合法性等)零触发。

## 2. MPU NoC 架构介绍

- **拓扑**:4 行 × 6 列的 mesh(逻辑 4×6,实现在 4×8 fabric 上,inactive 列
  不承载流量),共 24 个 router;**XY 维序路由**(先行后列,链路权重强制)。
- **节点**:每个 router 挂一个 L1 控制器(核侧)与一个目录控制器(DRAM
  网关),各自独立 NI 与本地端口;无 cache 一致性,目录对 trace 流量
  静默接收(理想 DRAM 网关)。
- **vnet 分配**:4 个 vnet——vnet0/1 为请求类(片内/跨die),vnet2/3 为
  响应类;本次 trace 流量全部在 **vnet0**(片内请求)。四个 vnet 已统一
  标注为 DATA 型(每 VC 4 个 flit 缓冲,见 §3 的 buffer-size-per-vc)。
- **flit 与链路**:flit = **128 B**,每条链路每方向 1 flit/cycle;消息的
  flit 数 = ceil(字节 / 128)(如 352 B → 3 flits,512 B → 4 flits)。
- **缓冲与 VC(baseline)**:buffer-size-per-vc = **4 flit**,
  vc-num-per-vnet = **2**。
- **时钟**:1 GHz(1 cycle = 1000 ticks),本文所有时延/makespan 均以
  cycle 计。

## 3. buffer-size-per-vc 与 vc-num-per-vnet 扫描(baseline:4 / 2)

makespan(cycles,repeat=1,分段模式):

| 配置 | s1-a | s1-b | s4-a | s4-b | 相对 baseline |
|---|---|---|---|---|---|
| **baseline: buffer-size-per-vc=4, vc-num-per-vnet=2** | 1911 | 1665 | 5571 | 5190 | 1.00 |
| buffer-size-per-vc=1 | 4028 | 3692 | 14293 | 13335 | 2.11-2.57× |
| buffer-size-per-vc=2 | 2454 | 2183 | 8094 | 7477 | 1.28-1.45× |
| buffer-size-per-vc=8 | 1858 | 1625 | 5465 | 5015 | 0.93-0.97× |
| buffer-size-per-vc=16 | 1858 | 1625 | 5455 | 5009 | 0.93-0.97× |
| vc-num-per-vnet=4 | 1822 | 1599 | 5416 | 4963 | 0.95-0.97× |
| vc-num-per-vnet=8 | 1821 | 1598 | 5344 | 4891 | 0.94-0.97× |
| vc-num-per-vnet=16 | 1821 | 1598 | 5342 | 4889 | 0.94-0.97× |

读法:
1. **buffer-size-per-vc 是第一敏感参数**:=1(上游"request vnet"默认)比
   =4 慢 2.1-2.6 倍——数据 burst 被信用往返逐 flit 串行化;F14 统一
   DATA_VNET(4-flit)后已修复;
2. =4 处于**膝点**:到 =8/16 仅再快 3-7%,两维(buffer、vcs)扫描都收敛到
   同一依赖下限(≈1820/1600/5340/4890);
3. vc-num-per-vnet 2→4 有 3-5% 收益(消除端口队头阻塞),≥8 饱和;
4. **最忙链路在所有配置下 ≤25%**——NoC 带宽不是瓶颈;
5. d=1 复现改型前行为(4434 vs 4422),交叉验证参数通路。

## 4. makespan 拆分:baseline vs 最大资源(vc-num-per-vnet=16, buffer-size-per-vc=16)

口径:窗口关键路径回溯,四类时间(链上和 = makespan):
- **T1 结构/依赖等待(改写)**:网内改写等待外部 after 数据;
- **T2 组串行等待(执行模型纪律)**:注入引擎"一次只发一个 burst 组"的
  串行化 + seq 发送次序(回放执行模型注入,非 NoC 能力问题);
- **T3 网络固有**:每跳无竞争传输 + 注入首 flit + 尾排空(P10 基线);
- **T4a 拥塞超额**:传输/汇聚超出 P10 的部分(排队、争用、汇聚点串行化)。

| 窗口 | 配置 | W (k cyc) | T1 | T2 模型纪律 | T3 固有 | T4a 拥塞超额 |
|---|---|---|---|---|---|---|
| s1-a | baseline | 1911 | 3.2% | 36.4% | 27.3% | 33.1% |
| s1-a | **max** | 1766 | 2.8% | 36.6% | 29.8% | 30.7% |
| s1-b | baseline | 1665 | 3.7% | 32.3% | 23.3% | 40.8% |
| s1-b | **max** | 1532 | 3.3% | 35.1% | 25.1% | 36.5% |
| s4-a | baseline | 5571 | — | 46.2% | 18.4% | 35.3% |
| s4-a | **max** | 5082 | 0.1% | 43.5% | 20.1% | 36.2% |
| s4-b | baseline | 5190 | — | 46.2% | 17.7% | 36.0% |
| s4-b | **max** | 4639 | 0.1% | 44.9% | 19.2% | 35.6% |

(T4r 资源等待在关键路径上 ≈0;T1 为网内改写的外部数据等待。)

读法:
1. **最大资源只比 baseline 快 8-11%**(makespan 0.89-0.92×)——配置杠杆
   在 d=4/vcs=2 附近已经基本用尽;
2. **T2(执行模型纪律)稳定在 35-46%**,不随资源配置变化——它是"一次一个
   burst 组"纪律造成的串行,属于回放执行模型,不是 NoC 能力问题;
3. **T4a(拥塞超额)在无限缓冲/VC 下仍占 30-37%**——这部分是**汇聚点的
   固有串行化**:A16 归约走廊、列 0 根等收敛点上,多支流共享 1 flit/cycle
   的输出端口,无论缓冲多深都要排队。它只能靠改变通信结构(算子/调度层)
   消除,不是 NoC 配置可解;
4. T3(固有传输)18-30%:纯物理成本,随链长线性。

## 5. 结论

1. **NoC 带宽与缓冲都不是瓶颈**:最忙链路 <25%,baseline(buffer-size-per-vc=4,
   vc-num-per-vnet=2)距无限资源仅 8-11%;统一 DATA_VNET(4-flit 缓冲)已
   消除最大的流控失真;
2. **makespan 由依赖关键路径主导**:T2(执行模型纪律 35-46%)+ 汇聚点的
   结构串行(T4a 残余 30-37%)是两大块;纯传输(T3)只占 18-30%;
3. **改进方向在通信结构而非 NoC 参数**:减少汇聚深度、打散 A16/列 0 类
   关键路径、放宽"一次一个 burst 组"的注入纪律(若真实系统允许)——
   这些在算子/调度层;NoC 侧的 buffer-size-per-vc / vc-num-per-vnet
   参数已在其收益平台上;
4. **口径警告**:回放无计算模型——T1/T2 的"依赖"是 trace 的 after/lclock
   结构(分析模型的执行安排),真实系统中依赖段还混有算子计算时间;
   T2 为回放执行模型所注入,外推到真实 GPU 时需按其注入引擎行为修正。

## 附:数据与工具

- 扫描原始数据:`log/aigcic_noc/trace/{bufscan_seg,vcsscan_seg,attr_*}/`;
- 归因工具:`tools/trace_plan/analyze_critical_path.py`(事件日志 → 四类
  时间 + 关键路径链);事件日志由 `--trace-event-log` 产出;
- 设计/验证规格:`traffic/trace/noc-trace-4x6/trace-replay-spec.md`;
- 对账与断言:每配置链路逐条零差异、msg 八项 PASS、运行时断言零触发。
