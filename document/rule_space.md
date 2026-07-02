下面给你一个**论文附录级（Appendix-ready）Rule Space Taxonomy 重构版本**。我按你当前系统的真实问题做了“结构升级”，不是简单扩充列表，而是做了三层设计：

1）现有 rule space 诊断版（你现在系统实际在用的）
2）重构后的 Bool Rule Space（增强语义+结构+三值逻辑）
3）重构后的 Symmetry / Structural Rule Space（从 lexical → graph-structure-level）

并且每一类都标注：

* 扰动类型
* 覆盖优化器行为
* PMC贡献方向

---

# Appendix A：Rule Space Taxonomy（重构版）

---

# A.1 现有 Rule Space（Calibration Version）

这一部分是你当前系统“真实在跑的规则空间”，从日志反推整理：

## Table A1：Current Rule Space (Baseline)

| Category | Rule Name                  | Transformation                     | Type                            | Coverage Focus           | Observed PMC Effect                  |
| -------- | -------------------------- | ---------------------------------- | ------------------------------- | ------------------------ | ------------------------------------ |
| Bool     | or_expr_null_contradiction | p OR (e IS NULL AND e IS NOT NULL) | Logical contradiction injection | Predicate simplification | High predicate_delta, low plan shift |
| Bool     | and_true_wrapper           | p AND true                         | Identity wrapper                | Constant folding         | Low structural impact                |
| Bool     | or_false_wrapper           | p OR false                         | Identity wrapper                | Constant folding         | Low structural impact                |
| Bool     | double_negation            | NOT NOT p                          | Boolean normalization           | Logical simplification   | Minimal impact                       |
| Bool     | nested_or_false            | p OR (q AND false)                 | Dead branch injection           | Dead code elimination    | Moderate predicate delta             |
| Symmetry | predicate_order_swap       | p1 AND p2 ↔ p2 AND p1              | Lexical permutation             | Expression ordering      | Low plan diversity                   |
| Symmetry | pattern_order_swap         | MATCH A,B ↔ MATCH B,A              | Clause ordering                 | Planner stability        | Medium operator_delta                |
| Symmetry | label_order_swap           | (n:L1:L2) ↔ (n:L2:L1)              | Label permutation               | Syntax normalization     | Very low structural effect           |
| Symmetry | variable_rename            | α-renaming                         | Symbol substitution             | Name invariance          | No plan impact                       |

---

## A.1 诊断结论（关键）

当前 rule space 特征：

* 80% 属于 **identity / lexical transformation**
* 15% 属于 **predicate-level mutation**
* 5% 才进入 graph structure

👉 直接导致：

> PMC ≠ performance-sensitive coverage

---

# A.2 重构 Bool Rule Space（增强版）

目标：
从“逻辑等价扰动”升级为：

> Logical + semantic + cost-model-sensitive mutation space

---

## Table A2：Enhanced Bool Rule Space (PMC-aware)

| ID | Rule Type                    | Transformation                                                       | Semantic Level                | Optimizer Target              | Expected Effect                  |
| -- | ---------------------------- | -------------------------------------------------------------------- | ----------------------------- | ----------------------------- | -------------------------------- |
| B1 | Constant Folding Stress      | p AND true → p AND (true OR x)                                       | Logical inflation             | constant folding failure      | predicate_delta + operator_delta |
| B2 | Dead Branch Amplification    | p OR false → p OR (q AND false)                                      | unreachable branch injection  | dead code elimination         | plan shape variation             |
| B3 | Null-aware contradiction     | e IS NULL OR e IS NOT NULL → TRUE branch embedded in predicate chain | 3-valued logic stress         | null propagation              | predicate + cardinality delta    |
| B4 | Predicate explosion          | p → p OR p OR p OR ...                                               | redundancy amplification      | optimizer deduplication       | operator redundancy              |
| B5 | Conditional flattening       | IF p THEN q ELSE q → q                                               | control-flow simplification   | logical equivalence detection | plan collapse test               |
| B6 | Boolean distribution stress  | p AND (q OR r) → distributed equivalent                              | algebraic rewrite sensitivity | pushdown testing              | join reorder impact              |
| B7 | Guarded contradiction        | (p AND NOT p) embedded inside subexpression                          | localized contradiction       | partial evaluation            | filter instability               |
| B8 | Multi-layer redundancy       | p → (p AND true) OR false                                            | nested identity inflation     | multi-stage simplification    | optimizer pipeline stress        |
| B9 | Null-guarded predicate shift | p → p AND (e IS NOT NULL OR TRUE)                                    | null safety injection         | null elimination path         | access path variation            |

---

## Bool Space升级本质

你从：

> “logical equivalence fuzzing”

升级为：

> “optimizer rewrite stress testing”

---

## Bool空间新增能力

✔ 触发 predicate pushdown failure
✔ 触发 partial evaluation instability
✔ 触发 null propagation edge cases
✔ 触发 multi-stage optimizer mismatch

---

# A.3 重构 Symmetry / Structural Rule Space（关键升级）

这是你当前系统最大短板。

---

## 设计目标

从：

> lexical symmetry (字符串变化)

升级为：

> structural symmetry (query plan topology invariance testing)

---

## Table A3：Structural Symmetry Rule Space (Enhanced)

| ID  | Rule Type                  | Transformation                               | Structural Level    | Optimizer Target    | Expected Effect           |
| --- | -------------------------- | -------------------------------------------- | ------------------- | ------------------- | ------------------------- |
| S1  | Predicate Commutativity    | p1 AND p2 ↔ p2 AND p1                        | Expression tree     | logical reorder     | plan ordering sensitivity |
| S2  | Clause Reordering          | MATCH A,B ↔ MATCH B,A                        | Query clause        | planner input order | join order instability    |
| S3  | Pattern Decomposition      | (a)-[:R]->(b)-[:S]->(c) ↔ split MATCH chains | graph topology      | join decomposition  | access path shift         |
| S4  | Pattern Fusion             | multiple MATCH → single MATCH chain          | graph topology      | join merging        | cardinality shift         |
| S5  | Optional Match Lift/Lower  | OPTIONAL MATCH restructuring                 | execution pipeline  | outer join behavior | cardinality explosion     |
| S6  | Direction Reversal         | (a)-[:R]->(b) ↔ (b)<-[:R]-(a)                | graph semantics     | index usage         | scan strategy shift       |
| S7  | Label Set Permutation      | (n:L1:L2) ↔ (n:L2:L1) under constraint       | schema symmetry     | label filtering     | index selection change    |
| S8  | Path Length Rewriting      | shortest path constraint expansion/reduction | traversal semantics | expansion strategy  | traversal cost change     |
| S9  | Variable Binding Shift     | WITH-based variable re-binding               | scope structure     | pipeline breaking   | memory + pipeline changes |
| S10 | Join Re-association        | (A⋈B)⋈C ↔ A⋈(B⋈C)                            | join tree           | join optimizer      | full plan rewrite         |
| S11 | Early/Late Filter Shift    | WHERE vs WITH placement swap                 | pipeline stage      | pushdown logic      | DB hits change            |
| S12 | Optional-to-Mandatory Flip | OPTIONAL MATCH ↔ MATCH + guard               | null semantics      | join type selection | cardinality divergence    |
| S13 | Projection Reordering      | RETURN field order swap (with dependencies)  | output pipeline     | projection pushdown | projection cost change    |
