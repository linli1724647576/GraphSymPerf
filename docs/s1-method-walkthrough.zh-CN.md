# GraphSymPerf 方法汇报：用 S1 走完一个查询对

本文沿用给定案例，说明抽象语法树（Abstract Syntax Tree，AST）如何经过位置选择、模式（Pattern）嵌入、图状态构建和局部变异，形成同图上的等价查询对。案例采用 Neo4j 的 Cypher 语法与作用域规则。本文保留 11 月 2 日归档的查询和图，当时存在性决定项复用 low 区域。新版为决定项单独构造见证，并按用例选择工作项。参见[现行模式目录](pattern-catalog.zh-CN.md)和[三个同宿主示例的生成脚本](../scripts/example_boolean_work_pool.py)。

**S1 测试的是：OR 左侧已经为真时，增大右侧的潜在计算量，是否仍会让查询明显变慢。** 测试从一棵完整的随机查询 AST 出发。嵌入后的源查询记为 Q₁，变异查询记为 Q₂，二者共用同一张图。

```text
生成完整符号 AST
  → 分析变量作用域，抽取兼容位置
  → 选择锚点与决定项，嵌入 S1
  → 绑定符号，构造背景图与高低成本区域
  → 实例化 Q₁，在副本上变异得到 Q₂
  → 比较完整结果，再测量耗时并检查执行计划
```

## 第 1 步：随机生成器展开完整 AST

生成器按语法产生式随机选择子句和表达式。生成一个布尔表达式时，可以选择属性读取、比较、AND、OR、CASE 或 EXISTS 等结构，再递归展开其子表达式。类型和当前可见变量限制每次选择，展开预算与嵌套深度控制查询大小。

本例的宿主结构如下。此时生成器还没有接收 S1 的编号。

```text
Query
├─ MATCH：定义节点变量 v1
├─ CALL：导入 v1
│  └─ Query
│     ├─ WITH：把 v1 起别名为 v2，读取属性得到 v3
│     └─ RETURN：读取 v2 的布尔属性，命名为 v4
└─ RETURN
   ├─ 返回 v1，命名为 v5
   └─ EXISTS 子查询，命名为 v8
      └─ RETURN DISTINCT：返回 CASE 表达式，命名为 v6
```

写成查询形式如下。尖括号内是尚未实例化的符号。

```cypher
MATCH (v1:<label_1>:<label_3>)

CALL (v1) {
  WITH v1 AS v2,
       v1.<property_float> AS v3

  RETURN v2.<property_bool> AS v4
}

RETURN v1 AS v5,
       EXISTS {
         RETURN DISTINCT
           CASE
             WHEN v1.<property_bool> THEN <constant_7>
             ELSE v1.<property_int>
           END AS v6
       } AS v8
```

### 可见变量表怎样更新

生成器维护的作用域（Scope）分为导入变量 `imported` 和局部变量 `local`；当前位置可见的是两者的合并。每个变量记录名称、类型、标签来源和可空性。这里还没有给变量分配具体的图节点。

| 生成到的位置 | 导入变量 | 局部变量 | 本次变化 |
| --- | --- | --- | --- |
| 外层查询开始 | 无 | 无 | 建立空作用域 |
| `MATCH` 之后 | 无 | `v1: node` | 加入 v1，记录它同时具有两个标签 |
| 进入 `CALL (v1)` | `v1: node` | 无 | 为子查询建立新作用域，只显式导入 v1 |
| 计算 `WITH` 的两个表达式 | `v1: node` | 无 | 两项都读取输入作用域中的 v1 |
| `WITH` 投影之后 | `v1: node` | `v2: node`、`v3: float` | 用投影别名重建局部表；v2 继承 v1 的标签来源 |
| 计算 `RETURN ... AS v4` | `v1: node` | `v2: node`、`v3: float` | 可以读取 v2 的属性；v4 此时还不是输入变量 |
| 离开 `CALL`，回到外层 | 无 | `v1: node`、`v4: bool` | 仅把子查询的返回列 v4 加入外层，v2、v3 不泄露 |
| 进入外层 `EXISTS` | `v1: node`、`v4: bool` | 无 | 表达式子查询继承外层可见变量，可以读取 v1 |
| 外层查询结束 | — | — | 对外输出 v5、v8；v6 仅是 EXISTS 内的返回列 |

`WITH v1 AS v2` 只给同一个节点起了新名字，不会复制节点。标签来源随别名保留，因此后面仍能知道 v2 来自 `MATCH (v1:<label_1>:<label_3>)`。Neo4j 的 CALL 导入变量在 WITH 后仍然可见，所以本例中 v1 和 v2 可以同时出现。

生成两个 WITH 表达式时，均使用投影前的作用域。两个表达式生成完毕后，才一次性更新局部表，同一投影中的新别名不会逐项加入输入作用域。

**符号绑定表记录“`<label_1> → Random_label_1`”这类对应关系，在构图阶段建立。** 它与这里的“`v2 → 节点类型及标签来源`”承担不同任务。

## 第 2 步：分析 AST，选择 S1 的替换位置

```python
site = cypher.choose_site(tree, pattern, rng, ...)
```

`choose_site()` 调用 `scope.analyze(tree)`，重新按查询顺序遍历整棵树。分析器每走到一个候选位置，就保存该位置的 AST 路径、节点和可见变量快照，同时检查引用的变量是否存在、类型是否匹配。

这次分析会重建第 1 步中的作用域变化。它不继续使用生成器内部的那张表，也不执行查询求取变量值。当前依赖分析主要解决“这里能引用谁、类型是什么、节点来自哪些标签”，尚不追踪所有表达式是否会被下游实际消费。

S1 接受布尔表达式位置。程序从兼容位置中随机选择，这次选中：

```text
RETURN v2.<property_bool> AS v4
       └────────────────┘
           替换整个表达式
```

该位置对应的变量快照为：

| 变量 | 类型 | 来源与可用信息 |
| --- | --- | --- |
| v1 | node | CALL 导入，标签为 `<label_1>`、`<label_3>`，非空节点 |
| v2 | node | `WITH v1 AS v2`，继承相同标签与非空性 |
| v3 | float | `WITH v1.<property_float> AS v3` 的标量投影 |

**Pattern 引用外层变量时，只能使用这份快照中的变量。** v4 尚未定义，v5、v8 属于后面的外层投影，都不能在这里被引用。

## 第 3 步：选择锚点和决定项，嵌入 S1

### 3.1 从可见节点中选择锚点

S1 的图查询需要一个出发节点，称为锚点（anchor）。`prepare()` 从位置快照中筛选“节点类型、有标签来源、非空”的变量。本例的候选是 v1、v2，v3 因为是浮点数被排除。

存在候选时，当前实现以 65% 的概率选择相关形式，再从候选中随机抽取一个；其余情况使用 Pattern 自己的独立根节点。本例抽到 **v2**，后面的局部子查询从当前输入行对应的 v2 出发。

### 3.2 决定项从候选池中抽取

S1 将当前位置替换为 `D OR W_low`。D 是决定项，要求对每个适用锚点都为真；`W_low` 是准备被变异的低成本工作项。`guard()` 按后端能力和锚点条件形成候选池，再随机选取一个候选。

本例采用 COUNT 子查询工作项，且存在外层锚点，因此可选以下五类：

| 候选 | S1 中的表达式形式 | 为真的依据 |
| --- | --- | --- |
| 布尔常量 | `true` | 字面量为真 |
| 常量比较 | `2 > 1` | 比较结果为真 |
| 小区域计数 | `COUNT { 低成本区域 } >= 0` | 计数不为负，零也满足 |
| 存在性检查 | `EXISTS { 低成本区域 }` | 构图为每个锚点补一条满足条件的匹配 |
| 节点属性 | `coalesce(v2.p_guard, true)` | 构图写入 `p_guard=true`，缺失时回退到 true |

候选池中的表达式必须连同保证条件一起定义。仅把任意布尔表达式放入池中，无法保证它是决定项。当前代码也没有随机组合出任意复杂的决定项；上表就是本例可用的候选类别。

本例抽到 `EXISTS`，决定项 D 为：

```cypher
EXISTS {
  MATCH (v2)-[:<p_low_edge>]->(p_low_node:<p_low>)
  WHERE p_low_node.keep = true
  RETURN 1 AS p_value
}
```

### 3.3 同时准备低成本和高成本工作项

`work()` 生成一段从锚点出发、过滤 `keep=true` 的匹配，再用 COUNT 统计行数。S1 需要布尔结果，因此再加上 `> 0`。

```cypher
// W_low
COUNT {
  MATCH (v2)-[:<p_low_edge>]->(p_low_node:<p_low>)
  WHERE p_low_node.keep = true
  RETURN 1 AS p_value
} > 0

// W_high
COUNT {
  MATCH (v2)-[:<p_high_edge>]->(p_high_node:<p_high>)
  WHERE p_high_node.keep = true
  RETURN 1 AS p_value
} > 0
```

此时在宿主中安装 `D OR W_low`，并把 `W_high` 保存到用例的附加信息 `payload` 中，留待变异使用。`payload` 同时记录选中的锚点、决定项类别与目标真值，以及两类区域的标签和关系类型。

```text
原位置：v2.<property_bool>
嵌入后：D OR W_low
待变异：D OR W_high
```

等价要求作用于嵌入后的 Q₁ 和 Q₂。原始的 `v2.<property_bool>` 被替换后不必保持原值。

## 第 4 步：构建满足 S1 条件的图状态

`build_graph()` 先确定符号取值，再生成背景图，最后调用 S1 的 `populate()` 补充局部区域。本例使用 **6 个背景节点、8 条背景边，高成本总预算 N=100000**。

### 4.1 建立两条查询共用的符号绑定表

`bind_symbols()` 收集宿主符号、已嵌入查询中的符号和 `payload` 中预备变异的符号，统一绑定。高成本区域此时还没有出现在源查询里，也必须提前分配名称。

| 符号 | 本例的具体绑定 |
| --- | --- |
| `<label_1>` | `Random_label_1` |
| `<label_3>` | `Random_label_3` |
| `<property_float>` | `property_float` |
| `<property_bool>` | `property_bool` |
| `<property_int>` | `property_int` |
| `<constant_7>` | `-1` |
| `<p_low>` | `Pattern_p_low` |
| `<p_low_edge>` | `Pattern_p_low_edge` |
| `<p_high>` | `Pattern_p_high` |
| `<p_high_edge>` | `Pattern_p_high_edge` |

标签和关系类型采用固定命名规则；本例中的整数符号从 `[-2, 4]` 中随机抽到 -1。`keep`、`p_guard` 和局部查询里的常量 1 已由 Pattern 明确写定，不需要再次抽样。共用结构还会登记 witness 区域的符号，但本例 S1 的见证直接使用 low 区域，不额外创建 witness 节点。

### 4.2 先生成随机背景图

`random_graph()` 使用宿主的标签和属性类型生成背景数据。本例只有两种宿主标签，每个背景节点随机带一个或两个标签；属性按类型随机赋值，也可能缺失。背景边随机连接背景节点。

这一步不求解宿主中的 CASE 或其他谓词。本例实际生成的标签分布为：

| 背景节点 uid | 标签 | 能否匹配外层 MATCH |
| --- | --- | --- |
| 0、1、3、5 | `Random_label_1` | 否，缺少 `Random_label_3` |
| 2、4 | `Random_label_1`、`Random_label_3` | 是 |

图状态用节点表和关系表保存。节点包含标签、属性和 uid；关系记录起点 uid、终点 uid 和关系类型。查询变量 v2 尚未绑定到某一个固定 uid，它会随输入行依次指向匹配到的节点。

### 4.3 沿 v2 的标签来源找到全部候选锚点

构图器将 `payload` 中的符号替换为具体名称，然后执行：

```python
roots = graph.candidates(payload["anchor"], create=True)
```

v2 继承了 v1 的两个标签，`Graph.candidates()` 要求节点同时包含这两个标签，因此得到 `roots = [2, 4]`。如果没有找到任何节点，`create=True` 会补建一个带这些标签的节点。

**构图按所有候选锚点补条件。** 若只给 uid=2 添加低成本匹配，uid=4 上的 EXISTS 就不能由构造保证为真。这里按标签选到的锚点恰好等于外层 MATCH 的实际输入，因为本例没有外层过滤；一般查询还可能被 WHERE、连接或分页筛掉，当前代码不在这一步求解这些条件。

### 4.4 每个锚点补一条存在性见证

`populate_regions()` 对每个锚点执行以下操作：

1. 写入 `p_guard=true`。这是共用构图逻辑，本例选中的 EXISTS 不读取该属性。
2. 新建一个 `Pattern_p_low` 节点，属性为 `keep=true, value=1`。
3. 从锚点连出一条 `Pattern_p_low_edge`，指向这个新节点。

于是两个锚点都满足 `EXISTS { 低成本区域 } = true`。low 区域同时供决定项 D 和工作项 `W_low` 使用。

### 4.5 按总预算扩张高成本区域

N 是**整个用例的高成本节点总数**。代码通过 `shares(N, 锚点数)` 分配预算，商作为基础配额，余数逐个补给前面的锚点。本例为：

```text
N = 100000，锚点数 = 2
high_allocations = [50000, 50000]
```

对每个锚点，新建 50000 个 `Pattern_p_high` 节点，均设置 `keep=true, value=1`，再分别连上 `Pattern_p_high_edge`。每个锚点的 low 区域仍只有 1 个节点。N 控制 high 区域的规模，low 区域的大小保持不变。

本例完成后的局部图如下。图中 uid 与当前代码的实际构造结果一致。

```text
uid=2  Random_label_1 + Random_label_3
  ├─ Pattern_p_low_edge  → uid=6           [1 个 Pattern_p_low, keep=true]
  └─ Pattern_p_high_edge → uid=7..50006    [50000 个 Pattern_p_high, keep=true]

uid=4  Random_label_1 + Random_label_3
  ├─ Pattern_p_low_edge  → uid=50007        [1 个 Pattern_p_low, keep=true]
  └─ Pattern_p_high_edge → uid=50008..100007 [50000 个 Pattern_p_high, keep=true]

另有 4 个单标签背景节点，及背景阶段生成的 8 条边。
```

最终共有 **100008 个节点、100010 条边**：6 个背景节点加 2 个 low 节点、100000 个 high 节点；8 条背景边加 2 条 low 边、100000 条 high 边。背景边使用独立的关系类型，不会混入本例的 low/high 匹配。

| 锚点 | D：low 区域是否存在匹配 | low 区域匹配数 | high 区域匹配数 |
| --- | --- | --- | --- |
| uid=2 | true | 1 | 50000 |
| uid=4 | true | 1 | 50000 |

**高低成本通过同一种计算访问不同规模的区域来实现。** `W_low` 和 `W_high` 都从已绑定的 v2 出发，沿指定类型的关系做一跳扩展，检查邻居的 `keep=true`，再用 COUNT 统计匹配数。两者的查询结构与过滤条件相同，区别在于关系类型、目标标签及其对应区域的规模。由于所有新增邻居都满足过滤条件，逐条计算完整 COUNT 时，low 每锚点只处理 1 条匹配，high 每锚点处理 50000 条匹配。

| 局部表达式 | 每个锚点上的数据 | 按匹配逐条求值时的预期工作 |
| --- | --- | --- |
| 决定项 `EXISTS { low }` | 1 条有效匹配 | 找到这条匹配即可确定 true，两条查询共用这一条件 |
| `W_low = COUNT { low } > 0` | 1 条有效匹配 | 扩展并检查 1 个邻居，计数得到 1，再比较 `1 > 0` |
| `W_high = COUNT { high } > 0` | 50000 条有效匹配 | 扩展并检查 50000 个邻居，计数得到 50000，再比较 `50000 > 0` |

固定两个锚点，若各执行一次完整计数，则 Q₁ 的右侧工作项合计处理 2 条匹配，Q₂ 合计处理 100000 条匹配。随着 N 增大，low 的这部分工作量保持不变，high 的这部分工作量线性增长。这一预期以执行计划从已绑定锚点按关系类型扩展、逐条过滤并聚合为前提。如果后端先扫描全部邻接边再筛选类型，low 也可能受 high 区域增大的影响，需要通过计划中的实际访问情况核对。

**S1 检查的是数据库能否避免这部分高成本工作。** 左侧 EXISTS 已经为真，理想的短路执行会直接返回 true，两侧的 COUNT 都无需执行。如果数据库先计算 COUNT，再计算 OR，高成本区域才会放大 Q₂ 的多余工作。两个查询返回的 v4 仍然相同，因此可以在结果不变的条件下观察成本差异。

50000 倍的匹配数差异不等于 50000 倍的运行时间差异。数据库可能把 `COUNT {...} > 0` 改写成存在性判断，在找到首条匹配后停止；也可能消除整个表达式。本例的外层还未使用 v4，相关对照见第 7 步。因此，N=100000 提供的是可扩张的潜在工作量，实际是否发生多余计数，要结合执行计划和配对计时判断。

### 4.6 完成图模式登记

构图器最后登记源查询与变异查询涉及的标签、属性类型和关系端点，形成图模式（schema），并按配置准备索引。到这里，low 和 high 两个区域已经同时存在于同一图状态中。后面生成 Q₂ 时不会增删节点或改变索引。

## 第 5 步：实例化完整查询

```python
instantiate(case, bindings)
```

`instantiate()` 递归遍历 AST 和 `payload`，用同一张绑定表替换符号，保留算子及其嵌套关系。例如，`<p_low>` 换为 `Pattern_p_low`，`<constant_7>` 换为整数 -1；OR、COUNT 和 CALL 的结构都不变。

本例得到源查询 Q₁：

```cypher
MATCH (v1:Random_label_1:Random_label_3)

CALL (v1) {
  WITH v1 AS v2,
       v1.property_float AS v3

  RETURN (
    EXISTS {
      MATCH (v2)-[:Pattern_p_low_edge]->(p_low_node:Pattern_p_low)
      WHERE p_low_node.keep = true
      RETURN 1 AS p_value
    }
    OR
    (COUNT {
      MATCH (v2)-[:Pattern_p_low_edge]->(p_low_node:Pattern_p_low)
      WHERE p_low_node.keep = true
      RETURN 1 AS p_value
    } > 0)
  ) AS v4
}

RETURN v1 AS v5,
       EXISTS {
         RETURN DISTINCT
           CASE
             WHEN v1.property_bool THEN -1
             ELSE v1.property_int
           END AS v6
       } AS v8
```

### 嵌入后新增变量怎样管理

进入 OR 左侧的 EXISTS 时，子查询继承 `{v1, v2, v3}`，MATCH 在自己的局部表中加入 `p_low_node`，WHERE 便能读取其 `keep` 属性。RETURN 输出局部列 `p_value`。离开这个子查询后，外层可见变量仍是 `{v1, v2, v3}`。

右侧 COUNT 建立另一个子查询作用域。它可以再次使用 `p_low_node` 和 `p_value` 这两个局部名称，二者不会与 EXISTS 内的同名变量混在一起。整个 OR 产生一个布尔值，CALL 只向外返回 v4。

具体执行到第一行时，v1 和 v2 都指向 uid=2，v3 为该节点的 `property_float=0.5`；第二行中 v1、v2 都指向 uid=4，v3 同样为 0.5。这是运行时每行的值绑定，与静态作用域表分开。

## 第 6 步：只变异右侧工作项

主流程先保存 Q₁，再复制用例，在副本上调用 `mutate()`。S1 的变异就是把 OR 的右操作数替换为已经准备并实例化的 `payload["high"]`：

```text
Q₁ 局部：D OR (COUNT { low 区域 } > 0)
Q₂ 局部：D OR (COUNT { high 区域 } > 0)
```

Q₂ 的目标 RETURN 变为以下形式，其余查询与 Q₁ 相同：

```cypher
RETURN (
  EXISTS {
    MATCH (v2)-[:Pattern_p_low_edge]->(p_low_node:Pattern_p_low)
    WHERE p_low_node.keep = true
    RETURN 1 AS p_value
  }
  OR
  (COUNT {
    MATCH (v2)-[:Pattern_p_high_edge]->(p_high_node:Pattern_p_high)
    WHERE p_high_node.keep = true
    RETURN 1 AS p_value
  } > 0)
) AS v4
```

决定项仍访问 low 区域。变异阶段不重新抽取锚点、常量或名称，也不重新构图。

对本例每个锚点，D 都为 true，因此 Q₁ 和 Q₂ 的局部结果均为 true。这里利用 OR 的吸收关系：`true OR x = true`，即使 x 为 null 也成立。等价性依赖 D 的真值，不依赖两个 COUNT 的数值相同。

CALL 每个输入节点返回一行，外层继续返回相同的 v1。最后那个 EXISTS 只有一个 RETURN，没有过滤或零 LIMIT，因此每次都有一行，v8 为 true。本例按语义推导得到两行：

| v5 | v8 |
| --- | --- |
| uid=2 对应的节点 | true |
| uid=4 对应的节点 | true |

随后 `check_pair()` 再分析两棵 AST，检查变量引用和最终输出列的名称、类型，并确认查询文本确实发生变化。Q₂ 的 COUNT 局部表改为包含 `p_high_node`，它依然不会泄露到外层。

## 第 7 步：检查结果，再判断有没有多余工作

执行器把图装载一次，在同一图上运行 Q₁、Q₂，比较完整的带类型结果多重集，包括列顺序和重复行数量。结果相同后，才交替按 Q₁→Q₂、Q₂→Q₁ 的顺序计时；每次执行仍检查结果。差异达到配置阈值后，再进行确认阶段，执行计划单独采集。

S1 关注**不影响 OR 结果的右侧工作项是否仍被执行，并随高成本区域扩大而增加耗时**。需要通过执行计划定位这部分工作，再用规模对照检查耗时变化。

**本例的 v4 没有被最终 RETURN 使用。** 优化器可能因为结果未被使用而消除整个目标表达式。这样的“未执行”无法单独证明 OR 内部发生了短路。

用于后续性能实验时，可以增加一个对照版本，让外层返回 `v4 AS v9`，并对 Q₁、Q₂ 同时作这一修改。这样 v4 成为可观察输出，再结合执行计划区分“整个目标被消除”与“保留目标、跳过右侧 COUNT”。

## 目前还需要解决的问题

1. **优先补足目标是否被使用的分析。** 本例说明，变量引用合法、图中有锚点，仍不能保证目标表达式被下游使用。需要沿投影别名追踪消费者，记录外层过滤是否阻断输入、返回值是否被丢弃，以及目标是否位于未选中的分支。将外层屏蔽与 Pattern 自身应当产生的短路分开统计。

2. **扩充决定项候选池，并同步扩充构图条件。** 当前五类候选的表达式形态较固定。可以加入有界计数比较、带空值处理的复合谓词、多层 CASE 和相关子查询。每增加一类，都应说明如何保证所有适用锚点上的目标真值。例如，新增 `COUNT {...} >= k` 时，构图必须为每个锚点提供至少 k 个有效匹配；仅新增表达式模板不够。

3. **提高初始 AST 的有效复杂度。** 当前已有多种匹配拓扑、WITH、CALL、UNWIND、UNION、条件查询和表达式嵌套，可调整子句数、表达式深度、子查询深度及展开预算。但单纯增大预算容易产生空结果或目标被屏蔽的查询。更合适的方向是记录产生式及其组合的出现情况，对低频结构增加抽样权重，并保留复杂度分层对照。

4. **分别统计语法覆盖和数据库分支覆盖。** 生成了更多不同 AST，只能说明输入结构更丰富。应先统计产生式、嵌套组合和目标位置覆盖，再结合执行计划或数据库代码插桩判断是否触及新的优化器分支。与此同时，记录有效输入率和目标可观察率，避免大量复杂查询只重复测试同一种消除规则。

5. **扩大工作项和图结构的组合。** 当前 S1 的主要工作项是固定的一跳 COUNT，low 每锚点一条，high 按总预算分配。可逐步增加多跳路径、共享中间节点和不同度数分布，区分遍历、聚合及相关子查询重复执行带来的成本。规模对照应固定宿主、决定项和背景图，只改变目标扩张区域。

## 本周工作简述

本轮重构完善了随机 AST、作用域分析、构图变异及多数据库执行，新增提交 5 个 issue。通过合并重复短路形式、按依赖关系拆分匹配交换，将 Pattern 由 10+13 项整理为 8+8 项，使每项对应明确的等价条件和构图规则。

汇报周期按 **2026-10-26 至 2026-11-01** 计，11 月 2 日核对提交记录。5 个 issue 均于 11 月 1 日提交：

| 系统 | Issue | 问题 |
| --- | --- | --- |
| ArcadeDB | [#11081](https://github.com/ArcadeData/arcadedb/issues/11081) | 只读 EXISTS 内的 LIMIT 0 仍计算输入 |
| ArcadeDB | [#11082](https://github.com/ArcadeData/arcadedb/issues/11082) | CALL UNION ALL 在外层 LIMIT 3 前物化分支 |
| ArcadeDB | [#11083](https://github.com/ArcadeData/arcadedb/issues/11083) | 交换独立 OPTIONAL MATCH 后重复查找同一根节点 |
| LadybugDB | [#1483](https://github.com/LadybugDB/ladybug/issues/1483) | COALESCE 已有非空常量，仍执行 COUNT 子查询 |
| FalkorDB | [#3637](https://github.com/FalkorDB/FalkorDB/issues/3637) | 反转单条无向路径的端点顺序后丢失索引扫描 |

## 为什么将 10+13 个 Pattern 调整为 8+8 个

**这次调整按等价条件和构图要求重新划分测试单元。** 每个 Pattern 都要说明为什么结果相同、哪些图条件必须成立，以及哪部分工作可以被放大。完整查询的语法复杂度由随机 AST 生成器提供，Pattern 负责局部变换及其成立条件。

旧目录的分类粒度不一致。一方面，旧 S3 的条件查询和 S4 的 CASE 分别编号，但都利用“条件已经选定分支，其他分支无需执行”的性质。另一方面，旧 P10 将独立匹配、共享锚点和链式匹配放在同一编号下，三者的变量依赖与构图条件却不同。沿用这套划分，会把部分语法差异计为不同 Pattern，同时把需要分别检查的依赖关系放在一起。

### 调整的三个动机

1. **把相同的分支选择原理集中表达。** 新 S3 使用 CASE，包含“替换未选中的结果”和“替换首个真分支之后的条件”两种变体。两者都固定已经选中的结果，再增大不应执行部分的工作量。决定项的常量、比较、COUNT、EXISTS 和属性形式则留在候选池中扩展，无需为每种写法新增 Pattern。旧条件查询与新 CASE 的语法覆盖并不完全相同，合并的是测试原理。

2. **按变量依赖拆分匹配交换。** 独立匹配对应两个互不依赖的输入；共享锚点分支读取同一个已绑定节点；链式匹配通过中间变量连接。它们分别可能放大组合枚举、重复扩展或无效的中间候选，所需图结构也不同。因此，将旧 P10 的三种形式拆为 Y5、Y6、Y7，分别规定可交换的位置与构图方式。可选分支另列为 Y8，因为还要保持未匹配时的空值扩展和结果重复次数。

3. **让当前实现集中在有明确构图方式的性能测试上。** 本轮保留短路、输出需求、路径反转和匹配顺序等测试关系，并为它们配套固定条件与扩张区域。例如，本例 S1 为每个锚点保留一条决定项见证，再通过 N 放大右侧 COUNT；Y4 则保留有效路径，并扩张经过失败节点的候选分支。其他旧变换暂未全部迁入，后续应分别补齐它们的等价条件和成本构造，再决定是否恢复为独立 Pattern。

### 数量具体怎样变化

这里的旧编号采用原论文目录 `Src/paper_patterns.py`，新编号采用当前 `cypher/mutator/__init__.py`。

| 旧 Pattern | 当前处理 | 调整含义 |
| --- | --- | --- |
| S1、S2、S5、S6 | 保留为 S1、S2、S5、S6 | 分别测试 OR、AND、零需求和有限需求 |
| S3 条件查询、S4 CASE | 在新 S3 中集中测试条件分支选择 | 两个旧类别对应一个当前类别，减少 1 项；当前局部实现使用 CASE |
| S7 COALESCE、S8 存在性、S9 空输入 | 分别对应新 S4、S7、S8 | 保留相关测试原理并重新编号 |
| S10 成员判断／空集合操作 | 当前未保留独立 Pattern | 减少 1 项；不能据此认为已由其他 Pattern 完整覆盖 |
| P1 单边镜像、P3 有界路径、P4 AND、P5 OR、P11 可选分支 | 分别对应 Y3、Y4、Y1、Y2、Y8 | 保留 5 类测试关系 |
| P10 的独立、共享锚点、链式匹配形式 | 拆成 Y5、Y6、Y7 | 一个旧编号拆成三个，增加 2 项 |
| P2 成对反向关系、P6 等式交换、P7 标签顺序、P8 关系类型顺序、P9 谓词位置、P12 UNION 分支交换、P13 CASE 对偶 | 当前未保留独立 Pattern | 共 7 项暂未迁入 |

因此，短路类为 **10 − 1（合并）− 1（未保留）= 8**；对称类为 **13 + 2（拆分）− 7（未保留）= 8**。P 改为 Y 只是新目录的编号变化。

这次调整使测试单元的等价条件和构图职责更清楚，同时也收缩了独立变换的覆盖范围。旧 P2 就需要图中存在成对的反向关系，不能直接用新 Y3 对同一条边反转书写方向来替代。类似地，随机 AST 中能够出现 UNION 或成员判断，也不等于已经测试了 UNION 分支交换或成员判断的短路行为。后续应检查这些遗漏是否能触及新的优化器行为，再补回有价值的测试关系。

## 代码对应与核对范围

| 文中步骤 | 当前实现 |
| --- | --- |
| 随机生成与变量维护 | [generator.py](../cypher/generator.py)、[scope.py](../cypher/scope.py) |
| 位置选择、变异后检查 | [embedding.py](../cypher/embedding.py) |
| S1、决定项候选池、局部构图 | [boolean.py](../cypher/mutator/boolean.py)、[operands.py](../cypher/mutator/operands.py) |
| 符号绑定、背景图、锚点和预算分配 | [builder.py](../graph_generation/builder.py)、[random_graph.py](../graph_generation/random_graph.py)、[graph.py](../data_structures/graph.py) |
| 实例化与完整流程 | [case.py](../data_structures/case.py)、[main.py](../main.py) |
| 结果比较与计时 | [executor.py](../executor.py) |

本文将给定宿主还原为当前 AST 对象，调用真实的位置选择、嵌入、构图、实例化和变异函数完成离线核对。固定宿主后的随机种子为 1397，高成本总预算为 100000，得到锚点 `[2, 4]`、扩张分配 `[50000, 50000]` 和上文列出的图；该种子不代表初始宿主由生成器随机产生的种子。本次未运行数据库。

核对数据保存在 [verification.json](../results/s1-walkthrough-20261102/n100000/verification.json)，完整查询为 [Q₁](../results/s1-walkthrough-20261102/n100000/query_a.cypher) 和 [Q₂](../results/s1-walkthrough-20261102/n100000/query_b.cypher)。历史代码与当前代码的 Pattern 目录分别见提交 `26f62dc` 的父版本 `Src/paper_patterns.py`、当前 [mutator/__init__.py](../cypher/mutator/__init__.py)。周报中的 issue 数量按公开提交记录统计，不等于独立根因数或开发者确认数。

## 各 Pattern 的 issue 数量（按提供清单，唯一主归属）

本节以本次提供的[两张 issue 清单](../results/s1-walkthrough-20261102/user-issue-inventory/user-provided-issues.csv)为统计来源，状态和 BUG 类型采用清单中的记录。**仅统计 pending、confirmed、fixed；4 个 not planned 和 1 个 uninterested 均排除在表外。** 纳入统计的共 **40 个 issue**，包含 36 个 Performance BUG 和 4 个 Logical BUG。

**每个 issue 只计一次。** 主归属按原报告的主要变换或问题确定。例如，FalkorDB #2081 只计入 S1，ArcadeDB #9579 只计入 Y1，ArcadeDB #11083 只计入 Y8。历史报告按相关语义归类，不代表全部由当前模板直接生成。[逐 issue 主归属明细](../results/s1-walkthrough-20261102/user-issue-inventory/primary-attribution.csv)保留完整清单；下表仅汇总其中的三种纳入状态。

其中 **34 个 issue 归入当前 16 个 Pattern，6 个尚未归类**。未归类项在表中单列，所有分类行相加等于 **40：pending 8 个、confirmed 7 个、fixed 25 个**。

| Pattern | 测试内容 | pending | confirmed | fixed | issue 数量 |
| --- | --- | ---: | ---: | ---: | ---: |
| S1 | 真条件下的 OR 短路 | 0 | 0 | 3 | 3 |
| S2 | 假条件下的 AND 短路 | 0 | 3 | 3 | 6 |
| S3 | CASE 未选结果或后续条件 | 0 | 0 | 0 | 0 |
| S4 | COALESCE 非空前缀 | 1 | 0 | 1 | 2 |
| S5 | 零输出需求 | 0 | 0 | 2 | 2 |
| S6 | 有限输出需求 | 0 | 0 | 2 | 2 |
| S7 | 存在性决定 | 0 | 0 | 1 | 1 |
| S8 | 空输入传播 | 0 | 0 | 0 | 0 |
| Y1 | AND 操作数交换 | 0 | 0 | 2 | 2 |
| Y2 | OR 操作数交换 | 0 | 0 | 0 | 0 |
| Y3 | 单边书写方向反转 | 2 | 0 | 0 | 2 |
| Y4 | 有界路径端点反转 | 2 | 1 | 2 | 5 |
| Y5 | 独立必选匹配交换 | 0 | 2 | 1 | 3 |
| Y6 | 共享锚点必选分支交换 | 1 | 0 | 2 | 3 |
| Y7 | 链式连接交换 | 0 | 0 | 0 | 0 |
| Y8 | 独立可选分支交换 | 0 | 1 | 2 | 3 |
| 未归类 | 暂未映射到当前 Pattern | 2 | 0 | 4 | 6 |
| **合计（每个 issue 仅计一次）** | — | **8** | **7** | **25** | **40** |
