# Gremlin 优化器证据审查（2026-10-18）

本审查只覆盖当前 GraphSymPerf 已支持的 12 项 Pattern、现有变换、图状态和 Gremlin 编译器。版本固定为 ArcadeDB `26.10.2` 与其依赖 TinkerPop `3.8.2`。没有增加 Pattern、手工改写查询或 Native 探针；审查进程没有启动、连接或操作数据库。

**当前结论：已检查的两个 P12 案例及已超阈值的 P4/P5 案例，均可由显式遍历顺序解释，不支持登记遗漏优化。P10 的实际计划已观察到后续图扫描没有使用已有类型/索引优化，与固定源码在首次类型替换后退出的控制流一致；但两个 P10 案例均未通过性能初筛，没有进入独立确认，不满足 BUG list 的报告阈值。**

## 1. 已检查的真实案例：P12 / scale 1000 / seed 0

证据来自正式性能轮次的既有文件，未重新执行查询：

- [result.json](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/0005-P12-n1000-b519e93b/result.json)
- [query_a.gremlin](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/0005-P12-n1000-b519e93b/query_a.gremlin)
- [query_b.gremlin](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/0005-P12-n1000-b519e93b/query_b.gremlin)

案例的 `input_hash` 为 `b519e93b0364169687ecb15d4bdffbc0fa9f21b570137aa26f391360b669d69d`，`complexity=0`、`data_seed=0`、`state_variant=original`。运行版本记录为 `ArcadeDB/26.10.2 (build e854692736b48971f92dff78a1e727bc570bb6a2/1792085686249/main)`。结果记录为一行，构造期望结果为 `{u.uid: 1, w.uid: 3}`；状态核查记录含有效的 `GspLow.id` LSM_TREE 属性索引。

### 普通执行时间

| 字段 | 结果 |
|---|---:|
| 状态 | `not_confirmed` |
| 原因 | `confirmation_gate_not_met` |
| 慢侧 | B |
| 配对时间比中位数 | 8.3562864444 |
| 配对时间差中位数 | 6.7724369001 ms |
| 比值置信区间 | [7.8973620940, 9.2686570039] |
| 方向一致率 | 1.0 |
| 配置要求的最小时间差 | 100 ms |

比值超过 5 倍，但时间差未达到 100 ms。以上是 `result.summary` 的配对测量统计，不是用 PROFILE 时间计算的结果。

### 原生 PROFILE 工作量

以下数值是原生计划中的 `elements`；本例列出的对应 `traversers` 数值相同。它们是步骤输出计数，不能一概当作底层磁盘读取数。

| A 的关键步骤 | 输出 elements |
|---|---:|
| `ArcadeFilterByIndexStep(vertex,GspLow,gsp_cost_lookup,eq([1]))` | 1 |
| `HasStep([id.eq(1)])` | 1 |
| 第一条 `VertexStep(OUT,[GSP_LINK],vertex)` | 1 |
| `HasStep([~label.eq(GspMiddle)])` | 1 |
| 第二条 `VertexStep(OUT,[GSP_LINK],vertex)` | 1 |
| `HasStep([~label.eq(GspHigh)])` | 1 |
| `ProjectStep` | 1 |

| B 的关键步骤 | 输出 elements |
|---|---:|
| `GraphStep(vertex,[])` | 3003 |
| 第一条 `VertexStep(OUT,[GSP_LINK],vertex)` | 2002 |
| `HasStep([~label.eq(GspHigh)])` | 1001 |
| `ArcadeFilterByIndexStep(vertex,GspLow,gsp_cost_lookup,eq([1]))` | 1001 |
| `HasStep([id.eq(1)])` | 1001 |
| 第二条 `VertexStep(OUT,[GSP_LINK],vertex)` | 1001 |
| `HasStep([~label.eq(GspMiddle)])` | 1001 |
| `WherePredicateStep(null,eq(_gsp_v0))` | 1 |
| `ProjectStep` | 1 |

A 从指定 Low 顶点的索引查找开始，只经过目标链。B 的原始文本首先执行无标签限制的 `.V()`，枚举 3003 个顶点，经首段展开及 High 标签过滤后形成 1001 个输入，再为这些输入执行 Low 索引查找和关联检查。因此 B 的额外工作可以由编译器明确写出的遍历顺序解释。

**B 已成功使用 Low 属性索引，没有发生“中途 GraphStep 没有索引化”。** B 的第一个 `.V()` 后没有相邻 `hasLabel`，没有触发本文件第 2 节讨论的类型扫描替换和提前退出。`outE().inV()` 在两侧计划里均已变成返回顶点的 `VertexStep`，也不能将这些组合步骤原样存在或未优化作为原因。

本例不证明任意优化都不可能；它说明在已经检查的规则范围内，没有发现额外的 provider 规则遗漏。即使后续扩大规模跨过时间阈值，也不能只据此将同一顺序效应登记为 missed optimization。

### 同规模 seed 17：已超阈值，但仍是显式遍历工作量观察

第二个已检查案例为 [0006-P12-n1000-6e979aad/result.json](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/0006-P12-n1000-6e979aad/result.json)，以及同目录的 [A 查询](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/0006-P12-n1000-6e979aad/query_a.gremlin)、[B 查询](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/0006-P12-n1000-6e979aad/query_b.gremlin)。其 `input_hash` 为 `6e979aad4fc8fd99c6a472373319641e494afb0803529e5fc7b71aaacb4efa1f`，`seed=17`、`complexity=0`、`data_seed=0`，角色选择为 `anchor_constraint=where`、`match_order=forward`；版本与 seed 0 案例相同，结果同为一行。

| 普通配对时间字段 | 结果 |
|---|---:|
| 状态 | `timing_observation` |
| 原因 | `explicit_traversal_work_difference` |
| 慢侧 | B |
| 时间比中位数 | 155.2889368981 |
| 时间差中位数 | 1423.1676675845 ms |
| 比值置信区间 | [146.8369498873, 194.1203698822] |
| 方向一致率 | 1.0 |

本例确已超过 5 倍和 100 ms 等现有时间阈值，不能称为“未通过时间门槛”。但时间门槛与遗漏优化归因是两个不同判断。

| 原生 PROFILE 的关键步骤 | A 输出 elements | B 输出 elements |
|---|---:|---:|
| 最初无标签限制的 `GraphStep` | 无此步骤 | 3003 |
| 最初展开并经 High 标签过滤后的输入 | 无此前置片段 | 1001 |
| `ArcadeFilterByTypeStep(vertex,GspLow)` | 1001 | 1002001 |
| 从 Low 出发的 `VertexStep` | 1001 | 1002001 |
| Middle 标签过滤 | 1001 | 1002001 |
| 将当前 Middle 与先前绑定顶点比较的 `WherePredicateStep` | 无此步骤 | 1001 |
| `TraversalFilterStep([SelectOneStep(...), PropertiesStep([id],value), IsStep(eq(1))])` | 1 | 1 |
| `ProjectStep` | 1 | 1 |

A 先枚举 1001 个 Low 顶点、展开到 Middle，再以相关 `id` 子遍历筛至 1 个输入，才展开下一段。B 先形成 1001 个 Middle→High 候选，每个候选执行一次 Low 类型扫描，因此产生 `1001 × 1001 = 1002001` 个 Low 输出及后续展开；Middle 关联检查留下 1001 个，再由 `id` 子遍历过滤留下 1 个。固定版本 [`ArcadeFilterByTypeStep.processNextStart()` 第 171–188 行](https://github.com/ArcadeData/arcadedb/blob/26.10.2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeFilterByTypeStep.java#L171-L188) 对非起始扫描按每个上游输入重新打开迭代器，与这条实际计数链一致。

这里 A/B 都没有使用 `GspLow.id` 索引，但不能由“索引存在”直接推出已有索引优化失效。当前编译器将 WHERE 属性比较发为 `filter(__.select(label).values('id').is(eq(...)))`；它是含映射步骤的相关子遍历，且位于展开之后，不是 `GraphStep` 后相邻 `HasStep` 的属性约束。第 2–3 节所列 provider 索引匹配及 TinkerPop 过滤内联规则不直接识别这一形状。B 的类型扫描已经成功替换，且之后没有另一个 GraphStep；它不是第 2 节的 `break` 候选。

这些重复工作在语义上存在进一步优化的可能，但消除它们需要额外的 `select/values/is` 谓词归一化、跨展开下推或遍历顺序改写；本轮尚无证据证明这种规则已适用于当前固定版本的输入。因此本例保留为超过阈值的显式遍历工作量观察，不写入 BUG list，也没有通过手工新查询扩展原案例。未在本审查进程中读取或归因 n10000 超时候选；超时不由此例的结果替代确认。

## 2. P10：源码候选已有计划证据，但性能未过阈值

以下先记录固定源码与离线生成形状，再列已观察的实际计划。当前状态是**存在计划层面的规则覆盖遗漏，普通性能未过初筛；不是已确认的超阈值性能缺陷**。

ArcadeDB [`ArcadeTraversalStrategy.apply()`，第 89–218 行](https://github.com/ArcadeData/arcadedb/blob/26.10.2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeTraversalStrategy.java#L89-L218) 逐个寻找相邻 `GraphStep + HasStep`，在具备单个等值标签时选择属性索引或类型专用扫描。索引选择针对支持的比较、有效索引和正确的顶点/边类型进行检查。没有索引时可以生成 `ArcadeFilterByTypeStep`，但随后第 217–218 行依据 `replacedWithFilterByType` 执行 `break`，不再检查同一遍历里的后续 GraphStep。

已离线实际调用当前 `prepare_case(None, pattern, 100, seed, 0, complexity, database=...)`，覆盖 P10/P11/P12、seed `{0,17}`、complexity `{0,1,2}` 的 18 种组合。结果如下：

| 输入 | 生成形状 | 候选判断 |
|---|---|---|
| P10，seed 0，全部三个复杂度，B | 先 `.V().hasLabel('GspHigh')`，后 `.V().hasLabel('GspLow').has('id',P.eq(...))` | 第一个类型替换可能阻断第二个本来满足形状的索引替换 |
| P10，seed 0，全部三个复杂度，A | 先带 `id` 属性约束的 Low，后 High | 先索引替换不会设置该退出标志，后续 High 类型替换仍有机会执行 |
| P10，seed 17，全部三个复杂度，A/B | 第一处均为类型约束，后面还有第二处 `.V().hasLabel(...)` | 后续可能至少遗漏类型专用扫描；Low 的 id 谓词在 `filter(select().values().is())` 内，不直接符合现有 `HasStep` 索引匹配形状 |
| P11，所查全部组合 | 只有一个 `.V()`，其余通过 `select(anchor)` 开始 | 不符合“提前退出漏掉后续 GraphStep”候选 |
| P12，所查全部组合，B | 第一处 `.V()` 没有 `hasLabel` | 不会先触发类型替换后的退出；seed 0 及 seed 17 complexity 1/2 的第二处 Low map 约束仍有索引替换机会 |

P10 原态的构造期望结果为 `[{a.uid:1,b.uid:2,c.uid:3}]`。`empty_low` 态为空，但高成本分支先运行、随后才发现低成本分支为空，本来就可能带来遍历成本差异；不能把这一部分全归到后续扫描遗漏。

第二处扫描并非天然禁止使用索引。固定版本 [`ArcadeFilterByIndexStep.processNextStart()`，第 453–471 行](https://github.com/ArcadeData/arcadedb/blob/26.10.2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeFilterByIndexStep.java#L453-L471) 明确支持 `isStart=false`，按每个上游输入重新打开迭代器；第 1 节的真实 P12 计划也确认了这一点。

对于其他尚未审查的案例，仍应保持原案例不变，核对实际步骤、索引有效性和输出计数。若报告这一机制，应描述“没有继续处理后续图扫描”，而非未经证明的“缺少通用连接重排”；必须另行通过现有普通时间阈值、独立确认、完整结果等价和根因去重。

### 已检查 P10 / scale 10000 / seed 0

[案例 0039 的 result.json](../results/gremlin-extension-20261018/performance-continuation/20261018T064839Z-9ce7e2/0039-P10-n10000-8e3659e3/result.json) 的 `input_hash` 为 `8e3659e3967fc931efaceb545392bcd716c6a169108310b42e3fae2001ccb564`，`complexity=0`，使用 map 形式的 Low.id 约束，A/B 完整结果均为预期的一行。实际 A 计划先以 `ArcadeFilterByIndexStep(vertex,GspLow,gsp_cost_lookup,eq([1]))` 输出 1 个 Low，再以 `ArcadeFilterByTypeStep(vertex,GspHigh)` 输出 10001 个 High；B 先以同一个 High 类型步骤输出 10001 个顶点，展开并经 keep 过滤留下 1 个输入后，后续 Low 片段仍是 `GraphStep(vertex,[])` 输出 50003 个顶点，再由 `HasStep([~label.eq(GspLow), id.eq(1)])` 留下 1 个。B 的后续片段仍具备相邻 GraphStep 与 label/id HasStep 的优化形状，A 也在同一图态成功使用对应索引；这证实了前述源码候选所预测的后续索引替换遗漏，不只是一般的连接顺序差异。但普通配对初筛只得到 B/A 中位比值 **1.2735360881**、时间差 **30.4036880843 ms**、方向一致率 1.0，未过 2 倍初筛阈值，状态为 `no_candidate / discovery_gate_not_met`，没有独立确认。不能用计划差异替代 5 倍、100 ms 等最终报告门槛，也不能把未经补丁对照分离的全部时差归给该规则。

### 已检查 P10 / scale 10000 / seed 17

[案例 0040 的 result.json](../results/gremlin-extension-20261018/performance-continuation/20261018T064839Z-9ce7e2/0040-P10-n10000-f36cb2a0/result.json) 的 `input_hash` 为 `f36cb2a0ddba176f643fd4438a591f26bda8969a6ad5df097b922162399b17b8`，`complexity=0`，Low.id 使用 WHERE 形式的映射子遍历。A 的首个 Low 类型扫描输出 1，而后续 High 片段仍为普通 GraphStep，先输出全部 50003 个顶点，再按标签留下 10001 个；B 的首个 High 类型扫描输出 10001，后续 Low 片段同样仍为普通 GraphStep，输出 50003 后按标签留下 1 个。两侧都观察到后续类型专用扫描未替换，与第一处类型替换后退出一致；由于 id 仍在映射子遍历中，本例不另行宣称后续 id 索引规则适用。普通初筛 B/A 中位比值 **1.0324721734**、时间差 **9.8657712806 ms**、方向一致率 **0.6666666667**，状态仍为 `no_candidate / discovery_gate_not_met`，没有独立确认，不写 BUG。

## 3. P4/P5：布尔短路与过滤重排不是同一保证

TinkerPop 固定版本的 [`AndStep.filter()`，第 35–40 行](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/filter/AndStep.java#L35-L40) 按已有子遍历顺序执行，首次 FALSE 返回；[`OrStep.filter()`，第 35–40 行](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/filter/OrStep.java#L35-L40) 首次 TRUE 返回。因此“昂贵子遍历在前，交换后更快”不等于短路失效。

更强的候选证据应当是：低成本决定项已经在前且在该输入上确定结果，昂贵后缀仍然产生可归属的工作；或者证明一条具体、适用的重排规则漏掉了这段遍历。

[`FilterRankingStrategy.apply()/getStepRank()`，第 110–217 行](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/FilterRankingStrategy.java#L110-L217) 排列同一遍历中的相邻过滤/排序步骤，并处理标签依赖。它没有按成本排序 AND/OR 的子遍历列表。普通映射和展开步骤没有可交换的过滤秩，不能据此推导任意连接或跨展开重排。

当前编译器常把低成本属性比较发为 `select().values().is()`，其中有非过滤步骤。它不满足 [`InlineFilterStrategy.processTraversalFilterStep()/processAndStep()`](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/InlineFilterStrategy.java#L187-L287) 对全部子步骤为 FilterStep 的要求；同文件 `processOrStep()` 的 OR 合并还要求同一键上的 HasStep。不能把这些条件略去，直接断言策略应当将高低成本子项排序。

已检查的真实 [P4 / scale 10000 / seed 17 / complexity 0 案例 0012](../results/gremlin-extension-20261018/performance-continuation/20261018T064839Z-9ce7e2/0012-P4-n10000-e7e40688/result.json)（`input_hash=e7e40688433dc89b3fd8b2c9efb369418ef8e319c6dcfabc72c6e7c07e04ae7f`）属于这一顺序效应：A/B 比较中 B 慢 95.5869995921 倍、配对时间差中位数 146.4971255045 ms；B/reference 中原始 B 慢 86.3136336869 倍、时间差 126.2681460939 ms；A/reference 为 `no_candidate`。三条真实查询的区别是 AND 子遍历顺序，以及现有 reference 将 `GSP_EXPAND` 换为 `GSP_REFERENCE`；A 和 reference 都先测试低成本 `coalesce(keep,false)=true`。原生计划保留这一顺序，A 与 reference 的高成本 `VertexStep` 均只输出 1 个见证邻居，COUNT 各输出 1 个标量；B 先对两个 anchor 执行 COUNT，`VertexStep` 输出 40001 个邻居，mark 过滤后总共只剩 1 个，COUNT 输出 2 个标量。这说明低成本在前时已跳过 noise anchor 的昂贵后缀，没有观察到短路失效；高成本在前时必须确定该无匹配邻域的 COUNT 结果。实际子项含 Coalesce、SelectOne、Properties、VertexStep、CountGlobalStep，不满足纯 FilterStep 内联条件，FilterRanking 也不对子遍历列表进行成本排序。尽管 COUNT 后保留空值测试并阻挡数值阈值识别，noise 邻域没有匹配，不能据此宣称漏掉了“首个匹配即停止”的可避免工作。故保留 `timing_observation`，不登记 BUG；计划中个别终端 IsStep 的零输出计数不作为“该子项没有执行”的证据。

已完成的 [P5 / scale 10000 / seed 17 / complexity 0 案例 0028](../results/gremlin-extension-20261018/performance-continuation/20261018T064839Z-9ce7e2/0028-P5-n10000-20d344be/result.json)（`input_hash=20d344be62e2bea65df4a489584c71623575179c00f587173e4fa96c0c755108`）也符合按 OR 子遍历顺序短路：A/B 中原始 B 慢 **117.3304126579** 倍、时间差 **162.7608747222 ms**；B/reference 中原始 B 慢 **127.0314122822** 倍、时间差 **165.9542922862 ms**；A/reference 为 `no_candidate`，完整输出均为两个 anchor。顶层 summary 采用 B/reference 统计，其 `slow_side=a` 表示该比较内部的第一个输入（原始 B），不能误写为原始 A 慢。原生 A 与 reference 均先测试低成本条件，只向 COUNT 分支输入 1 个 anchor，邻接展开输出 1；B 先向 COUNT 分支输入 2 个 anchor，邻接展开输出 40001，mark 过滤后剩 1，COUNT 输出 2 个标量，之后低成本分支仅为 COUNT 未成立的 anchor 求值。决定性 TRUE 在前时已经跳过昂贵后缀，已有 OrStep 短路行为正常；Coalesce/select/values/COUNT 形状同样不满足 OR 合并为同键 HasStep 的条件，也没有子遍历成本排序规则的失效证据。保留 `timing_observation`，不登记 BUG。

复杂度补测的 [P4 / scale 10000 / seed 0 / complexity 2 案例 0011](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0011-P4-n10000-9b5a1881/result.json)（`input_hash=9b5a18817c3969d21c92a585f9423595d9e9e8ed002e888e53c19c1880faaef7`）的顶层 summary 对应 A/B：B 慢 **249.4740672155** 倍、配对时间差 **597.6677502040 ms**，比值置信区间为 [230.8689184927, 287.9557889541]，噪声 p99 为 225.0802670326 ms，状态为 `timing_observation`。A/reference 为 `no_candidate`；B/reference 虽有 301.4366588154 倍、579.4162913226 ms 的差异，其噪声 p99 达 1201.3118627295 ms，高于时间差，故为 `not_confirmed`，不能与 A/B 混称全部通过确认。真实查询把高成本过滤嵌套为 `(mark 条件 AND u.uid=u.uid) OR NOT(u.uid=u.uid)`，低成本条件的多重 NOT 已由现有编译器表达为 `values('keep').is(neq(false))`。原生 A/reference 仍先执行低成本条件，只展开 1 个见证邻居；B 先运行 COUNT，展开及 mark 条件求值均为 40001 次。在 B 的内层 AND 中，等式 `u.uid=u.uid` 的取值片段只运行 1 次，说明 mark 为 FALSE 时已跳过其右项；内层 OR 的否定等式取值片段运行 40000 次，正好对应 mark 未成立的 noise 邻居，未产生匹配。内外层短路计数均与原表达式顺序一致，当前没有新的已有规则遗漏证据。将这些相关 select/coalesce/flatMap 属性比较折叠成恒真/恒假，需要本次所核对策略之外的归一化能力；不能以构图已知 uid 非空为由，假定该能力已由引擎实现。继续保留显式遍历工作量观察，不登记 BUG，也不将 B/reference 未通过噪声门槛的结果用作独立确认。

同轮 [P4 / scale 10000 / seed 17 / complexity 2 案例 0012](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0012-P4-n10000-a68eedc1/result.json)（`input_hash=a68eedc17b13adc5f6c1468fadc0bbaf5bc9bd71d073451021fd6463f7f73352`）的高成本操作是 EXISTS，原生计划没有 CountGlobalStep。A/B 中原始 B 慢 **184.4667023558** 倍、时间差 **1972.2166035790 ms**，为顶层 summary 的来源；B/reference 中原始 B 慢 **174.9429458120** 倍、时间差 **1989.6843123715 ms**，也为 `timing_observation`；A/reference 为 `no_candidate`。真实查询中 COALESCE 包裹的复合布尔标量由现有编译器展开为“先测试 TRUE，未成立再测试 FALSE，否则返回 NULL”的嵌套 choose。A/reference 的外层低成本条件在前，高成本展开和 mark 取值只处理 1 个见证邻居，未进入高成本的 FALSE 分支；B 先展开 40001 个邻居，其高成本外层 ChooseStep 输出 40001 个标量，TRUE 条件中的 mark 取值为 40001 次，FALSE 分支中的内层 ChooseStep 只处理 40000 个 noise 邻居，其 mark 取值为 40000 次，最后返回 false 40000 次、true 1 次，NULL 兜底未执行。该重复取值直接对应生成查询显式写出的 TRUE/FALSE 两套谓词；不能描述为引擎提前执行了未选 CASE 分支。无匹配 noise 邻域的 EXISTS 仍需穷尽，见证的未选 FALSE 分支确已跳过；COALESCE 得到非空 false 后也没有执行后续 NULL 兜底。当前计划显示外层顺序短路及内层选择正常，包含 Choose/Coalesce/Select/FlatMap 的子遍历不满足本审查已有纯过滤内联和同键 HasStep 合并规则，未发现新的适用规则遗漏。继续保留 `timing_observation`，不登记 BUG；不存在 COUNT 的本例也不能沿用 COUNT 空值包装的归因。

同轮 [P5 / scale 10000 / seed 0 / complexity 2 案例 0013](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0013-P5-n10000-2b35c271/result.json)（`input_hash=2b35c271bf2df0d958030ad3ecd6b0778bd442b9685f533f22d287d089c57ed3`）的顶层 summary 对应 A/B，原始 B 慢 **254.3731295809** 倍、配对时间差 **1254.0068957023 ms**，比值置信区间为 [182.0959678401, 357.6345412294]，噪声 p99 为 550.7406590041 ms，状态为 `timing_observation`；A/reference 为 `no_candidate`。B/reference 虽有 368.0000739334 倍、959.0285210870 ms 的差异，其噪声 p99 为 1535.7547571929 ms，故为 `not_confirmed`，不作为通过确认的比较。该变体高成本谓词为 `CASE WHEN u.uid=u.uid THEN mark 条件 ELSE null END`，外层 OR 的低成本项还包含否定自等式。实际 A/reference 先测试低成本项，仅将见证 anchor 送入 COUNT，展开及 COUNT 分别输出 1 和 1；B 先对两个 anchor 计算 COUNT，展开输出 40001，过滤后只有 1 个见证匹配，COUNT 输出 2 个标量。B 的 CASE 条件求值 40001 次并选择 THEN，ELSE NULL 输出为 0；THEN 内部的三值布尔标量转换先读取 mark 40001 次，对 FALSE 的 40000 个 noise 再读取 mark 并输出 false，见证输出 true 1 次，最终 NULL 兜底为 0。这些计数与显式查询中的 OR 顺序、CASE 分支选择及 TRUE/FALSE 两套判定一致，没有未选 CASE 分支被执行的证据。无匹配 noise 仍需穷尽；自等式、否定自等式和映射子遍历的进一步化简不属于本次已核对规则的直接匹配形状。保留 `timing_observation`，不登记 BUG，也不因较大比值放宽原有噪声门槛。

同轮 [P5 / scale 10000 / seed 17 / complexity 2 案例 0014](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0014-P5-n10000-19d82fcd/result.json)（`input_hash=19d82fcdff7d4b731ac879c31c3f0920b69be6491fc62f6edbcf099cfad93883`）的顶层 summary 仍来自 A/B：原始 B 慢 **95.2423509342** 倍、时间差 **271.4810413308 ms**，比值置信区间为 [89.9557108286, 97.8319767096]，噪声 p99 为 43.4353247518 ms；B/reference 中原始 B 慢 **90.9062139155** 倍、时间差 **320.8473755512 ms**，噪声 p99 为 126.4990986651 ms。两项均通过原有时间及噪声门槛，状态为 `timing_observation`；A/reference 为 `no_candidate`。本例高成本操作为 EXISTS，实际计划没有 CountGlobalStep。A/reference 的低成本 CASE/OR 在前，只向 EXISTS 输入见证 anchor，邻接展开输出 1；B 把 EXISTS 放前，对两个 anchor 展开 40001 个邻居。高成本过滤形状为 `(mark 条件 AND u.uid=u.uid) AND u.uid=u.uid`，B 的 mark 取值为 40001 次，但两段自等式的取值片段都只在见证上执行 1 次，说明 40000 个 noise 的后续 AND 项已跳过。B 低成本 CASE 只对 EXISTS 未成立的 1 个 anchor 求值，其 THEN 布尔转换返回 true 1 次，未选 FALSE/NULL 分支为 0；没有昂贵后缀或未选 CASE 分支被强行执行的证据。无匹配 noise 邻域仍需穷尽，现有子遍历列表不按成本重排，映射/选择/属性比较也不满足本审查的纯过滤内联或同键 HasStep 合并形状。保留显式遍历工作量观察，不登记 BUG。

## 4. P15 与 COUNT/EXISTS 的边界

[`ChooseStep` 第 85–90 行](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/branch/ChooseStep.java#L85-L90) 建立条件与真/假分支；[`BranchStep.standardAlgorithm()/applyCurrentTraverser()` 第 123–174 行](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/branch/BranchStep.java#L123-L174) 只向选中的分支注入输入。P15 的强候选信号是未选高成本分支仍产生随 noise 规模增长的扫描，而不是条件取补、COALESCE 或三值逻辑包装带来的固定开销。

EXISTS 子遍历通过 [`TraversalUtil.test()` 第 139–146 行](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/util/TraversalUtil.java#L139-L146) 的 `hasNext()` 判断是否有结果。没有匹配的邻域需要被穷尽，不能将此情形称作“EXISTS 没有在首个匹配停止”。

当前已有复杂度变体会产生 COUNT > 0，编译形状通常为 `count().is(P.neq(null)).is(P.gt(0))`。固定版本 [`CountStrategy.apply()/doStrategy()` 第 89–230 行](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/CountStrategy.java#L89-L230) 只分析紧邻 count 的第一个 is，插入的空值测试可能使它看不到后面的数值阈值。这是源码匹配范围的观察，不是当前 Pattern 已证明浪费的结论。

P4/P5 现有高成本 noise 邻域全部 `mark=false`，必须扫描完才知道没有匹配；唯一正例邻域较小。P15 的大 noise 邻域位于未选分支。因此不能只根据 COUNT 出现在计划中，就把其他图态或既有 Native/count-nonnull 报告的“本可首个匹配停止”证据归给这些案例。

ArcadeDB 的 [`applyEdgeCountFilterOptimization()` 第 312–364 行](https://github.com/ArcadeData/arcadedb/blob/26.10.2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeTraversalStrategy.java#L312-L364) 只匹配恰好三个子步骤 `VertexStep(Edge) → CountGlobalStep → IsStep`，还要求可用的 GAV provider。现有带 select、inV、标签和属性过滤的相关 COUNT 不满足这个形状，不应声称该 O(1) 度数规则必然适用。

## 5. 全部 12 项 Pattern 的审查口径

以下是待实测案例适用的审查口径，不是对全部 Pattern 已完成性能验证的声明。

| Pattern | 不能直接登记的现象 | 应核查的额外证据 |
|---|---|---|
| P1 | 交换起点后顶点枚举和邻接展开量不同 | 具体适用的 provider 规则遗漏，而非单纯选取了不同起点 |
| P2 | 反向边及相反遍历方向产生不同访问成本 | 相同结果和关系配对成立后，检查实际步骤和索引能力 |
| P4 | 高成本 AND 子项写在前因而先执行 | 决定性 FALSE 在前却未跳过后缀，或适用重排规则遗漏 |
| P5 | 高成本 OR 子项写在前因而先执行 | 决定性 TRUE 在前却未跳过后缀，或适用重排规则遗漏 |
| P6 | 比较操作数互换后生成的步骤形状不同 | 检查现有规范化/索引规则的具体匹配条件，不套用 SQL 保证 |
| P8 | 多关系类型枚举顺序的时间差 | 是否出现本不需要的额外遍历；完整结果仍需消费全部分支 |
| P9 | 谓词在展开前后的位置不同 | 是否存在实际适用的下推规则，不能用最终结果等价替代规则证据 |
| P10 | 独立分支的显式顺序与空输入传播不同 | 第 2 节已观察后续图扫描规则遗漏，但两例普通性能均未过初筛，不能登记超阈值缺陷 |
| P11 | 多个 Low 结果使后续 High 分支重复执行 | 单独证明适用规则遗漏；不默认存在通用连接重排 |
| P12 | 从无约束顶点开始、再逐输入执行 Low 扫描 | seed 0 成功使用索引；seed 17 的映射子遍历谓词不满足已有索引规则形状，二者目前均无额外 provider 遗漏证据 |
| P14 | UNION ALL 分支先后差异 | 两个分支均需完整消费；时间差还需额外工作量/机制证据 |
| P15 | 条件取补及三值逻辑包装开销 | 未选昂贵分支实际执行的证据，及普通运行时间确认 |

## 6. 去重与更新范围

已对当前本地 `BUG list` 做 `ArcadeTraversalStrategy`、`GraphStep`、`ArcadeFilterByTypeStep`、`ArcadeFilterByIndexStep` 相关检索，未找到该 ArcadeDB 提前退出候选的本地报告。定向 GitHub issue 检索返回的相关 [#8299](https://github.com/ArcadeData/arcadedb/issues/8299)、[#9140](https://github.com/ArcadeData/arcadedb/issues/9140)、[#9335](https://github.com/ArcadeData/arcadedb/issues/9335) 主要涉及中途写入可见性、计数、schema 正确性；尚未找到同一性能根因。这一有限检索不支持“全球首次”的声明。

本文件截至当前实际检查了十个已完成的正式性能案例：第 1 节的两个 P12 案例、第 2 节的两个 P10 案例，以及第 3 节的六个 P4/P5 案例（含四个 complexity 2 输入）。其中七个最终状态为 `timing_observation` 的案例均已逐例核对原生计划：P12 seed 17 一例、complexity 0 的 P4/P5 各一例、complexity 2 的 P4/P5 各两例；七例目前均不足以登记超阈值遗漏优化。另有一个 P12 未通过时间差门槛，以及两个 P10 虽有计划遗漏证据但未通过初筛。此范围不等于对整轮每个阴性或超时候选都作了源码归因审查。后续案例应逐个记录输入、原生计划、普通时间与判定；源码或计划发现不能自动升级为超阈值性能缺陷，更不能替代现有阈值、独立确认和 [BUG list/AGENTS.md](../BUG%20list/AGENTS.md) 的报告要求。
