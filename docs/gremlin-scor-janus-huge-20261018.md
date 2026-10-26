# S1–S10 接入 JanusGraph／HugeGraph：接口验证与有限扩规模记录

状态快照：2026-10-19 15:11（Asia/Shanghai，UTC+8）。JanusGraph 已完成 164 个唯一原计划输入、有限缩减与独立重放、五组原始统计审计及精确空库复现，登记两份本地报告：[S5／BUG004](../BUG%20list/JanusGraph/S5/BUG004.md)、[S6／BUG005](../BUG%20list/JanusGraph/S6/BUG005.md)。S8 按相同邻接预装载机制并入 S6 报告，S5 尾部 limit(0) 因噪声门槛未过而不报告。性能控制器和本轮专用复现容器均已停止；独立报告审稿与最终原生证据复核均已通过，本轮任务已完成。HugeGraph 保持用户取消状态，未提交上游。

**本轮有效覆盖为 150 个主输入＋14 个条件输入＝164 个唯一原计划输入：133 项 `no_candidate`、26 项 `not_confirmed`、5 项 `timing_observation`。另四个 S5 条件槽位明确未执行，遗漏槽位为零，所有未确认记录均保留。175 个物理存档是 164 个原槽位执行＋10 次有限验证＋一次旧外部休眠中断，不是 175 个独立输入或缺陷。** 三个最终代表各自的 40 对普通样本已独立复算原门槛；本地登记两份报告，独立跨库根因数量仍未知。门禁后冻结源码、既有 Pattern、测量协议与原条件预算均未改变。

## 范围与方法边界

本轮只接入仓库已有的 S1–S10 模板，复用 `instantiate → mutate → compile/run`。不新增 Pattern、中间表示（Intermediate Representation，IR）节点、变换规则、图配方或 Native 性能探针。手工语义检查图只作为独立结果校验基准，不计入 Pattern 性能贡献。

当前执行范围按最新用户要求收窄为 JanusGraph；HugeGraph 的既有适配代码、门禁与旧 BUG 保留，不为本轮取消的测试补测或强行形成结论。范围变化不改变 JanusGraph 的 Pattern、种子、规模、统计阈值或结果验证要求。

S1–S10 的源模板、变换器与图态定义分别来自 `Src/generator.py`、`Src/mutator.py`、`Src/state.py`。本轮起点记录于 [source-baseline.json](../results/gremlin-scor-janus-huge-20261018/source-baseline.json)，包括此前 P3／P13／P16 门禁的批准源码散列。v4 与 v5 门禁记录的 `generator.py`、`mutator.py`、`state.py`、`ir.py`、`main.py` 散列均与该方法基线一致。v4 适配修改限于目标能力名单、属性名称映射、缺失边属性的原生模式声明和精确错误分类；其冻结散列保存在 [v4 interface-gate.json](../results/gremlin-scor-janus-huge-20261018/interface-gate.json)。v5 仅校正 EXPLAIN／PROFILE 的结果上限包装，普通 execute、Pattern、图配方与测量协议保持原定义，并已为修改后的源码重新完成门禁与冻结，见 [v5 interface-gate.json](../results/gremlin-scor-janus-huge-20261018-v5/interface-gate.json)；两轮通过数分别来自各自完整执行。当前 JanusGraph 继续使用该 v5 冻结版本，`Src/executor.py` 的 SHA-256 为 `d760170f35659de834af7c83d4407bea2948cb69a029a31f44906cd3d0a5fc5e`，原 429 项门禁仍有效。

| 模板 | 现有受限机制 | 必须保持的语义 |
|---|---|---|
| S1 | OR 的吸收项跳过图计数或存在性分支 | TRUE／FALSE／NULL 的三值逻辑 |
| S2 | AND 的吸收项跳过图计数或存在性分支 | FALSE 阻断后续分支，NULL 不等于 FALSE |
| S3 | 顶层条件查询 | 仅 TRUE 选择 THEN；FALSE／NULL 选择 ELSE；保留完整结果多重集 |
| S4 | 标量 CASE | 按条件选择分支，NULL 条件选择 ELSE |
| S5 | 恒假过滤或 LIMIT 0 | 返回空多重集 |
| S6 | 常量投影和 LIMIT 1–3 | 保留重复常量行与实际限制行数 |
| S7 | COALESCE 的非 NULL 前缀 | 0、FALSE、空字符串均属于非 NULL |
| S8 | 有见证的 EXISTS | 保留根结果多重性，不复制见证行 |
| S9 | 空上游抑制独立后续匹配 | 后续匹配不能从空输入自行产生结果 |
| S10 | 恒空集合操作的既有适配 | 沿用 DISTINCT 与 NULL-safe EXISTS／NOT EXISTS；不称为数据库原生集合算子 |

三个先前扩展的 P 模板保持既有边界，P7 仍跳过。此前 ArcadeDB 的 S1–S10 结果单独保存在 [ArcadeDB 续测记录](gremlin-p10-scor-20261018.md)，不能作为两库接口通过、性能通过或根因已确认的证据。

## 目标环境与执行隔离

本轮使用新建、允许重置的专用容器，数据从空存储开始；仅复用既有测试容器配置，不复制其图数据。真实启动后的版本分别为 JanusGraph 1.1.0 和 Apache HugeGraph 1.7.0。镜像固定为：

- JanusGraph：`janusgraph/janusgraph@sha256:75f57aff4b152ca86b4cdaeefb5335154d7fad412cffef75fa44c7504adc96d4`。
- HugeGraph：`hugegraph/hugegraph@sha256:077b89e1f2d5e228d99598bdb537161fb2e8a7b5abedf4724d97cf4de11529ba`。

服务连接仅绑定本机回环地址；原双目标门禁和性能阶段串行执行，后续按用户要求只续测 JanusGraph。容器创建记录、空库数量、实际版本、镜像、进程与配置散列分别位于 [v4 runtime-environment](../results/gremlin-scor-janus-huge-20261018/runtime-environment/) 和 [v5 runtime-environment](../results/gremlin-scor-janus-huge-20261018-v5/runtime-environment/)。配置要求 `dedicated=true`、`allow_reset=true`，普通查询使用 Gremlin，读取完整结果并保留类型与重复行。

语义／接口失败不会变成性能阴性结果。原始失败记录保留，修复后使用新的验证记录；如果源码改变，旧版本上的通过项不能自动合并为最终完整门禁。

## 已发现并修复的 JanusGraph 适配问题

第一轮离线回归为 **1,446 passed，1,080 deselected**。JanusGraph 在 68 项独立语义检查通过后，第一项生成模板的建模式阶段失败，服务器明确返回 `Name cannot be in protected namespace: key`。S 图原本含有逻辑属性 `key`；不能为绕开目标限制而删除该属性、改名 IR 属性或换图配方。失败记录见 [首轮接口日志](../results/gremlin-scor-janus-huge-20261018/interface-janusgraph.txt) 和 [失败输入](../results/gremlin-scor-janus-huge-20261018/interface-janusgraph/pattern-S1-s0-c0.json)。

适配层补充了可逆的逻辑属性名到物理属性名映射：JanusGraph 保护名称及其大小写变体编码到 `_gsp_property_` 前缀，原本带此前缀的逻辑名称再加一层前缀，避免碰撞。例如：

| 逻辑属性 | JanusGraph 物理属性 |
|---|---|
| `key` | `_gsp_property_key` |
| `Key` | `_gsp_property_Key` |
| `_gsp_property_key` | `_gsp_property__gsp_property_key` |

映射应用于建模式、顶点／边属性装载、索引、查询中的属性访问和状态检查；列名、字符串值、布尔值、IR、GraphSpec、顶点／边拓扑保持原定义。独立映射检查同时覆盖保护名称、大小写、编码前缀、缺失属性、关系属性、并行边、索引和文字值，检查物理图解码后与逻辑图相同。这个名字适配是执行接口修复，不能作为数据库优化遗漏报告。

修复后第二轮离线回归为 **1,476 passed，1,081 deselected**。JanusGraph 在新建空库容器上完成 **339 passed**，包括 270 个现有 Pattern 输入、68 个独立语义检查和 1 个原生属性映射检查。证据见 [v2 测试日志](../results/gremlin-scor-janus-huge-20261018/interface-janusgraph-v2.txt)、[逐项摘要](../results/gremlin-scor-janus-huge-20261018/interface-janusgraph-v2/validation-summary.json)、[物理映射记录](../results/gremlin-scor-janus-huge-20261018/interface-janusgraph-v2/native-property-mapping.json)。此结论限定于 v2 对应源码；后续修复若改变共同执行代码，须复核最终门禁。

## HugeGraph 缺属性修复与原生查询边界

HugeGraph v2 在 **68 passed** 后停止于同一个独立属性映射检查。服务器返回的错误为：`GSP_PROP_MAP` 边类型声明的非可空属性未全部提供；缺失项包含 `vertex`、`edge`、`element`、`property`、`label` 及编码前缀属性。该检查图故意含有缺失属性，用于验证 NULL 和属性名透明性；适配层创建的原生边模式当时拒绝其中一条边。

适配器已在 NORMAL、PARENT、SUB 边标签上将用户属性声明为可空，必需的 `_gsp_edge_ordinal` sort key 继续必填；没有用户边属性时，原创建链文本保持不变。此项修复保留缺属性返回 NULL 的既有 IR 语义，不改变 S 图配方。第三轮已通过完整物理映射检查。接口装载错误不计为优化遗漏。证据见 [HugeGraph v2 日志](../results/gremlin-scor-janus-huge-20261018/interface-hugegraph-v2.txt)、[完整失败结构](../results/gremlin-scor-janus-huge-20261018/interface-hugegraph-v2/native-property-mapping.json) 与 [串行门禁状态](../results/gremlin-scor-janus-huge-20261018/gate-progress-v2.json)。


第三轮 JanusGraph 339 项再次通过；HugeGraph 119 项通过后，S1 seed=9 的原查询 `hasLabel(ScorRoot).has(uid,eq(1))` 被拒绝，原因是既有 schema 只有 `ScorRoot.id` 成本索引。固定 1.7.0 源码在没有匹配属性索引时直接抛出 `NoIndexException`，没有保留原 `.has` 查询的自动扫描配置。没有额外建 uid 索引、改为不透明 filter 或移除优化策略。[原生拒绝](../results/gremlin-scor-janus-huge-20261018/interface-hugegraph-v3/pattern-S1-s9-c0.json)、[固定版本边界审查](../results/gremlin-scor-janus-huge-20261018/source-audit/hugegraph-unindexed-uid.md)。

执行器只将这类精确的 HugeGraph 错误分类为 `unsupported_feature`，保留原始消息、错误码和输入。接口门禁逐输入记录，不禁用整条 Pattern 或整个种子；未选分支可能使同样包含 uid 的其他查询正常通过。第四轮把接口种子扩大到与性能阶段相同的 12 个，按 complexity=0 实际检查和编译哈希筛选性能输入；不支持项不进入性能阴性统计。v4 离线回归为 **1,516 passed、1,171 integration deselected**，见 [v4 日志](../results/gremlin-scor-janus-huge-20261018/offline-tests-v4.txt)；这不是 v5 新源码的回归结论。

第四轮旧源码结果为 **JanusGraph 429 passed、0 unsupported；HugeGraph 301 passed、128 unsupported_feature**，两库当时均没有剩余未分类的接口失败。逐项证据见 [JanusGraph v4 摘要](../results/gremlin-scor-janus-huge-20261018/interface-janusgraph-v4/validation-summary.json)、[HugeGraph v4 摘要](../results/gremlin-scor-janus-huge-20261018/interface-hugegraph-v4/validation-summary.json)。门禁 JSON 中的 `performance_started=false` 记录的是该门禁完成时的状态；[v4 matrix-progress.json](../results/gremlin-scor-janus-huge-20261018/matrix-progress.json) 记录随后矩阵因诊断包装问题停止。v5 的通过数由新的独立门禁产生，见下节，不能由旧轮次转移。

## 语义与接口检查内容

第四轮每库目标门禁扩大为 **429 项**，其中：

- **360 个生成模板输入**：S1–S10 × 12 个种子 `0,1,2,3,6,9,12,18,27,36,54,81` × 复杂度 0／1／2。规模为小图，只验证已有机制与接口。
- **68 项独立语义检查**：条件查询的 TRUE／FALSE／NULL、NULL 结果、重复行、空分支、列顺序、完整三值布尔真值表、CASE、COALESCE 假值、NULL-safe 成员关系、LIMIT 与空输入、EXISTS／NOT EXISTS，以及未选图分支的工作量。
- **1 项原生属性映射检查**：保护名称、大小写和前缀的单射映射、缺失属性、真实顶点／边属性、并行边、原生索引及查询读取。

每个生成模板输入检查构图、原成本事实、完整类型敏感结果多重集、真实原生分析计划（PROFILE）与解释计划（EXPLAIN），随后重新建立客户端连接、独立重建图并再次检查；最后执行存档写入／读回和编译一致性核对。重新建立客户端连接不等于重启数据库服务器。独立语义检查直接保留实际结果行；这批小图输出不因最大返回行数而截断。

未选分支工作量由当前目标原生计划的具体步骤和计数字段检查，不能仅凭最终结果相同推断已短路。接口测试中的 PROFILE 时间用于诊断，不能作为普通性能耗时或倍率。原生 PROFILE 未暴露某个分支或完整计数时，记录 `not_exposed`，不虚构零工作。原始失败及不支持记录分别保留，不与最终可执行输入混合统计。

## 诊断遍历包装差异与 v5 重验证

v4 的 `GremlinHTTP.execute` 向原遍历追加 `.limit(max_rows + 1).toList()`，本轮即 `.limit(10001).toList()`；EXPLAIN／PROFILE 当时没有追加这个相同的上限。S5 原查询自身已有 `limit(0)` 时，固定 TinkerPop 的范围合并策略可能把相继的两个范围合并成 `NoneStep`，它与未加结果上限的诊断计划并非相同执行形状。因此，旧 PROFILE 中仅有一条邻接输出，不能直接推出普通计时遍历只消费了一条邻接输出；也不能据此作预取量、全邻域物化或磁盘 I/O 的定量归因。具体源码证据与 v4 当时尚待实际有界计划验证的边界见 [S5 审查第 0 节](../results/gremlin-scor-janus-huge-20261018/source-audit/hugegraph-s5-first-pull.md#0-必须先对齐实际计时文本额外-cap-可触发-nonestep)；后续 v5 小图验证见下文。

为继续满足“先验证接口，再扩大规模”，v4 主矩阵已在扩规模之前主动停止。修复限于使 EXPLAIN／PROFILE 诊断的原始遍历加上与普通 execute 完全相同的结果上限，普通计时路径、查询对、图、种子、请求限制与统计门槛不变。v5 使用新的结果目录 `results/gremlin-scor-janus-huge-20261018-v5` 和空存储重新验证。v4 的普通计时、未加上限的原生诊断及未完成输入继续保留并明确标注版本，不覆盖原档。

v5 新门禁结果为 **JanusGraph 429 passed、0 unsupported，HugeGraph 301 passed、128 unsupported_feature**，对应真实接口测试总时长分别为 133.45s、600.84s；这些是整套测试时长，不是普通查询性能。离线回归为 **1,528 passed、1,171 integration deselected**。HugeGraph 原 PARENT／SUB 边身份与轨迹接口回归另有 **1 passed**。证据见 [v5 冻结门禁](../results/gremlin-scor-janus-huge-20261018-v5/interface-gate.json)、[JanusGraph 日志](../results/gremlin-scor-janus-huge-20261018-v5/interface-janusgraph-v5.txt)、[HugeGraph 日志](../results/gremlin-scor-janus-huge-20261018-v5/interface-hugegraph-v5.txt) 与 [原边接口回归日志](../results/gremlin-scor-janus-huge-20261018-v5/legacy-parent-interface.txt)。v5 门禁 JSON 的 `performance_started=false` 是门禁结束时的快照；当前 completed 状态以 [v5 根进度](../results/gremlin-scor-janus-huge-20261018-v5/progress.json) 为准，[recovery/progress.json](../results/gremlin-scor-janus-huge-20261018-v5/recovery/progress.json) 保留性能控制器正常结束、等待人工整合的快照，[recovery/matrix-progress.json](../results/gremlin-scor-janus-huge-20261018-v5/recovery/matrix-progress.json) 记录恢复矩阵，最终覆盖核对另叠加尾部原槽位，[v5 原 matrix-progress.json](../results/gremlin-scor-janus-huge-20261018-v5/matrix-progress.json) 保留停止历史，用户取消前的 HugeGraph 进度另存为 [before-janus-only-matrix-progress.json](../results/gremlin-scor-janus-huge-20261018-v5/before-janus-only-matrix-progress.json)。

v5 小图 N=3、S5 seed=0 的有界原生计划已在两库实际确认“双 Range → NoneStep”：初次与再次建图均从 HIGH 邻接输出 12 条、REF 输出三条，再得到空结果。[小图计划校准](../results/gremlin-scor-janus-huge-20261018-v5/source-audit/S5-bounded-plan-calibration.md) 保留原生 EXPLAIN 的优化阶段及 PROFILE 计数，将旧源码预测落实为这个受检小图的执行证据。12／3 是步骤输出量，不能改名为磁盘读取次数，也不能取代大图普通时间、原阈值确认或最终报告同图计划。两库该形状可能共用同一 TinkerPop 原因族，不能自动计为两个独立根因。

## 有限尺度与种子矩阵

规划与逐输入列表保存在 [v5 planning](../results/gremlin-scor-janus-huge-20261018-v5/planning/)，沿用原有限候选池。性能阶段只用现有复杂度 0 模板，固定 `data_seed=0`、`state_variant=original`。候选查询种子由原来的 9 个扩大为 **12 个**：`0,1,2,3,6,9,12,18,27,36,54,81`。按每个 Pattern 的完整 IR 查询对散列保留第一个代表，避免把相同查询重复计为新增模板。

| Pattern | 每尺度精确 IR 对 | 编译后含绑定查询对 | 主阶段输入／库 |
|---|---:|---:|---:|
| S1 | 10 | 9 | 20 |
| S2 | 10 | 9 | 20 |
| S3 | 8 | 7 | 16 |
| S4 | 10 | 8 | 20 |
| S5 | 5 | 5 | 10 |
| S6 | 7 | 7 | 14 |
| S7 | 8 | 8 | 16 |
| S8 | 6 | 6 | 12 |
| S9 | 5 | 4 | 10 |
| S10 | 6 | 5 | 12 |
| 合计 | **75** | **68** | **150** |

当前 JanusGraph 主阶段仍为 **N=1,000／10,000 × 去重代表，共 150 输入**。原双目标计划为每库 150、两库共 300 个输入，其中 HugeGraph 的剩余部分已取消。原九种子在固定尺度只有 58 个精确 IR 对、51 个含绑定编译查询对；本轮分别增加 17 个代表。不同 IR 编译成相同查询时仍保留 IR 输入并披露编译后计数，计划输入数不等于独立语义变体数。

当前 JanusGraph 条件扩规模仅为 **S5／S6／S8，N=100,000**，保留原先各自同一批去重代表，分别 5／7／6 输入，合计最多 **18 输入**。原双目标的 36 个条件计划输入和两个阶段共 336 个计划输入仍保存在 [v5 planned-cases.json](../results/gremlin-scor-janus-huge-20261018-v5/planning/planned-cases.json)。下表是用户收窄范围前的原生支持筛选结果，依据见 [v5 eligible-cases.json](../results/gremlin-scor-janus-huge-20261018-v5/eligible-cases.json)；HugeGraph 行及双目标合计只保留作历史，不再表示待执行工作：

| 目标 | 主阶段计划 | 主阶段可执行 | 主阶段不支持、未测 | 条件扩规模计划 | 条件扩规模可执行上限 | 条件扩规模不支持、未测 |
|---|---:|---:|---:|---:|---:|---:|
| JanusGraph | 150 | 150 | 0 | 18 | 18 | 0 |
| HugeGraph | 150 | 104 | 46 | 18 | 11 | 7 |
| 合计 | **300** | **254** | **46** | **36** | **29** | **7** |

这些数量表示经过 v5 原生门禁筛选的输入，实际完成数需另查测量记录。JanusGraph 的 150 主输入已全部完成；N=100,000 的最多 18 输入仍按原条件执行。原主控制器因 S5 已有符合完整保存计时门槛的主阶段候选而跳过该 Pattern 的整批扩规模，留待既有缩减与重放；S6 因原方向稳定信号进入条件阶段，七个原条件种子均已执行；seed=6 的旧休眠中断另留历史，以一次独立恢复重放替代其有效槽位，状态为 `not_confirmed`。S8 的六个原条件种子也已执行。恢复没有改原始失败档或合并中断前后的样本；完整保存输入与旧尝试记录分别记账。主控制器的跳过仅表示不继续该批输入，不能推断同 Pattern 下其他执行形状均无问题。HugeGraph 剩余主阶段、11 个原可执行条件输入及后续缩减、重放和新报告工作全部取消。跳过、未触发、不支持、用户取消及中断输入均不能报为性能阴性。

此前独立等待进程运行 [run_janus_s5_tail_followup.py](../results/gremlin-scor-janus-huge-20261018-v5/run_janus_s5_tail_followup.py)，只准备补测 **S5 尾部 `limit(0)`、seed=0、N=100,000 的一个原计划槽位**，用于核验主阶段 seed=3 候选未覆盖的既有执行形状。它没有增加 Pattern、方法、种子、尺度或独立模板数，该输入计入原 18 项条件上限。休眠中断后，[followup/progress.json](../results/gremlin-scor-janus-huge-20261018-v5/followup/progress.json) 和原 [focused/progress.json](../results/gremlin-scor-janus-huge-20261018-v5/focused/progress.json) 均已停止，未进行数据库调用。这个既定槽位随后已正常执行并记入第 14 个条件输入，状态为 `not_confirmed`：配对中位差值 2,181.2684ms 低于同查询噪声 p99 2,364.0140ms，未触发额外重放或报告。S5 其余四个 N=100,000 原条件槽位（seed=1／3／9／12）保留未测，不补齐或记为阴性。

所有 S 图仍包含一个根、4N 个 HIGH 目标和 3 个 REF 目标，共 **4N+4 顶点、4N+3 边**，最大计划规模为 400,004 顶点、400,003 边。属性值、相同匹配事实和原 `ScorRoot.id` 成本索引不变。根键为 `uid` 的既有变体没有额外补索引；需以实际原生计划判断访问路径，共同根扫描不能误写成某一侧遗漏索引。

本轮所有 S 查询对均为 **A/B，`reference_side=b`**：变换仅把现有 HIGH 关系类型换成同图 REF 关系类型，没有第三条 `query_ref`。A/B 与 A/ref、B/ref 必须分别统计；不能把总摘要的最大倍率自动解释成 A/B。

完整种子库存枚举复杂度 0 下 `0..323` 可达的有限组合，仅供离线核对；本轮 12 种子候选池没有覆盖全部 256 个可达 IR 对，更不等于任意角色组合的完整笛卡尔积。复杂度 1／2 的接口检查也不增加性能矩阵的覆盖数。

## 原阈值与确认流程

性能阶段沿用原普通查询配对测量流程：初筛为 3 对预热、6 对普通测量，倍率至少 2 且方向稳定才进入确认；之后做两次独立重建确认，每次 5 对预热、20 对 AB／BA 普通测量、各 10 对 AA／BB 同查询控制，bootstrap 重采样 10,000 次。请求超时仍为 30s，最大完整输出 10,000 行；S 模板预期输出只有 0–3 行。

满足全部以下条件，才通过正式报告的计时门槛：

1. 合并配对中位倍率 ≥5，95% 置信区间下界 ≥5。
2. 合并配对中位差值 >100ms，且大于两次确认中最大的同查询噪声 p99。
3. 合并方向一致性 ≥.95；每次独立确认分别满足倍率 ≥5、差值 >100ms、方向一致性 ≥.95。
4. 每次测量均完整读取结果并通过类型敏感多重集检查；状态、模式、索引和原成本事实通过。

自动状态 `timing_observation` 仍弱于上述逐次确认门槛；纯耗时差异也不足以确认优化遗漏。通过计时门槛后，还要建立原生计划／固定源码与可避免工作之间的因果证据，执行既有图规模缩减、独立重建及原存档重放，再从空库实测报告中的复现代码。普通计时决定耗时差异，PROFILE 的计数解释工作量；两者不能混用。

S1／S2／S3／S4／S7／S9／S10 若原生计划证实高分支步骤的元素与遍历器计数均为零，继续扩大 HIGH 图规模不会增加该分支工作，不应为追求 100ms 而盲目扩图。S5／S6／S8 的大阶段也须先有原生可避免工作证据，或方向稳定、至少 2 倍但绝对差值／噪声尚未过门槛的信号。准备操作或查询超时、未知取消、计划不可用均保留原记录并停止该目标，不能提高原超时或删除失败样本以制造稳定结果。

## 当前覆盖与统计审计

[覆盖核对](../results/gremlin-scor-janus-huge-20261018-v5/recovery/coverage.json)通过，遗漏槽位为零；[有效矩阵](../results/gremlin-scor-janus-huge-20261018-v5/recovery/matrix-progress.json)及尾部槽位覆盖层区分原输入、验证执行与失败尝试。未执行项不计性能阴性。

| 执行范围 | 完整的唯一原计划输入 | no_candidate | not_confirmed | timing_observation |
|---|---:|---:|---:|---:|
| N=1,000／10,000 主阶段 | 150 | 128 | 21 | 1 |
| S6／S8 及一个 S5 尾部条件槽位 | 14 | 5 | 5 | 4 |
| 合计 | **164** | **133** | **26** | **5** |

S1／S2／S3／S4／S7／S9／S10 的 114 个主输入均为 `no_candidate`；S5 主阶段为 4 个 `no_candidate`、5 个 `not_confirmed` 和 1 个 `timing_observation`；S6 主阶段为 4／10／0，S8 主阶段为 6／6／0。条件阶段完成 S6 七项、S8 六项和 S5 尾部一项，S5 的其余四个条件槽位 seed=1／3／9／12 明确未执行。

[独立原始统计审计](../results/gremlin-scor-janus-huge-20261018-v5/recovery/final-statistical-audit-review.json)顺序检查五个存档组，分别为 **155／8／10／1／1 档，全部零错误**。物理存档总数 **175＝164 个原槽位执行＋10 次有限验证＋一次旧外部休眠中断**；重放与缩减不增加 Pattern、原计划输入或根因数量。三个选中代表各自的两次确认、合计 40 对普通样本，均重新计算配对统计、10,000 次 bootstrap 置信区间和同查询控制噪声，通过原合并与逐次门槛。统计审计没有把单次新空库诊断算作性能样本，也不声称从没有保存完整结果正文的旧档独立重建全部结果多重集；结果语义由原执行检查及新空库实际验证共同支撑。

## 两份本地报告与去重

以下普通耗时取自最终独立重放的 40 对确认样本；倍率是配对比值的中位数，差值是配对差值的中位数，均不是两列单边中位数相除或相减得到。A 是 HIGH 侧，B 是 REF 侧。

| 本地报告 | 代表输入 | A 普通中位数 | B 普通中位数 | 配对中位倍率 | 配对中位差值 |
|---|---|---:|---:|---:|---:|
| [JanusGraph S5／BUG004](../BUG%20list/JanusGraph/S5/BUG004.md) | N=5,000，seed=3 | 110.684459ms | 1.579292ms | **70.214619×** | **109.139710ms** |
| [JanusGraph S6／BUG005](../BUG%20list/JanusGraph/S6/BUG005.md) | N=100,000，seed=0 | 255.401646ms | 1.371938ms | **184.037156×** | **254.459064ms** |

S5 报告针对**恒假子过滤仍展开全部邻居**。S6 报告针对**LIMIT(1) 消费前仍预装载邻接 EntryList**。S8 的原 N=100,000、seed=3 独立重放也通过原门槛，配对倍率 **161.723316×**、差值 **248.632062ms**、同查询噪声 p99 为 119.041663ms；它在最终原生证据中走相同 EntryList 邻接预装载路径，因此并入 S6 报告，不单开第三份 BUG。

两份报告已登记本地总账，状态为 `reproduced`／`local_experiment`，[本地发布记录](../results/gremlin-scor-janus-huge-20261018-v5/report-validation/local-report-publication.json)保留精确输入散列与报告散列。**未提交上游，也没有开发者确认。** 两份本地报告及本轮按机制合并的两个观察，不自动等于两个独立跨库源根因；独立跨库根因数量仍未知。最终独立审稿的77项检查与原生证据复核均已通过，根进度现为 `completed`，`task_complete=true`、`report_ready=true`；[最终资格记录](../results/gremlin-scor-janus-huge-20261018-v5/final-qualification.json)保存最终选择、去重和完整证据散列，原始控制器的待人工快照保持原样。

## 原生证据与新空库复现

S5 精确复现程序在新空存储实际构建并全量验证 **20,004 个顶点、20,003 条边**，包括属性、端点、边序号与索引检查；见 [S5 原生证据复核](../results/gremlin-scor-janus-huge-20261018-v5/report-validation/janusgraph-S5-n5000-s3/manual-native-review.json)。S6 精确程序在另一新空存储构建并全量验证 **400,004 个顶点、400,003 条边**，见 [S6／S8 原生证据复核](../results/gremlin-scor-janus-huge-20261018-v5/report-validation/janusgraph-S6-n100000-s0/manual-native-review.json)。

S8 先核对 20 项物理流字段，并证明两种配方的 40 万级顶点流和边流均全量逐行一致，再在**同一份已完整验证的 S6 新图**上实际执行只读查询、核对结果和有界原生计划；它没有另建一个独立 S8 容器，也没有增加普通性能样本。这里的新空库单次耗时、PROFILE duration 与计划计数只用于复现和工作量解释，不能替代正式普通配对耗时，更不能把 backend-query 元素数改名为磁盘 I/O 次数。

性能控制器正常结束为 `completed_manual_review_pending` 并停止其专用 JanusGraph；两个本轮新复现容器也均已停止。v5 的八个冻结源码散列保持一致，生成器、变换器、图配方、原统计协议与阈值没有改变。普通计时不含建图或 PROFILE。运行期间的 `caffeinate` 仅临时抑制闲置休眠，没有修改持久电源设置。

## 未报告输入与保留的失败

S5 尾部 `limit(0)` 的既定 N=100,000、seed=0 槽位已正常测完，独立审计的配对倍率虽为 **400.144603×**，但差值 **2,181.268354ms ≤ 同查询噪声 p99 2,364.013987ms**，原门槛未通过。因此不登记报告，也没有额外重放或重试；它不能与恒假子过滤的 S5／BUG004 视为同一条已确认缺陷。

S6 的 N=50,000／75,000 缩减分别因差值低于噪声而未确认，保留原档后用原 N=100,000 输入进行一次独立重放。S8 的 N=50,000 缩减最初有计时信号，但其一次重放差值 121.6802ms 低于噪声 206.0970ms；N=75,000 缩减也未过噪声门槛，均保留为未确认。最终通过的是原 N=100,000 输入的一次重放。没有删除失败、合并不同会话或通过反复试验挑选结果。

## 历史中断与 HugeGraph 范围边界

[环境记录](../results/gremlin-scor-janus-huge-20261018-v5/sleep-interruption/environment-interruption.json)保存 `pmset` 原始证据：本机于 **2026-10-19 03:29:47（Asia/Shanghai）** 因电量 1% 休眠，于 **06:02:45** 接入交流电唤醒。旧 S6 seed=6 在 `confirmation_1_setup` 网络超时，`confirmation_sessions=[]`、`summary=null`、`elapsed_lower_bound_ms=null`；这是外部中断尝试，不是数据库耗时下界或性能 BUG。旧 [matrix-progress.json](../results/gremlin-scor-janus-huge-20261018-v5/matrix-progress.json) 的 `stopped` 状态与失败档保持原样；一次恢复重放完成为 `not_confirmed`，独立存档并替代有效原槽位，前后样本不混合。

旧容器改名为 `gsp-janusgraph-scor-20261018-v5-sleep-20261019`，新容器用原名和全新存储、无数据复制；[创建溯源](../results/gremlin-scor-janus-huge-20261018-v5/recovery/runtime-environment/fresh-recovery-creation.json)核对固定镜像、环境、命令、入口点和宿主限制一致。S6 原四小时预算仍为 14,400s；恢复前活跃累计 8,523.14595s，恢复重放及剩余原 S6 阶段分别使用 1,888.31524s、1,088.89615s，合计 11,500.35735s，保守预算余量 2,898s。没有重置预算或再次扣除休眠时段；这是阶段预算账本，不是单条普通查询时间。

HugeGraph 已按用户要求停止后续测试和本轮新报告，保留的 v5 [历史进度](../results/gremlin-scor-janus-huge-20261018-v5/before-janus-only-matrix-progress.json)中有 80 个完整主输入；S5 六项均未确认，最大配对中位差值 49.78ms，S8 未完成输入由用户中止，不计超时或性能阴性。其剩余普通测试、条件扩规模和新报告均未补做。

v4 因诊断结果上限不匹配而停止的旧记录也保留：HugeGraph S1–S4 共 54 项无候选、S5 五项未确认及第六项外部中断。旧[矩阵进度](../results/gremlin-scor-janus-huge-20261018/matrix-progress.json)、[S5 日志](../results/gremlin-scor-janus-huge-20261018/matrix-hugegraph-S5-main.log)和[等待进程停止记录](../results/gremlin-scor-janus-huge-20261018/focused-wait-v4-progress.json)不覆盖、不移用为 v5 样本。未执行、不支持、用户取消和环境中断均不计性能阴性。
