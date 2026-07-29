# Deduplication note

Checked on 2026-07-22 against the upstream `neo4j/neo4j` issue tracker and the
local ledger of previously submitted Neo4j reports.

Search terms included:

- `elementId DISTINCT performance`
- `redundant DISTINCT unique`
- `elementId(n) DISTINCT`
- `Distinct operator elementId`

No report with the same `DISTINCT elementId(n)` performance trigger appeared.
The nearest reports have different scopes:

- [#13580](https://github.com/neo4j/neo4j/issues/13580) reports incorrect
  results after adding `DISTINCT` to a complex `OPTIONAL MATCH` query on Neo4j
  5.26.2. This candidate returns the correct result and isolates duplicate
  removal over `elementId(n)`.
- [#13423](https://github.com/neo4j/neo4j/issues/13423) concerns scalar
  functions, including `elementId()`, on virtual nodes and relationships
  produced by `UNWIND`. This candidate uses stored nodes from a label scan.
- [#13865](https://github.com/neo4j/neo4j/issues/13865), submitted earlier from
  this project, concerns an exact zero-hop path and does not use `DISTINCT` or
  `elementId()`.

The earlier LadybugDB primary-key report uses the broad idea that a projected
value is already unique, but its trigger is a declared primary key in another
database. This Neo4j candidate depends on the documented `elementId()`
contract and requires a different planner rule.

Conclusion: not a duplicate based on trigger, symptom, version, or expected
fix scope.
