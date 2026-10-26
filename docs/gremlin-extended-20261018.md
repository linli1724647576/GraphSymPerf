# P3、P13、P16 的 Gremlin 编译扩展与验证

日期：2026-10-18。目标：JanusGraph 1.1.0 和 Apache HugeGraph 1.7.0；P7 继续跳过。P3、P13、P16 编译与真实接口验证已完成，两库各完成 56 个既定性能输入；经缩减、独立重放及报告代码实测后，登记三份本地优化请求。它们对应本轮两个优化机会族，均未提交上游。

## 范围与方法保持

本轮补齐既有 P3、P13、P16 的查询中间表示（Intermediate Representation，IR）到 Gremlin 编译，以及 HugeGraph P3 必需的后端装载适配。没有增加 Pattern，没有改变原始与变换查询的定义，也没有引入手写性能探针。`Src/generator.py`、`Src/mutator.py`、`Src/state.py`、`Src/ir.py` 与当前 Git HEAD 无差异；基线提交和 SHA-256 见 [`source-manifest.json`](../results/gremlin-extended-20261018/source-manifest.json)。

实验仍按 `instantiate → mutate → compile/run` 产生查询对，沿用已有 `original`、`pruning_path`、`multi_low` 图态及种子机制。用于验证空值、自环和重复行的手写小图属于编译器语义测试，不计入 Pattern 性能发现。原有 ArcadeDB 结果与本轮两个目标分别保存；不能把一次编译通过当成三个数据库都完成运行验证。

## 实际目标与接口

两个专用 ARM64 容器均通过 Gremlin-Groovy HTTP 执行。主机为 macOS ARM64，实际版本请求、容器镜像摘要及 JVM 参数保存在 [`environment.json`](../results/gremlin-extended-20261018/environment.json)。

| 目标 | 实测版本 | TinkerPop | 本轮定位 |
|---|---|---|---|
| JanusGraph | 1.1.0 | 3.7.3 | P3、P13、P16 的主要实测目标 |
| Apache HugeGraph | 1.7.0 | 3.5.1 | 同上；P3 使用原生父／子边标签物理映射 |

运行时版本与固定发行源码一致，见 [JanusGraph v1.1.0 POM](https://github.com/JanusGraph/janusgraph/blob/v1.1.0/pom.xml) 和 [HugeGraph 1.7.0 POM](https://github.com/apache/hugegraph/blob/1.7.0/hugegraph-server/pom.xml)。编译器只使用这些版本可执行且通过本轮检查的步骤；没有借用新版 `all()` 等同名步骤假定兼容。请求参数、完整结果读取、物理列到逻辑列映射和类型多重集比较继续使用现有执行接口。

## P3：有界关系不重复路径

P3 的语义是关系不重复路径（TRAIL）：同一路径内不能重复使用同一条边，允许重复经过顶点。编译使用 `repeat()` 扩展，每条路径分别保留节点标签和边标签历史；通过 `select(Pop.all, edgeLabel)` 检查新边是否已经出现。它没有使用全局 `dedup()`，也没有用 `simplePath()` 代替 TRAIL。因此，两条平行边构成的“离开顶点再返回”仍可形成合法路径，同值投影的不同路径仍保留重复行。

无向一步展开由出边与入边分支组成；入边分支排除自环的第二次计入。长度上下界、零跳路径、终点标签／属性和已有绑定都独立检查。`length(p)` 返回当前模式中的边数；`nodes(p)` 只包含该模式的路径节点，不混入此前 MATCH 留下的遍历历史。

`pruning_path` 的 `ALL(nodes(p))` 保留在原 IR 对应的谓词位置。编译器没有提前把 `keep=true` 推入每一次展开，也没有用已知图态删除无效分支。该限制保留了用户要求测试的现有查询机制，但意味着 Gremlin 中明确指定的起点、展开次序和后置谓词成本需要在性能归因时单独判断。两端镜像后耗时不同，本身不足以证明优化遗漏。

有限路径仍只开放 TRAIL；`walk`、`acyclic`、重绑路径变量及具名路径关系不自动降级为其他语义。当前实现对 hop 上限保留 32,767 的明确边界，现有 P3 最大只有 3 跳；不能将本次有限模板支持表述为任意长度路径查询都已验证。

### HugeGraph 的 P3 物理映射

既有 P3 的逻辑关系类型 `GSP_LINK` 连接多个顶点标签对。HugeGraph 1.7.0 普通边标签只允许一对端点，因此后端将 `GSP_LINK` 建为原生父标签，每个端点对建一个确定命名的子标签。装载按对应子标签创建边；生成查询仍使用原来的 `outE('GSP_LINK')`／`inE('GSP_LINK')`，交给数据库的父类型查询处理。

这项映射不修改 GraphSpec、顶点标签、节点数、边数、方向、连接关系或属性；`_gsp_edge_ordinal` 保留平行边身份。状态检查同时核对父／子类型、父标识、端点对、属性、sort key、schema 状态，以及全部边的数量和 ordinal。清理时先删子类型，再删父类型，等待相关任务完成。固定提交的官方 schema API、测试和源码依据见 [`hugegraph-schema-audit.md`](../results/gremlin-extended-20261018/hugegraph-schema-audit.md)。

其等价边界是当前 IR 可观察的节点属性、边身份、路径长度和路径节点谓词。原始 `edge.label()` 仍返回物理子标签，不能声称原始边 JSON、边标签投影或未来 `type(r)` 扩展也透明。P7 的单顶点多标签合取不在这项适配范围内，P12 的多端点装载边界也没有顺带放开。

### 服务器重启与清理边界

初次接口门禁完成后，HugeGraph 服务器重启暴露了其 1.7.0 RocksDB 父／子边元数据持久化缺陷。原 8 条夹具边的父／子 ID 组合发生变化；重复 `drop()`、显式提交和直接 `remove()` 均留下 8 条边。第一轮性能输入因此停在重置阶段，921 秒后人工中断，没有形成有效查询耗时。原始 `started` 记录、旁边的 `interruption.json` 及排除的诊断轮次均保留；这不是 921 秒的单次查询性能结果。

后端修复只作用于 HugeGraph 清理：先核查本工具父／子标签的类型、father 和端点哈希，异常时明确 `stop_target`；正常清理按实体 ID 批量删除，检查相邻批次的重复 ID，并以初始数量限制批次数。JanusGraph 的清理逻辑、计时与测量方法未改。诊断源码及实测见 [hugegraph-parent-restart-diagnosis.md](../results/gremlin-extended-20261018/hugegraph-parent-restart-diagnosis.md)。

第二轮使用同固定镜像的全新专用存储，完整 102 项接口通过；最后 8 条父／子夹具边成功清空。P3 N=1,000 的 original 与 pruning_path 又分别通过新建、重连复核、独立重建和最终清空。性能在该 JVM 内继续，期间不重启带父／子 schema 的存储。这个范围不能表述为父／子图已通过服务器重启耐久性验证。[新门禁](../results/gremlin-extended-20261018/interface-gate-v2.json)与[清理重建证据](../results/gremlin-extended-20261018/interface-hugegraph-reset-v2/validation-summary.json)。

### 大图准备超时与第三版门禁

后续 HugeGraph N=10,000、seed=4 在 `discovery_setup` 阶段触发原 30 s 请求超时，尚无普通查询样本。该记录不能当成查询耗时或超阈值 BUG。为限制单次清理请求，后端将删除批次由 1,000 下调到装载器沿用的 250，并在清理异常中记录阶段、实体类型、批次和初始数量；原采样、超时和报告门槛不变。原超时记录没有更细的操作标签，因此这里不将失败精确归因到某一条删除请求。

新空存储再次完成 102 项接口检查；另用同一失败输入完成 **30,003 顶点、20,002 边**的装载、全套状态检查、A/B 结果核对和清空，442 个接口请求均完成。证据见 [大图准备门禁](../results/gremlin-extended-20261018/interface-large-reset-v3/validation-summary.json)及[逐请求诊断](../results/gremlin-extended-20261018/interface-large-reset-v3/requests.jsonl)。这轮属于接口复验，不计入性能输入。

通过门禁后才以 [interface-gate-v3.json](../results/gremlin-extended-20261018/interface-gate-v3.json) 冻结源码，并原样重放旧失败存档，已得到完整 `timing_observation`；旧 timeout 继续保留。此次仅调整 HugeGraph 的计时前清理逻辑，普通查询文本和 Pattern／测量方法均不变。当前离线回归仍为 **1,422 passed、742 deselected**，见 [v3 日志](../results/gremlin-extended-20261018/offline-tests-v3.txt)。

## P13：OPTIONAL 的绑定与空值延展

每个 OPTIONAL 将完整候选分支及其 WHERE 放入 `coalesce()` 的第一个子遍历；仅当该分支完全无结果时才产生一行空值回退。该结构保留全部匹配结果与平行边造成的重复行，没有把 OPTIONAL 变为“取第一个匹配”。两个独立分支的顺序仍来自原查询及现有变换，两个 Low 匹配与三个 High 匹配应产生六行。

`CoalesceStep` 的子遍历标签不能直接视为外层绑定：底层 `FlatMapStep` 取子迭代器输出的对象，再由父 traverser 分裂生成外层结果。因此编译先把本分支新变量装入明确的绑定 map，再在 `coalesce()` 外逐项恢复标签。匹配与空值回退使用相同键集合；已有父作用域变量不被覆盖。源码依据见 [TinkerPop 3.5.1 FlatMapStep](https://github.com/apache/tinkerpop/blob/3.5.1/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/step/map/FlatMapStep.java)。

JanusGraph 的可空实体直接绑定 NULL。HugeGraph 所用 TinkerPop 3.5.1 的标签路径实现会在向 NULL 当前对象添加标签时调用 `equals()`，本轮接口测试触发了空指针错误。因此仅对 HugeGraph 的可空节点、关系和内部路径对象使用布尔值 `false` 作为实体哨兵；它不会与 Vertex、Edge 或路径 map 相撞。属性读取、图展开、`length(p)` 和 `nodes(p)` 都先排除该内部标记，再按 IR 产生真实 NULL。最终 HTTP 物理结果包含 JSON NULL，不由客户端将任意布尔 `false` 改写成 NULL；合法布尔属性和标量 `false` 保持原值。对应旧版实现见 [B_LP_O_S_SE_SL_Traverser](https://github.com/apache/tinkerpop/blob/3.5.1/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/traverser/B_LP_O_S_SE_SL_Traverser.java)。

可空变量后续作为 MATCH、EXISTS、COUNT 的起点时先守卫实体有效性，不会把 NULL 当成未绑定变量而重新全图扫描。属性 `IS NULL`、缺失属性、可选分支自身 WHERE 不成立和两侧均无匹配也按空值延展检查。

## P16：服务器构造列表与三值 ALL

P16 保留已有 `[0]`／`[0,0]`、升降序整数范围、拼接交换和谓词包装。范围元素没有放进客户端 bindings，也没有在客户端展开。编译只做常数时间的基数与最终值计算；服务端使用 Long 常量、`sack(Operator.assign)`、`sack(Operator.sum)` 和 `repeat()` 生成每个整数，再由 `fold()` 构造列表。完整列表构造处于每次普通查询的计时范围内，编译器不会看到低成本列表含 0 就直接输出 FALSE。

终止条件比较当前数值与准确的最终元素，使用 `until()` 而不依赖大范围的 `times()`／`loops()`。这是因为旧版 traverser 用有符号 short 保存循环次数，超过 32,767 次会发生计数回绕；见 [TinkerPop 3.5.1 B_O_S_SE_SL_Traverser](https://github.com/apache/tinkerpop/blob/3.5.1/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/traverser/B_O_S_SE_SL_Traverser.java)。整数累加避免 `MathStep` 的 double 转换，因此没有把超过 2^53 的整数近似成浮点数。动态 range 端点、超过实现基数上限的范围仍显式拒绝。

拼接分别求值左右列表一次，并保留 A/B 顺序；任一列表为 NULL 时结果为 NULL。HugeGraph 使用 map 保存可能为 NULL 的列表，避免给 NULL 添加标签。ALL 的非 NULL 元素、总定义谓词分支采用单次有序的“是否存在不满足元素”遍历。通用三值分支先找 FALSE，再在未找到 FALSE 时找 UNKNOWN，因而最多扫描两次：空列表为 TRUE、NULL 列表为 NULL、`[1,NULL]` 对 `x>0` 为 NULL，而 `[NULL,0]` 和 `[0,NULL]` 都为 FALSE。

HugeGraph 仅对可能包含 NULL 的 ALL 标量绑定加一层 map 包装，在读取局部变量时解包；原始 P16 的非 NULL 整数元素不加此包装，布尔 `false` 也不充当标量空值标记。现有复杂度 1/2 的 CASE 等包装可能触发保守三值分支，但遇到 FALSE 后不会继续执行 UNKNOWN 扫描。此实现保留查询所要求的判断工作，不根据实验图态硬折叠谓词。

列表构造成本本来就必须保留；早／晚遇到 FALSE 导致正常短路耗时差，不直接登记为数据库 BUG。新增的列表投影还沿用数值 DISTINCT 保护，含浮点列表／拼接的潜在整浮点相等性差异在没有证明前明确拒绝。

## 初次语义和接口门禁

初次门禁的离线测试日志为 **1,416 passed，740 deselected**。JanusGraph 扩展套件 **99 passed、1 skipped**，跳过项为仅适用于 HugeGraph 的父／子边类型门禁。HugeGraph 通用套件先完成 **99 项**；父／子图测试夹具补齐 `counts()` 后，专用复跑 **1 passed**，其证据汇总现为 **100 项 passed**。这里分别记录真实运行，未把早期失败日志改写为整套一次通过。证据见 [`offline-tests.txt`](../results/gremlin-extended-20261018/offline-tests.txt)、[JanusGraph 日志](../results/gremlin-extended-20261018/interface-janusgraph-final.txt)、[HugeGraph 日志](../results/gremlin-extended-20261018/interface-hugegraph-final.txt) 和[父／子类型复跑](../results/gremlin-extended-20261018/huge-parent-gate-final.txt)。

| 检查类别 | 每个目标的检查项 | 主要覆盖 |
|---|---:|---|
| P3 小图语义 | 16 | TRAIL、顶点回访、自环、平行边、上下界、零跳、前置 MATCH 历史、节点谓词 |
| P13 小图语义 | 16 | 空值延展、重复行、分支 WHERE、多个新变量、后续可空起点与可选路径 |
| P16 列表／量词语义 | 33 | TRUE/FALSE/NULL、空列表、拼接顺序、升降范围、非整除步长、大整数和 40,000 元素范围 |
| 编译边界 | 1 | 动态 range 显式拒绝 |
| 既有 Pattern 输入 | 33 | 3 Pattern × 3 种子 × 3 复杂度，另加 2 个既有图态 × 3 种子 |
| HugeGraph 父／子类型 | 1，仅 HugeGraph | 多端点 schema、边身份、平行边、自环、正反向路径及实际父类型查询 |

Pattern 输入使用种子 0、3、17 和复杂度 0、1、2；`pruning_path`、`multi_low` 的额外接口检查使用复杂度 2。验证包含装载与完整状态核查、A/B 与独立预期值的类型多重集一致、重建后的结果、查询归档读回，以及 explain/profile 接口。原生结果、查询、bindings、状态和计划保存在 [`interface-janusgraph`](../results/gremlin-extended-20261018/interface-janusgraph) 与 [`interface-hugegraph`](../results/gremlin-extended-20261018/interface-hugegraph)。

门禁阶段修复的是本工具的兼容与实现问题：OPTIONAL 子标签没有逃逸、旧版 NULL 添加标签失败、长整数范围依赖 short 循环计数、HugeGraph 物理子标签导致旧状态检查不匹配，以及数值列表 DISTINCT 保护遗漏。这些不作为本轮数据库优化遗漏报告。此前失败、针对性重试和最终证据均保留。

### 补充修复后的门禁

零跳路径的 `length(p)` fallback 原为 Java Integer 0，而整数范围及 HTTP 整数绑定为 Long；两者作为嵌套列表去重键时会产生重复 `[0]`。JanusGraph 两个反例先失败，条件修复为 `low==0` 使用 `0L` 后两项均通过；HugeGraph 最新完整 102 项也包含这两项。正跳数模板保留原文本，已核对当时 57 份现有存档的编译对象与查询文件完全不变。[类型审查](../results/gremlin-extended-20261018/distinct-list-boundary-audit.md)、[编译一致性](../results/gremlin-extended-20261018/zero-hop-compile-equivalence.json)。

最新离线结果为 **1,422 passed、742 deselected**，见 [offline-tests-v3.txt](../results/gremlin-extended-20261018/offline-tests-v3.txt)。HugeGraph 在最新空存储的完整重跑为 **102 passed**，见 [interface-hugegraph-runtime4.txt](../results/gremlin-extended-20261018/interface-hugegraph-runtime4.txt)。JanusGraph 在最终缩减前又对当前源码完成整套 **101 passed、1 skipped**，见 [interface-janusgraph-v2.txt](../results/gremlin-extended-20261018/interface-janusgraph-v2.txt)；原 99 项及零跳修复前后日志仍保留。初次、第二轮和第三轮源码门禁分别保存在 `interface-gate.json`、`interface-gate-v2.json`、`interface-gate-v3.json`，没有覆盖原证据。

## 性能输入与原阈值

先对每组既有模板枚举种子 0–63，按无序 IR 查询对去重；没有把仅 A/B 互换的重复形状当成新覆盖。库存见 [`seed-inventory.json`](../results/gremlin-extended-20261018/seed-inventory.json)。本轮每库 **56 个输入均已完成**，统一复杂度 0、data_seed 0；每库范围和异常状态见 [覆盖清单](../results/gremlin-extended-20261018/coverage.json)。

| Pattern／既有图态 | 不同查询对 | 代表种子 | 规模 N | 每库计划输入 |
|---|---:|---|---|---:|
| P3 original | 8 | 0–7 | 1,000；10,000 | 16 |
| P13 original | 3 | 0、2、4 | 1,000；10,000 | 6 |
| P16 original | 8 | 0–7 | 1,000；10,000；100,000 | 24 |
| P3 pruning_path | 2 | 0、1 | 100；300 | 4 |
| P13 multi_low | 3 | 0、2、4 | 1,000；3,000 | 6 |

P3 pruning_path 原计划的 N=1,000／10,000 在该阶段启动前改为 100／300：现有反向遍历从 N 个 High 起点到达共享 Bad，再展开 N 个 Middle，末跳中间路径可能按平方增长。这里限制单次测试规模以遵守原 30 s 请求超时，不改变图配方、种子或判定门槛；计划仍为每库 56 个输入。[调整记录](../results/gremlin-extended-20261018/scale-plan-revision.json)

P13 multi_low 同样在启动前调整为 N=1,000／3,000：JanusGraph 原态 N=10,000 的完整原生 PROFILE 归档已达 305–396 MiB，多重匹配会增加分支评估次数。采用这个规模以保留完整计划并约束剖析器／客户端内存；不降低计时阈值，也不减少代表种子。[调整记录](../results/gremlin-extended-20261018/optional-scale-plan-revision.json)

N 是既有图配方／列表规模参数，不在不同 Pattern 间代表相同顶点数。P16 没有图数据，其高成本范围包含 4N 个整数。两个数据库各自执行与确认，不能混合原始样本计算倍率。

测量保持原约束：筛选预热 3 次、6 组平衡配对，筛选倍率 2；确认重建两次独立会话，每会话预热 5 次、20 组配对，以及每侧 10 组自身对照。报告门槛继续为配对中位倍率至少 **5×**、bootstrap 置信区间下界至少 **5×**、中位时间差严格大于 **100 ms**、方向一致率至少 **0.95**，并复核每个独立会话分别满足倍率与时间差门槛；同查询噪声按原有控制样本检查。bootstrap 使用 10,000 次重采样。单次请求超时 30 s、完整结果上限 10,000 行。

超过计时门槛后仍须检查源代码／原生计划所支持的额外工作、缩减与复验、归档重放和根因去重。显式遍历起点／分支顺序造成差距本身不构成 BUG；还需要证明可以在保留语义的前提下省去哪些工作。P16 正常短路、必需列表构造及编译器自身开销均不登记。通过这些检查的条目才按 [`BUG list/AGENTS.md`](../BUG%20list/AGENTS.md) 写入本地 BUG list。

## 最终审计与缩减重放

性能停止后才运行完整离线审计。主矩阵有 **112 份成功最终记录**，另保留 1 份准备超时和 2 份中断的 `started` 快照；超时输入在新空存储原样重放成功，没有增加查询形状。113 份最终归档全部通过输入完整性、IR／bindings 重新生成、原始样本统计和保存的状态行检查，审计错误为 0。原查询完整结果的类型多重集一致性由执行器逐次检查；主性能归档没有保存每次完整结果，不能宣称离线又独立核验过全部结果袋。最终报告则另有完整结果实测保存。

主矩阵重新核算 6,528 对普通查询、5,280 对同查询控制及 1,944 对预热，含 P16 的独立 AB、A/ref、B/ref 比较。29 个输入出现严格计时信号，共涉及 42 个比较；这些数量不是已证明的优化遗漏或独立查询形状数。证据见 [主矩阵审计](../results/gremlin-extended-20261018/campaign-audit.json)。

候选继续通过既有 `Src/main.py`，只缩小原图配方的 N，不改查询定义或阈值。三个目标族共完成 6 个缩减输入及 4 次原存档重放，10 份归档全部通过审计，重新核算 460 对普通查询与 400 对自身控制。[缩减／重放进度](../results/gremlin-extended-20261018/focused-progress.json)、[独立审计](../results/gremlin-extended-20261018/focused/campaign-audit.json)。

| 目标／图态 | 原矩阵通过种子 | 缩减和独立重放情况 | 最终报告规模 |
|---|---|---|---:|
| JanusGraph P3 pruning_path | N=300，种子 0、1 | N=150 缩减通过，独立重放差值 97.169 ms，失败保留；N=200 缩减及重放通过 | 200 |
| HugeGraph P3 pruning_path | N=300，种子 0、1 | N=150 未通过；N=200 缩减及重放通过 | 200 |
| HugeGraph P13 multi_low | N=3,000，种子 0、2、4 | N=1,500 未通过；N=2,000 缩减及重放通过 | 2,000 |

这里报告的是尝试过的较小可复现输入，没有证明 N 为全局最小规模。没有删除失败的重放，也没有用通过的单会话替代两次独立确认。

## 已登记的三份报告

| 报告 | q1 中位数 | q2 中位数 | 配对方向与倍率 | 配对时间差 | 95% 倍率区间 |
|---|---:|---:|---|---:|---|
| [JanusGraph P3 BUG003](../BUG%20list/JanusGraph/P3/BUG003.md) | 13.570770 ms | 182.579020 ms | q2/q1，13.910900× | 169.025042 ms | [13.370845, 14.258182] |
| [HugeGraph P3 BUG003](../BUG%20list/HugeGraph/P3/BUG003.md) | 9.419041 ms | 150.805104 ms | q2/q1，16.038107× | 141.197709 ms | [15.682554, 16.230745] |
| [HugeGraph P13 BUG004](../BUG%20list/HugeGraph/P13/BUG004.md) | 144.660625 ms | 19.656271 ms | q1/q2，7.335498× | 124.922334 ms | [7.071285, 7.485555] |

每行来自最终原存档重放的 40 对普通查询；倍率是配对比的中位数，不是前两列中位数的商。每个独立会话及合并样本均通过原门槛。三例最大自身控制 p99 分别为 40.087475、12.500498、10.340298 ms，均小于对应差值。

**P3 pruning_path。** 两库 N=200 的原生 `RepeatStep` 输出均为 q1=403、q2=40,403。反向遍历从 200 个分支 High 起点经过共享 Bad 后，还生成 40,000 条延长路径；这些前缀已经包含 `keep=false`，无法通过全路径条件。两侧都在 Repeat 后检查节点条件，ALL 本身已经短路；快侧通过较少起点避免组合，没有实际实现前缀剪枝。因此报告请求跨 `repeat → path-node projection → filter` 识别前缀单调性，不声称漏用索引、既有规则实现错误或补丁实测收益。初步源码与计划审查见 [P3 审查](../results/gremlin-extended-20261018/p3-pruning-optimizer-audit.md)，最终裁定及所用 N=200 原生指标以 [report-manifest.json](../results/gremlin-extended-20261018/report-manifest.json) 为准。

**HugeGraph P13 multi_low。** N=2,000 时，q1 LOW 先产生八个 anchor 1 的绑定，再为相同 anchor 重复执行 HIGH 分支；HIGH 邻接与 keep 属性输出均为 64,009，q2 为 8,002。HIGH 仅依赖 anchor、纯读图，不依赖 LOW 变量；q2 先执行 HIGH 后扩出 LOW 多重结果，保留完整十行与两个 NULL 行。报告请求避免重复计算，不把 q2 写成缓存已生效，也不要求任意 OPTIONAL／Coalesce 均可重排。[源码与机制审查](../results/gremlin-extended-20261018/p13-huge-optimizer-audit.md)。

两份 P3 共享同一跨步骤优化机会；与 Neo4j 既有 P3 的方向敏感原生谓词提取失败区分。P13 与既有 FalkorDB P13 共享逻辑机制，是新增 DBMS 复现。这三份报告对应本轮两个机会族，尚未证明三个独立源码缺陷，也没有上游开发者确认。

## 报告复现代码与未登记线索

正式性能容器停止后，三个报告分别在全新专用容器执行正文中的完整原生建图语句及存档查询。验证器核对全部顶点和边的身份唯一性、拓扑及属性的类型多重集、索引／状态、HugeGraph 物理映射与边序号、完整物理列和结果袋，并保存原生 PROFILE。三例均通过，容器均已停止。[复现进度](../results/gremlin-extended-20261018/report-reproduction-v2/progress.json)。验证器的单次 A→B 用时不计入报告的配对中位数；GraphSON 解码统一 Integer／Long，不能把类型多重集核查扩大表述为所有 Java 运行时类型的逐项独立检查。

首轮导出器以裸 map 返回建图计数，HTTP 将其展开为两条 map-entry 结果，检查因此停止。改为单元素列表包住该 map 后，在新的存储完整重验；这只修复报告导出器的返回形式，没有更改正式编译查询或性能样本。首轮失败保存在 `report-reproduction/`，通过证据另存 `report-reproduction-v2/`。

- P3 original：两侧从不同候选起点搜索，所见差距不足以证明漏用局部索引或可安全提前失败的前缀，不登记。[审查](../results/gremlin-extended-20261018/p3-optimizer-audit.md)。
- P16：两侧均构造必需的 4N 元素范围，早／晚遇到 FALSE 属正常短路；部分 A/ref、B/ref 超阈值来自参考查询省去列表构造，不登记为优化遗漏。[审查](../results/gremlin-extended-20261018/p16-optimizer-audit.md)。
- JanusGraph P13：在本轮 N=1,000／3,000、三个代表种子内未达到原阈值，不外推为不存在相关机会。
- P7 按用户要求跳过；S1–S10 的既有 ArcadeDB 结果单独保存，本轮没有声称它们在 JanusGraph／HugeGraph 已完成接口和性能验证。
