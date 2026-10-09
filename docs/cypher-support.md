# Cypher 扩展实测记录（2026-10-09）

新增 ArcadeDB、Apache AGE、LadybugDB 三个 Cypher 实验目标后，三个正式轮次共记录 **144 个输入，134 个有效案例，10 个静态能力限制案例**。语义保持对称查询（symmetry）没有性能候选；与参考查询比较（reference）得到 **5 个候选案例**。其中 LadybugDB 的 P16 差异包含预期的长列表构造工作，不能登记为遗漏优化；其余 4 个案例尚未完成人工根因核验。**本轮新登记的遗漏优化问题为 0，未新增 `BUG list` 报告。**

本文件记录具体版本、具体输入和本轮观测，不将有限实验推广为数据库对整个 Cypher 或所有 Pattern 的支持承诺。Gremlin 不属于这轮 Cypher 实验，其后续接入证据见 [Gremlin 实测覆盖](gremlin-support.md)；GQL 仍只有扩展接口。

## 环境和版本边界

| 目标 | 实际响应版本 |
| --- | --- |
| ArcadeDB | `ArcadeDB/26.10.1-SNAPSHOT (build ebdf527641ba444364bb9e29aa020e9e04824862/1788464335463/main)` |
| Apache AGE | `Apache AGE/1.7.0 PostgreSQL/18.1 (Debian 18.1-1.pgdg13+2)` |
| LadybugDB | `LadybugDB/0.21.2` |

上述值来自各正式轮次的 `run.json` 和实际执行记录中的 `database_version`。静态门控在连接数据库前拒绝的记录保留 `unknown`，不伪装成已经执行并读到了服务器版本。ArcadeDB 是本次实际运行的快照构建，不能用 `latest` 标签代替固定镜像摘要，也不能把其观测推广到同名未来快照；镜像和运行环境明细随 [环境归档](../results/cypher-expansion-20261009/environment.json) 保存。

新增容器目标使用的实际镜像摘要为：

- ArcadeDB：`arcadedata/arcadedb@sha256:c45a235d7e625199a047249b873ca6f76faa325edee54e9f0db292827110da73`。
- Apache AGE：`apache/age@sha256:4241e2d8bb86a6b2ea44e9ad06c73856e12b209de295124603a599dd7feb70eb`。

现有 Neo4j、Memgraph、FalkorDB 与新增三库一起执行了六库集成回归。性能发现轮次只在新增三库运行，不能把六库回归测试数量解释为六库性能发现样本量。

## 正式轮次与统计口径

三个轮次均选择 24 个 Pattern，每个 Pattern 取两个规模：`scale=100` 和 `scale=1000`。查询种子为 `17`，图状态种子为 `0`，`complexity=0`，使用原始图状态。P7 与 S3 未纳入本轮输入，不计入 48 个案例的分母。

| 目标 | 输入 | 有效案例 | `no_candidate` | `not_confirmed` | `candidate` | `unsupported_feature` | 对称查询候选 | 参考查询候选案例 / 查询对 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ArcadeDB | 48 | 48 | 42 | 4 | 2 | 0 | 0 | 2 / 2 |
| Apache AGE | 48 | 40 | 36 | 2 | 2 | 8 | 0 | 2 / 3 |
| LadybugDB | 48 | 46 | 44 | 1 | 1 | 2 | 0 | 1 / 1 |
| 合计 | 144 | 134 | 122 | 7 | 5 | 10 | 0 | 5 / 6 |

这里的有效案例是结果和构造状态验证完成后进入性能测试、最终状态为 `no_candidate`、`not_confirmed` 或 `candidate` 的输入。`not_confirmed` 表示筛选差异没有通过确认条件，不是数据库缺陷。`candidate` 仅表示通过当前性能预言，仍不等于已证实的遗漏优化。

候选统计读取 `result.comparisons` 与完整的 `result.reference_comparisons`，按每条记录的 `pair_kind` 判断比较类型，不依据查询对名称或顶层最快/最慢摘要推断。ArcadeDB 的 S7 在 `ab` 条目中命中，但该条目的 `pair_kind=reference`，B 本身是参考区域替换查询，因此计入参考比较、不计为对称查询候选。AGE 的 P5 同一输入同时命中 A/ref 与 B/ref，计作 **1 个候选案例、2 个参考查询对**。不同输入、比较对和语言再次命中同一根因，不应生成额外 BUG ID。

本轮采用默认确认配置：3 次发现预热、6 组发现测量；候选进入 2 次独立确认，每次 5 次预热、20 组配对测量和 10 组同查询控制。筛选比值为 2；确认要求中位比值至少 3、比值置信区间下界至少 2、方向一致性至少 0.75，并通过控制噪声条件。超时为 10 秒，结果上限为 10,000 行，`collect_plans=true`。上述设置和每次测量保存在相应 `result.json` 中；所有最终候选的完整参考比较均保存了两次确认及每次 20 组样本。

正式原始记录：

- [ArcadeDB：48 个输入](../results/cypher-expansion-20261009/performance-arcadedb/20261009T102339Z-15fc37/)
- [Apache AGE：48 个输入](../results/cypher-expansion-20261009/performance-age/20261009T095449Z-4cb96f/)
- [LadybugDB：48 个输入](../results/cypher-expansion-20261009/performance-ladybug/20261009T095737Z-fa135c/)

ArcadeDB 正式统计使用修正后的持久 HTTP 连接和串行执行轮次。先前 HTTP 实现下的轮次已移至 [旧传输实现归档](../results/cypher-expansion-20261009/pre-transport-fix/)，不并入正式统计。一次并发执行互相干扰的失败轮次另行隔离，并保存 [排除原因](../results/cypher-expansion-20261009/interfered-runs/20261009T101444Z-66821f/EXCLUDED.json)；它不计入有效案例、候选或正式性能结论。

## 各 Pattern 的有效覆盖

表中数字表示本轮两个规模中有效执行的案例数，**不是该 Pattern 所有种子、结构或表达式都受支持**。“门控”表示本批输入被能力检查拒绝；“未选”表示本轮没有安排该 Pattern。

| Pattern | ArcadeDB | Apache AGE | LadybugDB |
| --- | ---: | ---: | ---: |
| P1 | 2 | 2 | 2 |
| P2 | 2 | 2 | 2 |
| P3 | 2 | 2 | 2 |
| P4 | 2 | 2 | 2 |
| P5 | 2 | 2 | 2 |
| P6 | 2 | 2 | 2 |
| P7 | 未选 | 未选 | 未选 |
| P8 | 2 | 0：门控 | 2 |
| P9 | 2 | 2 | 2 |
| P10 | 2 | 2 | 2 |
| P11 | 2 | 2 | 2 |
| P12 | 2 | 0：门控 | 2 |
| P13 | 2 | 2 | 2 |
| P14 | 2 | 2 | 2 |
| P15 | 2 | 0：门控 | 2 |
| P16 | 2 | 0：门控 | 2 |
| S1 | 2 | 2 | 2 |
| S2 | 2 | 2 | 2 |
| S3 | 未选 | 未选 | 未选 |
| S4 | 2 | 2 | 2 |
| S5 | 2 | 2 | 2 |
| S6 | 2 | 2 | 2 |
| S7 | 2 | 2 | 0：门控 |
| S8 | 2 | 2 | 2 |
| S9 | 2 | 2 | 2 |
| S10 | 2 | 2 | 2 |

Apache AGE 本批输入的保守限制为：P8 `relationship_alternatives`、P12 `label_rebinding`、P15 `boolean_expression_coalesce`、P16 `all`。这些名称来自各拒绝记录中的编译原因。尤其 P12、P15 涉及该种子实例化后的具体形态，不能据此宣称 AGE 不支持任意 P12 或 P15。

LadybugDB 本批 S7 由 `mixed_type_coalesce` 门控：BOOL 前缀与整数 COUNT 混用会改变实际返回类型。该检查保留严格结果类型验证，不把字符串 `"True"` 转成布尔值，也不降低等价要求以增加覆盖。其余种子的兼容边界仍需逐例验证。

## 候选与问题登记边界

以下比值来自通过确认的具体参考比较，按候选案例列出。执行计划用各引擎原生字段保存，不把 PostgreSQL 缓冲区指标或其他引擎输出称为 Neo4j DB Hits。

| 目标 / Pattern / 规模 | 命中查询对 | 中位时间比值 | 当前判断 |
| --- | --- | ---: | --- |
| ArcadeDB / P4 / 1000 | B/ref | 7.04 | 参考查询性能候选；未人工核验根因 |
| ArcadeDB / S7 / 1000 | A/B，B 是参考查询 | 5.96 | 参考查询性能候选；未人工核验根因 |
| Apache AGE / P4 / 1000 | A/ref | 4.08 | 参考查询性能候选；未人工核验根因 |
| Apache AGE / P5 / 1000 | A/ref；B/ref | 4.19；4.98 | 同一案例，两条比较命中；未人工核验根因 |
| LadybugDB / P16 / 1000 | A/ref | 16.02 | 涉及完整长列表构造；不能登记为遗漏优化 |

P16 的原查询与参考查询具有不同列表构造量。性能比值即使稳定，也不能证明优化器遗漏了应当适用的规则。其他四个案例还需要解释哪些工作可以避免、优化为何应适用，并排除既有策略限制、测量或缓存影响；在证据齐备前保持候选状态，不进入 `BUG list`。

候选的完整样本和计划：

- [ArcadeDB P4](../results/cypher-expansion-20261009/performance-arcadedb/20261009T102339Z-15fc37/0026-P4-n1000-5913364e/result.json)
- [ArcadeDB S7](../results/cypher-expansion-20261009/performance-arcadedb/20261009T102339Z-15fc37/0024-S7-n1000-59dccdee/result.json)
- [Apache AGE P4](../results/cypher-expansion-20261009/performance-age/20261009T095449Z-4cb96f/0026-P4-n1000-03661b7a/result.json)
- [Apache AGE P5](../results/cypher-expansion-20261009/performance-age/20261009T095449Z-4cb96f/0020-P5-n1000-fb33f405/result.json)
- [LadybugDB P16](../results/cypher-expansion-20261009/performance-ladybug/20261009T095737Z-fa135c/0002-P16-n1000-365b12a4/result.json)

## 另行隔离的 LadybugDB 语义诊断

初次较复杂输入实验中的 LadybugDB 0.21.2 S4 出现结果不一致。原生驱动直接执行的最小复现只需一个节点：内层 `CASE WHEN n.id=1 THEN n.id=1 ELSE null END` 返回 BOOL `true`，直接作为外层 CASE 条件时却选择 ELSE，返回整数 `3`；将内层条件先投影为变量再使用时返回整数 `7`。三种查询各重复五次结果稳定。原 S4 两侧返回整数 `12` 和 `3`，均不等于构造参考值 `7`。

这是一处单独保存、待开发者确认的**逻辑问题候选**，没有进入性能比较或遗漏优化清单，也没有修改参考预言去消除差异。正式轮次使用 `complexity=0`，不会因此把上述嵌套 CASE 输入算作已通过。

同次 S7 诊断中的混合类型 COALESCE 返回 STRING `"True"`；同类型 BOOL/BOOL 和 INT/INT 对照分别返回正确的 BOOL 和 INT。后续静态门控据此拒绝 BOOL/INT 混合输入。这是当前目标的类型适配边界，不登记为遗漏优化。

原生类型、实际结果、最小复现和 PROFILE 见 [LadybugDB 诊断记录](../results/ladybug-diagnostics-20261009/README.md)及 [嵌套 CASE 原始证据](../results/ladybug-diagnostics-20261009/nested-case-minimal.json)。

## 分组回归验证

以下数值来自本次日志；最终提交前应以归档日志最后一行刷新，避免把旧快照当作新增测试后的总数。

| 验证组 | 日志结果 | 覆盖边界 |
| --- | --- | --- |
| [六库集成回归](../results/cypher-expansion-20261009/validation/six-dbms-integration-final.log) | 116 passed，22 skipped | 六种数据库的实际执行；跳过项不计为有效覆盖 |
| [Neo4j 回归](../results/cypher-expansion-20261009/validation/neo4j-regression.log) | 75 passed | 现有目标、图状态与参考查询回归 |
| [离线回归](../results/cypher-expansion-20261009/validation/offline-final.log) | 1039 passed，213 deselected | IR、生成、变换、能力门控、保存重放和统计；不冒充实际数据库实验 |

持久 HTTP 传输修正后，ArcadeDB 额外实际集成回归为 **23 passed**，随本次验证日志归档保存。

日志随 [实验归档目录](../results/cypher-expansion-20261009/) 保存。离线与集成测试验证代码行为；它们本身不证明候选属于数据库优化缺陷。新目标的验证、性能候选和问题登记始终使用不同统计口径。

汇总工具读取作用域内 `EXCLUDED.json` 的显式排除标记；`include_in_analysis=false` 下的结果及运行清单不参与统计，不根据目录名称推断。小图语义探测、旧传输计时和受并发干扰的轮次均已排除。最终 [summary.csv](../results/cypher-expansion-20261009/summary/summary.csv) 核算为 144 输入、134 有效、5 个参考比较候选、0 个人工登记 BUG。

既有 Neo4j 报告基线也已重放：P3 为 18／90,020 DB Hits，P4 为 800,013／20 DB Hits，结果保持一致。400,000 条噪声边采用 20,000 条一批构造以满足测试库单事务内存上限，图状态和原查询保持一致。[完整原生证据](../results/cypher-expansion-20261009/validation/existing-issues-revalidated.json)。

最终日志：[离线回归](../results/cypher-expansion-20261009/validation/offline-final.log)、[六库集成](../results/cypher-expansion-20261009/validation/six-dbms-integration-final.log)、[持久 HTTP 传输补测](../results/cypher-expansion-20261009/validation/arcade-transport-integration-final.log)、[144 份存档完整性检查](../results/cypher-expansion-20261009/validation/archive-replay-integrity.json)。
