# 随机外层查询的搜索空间

本文对应宿主语法 `typed-host-v2`。方法逻辑见 [方法文档 M1](ast-pattern-method.md#2-m1-基于-ast-的随机查询生成)，实现合同见 [代码技术文档 M1](ast-pattern-implementation.md#m1先随机生成抽象-ast)。下列变量名与关系名用于解释形状，不代表某次运行的原始输出。

## 1 从少量原语组合出更多查询

生成器先选择子句骨架，再在类型、作用域和预算允许的范围内展开每个位置。它没有保存一批完整查询供随机抽取。

顶层可选择的子句形式为：

```text
MATCH
OPTIONAL MATCH
WITH
WITH DISTINCT
RETURN
RETURN DISTINCT
```

RETURN 形式终止当前查询块。WHERE 随 MATCH 或 WITH 生成，不作为可以脱离变量定义单独排列的子句。COUNT、EXISTS、CASE、coalesce 属于表达式；UNION ALL 和条件结果表用于组合子查询的结果，不能混同为顶层线性骨架中的任意后继。

例如，可以生成：

```text
MATCH → WITH DISTINCT → RETURN
```

也可以生成：

```text
RETURN
└── COUNT
    └── UNION ALL
        ├── 含 Pattern 插入口的结果表
        └── 普通查询结果表
```

每次选择的是当前位置的下一条产生式；多样性来自这些选择的组合。

## 2 匹配结构有哪些变化

| 候选名 | 示意 | 变量关系 |
|---|---|---|
| `node` | `a` | 一个节点 |
| `edge` | `a → b` | 两个节点之间的一条关系 |
| `chain` | `a → b → c` | 下一条 MATCH 复用前一条的终点 |
| `branch` | `a → b` 与 `a → c` | 多条 MATCH 共享起点 |
| `converge` | `a → c` 与 `b → c` | 多条 MATCH 共享终点 |
| `cycle` | `a → b → c → a` | 末条 MATCH 连接回已有节点 |
| `disconnected` | `a → b` 与 `c → d` | 不同分量使用不同变量 |

箭头仅表示一种可选方向；单边还可以反向或无向。链、分支、汇合、闭环和独立分量均由多个独立 MATCH 实现。例如，链可以表示为：

```cypher
MATCH (a:A)-[:T1]->(b:B)
MATCH (b)-[:T2]->(c:C)
RETURN /* 随机表达式与一个 Pattern 插入口 */
```

这里的注释是结构占位说明，不是完整可执行查询。两个 MATCH 共享变量 b，因此整个查询具有两段连接关系。这一实现没有将两个 MATCH 合并为同一个多段路径；关系是否可以在不同匹配中重复使用，按实际子句边界处理。

每个形状还可以组合：

- 节点的多个标签。
- 一条关系允许的多个关系类型。
- 节点和关系的属性映射约束。
- 命名关系变量，供后续合法表达式读取。
- 对已有可见节点的复用。

例如，“两段链＋中间节点多标签＋第二条边允许两种类型”是这些维度的一次组合，而不是额外登记的一条完整模板。

## 3 表达式怎样扩大搜索空间

假设 RETURN 需要一个整数，可以选择整数叶子、CASE、coalesce 或 COUNT。CASE 会继续生成布尔条件和两个整数分支；COUNT 会继续生成输入结果表。布尔条件还可以选择六种数值/等值比较、空值判断、AND、OR、NOT、CASE、coalesce 或 EXISTS。

例如，生成过程可以逐层得到：

```text
返回整数
└── CASE
    ├── 条件：某个计数 > 某个整数
    ├── THEN：COUNT
    │   └── Pattern 结果表插入口
    └── ELSE：某个整数
```

还可以生成多个返回列，使目标结果与其他普通表达式共同出现在输出中。AND、OR 和 coalesce 的操作数数量由预算限制，不再固定为两项。显式 NULL 叶子与 IS NULL/IS NOT NULL 保留空值相关的生成机会。

当前 IR 将未具体绑定的属性访问视为未知类型，因此有序比较的操作数使用可确认的数值表达式。等值比较和空值判断仍可读取属性。类型筛选发生在随机选择之前，不能先任意拼接再忽略类型失败。

## 4 如何控制候选与复杂度

以下为 `generation.ast` 内的配置片段，未列出的参数沿用默认值：

```yaml
grammar:
  match_shapes: [node, edge, chain, branch, converge, cycle, disconnected]
  match_features: [multi_label, relationship_alternatives, property_maps, named_relationship]
  row_operators: [query, union_all, conditional, limit]
  comparison_operators: ['=', '<>', '<', '<=', '>', '>=']
ast_budget:
  clauses: 4
  match_edges: 3
  projections: 2
  expression_arity: 3
  expression_depth: 3
  subquery_depth: 3
  nodes: 180
```

`match_edges` 控制匹配组规模，`clauses` 还限制展开后各条 MATCH 与其他子句组成的查询块。若只剩一个子句名额，就不能强行放入需要两条 MATCH 的分支形状。`nodes` 为整棵 AST 的共享预算；增加候选不取消预算。

候选还受 `capabilities` 过滤。若目标不支持某种结构，应在对应候选中禁用；同一限制也作用于嵌套查询。

## 5 当前边界

| 结构或操作 | 当前边界 |
|---|---|
| Pattern 插入口 | 全树一个显式端口，位于最终 RETURN 表达式的子树中 |
| UNION ALL/条件结果表 | 分支列接口一致；端口只沿一条递归路径传递，可以落在分支或条件守卫中 |
| LIMIT | 普通宿主只放在不含目标端口的固定子树中；包含目标的无序截断不具有通用等价传播证明 |
| 裸有界路径、单边自环 | 不作为普通宿主形状生成；P3 使用已有单有界路径及节点局部 ALL 的专门支持片段 |
| 单 MATCH 多边路径 | 本轮采用多 MATCH 共享变量组合，没有新增该原子类型 |
| UNWIND、ORDER BY、SKIP | 尚未接入查询表示、构图和依赖传播全链 |

M1 扩大的是可生成结构的集合。具体结构仍须通过 M2 的接口检查、M3 的联合构图以及 M6 的结果关系和目标依赖检查，才能成为接受的测试用例。
