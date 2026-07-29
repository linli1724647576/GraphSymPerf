# Apache AGE candidate summary (2026-07-22)

## Promoted issue drafts

| Rule | Family | Result | Draft |
|---|---|---|---|
| `fixed_path_length_guard_symmetry` | SYM | fixed two-hop path becomes about 11x slower after adding `length(p) = 2` | `apache-age-fixed-path-length-guard-performance-v170/ISSUE.md` |
| `typed_relationship_type_guard_symmetry` | SYM | `[r:P]` becomes about 6x slower after adding the redundant `type(r) = 'P'` guard | `apache-age-static-relationship-type-guard-performance-v170/ISSUE.md` |

Both candidates returned the expected 20,000 rows and passed 12 alternating
comparisons on two independent AGE 1.7.0/PostgreSQL 18.1 instances. A/A and
B/B controls were small relative to the observed gap.

The related `head(nodes(p)) = start` and `last(nodes(p)) = end` rules also
reproduced, but they were not split into extra issues because they belong to
the same path-invariant optimization family as the fixed-length guard.

## Rejected as upstream duplicates

| Screen lead | Upstream overlap | Decision |
|---|---|---|
| directed pattern versus undirected pattern | #982 and #2187 | reject |
| one chain pattern versus two `MATCH` clauses | #2129 | reject |
| inline property map versus equivalent `WHERE` | #562, #1000, and #2348 | reject |

## Cross-database dedup boundary

The static relationship guard is not the same transformation as Memgraph
#4458 or the deferred AgensGraph/Neo4j screens. Those compare a typed pattern
with an untyped pattern plus `WHERE type(r)`. The AGE candidate retains `[r:P]`
on both sides and adds a redundant guard, so it has a different trigger and
fix scope.

## Not promoted

The remaining tested transformations either showed no result mismatch, did
not produce a stable material slowdown, or introduced work that a developer
could reasonably consider inherent in the more complex expression. The
OR-versus-IN and point-range candidates failed the two-instance validation.
Other examples include `MAX(DISTINCT x)`, singleton-list construction,
`count(*)` versus `sum(1)`, CASE partitioning, and aggregate arithmetic
identities.

No new logical-result issue survived validation in this run.
