# Deduplication decision

Decision: **new issue is justified**.

Checked against the current ArcadeDB issue tracker on 2026-07-24.

| Existing issue | Existing trigger and fix | Current trigger | Decision |
|---|---|---|---|
| [#5362](https://github.com/ArcadeData/arcadedb/issues/5362) | An `AND` predicate hid an indexed equality. The fix walks `AND` branches and parentheses. The maintainer explicitly noted that `OR` and `NOT` were left out. | De Morgan normalization must cross one `NOT`, one `OR`, and two inner `NOT` expressions before the indexed conjunction is visible. | Related planner area, different Boolean rule and fix scope. |
| [#5360](https://github.com/ArcadeData/arcadedb/issues/5360) | A parser bug dropped one token from a bare `NOT NOT` chain and changed results. | Parenthesized `NOT` expressions return the correct result; only the access path and runtime differ. | Not duplicate. |
| [#4405](https://github.com/ArcadeData/arcadedb/issues/4405) | Constant folding for `AND true` and `OR false`. | De Morgan normalization with no Boolean constants. | Not duplicate. |

Searches included `De Morgan`, `OpenCypher NOT index`, `NodeByLabelScan`,
`full label scan`, `negation optimizer`, and `NOT OR index`. No issue with the
same transformation was found.

This candidate is also distinct from the other two 2026-07-24 drafts:
`CASE` branch simplification and direct Boolean-property index extraction do
not require De Morgan normalization.
