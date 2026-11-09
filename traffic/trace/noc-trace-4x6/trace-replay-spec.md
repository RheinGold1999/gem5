# trace 回放实现规格 —— noc-trace-4x6 → gem5 NULL_AIGCIC_NOC

> **分支说明(trace-replay-segmented)**:本分支在 F10/F12 之上实现**分段模式**
> 作为实验替代:任何"弯"(方向改变/折返)都把包断开——到达流 EJECT 到本地 dir,
> 延续由本 router 的 tester 以新包重注入(leg);网内改写只发生在**同方向直行**上。
> 因此任何包在网内**从不换轴**,列上/列下/行东/行西四个方向的信道集互不相交且
> 各自成链 → 信道依赖图为四个 DAG 的并 → **单 VC(vcs_per_vnet=1)即无死锁,
> VC 类机制整体不再需要**(hook 全部 cls=-1,NI 注入不再强制 offset-0)。
> 编译器的类 CDG 检查保留且仍健全(运行时图 ⊆ 被检图)。运行时自检:跨弯改写
> 即 panic。实测代价:弯处从 cut-through(≈0)变为端点往返,四窗口 makespan
> +12~17%(中位时延 +1~2 cyc,MAX 不变);换来 vcs=1 可运行与 hook/VA 简化。

> **文档地位**:本文件是 trace 回放功能的**唯一权威实现规格**,由设计讨论定稿。
> 实现者(AI agent 或人)必须严格遵循;任何偏离(机制、规则、口径)都必须先经
> 用户明确批准并回写进本文件。与代码冲突时,以本文为准。
>
> 现状:工作树中已存在一版未提交实现(大体按本规格)。[A1]/[A2] 已于 2025-11-06
> 经用户确认采纳(见 §7);[M1] 合并路径缺陷已按 §5.1 规范行为修复。
> 后续任何偏离仍须先经用户批准并回写进本文件。

---

## 0. 变更控制

**冻结决策(F = Frozen,不得擅改):** 见 §4 的 F1–F13。
**已否决的备选(不得擅自复活):** 见 §0.1。
**已批准的修正案:** §7 的 A1、A2(用户 2025-11-06 确认;与讨论稿的差异以修正案为准)。
**已修复的缺陷:** §5.1 的 [M1](2025-11-06 修复)。

规格未覆盖之处:取与 AGENTS.md 冻结设计一致的最小附加改动;仍不确定时,**停下来问用户**。

### 0.1 已否决的备选方案(讨论中明确排除)

| 备选 | 否决原因 |
|---|---|
| 消息级端到端展开(多播/归约拆成 unicast) | 共享前缀字节被复制(首链路可达 4×),归约走廊 2–3× 膨胀,与 links.csv 不可对账 |
| 按 msg id 匹配到达流 | 分支点上同 router 有同 msg 多条出记录,msg id 有歧义;必须按 **rec id** |
| 开环预排程(after 图 + 标定单跳延迟) | 被闭环到达驱动模型取代;时序应由 NoC 涌现 |
| 复用 PUTX+WB_ACK 作为完成信号 | 响应流量不在 trace 里(反向链路污染);完成事件落在发送方而非接收方。→ 无响应,完成 = 到达回调 |
| fwd/rec 参与注入串行化链 | relay 不占注入引擎(网内直通);会让到达触发的中继被无关 send 卡住,并消灭 router 内多流 VC 并发 |
| 规则 b 只等"紧邻上一条" | 双向多播源同 msg 两条 send 并行时漏等;必须等**所有在途** |
| 汇聚点享受同 msg 流水线豁免 | 汇聚是真实数据汇合,必须等全部分支 |
| 让 tester 提前把 held rec 递给 NI | 破坏 ni_src IDLE/XFER/BUSY 语义;hold 必须在 tester 状态机内 |

---

## 1. 目标与保真口径

1. 在 4×6 active mesh(4×8 fabric,`--num-active-columns=6 --num-active-rows=4`)上
   回放 `traffic/trace/noc-trace-4x6/` 四个窗口(s1-layer-a / s1-layer-b /
   s4-layer-a / s4-layer-b)。
2. **逐跳中继回放**:trace 的一条记录 = 相邻 router 间一跳。多播/在网归约/allreduce
   的树形已"烘焙"在逐跳记录里——gem5 **不做**任何在网复制/加法运算
   (`joins`/`x<n>`/`@next` 只是标注),回放按记录搬运字节。
3. **保真口径**:inter-router 链路的字节/记录数与 `links.csv` **逐条精确一致**。
4. **闭环**:rec 的执行由真实网络事件(到达完成)驱动,无预排程、无标定延迟;
   拥塞自然回推注入(规则 b 的自节流性质)。
5. **零响应**:所有传输都是写请求,写响应暂不实现(见 F9)。
6. 不改变既有模式(`uniform_random`/`table`)的任何行为;trace 模式全部参数门控;
   `README-NOC.md` §6 基线不受影响。

## 2. 数据事实(设计依据,已用四窗口数据验证)

| 事实 | 数值(s1-layer-a;其余窗口同量级) | 用途 |
|---|---|---|
| 分支"弯"(轴切换或折返)次数 | 0 弯 771 / 1 弯 96,**无 ≥2 弯** | dateline 2 个 VC 类足够 |
| class-CDG(弯⇒升类的信道依赖图) | 150 节点 / 204 边 / **0 环** | 无死锁(单类 CDG 有环,不可省略类机制) |
| 跨消息"送达我"型依赖落点 | 100% 在被依赖消息目的集合内 | tap/终点到到达事件即可驱动全部依赖 |
| send rec 的 after 引用 | 100% 跨消息 | tester 的 WAIT_AFTER 纯由调度器驱动 |
| branch fanout(一到达的延续数) | {1:2742, 2:50, 3:44},≤3 | 第一分支 rewrite + 其余 leg 即可 |
| merge 组大小(ups+absorbed) | {2:116, 3:80};吸收腿 120 | 汇聚释放组的规模有界 |
| 注入引擎占用(规则 b 串行化) | 最忙 router ~30% | 规则 b 不会成为人为吞吐瓶颈 |
| tap(rec 的 owner ∈ dst) | 1297 | 虚拟投递 [A2] |

**router 编号**:trace `r行c列` → fabric id = `行×8 + 列`(FABRIC_COLS=8);
tester i ↔ L1 controller i ↔ router i;tester 24–31(闲置 router)不加载记录。
**地址**(mpu52):relay 目的 = 目标 router 的 4GiB dir 窗口基址
`(列<<2 | 行) << 32`,块偏移携带 rec 索引;dir 对其静默丢弃,偏移无意义。

## 3. 总体架构

```
compile_trace_plan.py ──► <window>/_plan/plan.txt (只读,运行时加载)
                              │
      ┌───────────────────────┼───────────────────────────┐
      ▼                       ▼                           ▼
 GarnetSyntheticTraffic   TraceReplay(garnet 内)    TraceScheduler(全局唯一)
 (每 router 一个 tester,   (InputUnit 头 flit 钩子:    (唯一状态权威:
  只注入自己的 T/R rec)      REWRITE/EJECT/terminal)     after 计数、到达完成、
      │                       │                          窗口重复、leg 分发)
      │ 写请求 RELAY_PUTX     │ 改写 dst / eject            │
      ▼                       ▼                            ▼
      L1(本地完成,无 TBE)──► NI(vnet0)──► router 流水线 ──► 下一跳 InputUnit
                                                  │
                       dir: RELAY_PUTX 静默 pop(终点到达 + 无 tap flit)
```

- **plan 格式**(`plan.txt`,一行一条记录,由编译器生成,**禁止手编**):
  `idx kind owner seq next bytes tap cls n_ups ups… n_abs abs… n_after afters… lclock msg op stage rid`
  - `kind`:T=tester send,R=tester root(无同消息上游的非 send rec,含二级多播源分支),F=relay(hook 执行);
  - `ups` = 同消息上游到达(成员),`abs` = 被吸收腿(传递约简隐藏的汇聚成员);
  - `cls` = dateline VC 类(0/1);`tap` = owner ∈ dst。
- 编译器职责(全部强制):seq 连续性与 routers/*.csv 一致性检查、依赖因果检查、
  `detect_absorbed`(恢复被传递约简隐藏的汇聚成员)、class 分配 + **class-CDG 无环检查
  (有环即 fatal,禁止跳过)**、引擎占用估计、与 links.csv/summary.json 的**精确对账**。

## 4. 执行模型(冻结规则)

- **F1 逐跳网内中继**:kind F 的 rec 由到达 router 的 InputUnit 钩子在**路由计算(RC)之前**
  处理;纯链中继在 **head flit** 到达即改写 dst(cut-through,同 msg 相继 hop 流水线化),
  body/tail 跟随输入 VC,只有 head 改写一次。源 router 对自己刚注入的流直接放行
  (NI 已按 rec.next 盖章)。
- **F2 完成定义(唯一)**:`ARRIVAL_DONE` = 该 rec 的 burst 在其 `next` router 的
  InputUnit **计满(tail flit 到达,逐 flit 计数)**。sequencer 本地完成、末 flit 离开
  本地 NI、整条消息端到端送达,都**不是**完成事件。
- **F3 规则 a**:注入串行化链**只覆盖 send rec**(T/R);relay 不参与。
- **F4 规则 b(对外名称:整组串行注入纪律)**:READY 的 send rec 准出条件 =
  本 router **所有在途 send rec 已 ARRIVAL_DONE**,同 msg 豁免(在途集合全为
  同 msg 时可放行)。不变式:在途集合恒为单一 msg 的"burst 组";换 msg 必须
  整组落地。
- **F5 规则 c**:汇聚点**不享受**同 msg 豁免——多成员组须等**全部成员 burst 计满**
  (每个成员的 ARRIVAL_DONE)且延续的外部 after 清零,延续**恰好实现一次**。
- **F6 head 改写的准许**:仅限"单成员组且外部 after 已清零";带外部依赖的单成员、
  多成员组的成员流,一律不得在 head 改写(见 §5.1 的释放组行为)。
- **F7 tap**:到达 router ∈ msg dst → 虚拟投递(调度器 `tapDelivered` 事件 + 计数),
  **不发本地口 flit**。[修正案 A2]
- **F8 branch**:一到达触发 k>1 延续 → 第一延续为 head 改写(cut-through),其余延续为
  调度器 `relayLeg` 请求,由其 owner tester 从动态 leg 队列**立即**注入。
- **F9 全部传输 = 写请求**:新 `CoherenceRequestType:RELAY_PUTX`(数据类消息,
  `MessageSize=Data`,`DataLength = rec.bytes`,复用 burst 机制、头不计);
  L1 收到后转发 vnet0 并**本地完成**(sequencer 回调,不占 outstanding、无 TBE);
  dir 收到 RELAY_PUTX **静默 pop**(无 DRAM、无 WB_ACK)。死锁规则:纯 vnet0 请求、
  无响应依赖,vnet 排序约束自动满足。
- **F10 dateline 两类 VC**:注入 = class0;凡"弯"(轴切换或折返)⇒ 升 class1;
  只升不降。要求 `vc_per_vnet ≥ 2`(trace 模式强制检查)。hook 按 plan 的 `cls`
  限制该跳 VA 的候选 VC 子集(`send_allowed` 按需求类等待、`vc_allocate` 按类选择);
  tester 注入流(含 leg)起始类恒为 class0,首个 hook 处升至 plan 类——沿路径
  类单调不降,死锁论证不受影响(环需类下降,不存在)。
- **F14 vnet 类型统一 DATA_VNET(2025-11-06 用户批准)**:AIGCIC_NOC 四个 vnet
  全部标注 `vnet_type="response"`(cache.sm/dir.sm 共 6 处),使它们全部解析为
  DATA_VNET_(`buffers_per_data_vc=4` flit 深缓冲)。原因:上游默认把 "request"
  型 vnet 映射到 1-flit CTRL 缓冲,而 vnet0/1 承载的是数据 burst(PUTX/RELAY)
  ——1-flit 深度使 burst 被信用往返串行化(makespan ×2.1-2.5)。
  `vnet_type` 的唯一消费者是缓冲深度选择(OutVcState);不影响 vnet 排序规则、
  路由与 dateline 类。仅改协议 .sm,其他协议零影响。
  注意:改后 `--buffers-per-ctrl-vc` 对 vnet0 不再生效(改读
  `--buffers-per-data-vc`,均未暴露 CLI);§6 基线需重测(默认配置行为已变)。
- **F11 窗口重复**:每遍独立实例化依赖 DAG(`rep_base` 命名空间);rep k+1 的启动
  条件 = rep k **全部 rec ARRIVAL_DONE** + 可选 `--inter-layer-cycles` 间隙。
- **F12 死锁防线**:单类 CDG 有环(已验证),因此 dateline 类机制是**必需品**;
  换任何新 trace 必须重跑编译器的 class-CDG 检查(强制 fatal 门),不得绕过。
- **F13 既有约束**:AGENTS.md 全部冻结设计继续适用(vnet 排序、`data_msg_size ≤
  cacheline_size`、4×6/4×8 掩码、mpu52 编码、SLICC 每缓冲事件复制教训、TBE 断言等)。

## 5. 机制细节

### 5.1 释放组(HoldGroup)——规范行为

每个 F rec 构成一个释放组:`group_size = |ups| + |abs|`,
状态 = `{arrivals_remaining, afters_ok, fired}`。

**单成员组(group_size == 1;纯链中继或 after-hold):**
```
head 到达 → 外部 after 已清零?
  是 → REWRITE:改写 dst/身份为延续 rec,按 cont.cls 进 VA   (纯链即此路径)
  否 → EJECT:流弹入本地 dir(dir 静默 pop),不持有任何信道;
        该 burst 计满(ARRIVAL_DONE)计入组,外部 after 清零(aftersCleared)
        → maybeFire → 恰好一条 relayLeg → owner tester 重注入延续流
```

**多成员组(merge,group_size > 1):**
```
每个成员流 head 到达 → 一律 EJECT(绝不 head-rewrite、绝不等价物);
该成员 burst 计满 → arrivals_remaining--
arrivals_remaining == 0 且 afters_ok 且 !fired
  → fired = true(单次)→ relayLeg → owner tester 重注入恰好一条延续流
被吸收腿(abs)同样 EJECT 计数,但其流被排干(不产生延续)
```

**[M1] 回放引擎缺陷(已修复,2025-11-06;共四处,冒烟中暴露)**:

1. **merge 路径**(原描述):单延续分支只检查 `g.afters_ok` → ①多成员组首个成员
   到达即 REWRITE;②无 `fired` 检查致延续流复制;③EJECT 路径
   `arrivals_remaining = 1` 覆盖多成员计数。已修:`group_size > 1` 一律 EJECT,
   `onArrival` 递减,`maybeFire` 单次门。
2. **id 空间混淆(停摆根因)**:`onArrival` 调 `maybeFire(e.hold_id)`,把**组 id**
   传给了期望 **continuation rec index** 的 maybeFire(内部做 `m_rec_group[cont]`
   映射)→ 所有汇聚/after-hold 释放都检查到无关组上,leg 永不发出,依赖链停摆。
   已修:`maybeFire(g.cont)`。
3. **端口号当 router id**:`InputUnit` EJECT 路径 `route.dest_router = m_id`,而
   `InputUnit::m_id` 是端口号(如 North=5)→ flit 的 dest_router 字段损坏
   (TABLE_ 路由按 net_dest 查表碰巧不出错,XY 路由下会走错端口)。
   已修:`m_router->get_id()`。
4. **改写身份未转全局 tid**:`hook` 返回的 `hr.cont` 是窗口局部 rec,REWRITE 把它
   直接写入 `RouteInfo::trace_id`;而 hook/onArrival 按 `tid - m_rep_base` 解析。
   rep 0 (rep_base=0) 碰巧正确,repeat≥2 即越界断言。已修:`hr.cont = m_rep_base + cont`
   (RouteInfo::trace_id 恒为全局 tid)。

编译期已验证:分支到达与多成员延续零交叉、成员不跨组共享(除分支外)。

### 5.2 钩子行为表(InputUnit 头 flit,RC 之前)

| 条件(hook 视角) | 动作 |
|---|---|
| rec.owner == 本 router | 源直通(身份/目的已正确) |
| rec.next != 本 router | panic(身份/路由 bug) |
| 无延续(conts 空) | 终点:正常路由弹入本地 dir,dir 静默 pop |
| 单成员组 & afters_ok | REWRITE(见 §5.1) |
| 单成员组 & after 未清零 | EJECT → leg 路径 |
| 多成员组成员 | 一律 EJECT → leg 路径(修复 [M1]) |
| branch(k>1 延续) | 第一延续 REWRITE;其余按到达完成由调度器发 relayLeg |

热路径 = 按 trace_id 查表 + 计数器,无跨对象同步调用;事件一律投递到
TraceScheduler 的事件队列异步处理。

### 5.3 tester 状态机(每 router 一个,只管自己的 T/R rec)

```
WAIT ──(调度器 AFTER_SATISFIED: pending 归零)──► READY
READY ──(规则 b:在途组空 ∨ 在途组全同 msg)──► FLIGHT(发 RELAY_PUTX,L1 本地完成)
FLIGHT ──(自己的 ARRIVAL_DONE → SELF_DONE)──► DONE
```
- seq 顺序推进(`m_trace_head`),规则 b 由 `{m_trace_inflight, m_trace_inflight_msg}`
  实现(在途组恒单 msg,见 F4 不变式);
- 动态 leg 队列(`m_trace_legs`):调度器 relayLeg 的延续/分支腿,**不受规则 b 阻塞**
  (它们是网络回调的直接后果,立即注入),按延续 rec 的 cls/bytes/next 发包;
- retry 分 HEAD/LEG 两种(端口拒绝时保留队头,leg 保序);
- 每窗口重复时重置(pending0 重装、状态归 WAIT)。

### 5.4 调度器事件协议(TraceScheduler,唯一状态权威)

```
入:ARRIVAL_DONE(tid)            // 来自接收 router 钩子(burst 计满)
   [内部] aftersCleared(cont)    // 外部 after 集清零(由调度器自身计数驱动)
出:traceAfterSatisfied(rec)      // → owner tester:外部 after 全清(WAIT→READY)
   traceSelfDone(rec)            // → owner tester:自己的 send 完成(FLIGHT→DONE,规则 b 清账)
   traceInjectLeg(tid)           // → owner tester:重注入延续/分支腿
   tapDelivered(tid, bytes)      // 虚拟 tap 投递(统计)
   WINDOW_DONE(rep)              // 全部 rec ARRIVAL_DONE → 下一遍(加间隙)/退出
```
调度器状态 = 每 rec `{外部 after 计数, 依赖者反边}`;testers 之间零直接通信;
router 钩子不直接调 tester。

### 5.5 组件/文件级改动清单(已大体存在,须对齐本规格)

| 部件 | 内容 |
|---|---|
| `tools/trace_plan/compile_trace_plan.py` | plan 编译器 + 强制 CDG 检查 + 对账(已存在) |
| `src/mem/ruby/network/garnet/TraceReplay.{hh,cc}` | plan 加载、释放组、钩子行为表(§5.1/5.2;**[M1] 待修**) |
| `src/mem/ruby/network/garnet/InputUnit.cc` 等 | 头 flit 钩子接入(REWRITE/EJECT/terminal)、`RouteInfo::trace_id` 随流携带 |
| `src/mem/ruby/network/garnet/Router.{hh,cc}` | `traceHook`/`traceOutportForNext`、VA 类候选限制(F10) |
| `src/mem/ruby/protocol/AIGCIC_NOC-msg.sm` | `CoherenceRequestType:RELAY_PUTX` |
| `src/mem/ruby/protocol/AIGCIC_NOC-cache.sm` | Relay 事件:发 RELAY_PUTX(DataLength=rec.bytes)+ 本地完成 |
| `src/mem/ruby/protocol/AIGCIC_NOC-dir.sm` | RelayPut:静默 pop |
| `src/mem/ruby/structures/TraceScheduler.*` | §5.4 协议 |
| `src/cpu/testers/garnet_synthetic_traffic/*` | TRACE_ 模式 + §5.3 FSM(`extraData` 高 32 位 = 全局 rec id+1,低 32 位 = burst 字节) |
| `configs/example/aigcic_noc_synth_traffic.py` | `--synthetic=trace --trace-plan=<dir>/plan.txt --trace-repeat --inter-layer-cycles` |

## 6. 统计与分析口径

**ni_src 门状态(trace 模式语义,冻结):**
held rec 永不出 tester 门(F4/§0.1),因此——

| 状态 | 含义 |
|---|---|
| IDLE | 模型规定的静默:WAIT_AFTER、WAIT_SEQ(规则 b 在途等待)、burst 已注入但未到达完成 |
| XFER | burst 正在注入 |
| BUSY | 门上 out-VC credit 耗尽 = **真网络反压**(饱和探针语义保持) |

**tester 驻留统计**:`rec_wait_after / rec_wait_seq / rec_wait_retry`(周期数),
用于把 IDLE 分解为"模型静默" vs "门下反压(retry 伪装成 IDLE 的部分)"。

**TraceReplay 计数器**:`trace_relay_rewrites / relay_holds / relay_drains(+flits) /
tap_delivered(+bytes) / branch_legs`。

**逐链路 flit 计数**:`NetworkLink` 注册了 per-link 标量
`system.ruby.network.int_links<N>.network_link.flits`(链路 src/dst 从 config.ini
的 `src_node/dst_node` 取),供与 links.csv 的逐链路对账使用——规格验收门
§8.4 要求逐链路零差异,聚合口径不充分。

**可选事件日志(--trace-event-log)**:调度器逐行记录每条 rec 的全部事件
(`tick EVENT tid rec msg op stage router [detail]`,EVENT ∈ ISSUE / RELAY
(act=REWRITE/BRANCH/EJECT+cont) / ARRIVE / TAP / LEG_DISPATCH / LEG_DEFER /
WINDOW_START / WINDOW_DONE;tid 恒为全局 id)。逐行 flush(exitSimLoop 可能
绕过析构)。供 msg 粒度对账使用,不开启时零开销。

**运行时断言(零容忍)**:到达字节数 == plan.bytes(以 burst flit 计数在
InputUnit 尾 flit 处检查,panic);merge 计数恰清零且延续单次;cls 单调不降;
`rec.next != 到达 router` 即 panic;pending 计数不为负。

**msg 粒度对账工具**:`tools/trace_plan/reconcile_event_log.py <events.txt>
<window>`——逐条对照 records.csv/messages.csv 检查(A)每 rec 恰好一次
ARRIVE;(B)每 F rec 恰好实现一次、每 T/R 恰好一次 ISSUE;(C)因果:
start(rec) ≥ ARRIVE(dep) ∀ 外部 after,汇聚成员按 F5 检查(单成员改写为
F1/F4 豁许的 cut-through,不检查);(D)每 router 的 ISSUE 严格按 seq 递增、
换 msg 等前组落地(规则 a/b 的正面验证);(E)每 msg 记录集合完备;(F)
每 msg 字节(= messages.csv 每跳字节数 × 记录数)与 makespan 表。全部 ✓ 才
算 PASS,换任何新 trace 直接复用。

**分析器**:trace 模式链路 flit 直接取 garnet 原始统计(单类消息混尺寸,两尺寸模型
失效);逐链路对账 links.csv;消息延迟首次 = NI-to-NI 单包延迟,可直接测;对照
`README-NOC.md` §6 记录结论。

## 7. 已批准的修正案(用户 2025-11-06 确认;否决过的回退方案见括号)

- **[A1] 汇聚/after-hold 的实现机制 = EJECT + tester 重注入 leg**(讨论稿原设想的
  input-VC hold 方案已弃用)。理由:被弹出的流不持有任何信道,信用死锁面为零;
  门控语义与决议 (c) 完全等价。代价:每个汇聚/after-hold 点 +2 次本地口穿越与 1 次
  dir pop(不进 links.csv 口径,inter-router 链路仍精确)。
- **[A2] tap = 虚拟投递**(调度器事件 + 计数),不向本地口发副本 flit(真实 tap
  副本方案已弃用)。理由:保持 inter-router 精确的同时消除 dir 侧负载;代价:本地口
  入向比真实系统少一份 sink 流量(低估远优于此前讨论的 4–9× 高估)。

## 8. 实施顺序与验收门

1. **修复 [M1]**(merge 路径按 §5.1 规范行为),重跑编译器:四窗口 CDG 检查必须
   全过、对账必须 EXACT。
2. **构建**:`scons build/NULL_AIGCIC_NOC/gem5.opt -j <N>`(构建必须零警告新增)。
3. **冒烟**:`s1-layer-a --trace-repeat=1`,sim-cycles 取窗口估计的 ≥10×余量。
   验收:全部 rec ARRIVAL_DONE 且 WINDOW_DONE 触发;运行时断言零触发;无 panic。
4. **对账**:分析器逐链路 flit 数 vs links.csv(逐条一致,允许 ±1 flit 的计口误差
   需注释说明);ni_src 三态矩阵与时钟周期自洽(idle+xfer+busy = 运行周期 ±1)。
   **已达成(2025-11-06)**:四窗口 repeat=1/2 及 s1-a repeat=3 全部通过——全部 rec
   ARRIVAL_DONE、WINDOW_DONE 自终止、断言零触发、104 条有向链路 flit 逐条零差异;
   msg 粒度对账(事件日志 + reconcile_event_log.py)A–F 八项全部 PASS。
   过程中修复的额外缺陷:**tester 按 plan 文件序(而非 per-router seq 序)发出
   send**——违反冻结规则 F3/F4 的 seq 纪律;已修(TraceSend 增 seq 字段、按 seq
   排序并重建位置索引),修后规则 a/b 的正面验证(D_seq/D_ruleb)全部 PASS。
   F14(DATA_VNET)生效后重跑:链路对账仍逐条零差异(时序变、流量不变),
   msg 对账八项 PASS。
5. **全量**:四窗口 × `--trace-repeat` 扫描 + 报告(对照 §6 基线,写回 README-NOC 新章节)。

## 9. 禁止事项

- 永远不用 `configs/example/ruby_random_test.py` 验证(与无一致性协议根本不兼容)。
- 不得改变 `uniform_random`/`table` 模式的任何行为或既有统计口径。
- 不得为 trace 模式引入写响应、DRAM 往返、GETX/DATA。
- 不得绕过编译器的 CDG 检查或对账检查换用任何新 trace。
- 不得让 held/READY rec 提前出 tester 门(破坏 ni_src IDLE 语义)。
- 不得手编 plan.txt;不得在热路径(router wakeup / 钩子)做跨 SimObject 同步调用。

## 10. 命令参考

```bash
# 编译 plan(换 trace 必跑;CDG/对账失败即 fatal)
python3 tools/trace_plan/compile_trace_plan.py traffic/trace/noc-trace-4x6/s1-layer-a

# 构建
scons build/NULL_AIGCIC_NOC/gem5.opt -j <N>

# 冒烟 / 运行
./build/NULL_AIGCIC_NOC/gem5.opt -d log/aigcic_noc/trace/<scenario> \
    configs/example/aigcic_noc_synth_traffic.py \
    --synthetic=trace \
    --trace-plan=traffic/trace/noc-trace-4x6/s1-layer-a/_plan/plan.txt \
    --trace-repeat=1 --sim-cycles=20000000

# 分析
python3 perf_analysis/analyze_mesh_bw.py log/aigcic_noc/trace/<scenario>/stats.txt
```
