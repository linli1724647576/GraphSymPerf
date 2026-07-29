# Deduplication decision

Decision: **new issue is justified**.

Checked against the current ArcadeDB issue tracker on 2026-07-24.

| Existing issue | Existing trigger and fix | Current trigger | Decision |
|---|---|---|---|
| [#5362](https://github.com/ArcadeData/arcadedb/issues/5362) | Indexed equality inside an `AND` predicate. | The equality is inside a `CASE` expression, not an `AND` branch. | Related index symptom, different AST and fix scope. |
| [#4405](https://github.com/ArcadeData/arcadedb/issues/4405) | `AND true` and `OR false` constant folding. | `CASE WHEN P THEN true ELSE false END` selection identity. | Different Boolean simplification rule. |
| [#5360](https://github.com/ArcadeData/arcadedb/issues/5360) | A `NOT`-chain parser bug that changed results. | No `NOT` expression; results are correct and only performance changes. | Not duplicate. |

Searches included `CASE WHEN index`, `OpenCypher CASE performance`,
`CASE NodeByLabelScan`, `CASE predicate optimizer`, and `CASE boolean
identity`. The returned issues concerned unrelated SQL updates, partition
pruning, and other query features; no matching issue was found.

This candidate is distinct from the direct Boolean-property draft because its
indexed equality is hidden by a `CASE` node. It is also distinct from the De
Morgan draft because no `NOT` or `OR` normalization is involved.
