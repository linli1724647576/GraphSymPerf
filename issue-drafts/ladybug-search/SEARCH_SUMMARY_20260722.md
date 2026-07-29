# LadybugDB 0.18.3 additional-rule search summary

## Promoted performance candidates

| Rule | Classification | Result | Independent validations | Draft |
|---|---|---|---|---|
| `primary_key_distinct_symmetry` | SYM | 500000 = 500000 | 0.46 vs 19.27 ms; 0.40 vs 18.57 ms | `ladybug-primary-key-distinct-performance-v0183` |
| `order_before_limited_count_symmetry` | SYM | 100000 = 100000 | 0.38 vs 136.40 ms; 0.39 vs 136.55 ms | `ladybug-order-before-limited-count-performance-v0183` |

Both candidates passed the result-equivalence, repeated-timing, direction, and
A/A plus B/B control checks in two fresh Python processes using independent
database files.

## Not promoted

- `optional_false_pattern_symmetry` was correct and consistently slower, but
  its absolute difference stayed below the 10 ms promotion threshold. At
  200,000 nodes with a nonempty relationship table it measured 0.58 vs 6.03
  ms; at 500,000 nodes with an empty relationship table it measured 0.23 vs
  4.25 ms.
- Fixed-path node-count and endpoint guards produced larger slowdowns, but they
  share the same path-invariant fix scope as the existing Apache AGE path
  draft. They were not counted as new LadybugDB rule families.
- LadybugDB issues #696 and #697 still reproduce on 0.18.3, but they are the
  previously submitted pattern-binding issues and were not filed again.
- No new logical result mismatch passed minimization and deduplication in this
  search batch.

## Evidence files

- `validation-search-v0183-n500000-r0-20260722.json`
- `validation-verify-v0183-n500000-r0-20260722.json`
- `validation-order-count-search-v0183-n500000-20260722.json`
- `validation-order-count-verify-v0183-n500000-20260722.json`
- `rule-screen-v0183-n100000-20260722.jsonl`
