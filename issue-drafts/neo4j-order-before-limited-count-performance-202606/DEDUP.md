# Deduplication note

Checked on 2026-07-22 against the upstream `neo4j/neo4j` issue tracker and the
local ledger of previously submitted Neo4j reports.

Search terms included:

- `ORDER BY LIMIT count performance`
- `redundant Top count`
- `sort before aggregate optimizer`
- `ORDER BY count(*) slowdown`

No report with the same `ORDER BY ... LIMIT ... count(*)` trigger appeared.
The nearest reports have different scopes:

- [#13376](https://github.com/neo4j/neo4j/issues/13376) reports a broad planner
  regression for a complex query after upgrading from Neo4j 3.5 to 4.4. It
  does not isolate an unused order before a cardinality-only aggregate.
- [#13355](https://github.com/neo4j/neo4j/issues/13355) concerns incorrect
  paging results when the ordering keys contain ties. This candidate returns
  the correct count and concerns a retained `Top` operator.
- [#6584](https://github.com/neo4j/neo4j/issues/6584) asks Neo4j to use a
  property index to satisfy an observed `ORDER BY ... LIMIT 1`. In this
  candidate, the final count does not observe row order, so the expected
  optimization is to remove the `Top` rather than find a faster sort source.
- [#13865](https://github.com/neo4j/neo4j/issues/13865), submitted earlier from
  this project, concerns an exact zero-hop variable-length path. It does not
  use `ORDER BY` or `LIMIT`.

The same rule previously exposed an optimization opportunity in LadybugDB,
but that is a different database and planner implementation.

Conclusion: not a duplicate based on trigger, symptom, version, or expected
fix scope.
