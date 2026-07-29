# Deduplication note

Checked on 2026-07-21 against the local Neo4j issue ledger and the upstream
`neo4j/neo4j` issue tracker.

Search terms included:

- `"count(1)" "count(*)"`
- `"count literal"`
- `NodeCountFromCountStore count(1)`
- `count store literal performance`

No report with the same single-label `count(1)` trigger appeared in those
searches. The closest upstream reports are related but use different triggers:

- [#12888](https://github.com/neo4j/neo4j/issues/12888) reports that an
  additional constant result column prevents `count(*)` from using the count
  store. Its workaround moves the count into a preceding `WITH`. The new
  candidate has only one result column and changes the aggregate argument.
- [#2754](https://github.com/neo4j/neo4j/issues/2754) reports that `count(n)`
  became slower as a label grew in Neo4j 2.1.2. In Neo4j 2026.06.0, the
  `count(n)` control uses `NodeCountFromCountStore`, while `count(1)` does not.
- [#12982](https://github.com/neo4j/neo4j/issues/12982) includes `count(1)` in
  a larger query, but reports a directed relationship-index problem under
  `OPTIONAL MATCH`, not count-store selection for a label count.
- [#13522](https://github.com/neo4j/neo4j/issues/13522) uses `count(1)` in a
  logical-result report involving a later false `MATCH`; it does not report a
  performance or count-store problem.

The previously submitted pattern-order family is also separate:

- [#13813](https://github.com/neo4j/neo4j/issues/13813) reorders
  comma-separated patterns.
- [#13814](https://github.com/neo4j/neo4j/issues/13814) changes a nullable
  boolean disjunct.
- [#13783](https://github.com/neo4j/neo4j/issues/13783) inserts an
  always-false disjunct.

The current Neo4j 2026.06 drafts cover `coll.insert()` static typing, integer
aggregate order, integer division overflow, zero-width regular-expression
handling, and an exact zero-hop path. None uses a non-null aggregate literal.

This candidate is in the same broad count-store optimization area as #12888
and #2754, but its trigger and likely planner rewrite are different. The public
report should remain focused on `count(1)` rather than claim a general
count-store regression.
