# Deduplication notes
Checked against current Memgraph issues on 2026-07-21.

- #3199 covers an `OR` filter that does not use an index. This candidate has
  one equality predicate wrapped in `NOT NOT`.
- #2583 added property-index support for `IN`. This candidate does not use
  `IN`.
- Searches for `NOT NOT index`, `double negation performance`, `negated
  inequality index`, and `expression prevents index` found no issue with the
  same trigger.

The trigger, query shape, and proposed normalization work differ from #3199
and #2583, so this draft is not a duplicate of those reports.

Links:

- https://github.com/memgraph/memgraph/issues/3199
- https://github.com/memgraph/memgraph/issues/2583
