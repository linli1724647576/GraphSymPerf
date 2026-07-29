# Neo4j 2026.06 rule-derived candidate summary

Date: 2026-07-19

Environment: two independent `neo4j:2026.06.0` Community Docker processes,
Neo4j Python Driver 5.28.4, macOS arm64 host. Each standalone reproducer used
six alternating executions per process. Neither container restarted nor was
OOM-killed during verification.

## Candidate ledger

| Candidate | Discovery rule | Result | Novelty / strength | Recommended handling |
|---|---|---|---|---|
| `coll.insert()` rejected by list consumers | `list_insert_composition` (SYM) | deterministic error-vs-value mismatch | independent, strong | submit as a new issue |
| integer `sum()` changes with ASC/DESC input | `aggregate_order_symmetry` (SYM) | `1 INTEGER` vs `0.0 FLOAT` | independent symptom, strong; overflow root cause may overlap older arithmetic work | submit, with related-overflow caveat |
| empty-regex replacement corrupts a supplementary character | `regex_zero_width_unicode` (SYM) | `-?-?-` vs `-🙂-` | no matching Neo4j issue found; medium because Java exposes the same UTF-16 behavior | ask maintainer to confirm the Unicode contract, then submit |
| `Long.MIN_VALUE / -1` wraps | `integer_division_negation` (SYM) | value vs `ArithmeticError` | deterministic but related to #13857/#12726/#5889 | attach to #13857 unless maintainers request a separate operator issue |

## Framework-level screening result

- Experimental rule screen: 122 cases, 3 eligible deterministic logical
  candidates. Eight approximate floating-point or `NaN` order differences were
  explicitly classified as ineligible rather than counted as bugs.
- Scalar boundary screen: 64 cases, 1 eligible deterministic logical candidate.
  Five temporal observations were classified as ineligible because the bounded
  harness does not yet fully canonicalize Neo4j temporal and duration values.
- All four candidate manifestations were then reproduced with focused,
  red-capable scripts against two independent database processes.

This supports a bounded effectiveness claim: the rule engine produced four
repeatable anomaly manifestations across type composition, exact aggregation,
Unicode handling, and checked arithmetic. It does **not** support claiming four
independent new issue families; the defensible count is two strong independent
candidates, one medium contract candidate, and one related-family path.

## Drafts

- `../neo4j-coll-insert-static-type-202606/ISSUE.md`
- `../neo4j-integer-aggregate-order-202606/ISSUE.md`
- `../neo4j-regex-replace-zero-width-unicode-202606/ISSUE.md`
- `../neo4j-integer-division-overflow-202606/ISSUE.md`

## Upstream deduplication checks

GitHub issue searches on 2026-07-19 found no matching report for
`coll.insert`, integer `sum` plus `ORDER BY`, or `string.regexReplace` plus
Unicode/zero-width terms. The division candidate is related to:

- neo4j/neo4j#13857, `abs()` on `Long.MIN_VALUE` (open, Neo4j 2026.06.0)
- neo4j/neo4j#12726, negation at the same boundary (closed)
- neo4j/neo4j#5889, checked arithmetic overflow policy (merged)

## Additional rule families screened on 2026-07-21

The follow-up screen added path-bound, aggregate-surface, relationship-type,
label-expression, GQL surface, branch-composition, and pattern-predicate
transformations.

| Candidate | Discovery rule | Result | Handling |
|---|---|---|---|
| exact zero-hop path | `zero_hop_path_symmetry` (SYM) | same endpoint rows; stable scan and expansion overhead in two processes | drafted as a new issue |
| `count(*)` to `count(1)` | `count_non_null_literal_symmetry` (SYM) | same count; stable label-scan overhead in two processes | drafted as a new issue, with count-store related-issue note |
| fixed hop to exact `*1..1` | `fixed_varlength_path_symmetry` (SYM) | slowdown did not remain large enough across scales and processes | rejected |
| static relationship type to `type(r)` filter | `relationship_type_surface_symmetry` (SYM) | strong screen result, but overlaps a previously studied cross-DB mutator and needs a fresh-store validation | deferred |
| GQL surface and branch-composition rules | several SYM rules | no repeatable logical mismatch or large performance regression | rejected |
