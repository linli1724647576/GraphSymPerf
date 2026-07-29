# Deduplication note

Checked on 2026-07-22 against the upstream `neo4j/neo4j` issue tracker and the
local ledger of previously submitted Neo4j reports.

Search terms included:

- `count CASE WHEN index performance`
- `conditional aggregation index`
- `count conditional predicate pushdown`
- `CASE WHEN NodeByLabelScan`

No report with the same indexable-predicate-inside-`count` trigger appeared.
The nearest reports have different scopes:

- [#13814](https://github.com/neo4j/neo4j/issues/13814), submitted earlier from
  this project, concerns a redundant nullable Boolean disjunct. It does not use
  aggregation, `CASE`, or an indexable equality inside `count`.
- [#13783](https://github.com/neo4j/neo4j/issues/13783), submitted earlier from
  this project, inserts an always-false disjunct into `WHERE`. This candidate
  moves an indexable condition across an aggregate boundary.
- [#13864](https://github.com/neo4j/neo4j/issues/13864), submitted earlier from
  this project, concerns the aggregate argument `count(1)` and count-store
  selection. This candidate concerns predicate extraction and index selection.
- [#13010](https://github.com/neo4j/neo4j/issues/13010) reports that adding
  indexes makes a complex `OPTIONAL MATCH` plan slower on Neo4j 5.2.0. This
  candidate isolates a conditional aggregate whose predicate is not exposed
  to an existing range index.

The trigger is a common conditional-aggregation form, not a contradiction or
always-false predicate. Its likely fix scope does not overlap the previously
submitted Boolean-normalization reports.

Conclusion: not a duplicate based on trigger, symptom, version, or expected
fix scope.
