# Overview 原图到六阶段方法的修改说明

本图依据 [六阶段方法汇报稿](../docs/GraphSymPerf-六阶段方法汇报稿.md) 修改用户提供的 Overview。保留原图左侧构造、右侧查询对、底部执行与验证的布局，以及黑色虚线容器、短路／对称分支、绿 L 红 H、查询纸张、数据库、结果表、计时器和缺陷图标。蓝色仅强调本轮新增或显式分离的环节。

## 必须新增或修改的部分

| 原图位置 | 修改后的内容 | 方法含义 |
|---|---|---|
| 左上 Skeleton Construction | 拆为① Random AST Generation 与② Pattern Embedding | 先独立随机生成 AST，再在带类型接口中嵌入一个兼容的局部测试模式；不能继续以两种完整固定骨架代表查询生成 |
| 原 Short-circuit / Symmetry skeletons | 改为 Short-circuit / Symmetry patterns | 保留原局部示例，但明确其身份是模式库中的局部规则，非整条查询 |
| 左下 Graph State Generation | 改为③ Pattern-aware Graph State | 同时接收完整 AST 的外层约束与局部 Pattern 前提，构造图、联合绑定和检查事实 |
| 右上 Base query 之前 | 新增④ Query Instantiation | 使用接受的图状态与绑定填充整个 AST，再得到完整 Q₁ |
| 原两个 mutation 分支 | 统一标为⑤ Pattern Mutation | 保留两类变异，但每个用例只应用指定 Pattern 的一次局部变换 |
| Query Pair 与 Execution 之间 | 新增⑥ Dependency Checking & Propagation | 从完整局部边界向根部传播结果关系，另行检查外层需求、逻辑输入多重性与共享读取 |
| 原 Equal results | 改为 Equal result bags | 比较完整结果的类型和值，同时保留重复行数量 |
| 原 Low / High cost | 改为 Low / High potential work | Few / Many 表示候选或读取区域规模，不能直接当作实际耗时 |
| 原 Missed optimization | 改为 Performance candidate | 结果相同且稳定变慢形成性能候选；遗漏优化的结论还需计划／源码确认 |

①—⑥对应六个方法阶段。Execution 与 Result & Performance Validation 作为后续运行验证保留，不再与方法阶段共用步骤编号。

## 图注建议

GraphSymPerf 首先生成带类型接口的随机 AST，再嵌入局部 Pattern。模式感知图状态构造联合满足局部前提与外层约束，接受的绑定用于实例化完整查询；一次指定变异形成共享同一图状态的查询对。查询对经结果关系与依赖检查后进入数据库执行，通过完整结果多重集比较和重复计时形成性能候选。遗漏优化结论另需执行计划或源码证据。

图内最小字号为37个SVG单位，按180毫米整图宽度放置时约为6.1磅；最终投稿仍应按目标版式确认实际字号。

## 数据流与原图相比的调整

1. 随机 AST A₀ 先进入 Pattern 嵌入，得到嵌入后的 AST A。
2. A 与 Pattern 要求进入图状态构造；A 同时作为整查询实例化的输入。
3. 构图输出 `(G, β, I)` 进入实例化，其中 β 为接受的终端联合绑定，I 为已检查图事实。
4. 实例化得到 Q₁，再按一个指定 Pattern 产生 Q₂；其余结构及同一份 G 保持不变。
5. 查询对以及图状态、图事实先进入依赖检查，通过后才进入数据库执行。
6. G 仍单独进入 Graph DBMS，保留原图中的建图／加载关系。
7. 依赖关系未知或整个目标无效时，按预算重新生成；细虚线标出重采样关系。

依赖检查针对完整 Pattern 边界。例如，S7 的 `coalesce` 整体被外层使用，而内部回退 H 应被短路，这是有效目标；整个 `coalesce` 的值被外层忽略才属于目标被屏蔽。

## 文件与复现

- [可编辑 SVG](Overview_six_stage.svg)
- [PNG 预览](Overview_six_stage.png)
- [SVG 生成脚本](generate_overview_six_stage.py)

在仓库根目录运行 `.venv/bin/python picture/generate_overview_six_stage.py` 可重建 SVG。图中全部文字、节点、边框和图标均为原生矢量元素，没有嵌入原图位图。图示不包含实测性能数值。
