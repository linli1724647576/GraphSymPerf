# Gremlin 编译边界：P3、P7、P13、P16

更新日期：2026-10-18。以下使用代码中的 P1–P16 编号。**P3、P13、P16 已在 JanusGraph 1.1.0／TinkerPop 3.7.3 和 HugeGraph 1.7.0／TinkerPop 3.5.1 补齐并通过语义与接口门禁；P7 继续跳过。** 详细源码、实现和实测证据见 [编译扩展与验证](gremlin-extended-20261018.md)。ArcadeDB 的运行能力名单保持此前 12 项 P 加 S1–S10；共用编译器扩展不代表它也完成相同的真实验证。

| Pattern | 当前实现 | 保留边界 |
|---|---|---|
| P3 | repeat 中按边身份实施 TRAIL，保留顶点重访、平行边、自环、长度和路径节点谓词 | 不用 simplePath，不提前下推原路径谓词；非 TRAIL、重绑路径及部分具名关系路径仍拒绝 |
| P7 | 继续明确拒绝 | 单标签模型不能直接表达原生多标签合取；父／子边标签不解决这个问题 |
| P13 | 完整 OPTIONAL 分支与 WHERE，无匹配时一份 NULL 延展，显式传递变量并保持重复行 | HugeGraph 旧版通过内部实体哨兵兼容 NULL 添加标签限制，最终结果仍是真实 NULL |
| P16 | 服务端 Long 范围、列表拼接、局部变量和三值 ALL | 动态范围端点、过大范围及未验证的数值 DISTINCT 拒绝；通用可空 ALL 可能扫描两遍 |

## 为什么不能机械替换

**P3：TRAIL 不是 simplePath。** TRAIL 只禁止同一关系在一条路径中重复使用，允许返回已经访问过的顶点。用两条不同平行边离开并返回同一顶点仍是合法路径；simplePath 会因顶点重复删掉它。当前实现按每条路径的边历史检查，不做全局去重；零跳、前置 MATCH 历史和反向书写均有独立结果检查。现有 P3 最大 3 跳，实现保守限制最大 hop 为 32,767。[语义测试](../tests/test_gremlin_extended_integration.py)

HugeGraph 原 P3 的同一 GSP_LINK 连接多种端点标签。后端使用原生父／子边类型作物理映射，查询仍用 GSP_LINK，不改 GraphSpec、拓扑、方向、属性或边身份。真实门禁额外检查父标签下的自环、平行边和反向身份；原始 edge.label() 仍暴露子类型，透明性仅覆盖当前 P3 可观察片段，不能推广到边标签投影或未来 type(r)。[固定版本模式审计](../results/gremlin-extended-20261018/hugegraph-schema-audit.md)

**P7：多标签合取不是任选标签。** 原模板要求同一顶点同时带 Rare 和 Common；多参数 hasLabel 表示任选一个，不能替换合取。集合属性编码会改成属性过滤／属性索引机制，因此本轮继续拒绝。TinkerPop 的 hasLabel 实现采用 within，见 [固定源码](https://github.com/apache/tinkerpop/blob/3.7.3/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/dsl/graph/GraphTraversal.java)。

**P13：回退输入不等于 NULL 延展。** optional 不自动建立缺失变量的 NULL 绑定，coalesce 子遍历标签也不自动传到外层。现在分支先输出绑定 map，再在外层恢复变量；无匹配才产生一行回退。HugeGraph 的可空实体使用与 Vertex／Edge／路径对象不相交的内部布尔哨兵，读取时恢复空值含义，不会把合法标量 FALSE 改成 NULL。重复匹配、分支 WHERE、双侧缺失、后续 EXISTS／COUNT 和可选路径都已实测。[编译器](../Src/gremlin.py)

**P16：同名步骤不是同种标量语义。** Gremlin range 是流截取，不能替代整数序列生成；新版 all(P) 也不是完整三值布尔标量。当前在服务器逐个生成整数，完整列表构造纳入计时，再按列表顺序判断 ALL。空列表为 TRUE，NULL 列表为 NULL，UNKNOWN 与 FALSE 同时出现时 FALSE 优先；超过 2^53 的整数保持精确。数值 until 终止避免旧版 short 循环计数回绕。[语义与固定源码审查](../results/gremlin-extended-20261018/source-audit.md)

HugeGraph 1.7.0 RocksDB 的父／子边元数据存在已复现的重启恢复问题：恢复出的边身份发生变化，删除无法生效。本工具只将全新存储、同一 JVM 内通过完整状态检查的 P3 运行纳入性能；发现恢复异常即停止，不修补数据库内部 schema。P13／P16 不依赖该父／子边映射，但共享测试实例若已有异常残留，也会被清理门禁拒绝。[原生诊断与边界](../results/gremlin-extended-20261018/hugegraph-parent-restart-diagnosis.md)

零跳路径的长度 fallback 已改为 Long 0，使 `[length(p)]` 与 `range(0,0)` 的列表去重一致。现有 P3 性能模板均为正跳数，已逐存档确认查询文本、绑定、列和能力需求未改变。

## 性能解释范围

扩展保持原 Pattern、变换和图配方，不表示数据库必定把显式 Gremlin 遍历改成低成本的另一条遍历。P3 的起点镜像、P13 的分支顺序和 P16 的早／晚短路可能产生正常耗时差异；超过时间阈值本身不足以证明遗漏优化。通用可空 ALL 的额外扫描也属于编译映射成本，不能归给数据库。

性能测试仍需完整结果检查、原阈值、两次独立确认、噪声控制、缩减复验、源码／计划解释及根因去重。只有符合这些条件的案例写入 BUG list；接口反例小图和编译拒绝检查不计为性能发现。
