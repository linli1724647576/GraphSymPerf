# Neo4j validated runtime yield 提升设计

- 状态：设计内容已逐节批准，待书面规格复核
- 日期：2026-07-14
- 适用仓库：GraphSymPerf
- 目标 DBMS：Neo4j
- 设计路线：RulePlan 驱动的分阶段漏斗

## 1. 背景与问题定义

2026-07-13 的 Neo4j 运行完成 3 个 shard，每个 shard 得到 1000 个完整类型结果等价查询对，共 3000 对。发现阶段只有一个查询对达到 `B/A >= 2.0`，但两个独立验证会话仅复现出 `1.0476x` 和 `1.0773x`，绝对差分别为 `0.0215 ms` 和 `0.0316 ms`，低于 A/A 噪声地板，最终没有 submit-ready issue。

该结果不能直接解释为 Neo4j 缺少性能异常。真实执行路径存在以下结构性限制：

1. worker 对全部 3000 对硬编码使用 `and_excluded_middle_or_null`，配置中的规则调度、每条 CORE 规则探索下限和 `max_variants_per_seed` 没有进入真实规则选择路径。
2. 已确认的 Neo4j 强异常属于 `pattern_order_symmetry`，但本轮没有执行任何 SYM 规则。
3. 三张图均只有 128 个节点和 94–210 条关系，查询正常 runtime 多处于亚毫秒区间，倍率容易被固定开销和长尾噪声放大。
4. 发现阶段只保留跨过 ratio 门槛的候选，没有保存全部 accepted pair 的计时摘要，无法事后审计低倍率、高绝对差案例。
5. 最终验证采用 `ratio AND absolute delta`，可能遗漏稳定但主要体现为绝对时间损失的异常。

本设计的核心目标不是提高初筛候选数量，而是提高经过独立验证、归约、归约后复验和去重后形成的 submit-ready 独立 issue family 产出率。

## 2. 目标、指标与非目标

### 2.1 主指标

本设计将未加限定的 `validated_runtime_yield` 规范化为以下 submit-ready unique family 指标：

```text
submit_ready_unique_yield_per_1k
= 1000 * 新发现且去重后的 REDUCED_VALID issue family 数
  / CORE discovery accepted pairs
```

该指标只统计 fresh discovery 数据。CONTROL、已知 regression、EXPERIMENTAL、quarantined 和 infrastructure-failed 单元不进入分子或分母。

### 2.2 诊断指标

```text
validation_pair_yield_per_1k
= 1000 * independent-validation-passed query pair 数
  / CORE discovery accepted pairs
```

同时报告 applicability、accepted、Stage-1、Stage-2、validation precision、reducer survival、duplicate-family rate、timeout rate 和每 1000 accepted pairs 的运行成本。

### 2.3 非目标

- 不通过降低独立 validation 或 reduced validation 门槛制造命中率。
- 不将 PROFILE、执行计划、PMC 或开发者状态反馈用于 discovery promotion 或规则动态调度。
- 不将已知 BUG001 重新计为新发现。
- 首轮不同时调节 GDsmith clause size、label 数量和其他查询复杂度参数。
- EXPERIMENTAL 规则不进入首轮正式发现链。

## 3. 总体架构

```text
ExperimentSpec
    |
RuleRegistry -> RulePlanner -> immutable RulePlan
    |
AcceptedPairBuilder
    |
CompactRuntimeStore
    |
Stage-1 Screen -> per-cell Top-K -> Stage-2 Screen
    |
IndependentIssueValidator
    |
CandidateReducer -> Reduced Validation
    |
IssueDeduplicator -> SubmitReadyIssue
    |
YieldAggregator
```

worker 只编排单个 RulePlanEntry 的生命周期、资源隔离和错误传播，不再硬编码规则、阈值或候选写入语义。

## 4. RuleRegistry 与规则安全契约

### 4.1 RuleSpec

每条规则必须由 Registry 暴露：

```text
RuleSpec
- canonical_rule_id
- family: BOOL | SYM
- tier: CORE | CONTROL | EXPERIMENTAL | REMOVED
- submit_eligible
- proof_id
- proof_version
- purity_requirements
- supported_dialects
- required_capabilities
- allowed_site_classes
- transform_factory
```

配置只能选择规则，不能覆盖 tier、`submit_eligible`、证明版本、方言能力或安全 site。CORE 规则只有同时满足以下条件才能进入 discovery：

1. transform 实现存在；
2. 三值逻辑或结构等价证明可执行；
3. SafetyCertificate 可生成；
4. Neo4j 方言和 capability 检查通过；
5. 属性测试与动态等价测试通过；
6. `submit_eligible=true`。

### 4.2 规范规则名与历史别名

新实验统一使用规范 ID。`and_excluded_middle_or_null` 作为历史制品别名映射到 `null_complete_excluded_middle`，不能在新 RulePlan 中形成第二条规则或重复预算。

### 4.3 首轮 CORE 候选

Registry 入场审计的预期候选为：

BOOL：

- `constant_true_disjunction`
- `constant_false_conjunction`
- `null_partition_tautology`
- `null_contradiction`
- `null_complete_excluded_middle`

SYM：

- `pattern_order_symmetry`
- `predicate_order_symmetry`
- `node_order_symmetry`
- `label_order_symmetry`，仅 Neo4j capability gate 通过时启用

该列表是审计候选，不是强制凑齐的数量目标。任何候选未满足 CORE 入场条件时必须保留原 tier，不能为了扩大实验矩阵降低证明或测试要求。

### 4.4 `pattern_order_symmetry` 的 CORE 边界

CORE 版本只允许在同一个普通 MATCH clause、同一个 graph pattern 内，对逗号分隔且证明可交换的 pattern atom 重新排序。以下变换不属于该 CORE 规则：

- 交换两个独立 MATCH clause；
- 跨 OPTIONAL MATCH；
- 穿过 WITH、聚合或作用域边界；
- 改变 relationship uniqueness scope；
- 对无法证明独立性的共享变量结构排序。

已确认 BUG001 使用固定图和固定 query pair 进入 regression corpus。它只验证规则生成、计时和 validation 链没有退化。

### 4.5 CONTROL

首轮 CONTROL 为：

- `alpha_renaming_symmetry`
- `edge_notation_symmetry`

CONTROL 使用同一 Stage-1/Stage-2 测量协议，并按固定抽样进入 control validation，以检查协议是否会把预期无差异变换判为强异常。CONTROL 可以产生 `control_gate_hit` 和 `control_validation_pass` 诊断事件，但不能进入 discovery candidate 排名、issue materialization 或主指标。

CONTROL 同样必须具备可执行的等价证明、SafetyCertificate、方言能力检查和动态等价测试，只是固定为 `submit_eligible=false`。每个 CONTROL 单元对所有 Stage-2 gate hit 进行复验，最多 2 个；若没有 gate hit，则仍复验综合噪声归一化分数最高的 1 个 pair。只有达到第 9 节任一最终门槛才记为 `control_validation_pass`，因此该诊断既能估计假阳性，也不会因零 gate hit 而被机械记为成功。

## 5. RulePlan 与预算语义

### 5.1 RulePlanEntry

planner 为每个执行单元生成不可变条目：

```text
RulePlanEntry
- rule_id
- family
- tier
- graph_scale
- accepted_pair_quota
- max_generated_seed_budget
- max_variants_per_seed
- shard_seed
- proof_version
- query_timeout_seconds
- max_entry_runtime_seconds
- measurement_protocol_id
- stream: discovery | control | regression
```

正式预算单元固定为 `(rule_id, graph_scale, shard_seed)` 的 accepted equivalent pair 数量。planner 必须把目标系统 manifest 中的 query timeout、实验配置中的 entry 墙钟预算和测量协议解析进不可变 RulePlan；worker 不能运行时改写。worker 必须对每个单元独立 top-up。规则适用率不足时写入 `UNDERFILLED_RULE_BUDGET`，不得将剩余额度静默转给其他规则。

### 5.2 三条数据流

| 数据流 | 规则范围 | 可形成 submit-ready | 进入主指标 |
|---|---|---:|---:|
| discovery | CORE 且 submit eligible | 是 | 是 |
| control | CONTROL | 否 | 否 |
| regression | 已知 issue corpus | 否 | 否 |

### 5.3 分阶段预算

| 阶段 | 图规模 | 每条 CORE 规则预算 | 目的 |
|---|---|---:|---|
| P0 | 128 | 50 accepted pairs | 接线、证书、等价性、制品和 regression gate |
| P1 | 128/256/512 | 每个规则规模组合 1 个 fresh shard seed、200 accepted pairs | 比较规则与规模信号 |
| P2 | 入选组合 | 3 shards x 1000 accepted pairs | 计算 fresh 正式 yield |

若 9 条预期 CORE 全部通过，P1 总计 5400 accepted pairs。CONTROL 预算为 `2 rules x 3 scales x 50 = 300`，独立结算。

P2 扩展组合总数默认不超过 6 个，最大正式预算为 18000 accepted pairs。

### 5.4 P1 到 P2 的选择

硬门槛：

- 零不可解释类型结果 mismatch；
- proof 与 SafetyCertificate 完整；
- `accepted_pairs == accepted_pair_quota`，任何 `UNDERFILLED` 单元均不得扩展；
- 无 DBMS health event、图污染或制品缺口；
- CONTROL 没有 validation-pass；
- RulePlanEntry 未以 `RESOURCE_EXHAUSTED` 或 `INFRASTRUCTURE_FAILED` 终止。

单 query timeout 使用目标系统 manifest 中冻结的 timeout 值，RulePlanEntry 总墙钟预算使用 `max_entry_runtime_seconds`。超过总预算即进入 `RESOURCE_EXHAUSTED`。timeout rate 作为排名和成本诊断指标，不单独构成淘汰条件；单侧稳定 timeout 仍可进入专门的 timeout validation。

通过硬门槛后按以下顺序排名：

1. `validation_passed_count > 0`；
2. `validation_pair_yield` 及 Wilson 95% 置信区间下界；
3. 复验后的绝对 runtime delta；
4. 复验 ratio 和 ratio CI 下界；
5. `delta / noise_p99`；
6. applicability、accepted 成本和 timeout 率。

所有出现 validation-pass 的组合自动入选。在剩余名额中，BOOL 和 SYM 各最多保留一个零 validation-pass、但 Stage-2 信号最强的探索组合。每条规则默认只选择一个图规模；两个规模均出现独立 validation-pass 时可以同时入选。

## 6. GDsmith 图规模配置

实验 schema 增加：

```yaml
matrix:
  graph_scales: [128, 256, 512]
```

planner 将规模展开为 `ShardTask.graph_scale`。GDsmith runner 必须显式传递：

```text
--max-node-num <graph_scale>
```

每个 shard 记录请求规模、实际节点数、实际关系数、label/type 数量、索引数量和图指纹。分析使用实际图规模，不能只使用配置值。

P1 与 P2 使用不重叠的 GDsmith seed、图快照、query pair 和 measurement schedule seed。

## 7. 紧凑 runtime 制品

每个 accepted discovery pair 都写入 `pair_runtime.jsonl`，至少包含：

```text
pair_hash
rule_id
graph_scale
shard_seed
query_a_hash
query_b_hash
site_class
proof_version
safety_certificate_hash
equivalence_summary
noise_floor_id
paired samples in integer microseconds
median_a_ms
median_b_ms
median_delta_ms
median_ratio
delta_over_noise
direction_consistency
timeout_state
screen_stage
protocol_version
lifecycle_status
```

普通 pair 不保存完整结果、PROFILE 或计划。Stage-2 candidate 才升级保存完整查询文本和更完整的运行证据。accepted pair 数量必须与 runtime record 数量一一对应；写入失败使 RulePlanEntry fail-closed。

## 8. 噪声标定与二阶段筛选

### 8.1 shard 级噪声标定

每个 `(DBMS, graph_scale, shard)` 在 discovery 前使用 20 条代表性基础查询，每条执行 10 个 A/A pair，得到至少 200 个有效绝对差，计算 p95、p99、顺序偏置和长尾稳定性。样本不足、顺序偏置明显或 CONTROL 异常时，shard 不进入 discovery。

### 8.2 Stage-1

所有 accepted pair 执行：

- 2 次预热；
- 3 个随机 AB/BA pair；
- 不使用单一 ratio 硬门槛。

每个 `(rule_id, graph_scale, shard)` 建立三条排名队列：

1. `Q_abs`：按 `median_delta_ms`；
2. `Q_ratio`：按 `median_ratio`，但要求 `median_delta >= 0.5 * noise_p99`；
3. `Q_noise`：按 `median_delta_ms / noise_p99`。

三条队列各获得约三分之一名额，合并后去重：

```text
K1 = min(30, max(3, ceil(0.05 * accepted_pairs)))
```

P1 每个 200-pair 单元最多 10 个进入 Stage-2；P2 每个 1000-pair 单元最多 30 个。

单侧 timeout 且 DBMS 健康时进入独立 timeout queue；出现 health event 时进入 ReliabilityFinding 路径，不能作为 runtime issue 候选。

### 8.3 Stage-2

Stage-2 执行：

- 3 次预热；
- 12 个严格平衡 AB/BA pair；
- ratio bootstrap CI；
- paired-delta bootstrap CI。

相对差异宽召回门槛：

```text
median_ratio >= 1.5
ratio_CI_low > 1.1
median_delta > noise_p99
direction_consistency >= 0.75
```

绝对差异宽召回门槛：

```text
median_delta >= max(2 * noise_p99, 10 ms)
paired_delta_CI_low > 0
median_ratio > 1.1
direction_consistency >= 0.75
```

通过任一路径才可进入 independent validation。P1 每个规则规模单元最多验证 2 个；P2 每个 shard 最多验证 5 个。名额不要求用满。

## 9. Independent validation 与双提交门槛

所有候选必须执行：

- 两个独立 Python 进程；
- 每个 session 重新连接并重载图；
- 每 session 5 次预热、20 个平衡 AB/BA pair；
- 完整类型结果等价；
- 相同图指纹与 DBMS 指纹；
- 无 health event；
- 两个 session 同方向；
- 10000 次 bootstrap。

相对倍率提交路径：

```text
每个 session median_ratio >= 2.0
合并 median_ratio >= 3.0
ratio_CI_low >= 2.0
median_delta > noise_p99
```

强绝对差提交路径：

```text
每个 session 同方向且 median_delta > 0
合并 median_delta >= max(5 * noise_p99, 50 ms)
paired_delta_CI_low > noise_p99
合并 median_ratio >= 1.5
ratio_CI_low >= 1.2
```

`10 ms` Stage-2 阈值和 `50 ms + 5x noise` 最终绝对差门槛写入 immutable manifest，不能观察候选后修改。通过任一路径后仍必须经过 reducer 和 reduced validation。

## 10. 生命周期与失败处理

RulePlanEntry 生命周期：

```text
PLANNED
-> GENERATING
-> BUILDING_ACCEPTED_PAIRS
-> STAGE1_COMPLETE
-> STAGE2_COMPLETE
-> VALIDATION_COMPLETE
-> REDUCTION_COMPLETE
-> MATERIALIZED
-> COMPLETED
```

终止状态为：

- `COMPLETED`
- `UNDERFILLED`
- `QUARANTINED`
- `RESOURCE_EXHAUSTED`
- `INFRASTRUCTURE_FAILED`

失败语义：

| 事件 | 处理 |
|---|---|
| rule 不适用 | `RULE_NOT_APPLICABLE`，pair 拒绝并继续 top-up |
| proof 明确拒绝当前 site | `PROOF_PRECONDITION_REJECTED`，pair 拒绝并继续 top-up |
| proof/certificate 内部不变量违反 | 首次可信出现即 quarantine 对应规则单元并保存反例 |
| CORE 动态等价 mismatch | 立即 quarantine 对应规则单元并保存反例 |
| 普通 query execution error | pair 拒绝并分类 |
| 单侧 timeout、DBMS 健康 | timeout queue，不直接提交 |
| DBMS 重启、OOM、连接失效 | RulePlanEntry 作废为 infrastructure failure |
| quota 未达到 | `UNDERFILLED`，不得按完整 quota 报告 |
| runtime 制品写入失败 | fail-closed |
| reducer 破坏等价性或 runtime 信号 | `REDUCTION_INVALIDATED` |
| known regression 在其 manifest 指定的受影响版本上未复现 | 阻断 P1，但不计新发现失败 |

同一 Neo4j 实例同时只允许一个 measurement session；validation 期间暂停其他 discovery。重试写入新的 attempt 目录，不能覆盖失败证据。

已知 regression manifest 必须记录受影响的 Neo4j 版本和容器镜像指纹。P0 regression gate 在隔离实例上使用该受影响版本运行，不能要求已经修复的新版本继续表现出历史异常；目标版本上的不复现结果单独记录为版本回归状态。

## 11. Reducer、去重与 SubmitReadyIssue

候选必须依次满足：

```text
CORE + submit_eligible
-> proof/certificate
-> full typed equivalence
-> Stage-1/Stage-2
-> independent validation
-> reducer
-> reduced validation
-> regression exclusion
-> issue-family deduplication
```

只有 `REDUCED_VALID` 进入 submit-ready 主指标；`REDUCTION_PARTIAL` 只进入人工审查。

精确签名：

```text
DBMS + canonical_rule_id
+ reduced_query_A_hash + reduced_query_B_hash
+ runtime_direction
```

结构 family 签名：

```text
DBMS + canonical_rule_id
+ transformation_site_class
+ normalized_reduced_AST_pair
+ runtime_direction
```

AST 归一化允许 alpha renaming、常量抽象、label/type 占位符和不影响结构的属性名替换。精确签名相同的候选可以自动合并；只有结构签名、runtime direction、提交路径和 reduced-validation 表型均一致时才自动合并为同一 family，任一项不确定时保持为不同 family。不得使用推测性 root-cause label 或 PROFILE plan 决定自动合并。解释证据只能在 family 形成后辅助人工审查。

## 12. 产物与报告

每次运行至少输出：

- `rule_plan.json`
- `pair_runtime.jsonl`
- `cell_summary.json`
- `pilot_selection.json`
- `validation/`
- `reduction/`
- `issue_families.json`
- `yield_report.json`
- `yield_report.md`
- `regression_report.md`
- `claim_evidence_table.md`

报告按总体、BOOL/SYM、rule、graph scale、shard seed 展开。查询对 yield 使用 Wilson 95% 置信区间；unique family yield 按 shard/seed 做分块 bootstrap。零命中单元必须报告区间和预算，不得省略。

## 13. 测试与验收

### 13.1 单元测试

- Registry canonical ID、legacy alias 和 tier 不可被配置覆盖；
- RulePlan 的规则、规模、shard 笛卡尔展开与 seed 隔离；
- worker 不再硬编码规则；
- GDsmith 命令包含正确的 `--max-node-num`；
- accepted pair 与 runtime record 一一对应；
- 三队列 Top-K 选择确定且可复算；
- relative/absolute 双门槛边界；
- CORE mismatch 自动 quarantine；
- regression/control 不进入 yield；
- exact/structural family 去重。

### 13.2 属性测试

- BOOL 三值逻辑；
- SYM AST 等价与变量作用域；
- `pattern_order_symmetry` 不跨 MATCH、WITH 或 OPTIONAL MATCH；
- 随机小图上的动态结果等价。

### 13.3 集成测试

- fake adapter 跑通 RulePlanEntry 生命周期；
- 原子 checkpoint 恢复；
- 制品写入失败、timeout、DBMS restart 和 underfilled 均 fail-closed。

### 13.4 Neo4j P0 gate

- 每条 active CORE 完成 50 accepted pairs；
- 零动态等价 mismatch；
- accepted pair 与 runtime record 数量一致；
- CONTROL validation-pass 为 0；
- 已知 BUG001 在其受影响的固定 Neo4j 版本上通过新 validation 协议；
- 无重启、OOM 或图指纹漂移；
- 所有 RulePlanEntry 都有明确终态。

## 14. 成功标准与结论纪律

工程成功：配置声明与真实执行一致，规则、规模、预算、制品和指标可审计。

发现成功：P2 至少产生一个 fresh `REDUCED_VALID` 独立 issue family，且 `submit_ready_unique_yield_per_1k > 0`。

比较成功：只比较同一新协议下、使用 fresh P2 seeds 的规则和规模。旧 3x1000 结果只能作为历史参考，不能作为严格统计 baseline。

若 P2 仍为零命中，应报告可信负结果和置信区间，不能继续降低门槛直至出现 issue。
