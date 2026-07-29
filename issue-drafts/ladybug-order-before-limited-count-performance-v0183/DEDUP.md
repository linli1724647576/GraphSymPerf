# Deduplication note

Checked on 2026-07-22 against the complete LadybugDB issue list and targeted
GitHub searches in both `LadybugDB/ladybug` and the archived `kuzudb/kuzu`
repository.

Search terms included:

- `ORDER BY count LIMIT`
- `TOP_K count`
- `redundant order optimizer`
- `sort limit aggregate optimizer`

No report with the same `ORDER BY ... LIMIT ... count(*)` trigger appeared.
The nearest reports have different scopes:

- [LadybugDB #89](https://github.com/LadybugDB/ladybug/issues/89) optimized
  `count(*)` over a relationship pattern. It does not contain `ORDER BY`,
  `LIMIT`, or an unused `TOP_K` operation.
- [LadybugDB #529](https://github.com/LadybugDB/ladybug/issues/529) concerns
  direction-dependent relationship-degree counting. This candidate scans one
  node table and concerns removing an order that cannot affect a later count.
- [Kùzu #5228](https://github.com/kuzudb/kuzu/issues/5228) proposes rewriting a
  `WITH` boundary between pattern matches. It does not discuss ordering a
  limited row set before a cardinality-only aggregate.

The `LIMIT 500000` and `SKIP 100000` variants show the same plan family and
slowdown, so the trigger is not tied to one limit value.

Conclusion: not a duplicate based on trigger, symptom, or expected fix scope.
