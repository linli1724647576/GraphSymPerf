# Neo4j significant rule-family search summary

Date: 2026-07-22

Tested version: Neo4j 2026.06.0 Community, Cypher 25, Docker arm64

## Result

The expanded search found four reproducible performance candidates from four
rule families. All four query pairs returned the same result in two fresh
Neo4j processes. No new logical mismatch survived validation.

| Priority | Rule family | Result | Process 1 | Process 2 | Draft |
|---|---|---:|---:|---:|---|
| High | `order_before_limited_count_symmetry` | 250,000 | 25.45 vs 261.71 ms | 14.09 vs 175.65 ms | `neo4j-order-before-limited-count-performance-202606` |
| High | `element_id_distinct_symmetry` | 500,000 | 67.71 vs 231.26 ms | 71.97 vs 196.56 ms | `neo4j-element-id-distinct-performance-202606` |
| High | `union_all_count_pushdown_symmetry` | 1,000,000 | 76.71 vs 4.81 ms | 70.40 vs 4.58 ms | `neo4j-union-all-count-pushdown-performance-202606` |
| Medium | `conditional_count_filter_symmetry` | 1 | 5.15 vs 141.94 ms | 3.88 vs 96.43 ms | `neo4j-conditional-count-index-performance-202606` |

The table reports the median time from 20 mixed-order runs after five warm-up
runs. The two columns come from independent databases in separate Neo4j
processes.

## Submission order

1. `UNION ALL` count pushdown. It has the largest stable improvement among the
   new high-priority families and a clear plan difference: two label scans
   become two count-store reads.
2. `ORDER BY` before limited count. It keeps a `Top` whose order is not observed
   by the final cardinality-only aggregate.
3. `DISTINCT elementId(n)`. It keeps duplicate removal even though Neo4j
   documents element IDs as unique within a transaction.
4. Conditional count predicate extraction. The effect is large, but the
   expected optimizer rewrite is broader than the other three, so this is the
   lower-priority submission.

## Deduplication boundary

Targeted upstream searches found no report with any of these four exact
triggers. They also do not duplicate this project's open Neo4j reports:

- [#13864](https://github.com/neo4j/neo4j/issues/13864) is about recognizing
  `count(1)` as a count-store-compatible aggregate.
- [#13865](https://github.com/neo4j/neo4j/issues/13865) is about an exact
  zero-hop variable-length path.

The new `UNION ALL` report is in the broad count-optimization area of #13864,
but it needs aggregate pushdown across union branches. The other three reports
use unrelated planner rules.

## Screened but not promoted

- Relationship-type placement showed a slowdown, but it is close to the
  previously submitted relationship-estimation family and repeats a mutator
  already seen in another database.
- A statically false `OPTIONAL MATCH` also slowed down, but it repeats an older
  Boolean family and is less likely to be accepted as a practical query shape.
- `min(DISTINCT x)` and `max(DISTINCT x)` did not show a stable large effect.
- `COUNT { pattern } > 0` and `EXISTS { pattern }` already received comparable
  plans in the tested form.
- Empty `UNION` branches, repeated bound-node patterns, correlated identity
  subqueries, and unique-property `DISTINCT` did not meet the promotion bar.

## Evidence

- `significant-validation-search-v202606-n500000-20260722.json`
- `significant-validation-verify-v202606-n500000-20260722.json`
- `new-rule-screen-v202606-n100000-r250000-20260722.jsonl`
- `expanded-rule-screen-v202606-n100000-r250000-20260722.jsonl`
- `expanded-rule-screen-v2-v202606-n100000-r250000-20260722.jsonl`

The public drafts contain direct Cypher statements and do not require a
reproduction script.
