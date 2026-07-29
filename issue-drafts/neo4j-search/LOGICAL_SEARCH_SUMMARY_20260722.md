# Neo4j logical-rule search summary

Date: 2026-07-22

Environment: two independent `neo4j:2026.06.0` Community Docker processes on
an arm64 macOS host, using Cypher 25 and Neo4j Python Driver 5.28.4.

## Scope

The screen contains 56 transformation cases across 28 SYM rule IDs. It checks
complete typed results and includes a deliberately failing self-test to verify
that the feedback loop detects a real mismatch.

The added rule families cover:

- `FILTER`, `FOR`, `LET`, conditional queries, and `NEXT` surface rewrites;
- simple and extended `CASE` expressions;
- path modes, match modes, quantified paths, and fixed/variable path surfaces;
- `COUNT`, `COLLECT`, `EXISTS`, and optional-subquery rewrites;
- concatenation aliases, property projections, entity IDs, `UNION`, and
  `DISTINCT`.

## New logical candidate

| Candidate | Rule | Result | Classification |
|---|---|---|---|
| read-only `UNION` branch swap with `1` and `1.0` | `union_branch_order_symmetry` | later division returns `0` versus `0.5` | new SYM logical issue |

The mismatch reproduced in six alternating executions in each of two fresh
Neo4j processes. Both slotted and pipelined runtimes reproduce it. Three
controls pass: `UNION ALL`, explicit common `FLOAT` conversion, and unequal
numeric inputs. Neither process restarted or was OOM-killed during validation.

Standalone `DISTINCT` and nested list/map forms expose the same underlying
representative-order behavior. They are supporting cases for this issue, not
additional issue counts.

## Other results

The other screened rule families produced equivalent complete results in this
environment. No second independent logical issue was promoted from this pass.

Draft: `../neo4j-union-numeric-type-order-logical-202606/ISSUE.md`

Deduplication record:
`../neo4j-union-numeric-type-order-logical-202606/DEDUP.md`
