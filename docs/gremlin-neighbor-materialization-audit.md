# ArcadeDB Gremlin 邻居列表物化审查

核查日期：2026-10-18。对象是本轮 **ArcadeDB 26.10.2、build `e854692736b48971f92dff78a1e727bc570bb6a2`、TinkerPop 3.8.2** 的现有 S8 与 S6 查询。ArcadeDB 源码链接固定到实际运行 build；以下行号按原始源码逐行核对，不采用网页阅读工具压缩空行后的序号。

**结论：源码证实 `ArcadeVertex.vertices()` 会在返回第一个邻居之前，先构造完整的匹配邻居对象列表。S8 的 EXISTS 和 S6 的 LIMIT 已经限制上层输出，但无法阻止这个接口调用内部的提前全量包装。** 本页详细核查的两个初始案例未通过报告阈值；后续更大规模的 S8 经缩减和原存档重放通过，登记为 [BUG003](../BUG%20list/ArcadeDB/S8/BUG003.md)。不能将后续通过结论倒填到本页两个未达标输入上。

本文不修改 Pattern、图配方、查询变换或编译结果，也不引入原生性能探针（Native probe）。后续扩大规模、缩减和重放仍须通过现有流程，最终判定以 [续测记录](gremlin-p10-scor-20261018.md) 为准。

## 1. 已观察到的实际查询与计划

### S8：只消费一个成功见证

案例为 [`0001-S8-n10000-587b9ecd`](../results/gremlin-p10-scor-20261018/scor-performance/20261018T074851Z-2ca749/0001-S8-n10000-587b9ecd/result.json)，`seed=0, complexity=0, data_seed=0`，输入哈希为 `587b9ecd53d85a7c6603444313f01088cd278e3531506910079bb874ce0ac1d6`。A/B 分别访问 `GSP_SC_HIGH`、`GSP_SC_REF`，其余实际遍历相同；完整查询见 [A](../results/gremlin-p10-scor-20261018/scor-performance/20261018T074851Z-2ca749/0001-S8-n10000-587b9ecd/query_a.gremlin) 与 [B](../results/gremlin-p10-scor-20261018/scor-performance/20261018T074851Z-2ca749/0001-S8-n10000-587b9ecd/query_b.gremlin)。两侧均返回一行 `{"u.uid":1}`。

现有图态具有一个 ScorRoot、`4N=40000` 个 HIGH 邻居及 3 个 REF 邻居，所有邻居都满足当前 EXISTS 的标签和 key 等式。原始遍历的 `outE(...).inV()` 在实际计划中已成为返回顶点的 `VertexStep`，不是返回边的 `VertexStep`。

以下为保存的原生计划摘录；`cost`、`totalCost` 单位为纳秒，属于 PROFILE，不能当作普通运行时间：

| 查询 | 原生步骤 | elements | traversers | cost / totalCost |
|---|---|---:|---:|---:|
| A | `VertexStep(OUT,[GSP_SC_HIGH],vertex)` | 1 | 1 | 13,279,332 / 13,279,332 |
| B | `VertexStep(OUT,[GSP_SC_REF],vertex)` | 1 | 1 | 1,018,874 / 1,018,874 |

独立普通配对确认的合并中位比值为 **A/B 5.9512 倍**、中位差值 **8.1109 ms**；两次确认的差值分别为 **8.5040 ms、7.4167 ms**。结果为 `not_confirmed / confirmation_gate_not_met`，明显未达到 100 ms 差值门槛。

### S6：LIMIT 生效，但调用相同的邻居接口

案例为 [`0067-S6-n100000-039465ab`](../results/gremlin-p10-scor-20261018/scor-performance/20261018T074851Z-2ca749/0067-S6-n100000-039465ab/result.json)，`seed=3, complexity=0, data_seed=0`，输入哈希为 `039465abb2ed0210d7d0fa0d2a8b2b1a14b108b89a06e8de1e8b4b542beae6fe`。A/B 都以 `constant(0L)` 投影并以 `limit(1)` 结束，只有关系类型分别为 HIGH、REF；实际查询见 [A](../results/gremlin-p10-scor-20261018/scor-performance/20261018T074851Z-2ca749/0067-S6-n100000-039465ab/query_a.gremlin) 与 [B](../results/gremlin-p10-scor-20261018/scor-performance/20261018T074851Z-2ca749/0067-S6-n100000-039465ab/query_b.gremlin)。两侧均返回一行 `{"value":0}`。

原生计划摘录如下，时间字段同样仅为 PROFILE 纳秒：

| 查询 | 原生步骤 | elements | traversers | cost / totalCost |
|---|---|---:|---:|---:|
| A | `VertexStep(OUT,[GSP_SC_HIGH],vertex)` | 2 | 2 | 100,811,210 / 100,811,210 |
| B | `VertexStep(OUT,[GSP_SC_REF],vertex)` | 2 | 2 | 10,063,875 / 10,063,875 |
| A | `RangeGlobalStep(0,1)` | 1 | 1 | 51,043 / 51,043 |
| B | `RangeGlobalStep(0,1)` | 1 | 1 | 15,792 / 15,792 |

该实例确实进入与 S8 相同的顶点邻接接口；LIMIT 只输出一行。不能把上游的 2 个输出误写为“只构造两个邻居”，也不能把它改写成实测输出 400000 个。TinkerPop 的范围过滤器在收到输入后检查高端计数并终止，见 [`RangeGlobalStep.filter():73–108`](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/filter/RangeGlobalStep.java#L73-L108)；此处重点是已保存的上层实际计数与桥接层内部工作不同。

该案例自动状态为 `timing_observation`，合并配对中位比值 **11.8883 倍**、差值 **113.9235 ms**。但两次独立确认分别为 **13.8929 倍 / 124.9149 ms**、**11.0027 倍 / 92.3511 ms**。第二次确认未超过 100 ms，因此按当前逐次报告复核要求不能登记 BUG；不能只取合并统计或较慢的一次确认。

## 2. 从上层短路到桥接层的调用链

| 层次 | 已核实行为 | 固定源码 |
|---|---|---|
| S8 过滤 | `TraversalFilterStep.filter()` 调用 `TraversalUtil.test()` | [TraversalFilterStep:61–64](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/filter/TraversalFilterStep.java#L61-L64) |
| 首次匹配 | `TraversalUtil.test()` 只测试子遍历 `hasNext()`，然后关闭遍历 | [TraversalUtil:139–146](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/util/TraversalUtil.java#L139-L146) |
| 邻接展开 | `VertexStep.flatMap()` 在返回类型为 Vertex 时调用 `vertices(direction, edgeLabels)` | [VertexStep:70–75](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/map/VertexStep.java#L70-L75) |
| 迭代器建立 | `FlatMapStep` 先取得 `flatMap()` 返回的迭代器，随后逐项输出 | [FlatMapStep:41–51](https://github.com/apache/tinkerpop/blob/3.8.2/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/map/FlatMapStep.java#L41-L51) |
| ArcadeDB 桥接 | `vertices()` 内部先填满 `ArrayList<Vertex>`，再返回 `result.iterator()` | [ArcadeVertex:252–302](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeVertex.java#L252-L302) |

因此，上层第一次请求一个邻居时，就会同步完成桥接函数的循环。随后 EXISTS 或 LIMIT 的停止能阻止更多输出，但不能撤回已经完成的列表构造。这里没有证据表明 EXISTS 继续消费所有成功见证，也没有排序操作的源码证据。

## 3. OLTP 底层具备逐项迭代，桥接层却强制耗尽

在线事务处理（Online Transaction Processing，OLTP）分支位于 [`ArcadeVertex:284–302`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeVertex.java#L284-L302)。当指定关系类型时，295–300 行遍历 `baseElement.getVertices(direction, edgeLabels)`，逐个检查记录是否存在，并创建包装对象。302 行才把列表迭代器交还上层。

继续向下的调用链为：

1. [`ImmutableVertex.getVertices():205–208`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/ImmutableVertex.java#L205-L208) 转发给 GraphEngine。
2. [`GraphEngine.getVertices()` 的 OUT 分支：2167–2185](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/GraphEngine.java#L2167-L2185) 返回可迭代对象；其 `iterator()` 返回 `outEdges.vertexIterator(edgeTypes)`。这个循环没有调用 `size()` 或 `count()` 的必要。
3. [`EdgeLinkedList.vertexIterator():230–235`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/EdgeLinkedList.java#L230-L235) 对带类型约束的请求创建 `VertexIteratorFilter`。
4. [`VertexIteratorFilter:37–63`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/VertexIteratorFilter.java#L37-L63) 的 `hasNext()` 每次准备一个邻居，`next()` 返回该邻居。
5. [`IteratorFilterBase.hasNext():59–129`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/IteratorFilterBase.java#L59-L129) 先按关系 bucket 过滤，再对匹配邻居调用 `lookupByRID(nextVertex, false)`，最后返回 TRUE；不会预先把全部邻居装入列表。

关系类型筛选在 [`MutableEdgeSegment.nextEntryInBuckets():552–568`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/MutableEdgeSegment.java#L552-L568) 按原始邻接条目顺序读取 bucket 编号，并跳过其他类型的编码条目。**这不等于读取那些不匹配边的完整记录或不匹配邻居的属性。** 在无损坏的 OLTP 邻接链被完全耗尽时，两条查询都会走到 OUT 邻接条目末端；其匹配邻居数量分别为 `4N` 和 3，进入后续查找、存在性检查和包装的数量也不同。

超级节点可能使用分条邻接表。[`GraphEngine.buildEdgeList():2559–2564`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/GraphEngine.java#L2559-L2564) 根据 head 类型选择普通或分条列表；[`StripedEdgeList.vertexIterator():443–445`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/StripedEdgeList.java#L443-L445) 与 [596–625 行](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/graph/StripedEdgeList.java#L596-L625) 组合各链的迭代器。本文未从运行时证据辨认本例实际采用哪种布局，不能声称已测得具体条带数量；两种布局都不消除上层桥接循环的完整耗尽。

## 4. GAV 分支与可量化边界

图分析视图（Graph Analytical View，GAV）可提供邻接数组。`ArcadeVertex.vertices()` 先检查 provider，若命中则取得邻居编号数组，再对整个数组逐项取得 RID、转换顶点并创建包装对象，之后才返回迭代器，见 [`ArcadeVertex:255–280`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeVertex.java#L255-L280)。本文没有运行时 provider 选择证据，也不假定该数组本身每次都重新构造。

以下结论需分开表述：

| 证据类型 | 可以陈述 | 不能据此陈述 |
|---|---|---|
| 已保存原生计划 | S8 两侧 VertexStep 各输出 1；所核查 S6 两侧输出 2、Range 各输出 1 | PROFILE 已测得内部创建了 4N 个对象 |
| 桥接源码与已校验图态 | 有效匹配邻居全部被包装后才返回；现有图态对应 HIGH 4N 个、REF 3 个 | 已通过分配采样实测对象数；每个对象都发生一次物理磁盘读取 |
| OLTP 迭代器源码 | 类型过滤逐项跳过原始邻接条目；匹配邻居按需查找；完整耗尽会遍历到链末端 | 两侧都加载了所有边记录或全部顶点属性 |
| 分支源码 | OLTP 与 GAV 两个桥接分支均有完整包装循环 | 本轮已确认选中了某个 GAV，或已测得具体分条布局 |
| 普通配对计时 | 两个具体案例保存的倍率与差值 | PROFILE 纳秒是普通运行时间；合并统计达标就代表每次独立确认达标 |

这里的“4N 与 3”是**按源码和图态推导的内部匹配邻居处理量**，不是新增的实测工作量指标。以无删除、无损坏且所有相关记录存在的现有配方为前提，可请求避免首个成功结果之后的剩余邻居查找、存在性检查和包装；不能在缺少补丁对照或运行时追踪时，把全部 A/B 时差精确分摊给某一个内部调用。

## 5. 优化请求与后续判定

可讨论的优化是保留原有顺序、重复项、删除过滤、事务可见性和终止检查，以惰性包装向 TinkerPop 返回邻居；上层停止消费后，不再处理后续匹配邻居。底层已有逐项迭代接口，故该请求不是要求新建图索引，也不是要求任意遍历重排。

S8 与这里已检查的 S6 具有相同 `VertexStep(...,vertex) → ArcadeVertex.vertices()` 调用链，可作为同一候选根因的两种既有 Pattern 触发方式，不应各计一个独立根因。返回边的 `edges()` 也存在列表构造，但本文两个实际计划走的是顶点路径，不能把未测的边路径算作额外发现。

本页所列初始案例仍不满足最终报告条件。后续 S8 在 N=200,000 时因同查询噪声过大未通过；N=150,000、seed=3 缩减案例及其原存档重放分别通过完整门槛，后者普通配对中位倍率为 26.65743×、差值 154.79 ms，两次独立确认分别为 149.51／158.92 ms。其报告按 [`BUG list/AGENTS.md`](../BUG%20list/AGENTS.md) 登记为 BUG003；S6 不另增根因。具体原始记录、失败保留和报告建图验证见 [续测记录](gremlin-p10-scor-20261018.md)，不据此预测修复后耗时。
