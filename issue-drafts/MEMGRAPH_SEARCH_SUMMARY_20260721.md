# Memgraph issue search summary

Target: Memgraph 3.10.1
Date: 2026-07-21

## New issue drafts

| Rule family | Finding | Draft |
|---|---|---|
| Boolean predicate normalization | `NOT NOT (n.key = 42)` stops using `:Item(key)` and becomes much slower | `memgraph-indexed-double-negation-performance-20260721/` |
| Relationship type placement | `WHERE type(r) = 'LINK'` expands unrelated relationship types and becomes much slower | `memgraph-relationship-type-where-performance-20260721/` |
| Static property access | `n['key'] = 42` scans all nodes while `n.key = 42` uses `:Item(key)` | `memgraph-static-subscript-property-index-performance-20260721/` |
| Zero-hop path | `*0..0` retains `ExpandVariable` and takes about twice as long as the equivalent node match | `memgraph-zero-hop-path-performance-20260721/` |

Both drafts passed two fresh-process runs. Each validator rebuilt the graph,
compared complete result rows, alternated query order, and included an A/A
control.

## Rejected candidates

- `key = 42 OR key = 43` versus `key IN [42, 43]` produced a large
  difference, but Memgraph #3199 already reports that an `OR` filter does not
  use an index. No issue draft was created.
- Fixed-path endpoint reversal reached about 2x on one 50,000-node screen, but
  the difference was about 2 ms and both plans started from the same endpoint.
  The candidate did not pass the performance gate.
- The original under-covered rules, including De Morgan, property-map/WHERE,
  label/WHERE, one-hop fixed/variable-length, MATCH clause order, node order,
  and bound `count(*)`/ `count(n)`, produced equal results without a stable
  large slowdown.
- Duplicating a constant in an indexed numeric `IN` list duplicated result
  rows, but open Memgraph #4123 already covers duplicated rows from repeated
  constants in an `IN` predicate. The numeric indexed reproducer is supporting
  evidence for that issue, not a new issue.
- `min(DISTINCT x)` and `max(DISTINCT x)` were slower than `min(x)` and
  `max(x)` on high-cardinality data. The stable 500,000-node difference was
  about 1.5 to 1.6 times, and #4204 already tracks redundant de-duplication in
  the new planner. The evidence was retained, but no issue draft was created.

## Deduplication boundary

The double-negation draft is separate from:

- https://github.com/memgraph/memgraph/issues/3199
- https://github.com/memgraph/memgraph/issues/2583

The relationship-type draft is separate from:

- https://github.com/memgraph/memgraph/issues/2164
- https://github.com/memgraph/memgraph/issues/2177
- https://github.com/memgraph/memgraph/issues/662

The static-property-access draft is separate from:

- https://github.com/memgraph/memgraph/issues/2583
- https://github.com/memgraph/memgraph/issues/2665
- https://github.com/memgraph/memgraph/issues/3199
- https://github.com/memgraph/memgraph/issues/4208

The zero-hop draft is separate from:

- https://github.com/memgraph/memgraph/issues/2706
- https://github.com/memgraph/memgraph/issues/2707
- https://github.com/memgraph/memgraph/issues/2732

The draft directories contain the full issue text, standalone reproducer,
query files, validation summary, and deduplication notes. The raw validation
records remain under `issue-drafts/memgraph-search/results/`.
