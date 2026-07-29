# Deduplication decision

Decision: **new issue is justified; no exact upstream or locally submitted
duplicate was found**.

## Existing submitted ArcadeDB issues

| Issue | Existing trigger | Current trigger | Decision |
|---|---|---|---|
| #4405 | Boolean constant simplification | single-label predicate causes optimizer fallback | not duplicate |
| #5116 | shared-variable MATCH pattern order | pattern label versus label predicate | not duplicate |
| #5117 | independent MATCH pattern order | pattern label versus label predicate | not duplicate |

## Closest upstream label issues

- #5226: wrong results in the new `CALL` subquery path when a label name
  collides with an edge type. The current query is top-level, has no collision,
  returns correct rows, and shows a performance regression.
- #5084: wrong `count(n)` on a multi-label pattern. The current query uses one
  label and compares fully materialized ordered rows.
- #5095: missing multi-label enforcement inside `exists()`. The current query
  uses neither `exists()` nor multi-label matching.
- #4087: proposed partition-aware bucket pruning for equality predicates on
  partition keys. It does not cover OpenCypher label-predicate lowering or the
  traditional-execution fallback reproduced here.

## Searches performed on 2026-07-21

- `WHERE n:Label optimizer`
- `label predicate performance`
- `traditional execution label predicate`
- `NodeByLabelScan label predicate`
- `MATCH (n) WHERE n:Label`
- exact fallback text: `Query pattern not yet supported by optimizer`

No issue with the same direct-query trigger and performance symptom was found.

## Submission recommendation

Submit as a new performance issue. Mention #5226 only as a related label-path
issue so maintainers do not confuse a correct-result optimizer gap with that
subquery wrong-result bug.
