# ArcadeDB Issue 提交顺序

## 2026-07-21 新规则族优先级

### 1. 优先提交标签谓词性能 Issue

- 标题：`A label predicate in WHERE scans unrelated vertex types in OpenCypher`
- 正文：`arcadedb-label-predicate-performance-20260721/ISSUE.md`
- 证据：两份全新数据库、各 300,000 个无关节点、每份 20 组平衡配对；
  汇总 61.17x，倍率置信区间下界 46.55x，完整有序结果始终相同。
- 建议附件：`verify.py`、`setup.sql`、`setup.cypher`、`query_a.cypher`、
  `query_b.cypher`、`validation_summary.json`。
- 去重：与 #5226、#5084、#5095 仅同属标签相关路径，但触发条件、症状和
  执行上下文均不同；没有发现精确重复。

### 2. 再提交重复谓词禁用索引 Issue

- 标题：`A duplicate equality predicate changes NodeIndexSeek to NodeByLabelScan in OpenCypher`
- 正文：`arcadedb-duplicate-predicate-index-performance-20260720/ISSUE.md`
- 证据：两份全新数据库、20,000 个唯一索引节点、汇总 13.19x，倍率置信区间
  下界 10.83x。
- 去重：在 Related issues 中主动链接 #4405 和 #5107；说明 #4405 只修复
  常量布尔恒等式，而当前触发器是 `P AND P` 幂等律。

### 3. 暂不提交空值恒等式索引退化

- 相对倍率和计划变化都很强，但绝对噪声门槛未闭环。
- 保留原始 session 证据，不把大倍率直接表述为 submit-ready issue。
- 后续只有在两次独立会话都满足 A/B 差值高于 A/A、B/B 对照预算后再升级。

### 4. 在 #3403 跟进双重否定 logical 回归

- 优先评论：`arcadedb-double-negation-logical-followup-3403-20260721/FOLLOWUP_3403.md`
- 不直接新建：#3403 已覆盖括号与 `NOT` 改变结果，修复提交也位于同一个
  `parseBooleanExpression` 路径。
- 新触发器是纯标量 `NOT NOT P`，两份新库均稳定把 `[true]` 结果翻转为
  `[false]`；计划中也只剩单个 `NOT`。
- 若维护者要求拆分，再使用同目录的 `ISSUE.md`。

以下 2026-07-20 的 pattern-order 提交顺序保留为历史批次，不与本次两个新规则
族合并。

## 1. 先提交逻辑正确性 Issue

- 新建 Issue，不要作为 #5116 的重复项关闭。
- 标题：`Reordering two comma-separated patterns on the same variable drops a label constraint`
- 正文：`arcadedb-pattern-order-logical-20260720/ISSUE.md`
- 建议附件：
  - `reproducer.py`
  - `setup.sql`
  - `query_a.cypher`
  - `query_b.cypher`
- 在正文中保留 #5116 链接，说明是相关修复路径，但本问题是新版本的错误结果，不是原报告的性能差异。

## 2. 再在 #5117 发性能回归跟进评论

- 优先评论请求 reopen，不直接新建同根因 Issue。
- 评论正文：`arcadedb-pattern-order-performance-20260720/FOLLOWUP_5117.md`
- 建议附件：
  - `setup.cypher`
  - `query_a.cypher`
  - `query_b.cypher`
  - `reproducer.py`
  - `minimal_validation.json`
- 如维护者要求单独建回归 Issue，再使用 `ISSUE_IF_NEW.md`。

## 3. 对外口径

- 两个发现都由 `pattern_order_symmetry` 规则变换产生。
- 逻辑问题由完整类型结果等价门禁拦截，并被缩减为 1 个节点。
- 性能问题的最小查询已重新生成规则安全证书，并通过两个独立会话的生产级验证。
- 不宣称两个候选是两个全新根因；逻辑问题是独立错误类型，性能问题按 #5117 的修复覆盖缺口处理。
