# ArcadeDB candidate deduplication decision

| Draft | Symptom | Trigger | Closest submitted issue | Decision |
|---|---|---|---|---|
| `arcadedb-double-negation-logical-followup-3403-20260721` | Wrong result: double `NOT` behaves as single `NOT` in `WHERE` | `P` versus bare `NOT NOT P` over true/false/missing values | #3403 | Distinct scalar trigger but overlapping parentheses/NOT parser path. Follow up on #3403 first; open a new issue only if requested. |
| `arcadedb-label-predicate-performance-20260721` | Correct results but a pooled 61.17x runtime gap; label predicate falls back to traditional execution | `MATCH (n:Parent)` versus `MATCH (n) WHERE n:Parent` | #5226 is the nearest label-path issue, but concerns wrong results inside `CALL` | New performance issue; no exact duplicate found. |
| `arcadedb-duplicate-predicate-index-performance-20260720` | Correct results but a pooled 13.19x runtime gap; unique-index seek becomes a label scan | `P` versus `P AND P` | #4405 | Related Boolean simplification family, but a distinct idempotence rule and current indexed runtime regression. New issue is justified. |
| `arcadedb-pattern-order-logical-20260720` | Wrong result: `1` versus `0` | The same variable appears as `(n:L5), (n)` versus `(n), (n:L5)` | #5116 | Not an exact duplicate. #5116 was a performance issue and explicitly stated that results were unchanged. Submit a new correctness issue and link #5116 as related. |
| `arcadedb-pattern-order-performance-20260720` | Equivalent results but an 8.15x / 764.6 ms runtime gap | Three independent patterns after `WITH 1 AS x`; only the last two are reordered | #5117 | Same root-cause family or incomplete fix coverage. Post `FOLLOWUP_5117.md` first and request reopen; open `ISSUE_IF_NEW.md` only if the maintainer requests a separate issue. |

## 2026-07-21 different-family decision

- The label-predicate candidate is independent of the previous pattern-order and
  Boolean-simplification families. It passed two fresh-database sessions and
  the negative-control artifact budget, so it is the strongest new submission.
- The duplicate-predicate candidate also passed independent validation. It is
  related to #4405, but #4405 implemented constant identities (`AND true` /
  `OR false`), not conjunction idempotence (`P AND P`).
- The double-negation logical candidate is independently reproducible and
  severe, but #3403 already covers parentheses-dependent `NOT` result changes
  in the same parser function. Treat it as an incomplete-coverage follow-up,
  not a third new issue by default.
- `null_partition_tautology` produced a real index-to-scan plan change and
  approximately 19x-33x relative gaps, but one negative-control outlier in each
  attempted campaign exceeded the absolute A/B median delta. It remains a held
  candidate and must not be submitted yet.
- Indexed exact-one-hop varlength, property-map/WHERE, numeric equality/range,
  and label-inheritance logical screens did not meet the issue gate.

## Why the two new drafts are not duplicates of each other

The logical draft changes the result set and uses two occurrences of the same variable, one labelled and one bare. The performance draft preserves complete typed results and uses three independent pattern components after a pipeline boundary. They differ in observable failure, variable-binding structure, minimal graph, and appropriate developer action.

## Existing issue comparison

- [#5116](https://github.com/ArcadeData/arcadedb/issues/5116) was closed after single-node patterns were reordered by selectivity. Its maintainer comment says the result set remains unchanged. The new one-node case contradicts that invariant on `26.8.1-SNAPSHOT`, so it deserves a separate correctness report.
- [#5117](https://github.com/ArcadeData/arcadedb/issues/5117) was closed after independent patterns were reordered to avoid written-order nested loops. The minimized current case has the same performance mechanism, so treating it as a follow-up/regression first is the conservative non-duplicate path.
