# GraphSymPerf 图原生 mutator 候选调研

> 日期：2026-07-29
>
> 状态：方案候选，不代表当前仓库已经实现或验证。
>
> 边界：所有正式候选都作用在同一个任意固定图状态 \(G\) 上；不依赖图状态自同构，不使用
> 布尔冗余、普通谓词重写或纯格式变化补足查询族成员。

## 1. 结论

在已锁定的两个第一批 CORE 之外，建议新增五个 `INCUBATING` mutator：

1. `cycle_pattern_dihedral_linearization`
2. `fixed_path_segmentation`
3. `repeated_node_constraint_reanchoring`
4. `relationship_uniqueness_scope_refactoring`
5. `bounded_path_quantifier_representation`

另增加三个 `DIALECT_CONTROL`：

1. `explicit_default_match_mode`
2. `explicit_default_path_mode`
3. `label_intersection_notation`

这些规则扩大的是 GDBMS 特有语义面，而不是把一般等价重写重新包装成对称性。正式查询族
仍须满足：

- 至少 4 个语义等价、表面表示不同的成员；
- 单规则或多规则生成元通过联合证书；
- 任一未知语义义务 fail-closed；
- 完整类型化结果在同一图快照上动态一致；
- 族级平衡运行时预言和独立复验通过。

## 2. 原始语义依据

### 2.1 图模式、equijoin 与关系唯一性

Neo4j 当前 Cypher reference 把 graph pattern 定义为一个或多个逗号分隔的 path pattern；
同一节点变量重复出现表达 equijoin，默认 match mode 对整个 graph pattern 施加关系唯一性。
这为循环旋转、路径分段和节点约束重锚定提供了语言语义基础：

- [Path patterns and graph patterns](https://neo4j.com/docs/cypher-manual/current/patterns/reference/path-patterns-and-graph-patterns/)
- [Match modes and path modes](https://neo4j.com/docs/cypher-manual/current/patterns/reference/match-modes-and-path-modes/)
- [Formal Semantics of the Language Cypher](https://arxiv.org/abs/1802.09984)

### 2.2 属性图约束

节点模式和关系模式可以携带 label/type、property map 和 element predicate；关系具有单一
type，而节点可以具有多个 label：

- [Node and relationship patterns](https://neo4j.com/docs/cypher-manual/current/patterns/reference/node-and-relationship-patterns/)

因此“关系类型集合两两不交”可以作为跨 `MATCH` 改变关系唯一性作用域的一个静态充分
条件；它不依赖当前图中碰巧不存在某条边。

### 2.3 量化路径表示

Neo4j 文档明确给出 quantified relationship、展开的 quantified path pattern 和 legacy
variable-length relationship 之间的表示对应，同时指出 group variable、量词语法、类型
表达和 inline predicate 的差异：

- [Variable length paths](https://neo4j.com/docs/cypher-manual/current/patterns/reference/variable-length-paths/)

因此该候选只能在有限、不可观察路径值的子域中使用，并必须按方言能力启用。

## 3. 与最近邻工作的边界

- GRev 使用 Abstract Syntax Graph（ASG）和 Random Walk Covering（RWC）从不同 path
  covers 生成等价查询，已经覆盖一般图模式重新线性化、链分解和约束重新分配的核心思想。
  [Mang et al., ICSE 2024](https://doi.org/10.1145/3597503.3639200)
- Gamera 基于图原生结构定义蜕变关系，证明“使用图特有语义构造测试关系”本身已有直接
  先例。[Zhuang et al., PVLDB 2023](https://doi.org/10.14778/3636218.3636236)
- GraphQE 使用面向 Cypher 的代数表示和 SMT 证明查询等价，说明图查询等价不能简单交给
  SQL prover；GraphSymPerf 的证书设计应把它视为证明方向上的最近邻工作。
  [Tang et al., ICDE 2025](https://arxiv.org/abs/2504.15742)
- GRAF 已强调 graph-context-aware generation 和 graph-specific mutation；“规则数量更多”
  不能作为独立新颖性主张。[Li et al., arXiv 2026](https://arxiv.org/abs/2607.03741)

因此新增规则的论文定位只能是：**结构化对称族框架的多样实例和覆盖面**。方法贡献仍然
来自显式查询族、联合适用证书和族级性能不一致预言。

## 4. 候选漏斗

先从 GDBMS 语义面发散 16 个候选，再按“任意 \(G\) 等价、图原生、可认证、非普通 EQR、
可形成四成员族”收敛：

| ID | 原始候选 | 结论 | 原因 |
|---|---|---|---|
| C01 | 循环起点旋转 | 保留 | 图拓扑表示自由度，轨道规模高 |
| C02 | 循环遍历反射 | 合并 C01 | 与旋转共同形成二面体动作 |
| C03 | 固定链分段/合并 | 保留 | 同一 graph pattern 的 path-cover 表示 |
| C04 | 一般 connected-pattern path cover | 不单列 | 与 GRev RWC 直接重叠且实现面过大 |
| C05 | 重复节点 label 重锚定 | 保留 | 同一变量 equijoin 的属性图约束 |
| C06 | 重复节点 property 重锚定 | 合并 C05 | 共享 occurrence 与证书模型 |
| C07 | `MATCH` 分段/融合 | 有条件保留 | 需证明关系唯一性作用域不变 |
| C08 | quantified relationship 展开 | 保留 | 路径量词的图语言特有表示 |
| C09 | legacy var-length 与 quantified bridge | 合并 C08 | 共享路径可观察性和方言义务 |
| C10 | 单跳 `*1..1` 与 fixed edge | 不单列 | group/list 变量差异大，作为 C08 边界样例 |
| C11 | 显式/隐式 default match mode | 控制 | 语法规范化价值高，方法新颖性低 |
| C12 | 显式/隐式 default path mode | 控制 | 方言窄且主要测试 parser/planner normalization |
| C13 | multi-label notation | 控制 | 属性图特有但基本是表面语法 |
| C14 | shortest-path selector 改写 | 拒绝 | 多条最短路径时可观察 path identity 不稳定 |
| C15 | `OPTIONAL MATCH` 重排 | 拒绝 | NULL 扩展和相关变量会改变基数 |
| C16 | inverse relationship type 反转 | 拒绝 | 需要 schema/data 事实，不对任意 \(G\) 成立 |

另外拒绝把 inline predicate 与外部 `WHERE` 迁移、匿名变量引入、命名路径引入和重复约束
当作正式规则：它们分别退化为普通 selection rewrite、无用绑定或冗余构造。

## 5. 五个孵化规则

### 5.1 `cycle_pattern_dihedral_linearization`

设固定循环模式包含 \(k\) 条关系。旋转生成元 \(\rho\) 改变文本起点；反射生成元 \(\tau\)
改变遍历方向，并同步改变每条关系的箭头书写，使 source/target 变量不变。候选轨道为二面体
群 \(D_k=\langle\rho,\tau\rangle\)，最多包含 \(2k\) 个表示。

证书义务：

- ordinary mandatory `MATCH`；
- fixed-length cycle；
- 首尾重复节点变量表达同一节点 equijoin；
- 无命名路径、quantified pattern、path selector 或重复关系变量；
- 每条关系的变量、type、property、方向和端点身份保持；
- `RETURN/WITH` 的变量身份和结果顺序契约不变。

可行性：中高。现有 parser 已支持 fixed chain，`PatternIR` 需要增加 cycle identity 和规范化
遍历序列。与 GRev 重叠高，定位为 family/certificate 实例。

### 5.2 `fixed_path_segmentation`

对包含 \(k\) 条固定关系的 chain，在 \(k-1\) 个内部边界选择 cut set。每个 cut set 把同一
chain 表示成同一个 graph pattern 中的若干 constituent path patterns，最多形成
\(2^{k-1}\) 个成员。

证书义务：

- 所有分段仍位于同一 `MATCH` 和同一 match mode；
- cut 两侧共享同一节点变量；
- 关系集合、方向、类型、属性和变量身份保持；
- 无 named path 或任何路径值投影；
- 不把分段移动到多个 `MATCH`。

可行性：高。当前 AST 已同时表示 chain 与 comma-separated `PatternList`。与 GRev 重叠
最高，应作为外部基线覆盖与证书开销实验，不承担规则新颖性。

### 5.3 `repeated_node_constraint_reanchoring`

若节点变量 \(v\) 在同一个 graph pattern 中出现于 occurrence 集合
\(\{o_1,\ldots,o_t\}\)，则把属于 \(v\) 的一个静态 label/property constraint 从一个
occurrence 移到另一个 occurrence。多个约束与多个 occurrence 可形成组合轨道。

证书义务：

- 所有 occurrence 在同一 mandatory graph pattern 中；
- 它们通过同名变量 equijoin 到同一节点；
- 每个 constraint 恰好保存一次；
- 只支持静态 label 和 literal/parameter property map；
- 不移动 inline `WHERE`，不跨 scope 或 optional boundary；
- 方言支持目标 label/property 语法。

可行性：中。现有 parser 已提取 `NodePattern.labels/properties`，但当前
`lower_pattern(EdgePattern)` 没有把端点节点约束纳入 edge canonical form。必须先改
`PatternIR`，否则旧 RA proof 会产生伪证书。

### 5.4 `relationship_uniqueness_scope_refactoring`

Cypher 默认关系唯一性作用于一个 graph pattern；多个连续 `MATCH` 具有不同作用域。因此
一般的 `MATCH` split/fuse 并不等价。只有在静态证明不同分段不可能匹配同一关系时才允许
构造轨道。

第一阶段采用保守充分条件：

- 每个分段只含固定长度关系；
- 各分段的静态 relationship type 集合非空且两两不交；
- 关系没有预绑定，也没有跨分段重复变量；
- 没有 `WHERE`、path selector、quantified path、optional/subquery/write boundary；
- 目标方言确认同一 graph pattern 采用 relationship-isomorphic matching。

若有 \(k\) 个顺序固定的分量，则 \(k-1\) 个 clause cut positions 可形成
\(2^{k-1}\) 种 groupings。这个轨道改变的是关系唯一性的语法作用域，同时用证书证明该变化
在具体 query 上不可观察。

可行性：中低。需要新增跨子句 bag semantics，而非复用当前 join canonicalization。它是
五个候选中差异化最强、也最容易出 unsoundness 的规则。

### 5.5 `bounded_path_quantifier_representation`

在 capability 支持时，把同一个有界 relationship traversal 表示为：

1. legacy variable-length relationship；
2. quantified relationship；
3. expanded quantified path pattern。

证书义务：

- lower/upper bounds 相同且 upper bound 有限；
- direction、relationship type expression 和 endpoint identity 相同；
- path、relationship-list 和 group variables 均不可观察；
- 不含 inline predicate、零次迭代、shortest selector 或特殊 match/path mode；
- 每个表示按目标方言重新解析；
- 在小图穷举测试中验证路径 bag/multiplicity，而不只比较 distinct endpoints。

可行性：中低。当前 AST 只有 legacy finite undirected var-length fragment，需要新增
`QuantifiedRelationshipAtom`、`QuantifiedPathAtom` 和 group-variable contract。该规则先以
Neo4j capability 开始，不能假定 Memgraph、FalkorDB、AGE、AgensGraph 或 ArcadeDB 支持
同一语法。

## 6. 方言控制规则

### 6.1 `explicit_default_match_mode`

在支持的方言中比较 implicit default 和显式 `DIFFERENT RELATIONSHIPS`。它直接检查 parser
和 planner 是否对等价 match-mode 表达异常敏感，但本质上是显式默认值，因此只作控制。

### 6.2 `explicit_default_path_mode`

只有在目标版本明确声明默认有效约束与显式 `TRAIL` 相同，并且量词有有限上界时启用。
不同版本和 DBMS 的默认路径语义可能不同，capability 必须带版本。

### 6.3 `label_intersection_notation`

比较受支持方言中的 legacy multi-label colon form 与 label-intersection expression。它可
测试 label scan/index normalization，但只属于方言表面表达控制。

## 7. 实现优先级

| 优先级 | 规则 | 原因 | 进入正式统计的前提 |
|---|---|---|---|
| P0 | `cycle_pattern_dihedral_linearization` | 轨道天然大、图原生、与现有 chain parser 接近 | cycle proof + ≥4 成员 + 联合证书 |
| P0 | `fixed_path_segmentation` | 当前 AST/RA 最接近 | relationship uniqueness contract |
| P1 | `repeated_node_constraint_reanchoring` | 可压力测试标签/索引锚点 | 修复 PatternIR 节点约束语义 |
| P2 | `relationship_uniqueness_scope_refactoring` | 差异化最强 | 跨子句 bag proof + 方言能力 |
| P2 | `bounded_path_quantifier_representation` | 路径语义覆盖独特 | quantified AST + multiplicity tests |
| CONTROL | 三个 dialect controls | 低成本校准 parser/planner | 永不进入正式产率 |

不建议五条同时实现。先完成两个已锁定 CORE，再用 P0 规则验证扩展接口；P1/P2 是否继续，
由 accepted-family yield、证书拒绝分布和真实 DBMS 先导结果决定。

## 8. 最小验证矩阵

每个孵化规则至少包含：

1. AST round-trip 和 identity hash；
2. 正向性质测试：所有成员 `semantic_hash` 相同；
3. 反例测试：删除一个证书前提后必须拒绝；
4. 小图穷举：自环、平行边、重复节点、不同 relationship type、NULL property；
5. 完整 typed bag result comparison；
6. 两个独立作用位点组合形成至少 4 成员；
7. 伪造联合证书与 action prefix 被拒绝；
8. 每个目标 GDBMS 的语法 capability probe；
9. 普通执行的平衡 block runtime；
10. 与 GRev 能否生成同类表示的覆盖对照。

## 9. 严格审查结论

三个最致命风险是：

1. **GRev 重叠**：C01–C06 中多个规则都可被 ASG/RWC 覆盖，不能以“新增 mutator 数量”作为
   新颖性证据。
2. **伪等价**：关系唯一性、group variable、路径多重度和可观察 path identity 任一遗漏，
   都会把正确性差异误报为性能异常。
3. **工程地基不足**：当前 AST/RA 只覆盖有限 Cypher fragment，特别是 edge lowering
   没有完整携带端点节点约束；直接加 renderer 会先扩大伪证书面。

判决：**有条件通过**。五个孵化规则足以增加拓扑、约束、路径和作用域多样性，但只能按
P0 → P1 → P2 分阶段落地；升级 CORE 必须另行决策，不得因规则实现完成而自动晋级。

按 systems profile 的候选组合级审查分为：创新性 68、理论/语义严谨性 72、workload
支撑 70、实验可控性 82、方法贡献 76、差异化 65、工程可行性 74、影响力 73；使用审查
模板的经验权重得到 72.48/100。该分数只用于方案内排序，不是会议录用概率或实验结果。

最可能的三个拒稿理由：

1. “新增规则大多已被 GRev 的一般 path-cover 机制覆盖。”
2. “关系唯一性和量化路径规则没有完整 bag/multiplicity 证明，性能差异可能来自伪等价。”
3. “规则面扩张快于统一 `PatternIR`、证书和 family oracle 的实现，系统贡献仍像规则集合。”

对应的单变量压力测试：

- 若移除联合证书和 family-level oracle 后方法与 GRev 无实质差异，则规则数量不能进入贡献
  陈述；
- 若 `PatternIR` 不能保留端点节点约束，则禁用 constraint reanchoring，而不是依赖动态
  同结果掩盖证明缺口；
- 若 quantified path representation 只在单一 Neo4j 版本可用，则把它降为 dialect-specific
  case study，不用它支撑跨 GDBMS 普适性。
