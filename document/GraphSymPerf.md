# **GraphSymPerf: Detecting Symmetry-Violation Performance Bugs in Graph DBMSs via Query Symmetry Testing**
GraphSymPerf: Finding Graph Query Optimizer Performance Anomalies via Semantics-Preserving Boolean Transformations


or


Finding Performance Anomalies in Graph Database Engines via Semantics-Preserving Boolean Transformations

## 1. Introduction（引言）

**目标：** 明确图数据库中的研究问题：图查询在语义上存在大量对称结构，但现实图数据库可能无法稳定优化这些对称查询，从而导致性能异常。

- **背景：**
   图数据库管理系统（Graph DBMSs）广泛应用于社交网络、金融风控、知识图谱、推荐系统和网络安全分析等场景。与关系型数据库不同，图数据库的核心查询通常围绕节点、边、路径、模式匹配和可变长度遍历展开，因此性能高度依赖查询优化器对图结构和遍历方向的理解。
- **问题：**
   在图查询中，很多查询在语义上是对称的。例如，两个节点模式交换顺序、无向边的匹配方向反转、等价谓词位置调整、路径起点和终点互换等，本质上不改变查询结果。然而，在真实 Graph DBMS 中，这些表面形式上的变化可能导致完全不同的执行计划，从而引发数量级性能差异。
- **核心假设：Symmetry Hypothesis**
   对于一组语义等价或近似等价的对称查询，如果它们在图结构、选择性和结果规模上没有本质差异，那么 DBMS 应当表现出相对稳定的性能。如果仅仅因为查询书写顺序、边方向表达、节点变量位置或模式排列发生变化，就导致执行时间显著恶化，则说明系统可能存在潜在的性能 bug。
- **核心观察：**
   图查询优化器往往对“从哪里开始扩展”“沿哪个方向扩展”“先匹配哪个节点/边模式”“谓词放在哪个变量上”等因素非常敏感。即使两个查询在语义上等价，优化器也可能因为查询表面结构不同而选择低效的起始点、错误的扩展方向或不合理的过滤顺序，最终导致性能异常。
- **方法概述：**
   本文提出 **GraphSymPerf**，一种面向图数据库的黑盒性能测试框架。它从基础图查询出发，自动构造一组具有对称关系的查询变体；随后比较这些查询在执行计划和运行时指标上的差异，识别由于对称性破坏导致的性能异常。
- 贡献概述：
  1. 提出一种面向 Graph DBMS 的 **查询对称性性能 bug 假设**，将语义对称性转化为性能测试 oracle；
  2. 系统归纳图查询中的多类对称变换，包括节点交换、边方向反转、谓词位置对称、路径端点交换和模式排列对称；
  3. 设计一种基于对称查询族的黑盒测试框架 GraphSymPerf，用于发现 Graph DBMS 中的计划不稳定和性能异常；
  4. 在多个主流图数据库上验证该方法，评估其发现真实性能 bug 的能力。

------

## 2. Background and Motivation（背景与动机）

**目标：** 解释为什么图数据库中特别适合使用“查询对称性”作为 performance oracle。

- **图查询的基本特征：**
   图数据库通常使用声明式图查询语言，如 Cypher、openCypher、GQL 风格语言等。典型查询由节点模式、边模式、路径模式和过滤谓词组成。例如：

```
MATCH (a:User)-[:FRIEND]->(b:User)
WHERE a.age > 30
RETURN b
```

这类查询并不是简单扫描表，而是涉及节点集合选择、边扩展、路径遍历、模式连接和谓词过滤等多个阶段。

- 图查询优化的关键问题：

  图查询性能主要受以下因素影响：

  1. 从哪个节点集合开始匹配；
  2. 沿哪个边方向进行扩展；
  3. 多个模式片段以什么顺序连接；
  4. 谓词是否能尽早下推；
  5. 是否能利用标签、属性索引和边类型索引；
  6. 是否能准确估计节点度数、边选择性和路径扩展规模。

- 为什么图查询天然存在对称性？

  图数据模型中大量查询结构本身具有对称关系。例如：

  1. 无向关系可以从任一端开始匹配；
  2. 两个等价节点模式交换顺序后结果不变；
  3. `a--b` 与 `b--a` 在无向匹配中语义等价；
  4. `p AND q` 与 `q AND p` 在谓词逻辑上等价；
  5. 某些路径查询在反向边或逆关系存在时具有端点对称性；
  6. 多个独立 pattern 的排列顺序不应影响最终结果。

- **现有性能测试方法的不足：**
   传统 Graph DBMS 测试更多关注：

  1. 查询是否崩溃；
  2. 查询结果是否正确；
  3. 某个单独查询是否执行过慢；
  4. 不同版本之间是否出现性能回归。

  但这些方法很难判断一个查询“到底应该多快”。单个查询执行慢并不一定是 bug，因为它可能本身就很复杂。相比之下，对称查询之间的性能差异提供了一个更可靠的判断依据：如果两个查询语义等价、结构相似、结果规模一致，却表现出数量级性能差异，那么这种差异更可能来自优化器缺陷。

- **本文切入点：**
   本文不再假设“某个高成本操作本应被跳过”，而是提出一种更一般的假设：
   **Graph DBMS 的性能不应对语义对称的查询表达形式表现出非合理敏感性。**

------

## 3. Symmetry Hypothesis（查询对称性假设）

**目标：** 正式定义这个假设，并说明它为什么可以作为 performance oracle。

### 3.1 基本定义

给定一个图查询 `Q`，通过某种对称变换 `T` 得到查询 `Q'`。如果满足：

1. `Q` 与 `Q'` 语义等价，或者在受控图数据上结果等价；
2. `Q` 与 `Q'` 的输入数据规模、选择性和结果规模相同或高度接近；
3. `Q` 与 `Q'` 只在表面结构上存在对称变化，例如变量顺序、边方向、谓词顺序或模式排列变化；

那么理论上，DBMS 对 `Q` 和 `Q'` 的执行性能不应出现数量级差异。

可以写成：

```
If Q ≡ T(Q), then Performance(Q) ≈ Performance(T(Q)).
```

如果出现：

```
Result(Q) = Result(T(Q))
but
Time(T(Q)) >> Time(Q)
```

则说明 `T(Q)` 可能触发了优化器中的性能异常。

### 3.2 与 SCor 假设的区别

SCor 的假设是：

> 查询结果可以由低成本部分确定，但 DBMS 仍然执行了高成本部分。

而 Symmetry Hypothesis 的假设是：

> 两个语义对称的查询不应因为表面书写形式不同而产生极端性能差异。

两者的区别在于：

| 维度        | SCor / Redundant Computation    | Symmetry Hypothesis                             |
| ----------- | ------------------------------- | ----------------------------------------------- |
| 核心依据    | 某些计算语义上不必要            | 两个查询语义上对称                              |
| bug 信号    | 不必要高成本操作被执行          | 对称查询性能严重不一致                          |
| oracle 类型 | 局部语义剪枝 oracle             | 查询族性能一致性 oracle                         |
| 适合场景    | Boolean short-circuit、冗余谓词 | join order、路径扩展、方向选择、pattern order   |
| 新颖性      | 检测 missed optimization        | 检测 optimizer instability / symmetry violation |

### 3.3 为什么它适合图数据库？

图数据库比关系型数据库更适合这个假设，因为图查询优化高度依赖 **遍历方向和起始点选择**。

例如：

```
MATCH (a:Person)-[:KNOWS]-(b:Person)
WHERE a.id = 1
RETURN b
```

和：

```
MATCH (b:Person)-[:KNOWS]-(a:Person)
WHERE a.id = 1
RETURN b
```

在无向边语义下，两个查询应返回相同结果。但如果优化器在第二个查询中错误地从 `b` 开始扩展，而不是先利用 `a.id = 1` 的选择性约束，就可能导致大规模邻居扩展。

这类问题不是简单的“多执行了一个冗余分支”，而是 **对称表达破坏了优化器的计划稳定性**。

------

## 4. Motivating Example（动机示例）

**目标：** 给出一个清晰例子，说明对称查询如何诱发性能 bug。

### 4.1 示例图数据

假设图数据库中有如下数据：

```
(:User {id, age, country})
(:Post {id, topic})
(:User)-[:FOLLOWS]->(:User)
(:User)-[:LIKES]->(:Post)
```

其中：

- `User.id` 上有索引；
- 图中有 1 亿个 `User` 节点；
- `id = 123` 的用户只有一个；
- `FOLLOWS` 边数量非常大；
- 每个用户平均关注数较高。

### 4.2 原始查询

```
Q1:
MATCH (u:User {id: 123})-[:FOLLOWS]-(v:User)
RETURN v
```

这个查询中，`u.id = 123` 是高选择性条件。优化器应当先通过索引找到 `u`，然后扩展其邻居。

### 4.3 对称查询

```
Q2:
MATCH (v:User)-[:FOLLOWS]-(u:User {id: 123})
RETURN v
```

在无向匹配语义下，`Q1` 和 `Q2` 返回相同结果。它们只是节点变量的书写顺序发生了对称变化。

### 4.4 潜在性能异常

理想情况下：

```
Time(Q1) ≈ Time(Q2)
```

但真实系统中可能出现：

```
Time(Q1) = 10 ms
Time(Q2) = 30 s
```

原因可能是：

- `Q1` 从 `u.id = 123` 开始，通过索引定位单个节点；
- `Q2` 被优化器误判为从 `v:User` 开始，扫描大量用户节点；
- 然后再扩展 `FOLLOWS` 边并检查另一端是否满足 `u.id = 123`；
- 最终结果一样，但执行代价相差巨大。

### 4.5 这个例子说明什么？

这个例子说明：

1. 对称查询之间的性能差异可以暴露优化器的计划不稳定；
2. 图数据库优化器可能对 pattern 中变量出现顺序过度敏感；
3. 即使查询语义不变，仅改变书写方向也可能导致严重性能退化；
4. 因此，对称性可以作为一种黑盒性能 oracle。

------

## 5. Taxonomy of Query Symmetries（查询对称性分类）

**目标：** 系统归纳可以用于测试的对称变换类型。

### 5.1 Node-Order Symmetry（节点顺序对称）

**定义：** 在同一个图模式中交换两个节点变量的位置，但保持边关系和过滤条件不变。

示例：

```
Q:
MATCH (a:User)-[:FRIEND]-(b:User)
WHERE a.id = 1
RETURN b
Q':
MATCH (b:User)-[:FRIEND]-(a:User)
WHERE a.id = 1
RETURN b
```

**潜在问题：**

- 优化器依赖 pattern 书写顺序；
- 没有优先选择高选择性节点；
- 节点扩展起点选择错误；
- 索引查找被延迟。

------

### 5.2 Edge-Direction Symmetry（边方向对称）

**定义：** 对于无向查询或可逆关系，反转边方向后语义保持不变。

示例：

```
Q:
MATCH (a)-[:KNOWS]-(b)
RETURN a, b
Q':
MATCH (b)-[:KNOWS]-(a)
RETURN a, b
```

如果是有向边，也可以在存在反向边类型时构造对称：

```
MATCH (a)-[:PARENT_OF]->(b)
```

与：

```
MATCH (b)-[:CHILD_OF]->(a)
```

**潜在问题：**

- 正向扩展和反向扩展代价估计不一致；
- 边索引只在一个方向上被有效利用；
- 度数统计对方向敏感；
- 反向遍历执行器实现低效。

------

### 5.3 Predicate-Order Symmetry（谓词顺序对称）

**定义：** 交换逻辑谓词的顺序，但保持语义等价。

示例：

```
Q:
MATCH (u:User)
WHERE u.id = 1 AND u.age > 30
RETURN u
Q':
MATCH (u:User)
WHERE u.age > 30 AND u.id = 1
RETURN u
```

理论上，优化器应优先利用 `u.id = 1` 的高选择性条件，而不应受谓词书写顺序影响。

**潜在问题：**

- 谓词重排缺失；
- 过滤条件下推失败；
- 高选择性条件没有优先执行；
- 表达式求值顺序影响计划选择。

------

### 5.4 Pattern-Order Symmetry（模式顺序对称）

**定义：** 多个独立或弱相关的 `MATCH` pattern 交换顺序后，语义保持不变。

示例：

```
Q:
MATCH (u:User {id: 1})-[:LIKES]->(p:Post)
MATCH (u)-[:FOLLOWS]->(v:User)
RETURN p, v
Q':
MATCH (u)-[:FOLLOWS]->(v:User)
MATCH (u:User {id: 1})-[:LIKES]->(p:Post)
RETURN p, v
```

**潜在问题：**

- 多 pattern join order 选择错误；
- 优化器受 `MATCH` 子句顺序影响；
- 中间结果爆炸；
- 过滤条件无法跨 pattern 提前传播。

------

### 5.5 Path-Endpoint Symmetry（路径端点对称）

**定义：** 对于无向路径或可逆路径查询，交换路径起点和终点后语义保持不变。

示例：

```
Q:
MATCH p = (a:User {id: 1})-[*1..3]-(b:User {country: 'US'})
RETURN p
Q':
MATCH p = (b:User {country: 'US'})-[*1..3]-(a:User {id: 1})
RETURN p
```

如果 `a.id = 1` 选择性远高于 `b.country = 'US'`，优化器应从 `a` 开始扩展，而不是机械地从书写起点开始。

**潜在问题：**

- 可变长度路径扩展起点选择错误；
- BFS / DFS 扩展方向不合理；
- 路径数量爆炸；
- 没有利用高选择性端点约束。

------

### 5.6 Join-Commutativity Symmetry（图模式连接交换对称）

**定义：** 图模式匹配本质上可以视作多个关系模式的 join。若两个 pattern 的连接满足交换律，则交换匹配顺序不应导致极端性能变化。

示例：

```
Q:
MATCH (u:User {id: 1})-[:FOLLOWS]->(v)
MATCH (v)-[:LIKES]->(p)
RETURN p
Q':
MATCH (v)-[:LIKES]->(p)
MATCH (u:User {id: 1})-[:FOLLOWS]->(v)
RETURN p
```

**潜在问题：**

- 图模式 join 顺序枚举不充分；
- 中间节点 `v` 的基数估计错误；
- 关系扩展顺序不稳定；
- 高选择性 anchor 没有被优先使用。

------

## 6. Method Overview（方法概述）

**目标：** 描述 GraphSymPerf 的整体框架。

GraphSymPerf 包含五个主要模块：

1. **Seed Query Generator：基础查询生成器**
2. **Symmetry Transformer：对称变换器**
3. **Equivalence Validator：等价性验证器**
4. **Performance Profiler：性能剖析器**
5. **Symmetry Violation Detector：对称性违例检测器**

整体流程如下：

```
Graph Schema + Data
        ↓
Seed Query Generation
        ↓
Symmetry Transformation
        ↓
Equivalent Query Family
        ↓
Execution + Profiling
        ↓
Plan / Runtime Comparison
        ↓
Symmetry-Violation Performance Bugs
```

------

## 7. Detailed Design（具体设计）

### 7.1 Seed Query Generator（基础查询生成）

**目标：** 生成具有可控结构和选择性的基础图查询。

- 输入：
  - 图数据库 schema；
  - 节点标签；
  - 边类型；
  - 属性信息；
  - 索引信息；
  - 数据分布统计；
  - 用户指定的查询模板。
- 生成的基础查询类型：
  1. 单边匹配查询；
  2. 多跳路径查询；
  3. 多 pattern 连接查询；
  4. 带属性过滤的节点查询；
  5. 带边属性过滤的关系查询；
  6. 可变长度路径查询；
  7. 聚合查询；
  8. Top-K / LIMIT 查询。

示例模板：

```
MATCH (a:L1 {k: v})-[r:R]-(b:L2)
WHERE b.p > c
RETURN b
```

- 生成原则：
  1. 至少包含一个高选择性 anchor，例如 `id`、唯一属性或稀有标签；
  2. 至少包含一个可能导致大规模扩展的关系类型；
  3. 查询结果应可在合理时间内返回；
  4. 对称变换后语义应易于验证。

------

### 7.2 Symmetry Transformer（对称变换器）

**目标：** 对基础查询应用一组语义保持的对称变换，生成查询族。

- **输入：** 原始查询 `Q`
- **输出：** 对称查询集合 `{Q1, Q2, ..., Qn}`

支持的变换包括：

#### 变换一：节点交换

```
MATCH (a)-[r]-(b)
```

变为：

```
MATCH (b)-[r]-(a)
```

#### 变换二：边方向反转

```
MATCH (a)-[r:R]->(b)
```

在存在反向语义时变为：

```
MATCH (b)<-[r:R]-(a)
```

或对无向模式：

```
MATCH (b)-[r:R]-(a)
```

#### 变换三：谓词顺序交换

```
WHERE p1 AND p2 AND p3
```

变为：

```
WHERE p3 AND p1 AND p2
```

#### 变换四：MATCH 子句交换

```
MATCH pattern1
MATCH pattern2
```

变为：

```
MATCH pattern2
MATCH pattern1
```

#### 变换五：路径端点交换

```
MATCH p = (a)-[*1..k]-(b)
```

变为：

```
MATCH p = (b)-[*1..k]-(a)
```

#### 变换六：等价 pattern 重写

```
MATCH (a)--(b)
```

变为：

```
MATCH (b)--(a)
```

或：

```
MATCH (a)-[:R]-(b)
```

变为：

```
MATCH (b)-[:R]-(a)
```

------

### 7.3 Safe Transformation Rules（安全变换规则）

**目标：** 保证生成的对称查询语义等价，避免误报。

不是所有查询都可以随意交换。GraphSymPerf 需要定义严格的安全条件。

#### 规则一：变量绑定一致性

交换节点顺序后，返回变量必须保持一致。

例如：

```
MATCH (a)--(b)
RETURN a, b
```

变换后应该是：

```
MATCH (b)--(a)
RETURN a, b
```

而不是简单返回 `b, a`，否则结果列语义可能变化。

#### 规则二：方向语义安全

只有在以下情况中允许边方向对称变换：

1. 使用无向边模式；
2. 图中显式存在反向边类型；
3. 查询语言语义允许方向无关匹配；
4. 通过结果验证确认变换后结果一致。

#### 规则三：谓词交换安全

仅交换满足交换律和结合律的逻辑谓词：

```
p AND q ≡ q AND p
p OR q ≡ q OR p
```

但需要避免：

1. 含有非确定函数的谓词；
2. 含有随机函数的谓词；
3. 含有副作用的函数；
4. 含有异常敏感表达式的谓词，例如除零风险。

#### 规则四：路径查询安全

路径端点交换只在以下情况下使用：

1. 无向路径；
2. 反向路径语义明确；
3. 路径长度约束保持一致；
4. 返回结果经过 canonicalization 后一致。

#### 规则五：结果规范化

由于图查询返回结果可能存在顺序差异，GraphSymPerf 需要对结果进行规范化：

1. 对结果行排序；
2. 对节点 ID 和边 ID 进行标准化；
3. 对路径表示进行 canonicalization；
4. 忽略无语义意义的返回顺序；
5. 比较 multiset 语义，而不是简单字符串比较。

------

### 7.4 Equivalence Validator（等价性验证器）

**目标：** 确认对称查询确实返回相同结果。

GraphSymPerf 可以使用两级验证：

#### 第一级：静态安全检查

根据变换规则判断是否理论安全：

```
Transformation is safe if:
- variables are consistently renamed;
- edge direction semantics are preserved;
- predicates are deterministic;
- return schema is unchanged.
```

#### 第二级：动态结果验证

实际执行 `Q` 和 `Q'`，比较结果：

```
Result(Q) = Result(Q')
```

如果结果不一致，则丢弃该查询对，不作为性能 bug 候选。

#### 为什么需要动态验证？

图数据库语言存在很多细节：

1. 路径返回顺序可能不同；
2. 重复路径数量可能不同；
3. 有向边和无向边语义在不同系统中可能不同；
4. NULL / missing property 语义可能影响谓词；
5. 多重边可能导致结果重复度变化。

因此，仅靠静态推理不够，需要运行时结果确认。

------

### 7.5 Performance Profiler（性能剖析器）

**目标：** 收集查询执行时间和执行计划信息。

对每个查询 `Q`，执行多次并记录：

1. 平均执行时间；
2. 中位数执行时间；
3. P95 执行时间；
4. 冷缓存执行时间；
5. 热缓存执行时间；
6. 结果数量；
7. 执行计划；
8. 节点扫描数；
9. 边扩展数；
10. 过滤操作数量；
11. 中间结果规模；
12. 内存使用；
13. 是否使用索引；
14. 是否发生路径爆炸。

如果系统支持：

```
EXPLAIN
PROFILE
```

则 GraphSymPerf 进一步解析计划算子，例如：

- `NodeByLabelScan`
- `NodeIndexSeek`
- `Expand`
- `Expand(All)`
- `Expand(Into)`
- `Filter`
- `HashJoin`
- `Apply`
- `VarLengthExpand`
- `Sort`
- `Aggregation`

------

### 7.6 Symmetry Violation Detector（对称性违例检测器）

**目标：** 判断一组对称查询是否存在性能异常。

给定一组对称查询：

```
S = {Q1, Q2, ..., Qn}
```

定义性能比值：

```
Ratio = max(Time(Qi)) / min(Time(Qi))
```

如果：

```
Ratio > threshold
```

例如：

```
Ratio > 10x
```

则标记为候选 symmetry-violation performance bug。

进一步结合计划差异判断：

```
PlanDiff(Qfast, Qslow)
```

如果慢查询相比快查询出现以下差异，则增强 bug 置信度：

1. 快查询使用索引，慢查询使用全节点扫描；
2. 快查询从高选择性节点开始，慢查询从低选择性节点开始；
3. 慢查询边扩展数量显著更多；
4. 慢查询中间结果规模显著更大；
5. 慢查询发生额外排序或聚合；
6. 慢查询路径扩展方向不合理；
7. 慢查询未下推过滤条件。

------

## 8. Oracle Design（测试 Oracle 设计）

**目标：** 说明 GraphSymPerf 的 oracle 不是绝对时间，而是对称查询之间的相对性能一致性。

### 8.1 Result Equivalence Oracle

首先保证：

```
Result(Q) = Result(Q')
```

如果结果不同，则不是性能问题，而可能是语义差异或 correctness bug。

### 8.2 Performance Symmetry Oracle

在结果一致的前提下，检测：

```
Time(Q') >> Time(Q)
```

如果性能差异超过阈值，则认为存在潜在性能异常。

### 8.3 Plan Symmetry Oracle

进一步检测执行计划是否违反对称性：

```
Plan(Q) should be structurally similar to Plan(Q')
```

不要求计划完全相同，但如果出现明显不合理差异，则认为更可能是真实 bug。

示例：

| 快查询计划                 | 慢查询计划               | 可能问题         |
| -------------------------- | ------------------------ | ---------------- |
| NodeIndexSeek              | NodeByLabelScan          | 索引选择不稳定   |
| Expand from selective node | Expand from dense node   | 扩展起点错误     |
| Filter before Expand       | Filter after Expand      | 谓词下推失败     |
| Bounded VarLengthExpand    | Full VarLengthExpand     | 路径扩展控制失败 |
| Small intermediate result  | Huge intermediate result | join order 错误  |

### 8.4 Robustness Oracle

同一个查询族中，如果大多数对称变体性能接近，但某一个变体异常慢，则该变体更可能暴露 bug。

例如：

```
Q1: 20 ms
Q2: 22 ms
Q3: 19 ms
Q4: 18 s
```

此时 `Q4` 是明显异常点。

------

## 9. Implementation Note（实现注记）

当前工程实现已将研究主线进一步结构化为三类产物：

1. **Plan Metamorphic Coverage（PMC）**
   对每个结果等价的 query pair 提取 `CoverageUnit`，覆盖 `structural`、`operator_delta`、`access_delta`、`predicate_delta`、`barrier_delta`、`cardinality_delta` 与 `work_delta` 等维度，并在 run 级维护 `pmc_summary.json` / `pmc_evidence.jsonl`。

2. **Feedback-guided Equivalent Transformation Selection**
   对 `bool_redundancy` 与 `symmetry` 两个变换族的 rule 维护 `RuleStats`，使用可解释 reward 组合 `equivalence_passed`、`candidate_count`、`pmc_new_units`、`timeouts` 等信号，输出 `feedback_summary.json` 与 `rule_stats.json`。当 `feedback.enabled=false` 时保持近似静态顺序；启用后则优先选择高 reward、高 novelty 的 rule。

3. **Candidate Classification, Signature, Clustering and Deduplication**
   对候选样本生成 root-cause label、plan signature、metric signature 与 PMC signature，并据此形成 `issue_family_key`、`unique_issue_key` 与 `duplicate_key`。运行结束后输出 `candidate_signatures.jsonl`、`issue_families.json`、`unique_issues.json`、`duplicate_groups.json` 与 `clustered_summary.json`。

这些实现使论文中的三个问题可以直接落到 artifact 上回答：

- 哪些 transform rule 真正贡献了新的 plan-level coverage；
- 哪些候选只是重复触发同一个根因；
- 哪些候选具有更高支持级别，应优先进入人工复核或 issue 报告。
