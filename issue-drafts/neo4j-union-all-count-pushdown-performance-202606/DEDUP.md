# Deduplication note

Checked on 2026-07-22 against the upstream `neo4j/neo4j` issue tracker and the
local ledger of previously submitted Neo4j reports.

Search terms included:

- `UNION ALL count pushdown`
- `aggregation pushdown UNION`
- `NodeCountFromCountStore subquery`
- `count UNION performance`

No report with the same outer-count-over-`UNION ALL` trigger appeared. The
nearest reports have different scopes:

- [#13864](https://github.com/neo4j/neo4j/issues/13864), submitted earlier from
  this project, reports that `count(1)` does not use the count store. In this
  candidate, the manually pushed-down `count(n)` already uses
  `NodeCountFromCountStore`; the missing optimization is aggregate pushdown
  across `UNION ALL`.
- [#12888](https://github.com/neo4j/neo4j/issues/12888) reports that returning
  an additional constant column prevents a label count from using the count
  store. Neither query here returns an extra branch column; the difference is
  whether the outer count is pushed through `UNION ALL`.
- [#13753](https://github.com/neo4j/neo4j/issues/13753) concerns implicit
  variable import into `CALL` subqueries and incorrect query behavior. It does
  not concern aggregation or `UNION ALL` performance.
- [#13720](https://github.com/neo4j/neo4j/issues/13720) reports an internal
  error for `FOREACH`, a `COUNT {}` subquery, and `UNION ALL`. This candidate is
  a read-only query that succeeds and returns the correct result.
- [#13630](https://github.com/neo4j/neo4j/issues/13630) reports different
  results when a variable-length `OPTIONAL MATCH` is expanded into several
  `OPTIONAL CALL` union branches. This candidate has no optional match or
  variable-length path and concerns only outer-count pushdown.
- [#2725](https://github.com/neo4j/neo4j/issues/2725) requested syntax for
  processing a result after `UNION` before `CALL` subqueries provided the form
  used here. This candidate uses the supported syntax and concerns its plan.
- [#13865](https://github.com/neo4j/neo4j/issues/13865), submitted earlier from
  this project, concerns an exact zero-hop path and has no subquery or union.

This candidate and #13864 both benefit from the count store, but their triggers
and expected planner changes are separate. Fixing literal recognition for
`count(1)` would not push an outer count into `UNION ALL` branches.

Conclusion: not a duplicate based on trigger, symptom, version, or expected
fix scope.
