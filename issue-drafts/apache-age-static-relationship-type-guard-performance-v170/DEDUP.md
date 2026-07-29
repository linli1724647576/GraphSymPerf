# Deduplication record

Checked on 2026-07-22 against the current `apache/age` issue tracker, the local
submitted-issue inventory, and Memgraph #4458.

## Verdict

No issue with the same trigger, symptom, version, and likely fix scope was
found.

## Closest upstream issues

- Apache AGE #2479 reports that relationship-type alternation such as
  `[:A|B]` scans all edge label tables and filters them afterward. This
  candidate uses one static type, `[r:P]`, and scans only the `P` table. Its
  extra work comes from a separate, redundant `type(r) = 'P'` predicate.
- Apache AGE #982 concerns undirected relationship matching and nested joins.
  This candidate uses a directed pattern and both variants have the same
  joins.
- Apache AGE #2428 concerns a contradictory NULL property predicate, not
  relationship type metadata.

## Cross-database comparison

Memgraph #4458 compares `[r:LINK]` with an untyped `[r]` followed by `WHERE
type(r) = 'LINK'`. Its slow query expands unrelated relationship types and
filters afterward. This AGE candidate keeps `[r:P]` in both queries, scans only
the `P` table, and adds an always-true guard. The likely fixes therefore differ:
type-filter pushdown for #4458 versus removing a guard already implied by the
static pattern type here.

Searches included `"type(r)" performance`, `"WHERE type" relationship`,
`relationship type filter performance`, `"type predicate"`,
`age_type performance`, and `_agtype_build_edge`.
