# Deduplication note

Checked on 2026-07-21 against the local Neo4j issue ledger and the upstream
`neo4j/neo4j` issue tracker.

Search terms included:

- `"0..0" performance`
- `"*0..0" Cypher`
- `"zero-hop"`
- `"zero length" path`
- `VarLengthExpand performance`

No report with the same exact-zero-hop trigger appeared in those searches.
One upstream title mentions a zero-size path, but its trigger and symptom are
different:

- [#13017](https://github.com/neo4j/neo4j/issues/13017) reports incorrect
  results when the same relationship-list variable is reused across two
  variable-length patterns. The Cypher team confirmed that relationship-list
  reuse was the bug. The new candidate uses one unnamed exact `*0..0` pattern,
  returns correct rows, and reports unnecessary expansion work.

The closest local reports also use different transformations:

- [#13813](https://github.com/neo4j/neo4j/issues/13813) reorders
  comma-separated patterns.
- [#13814](https://github.com/neo4j/neo4j/issues/13814) changes a nullable
  boolean disjunct.
- [#13783](https://github.com/neo4j/neo4j/issues/13783) inserts an
  always-false disjunct.

The current four Neo4j 2026.06 drafts cover `coll.insert()` static typing,
integer aggregate order, integer division overflow, and zero-width regular
expression handling. None uses a path-bound transformation.
