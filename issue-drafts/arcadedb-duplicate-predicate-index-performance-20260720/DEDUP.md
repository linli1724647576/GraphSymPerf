# Deduplication decision

Decision: **new issue is justified; related to #4405, but not an exact duplicate**.

## Compared with locally submitted ArcadeDB issues

| Existing issue | Existing trigger/component | Current trigger/component | Decision |
|---|---|---|---|
| #4405 | `AND true` / `OR false` constant simplification; no indexed runtime regression was demonstrated | `P AND P` idempotence is not simplified; unique-index read becomes a label scan with a validated runtime gap | related, distinct missing rewrite |
| #5116 | shared-variable MATCH pattern order | duplicate WHERE equality | not duplicate |
| #5117 | independent MATCH pattern order | duplicate WHERE equality | not duplicate |

## Compared with other upstream issues

- #5107: fixed index lookup for a leading MATCH in write statements on the
  legacy path. The current pair is a pure read on the cost-based optimizer.
- Exact and keyword searches performed on 2026-07-20 included: `duplicate
  predicate index`, `redundant predicate index`, `opencypher duplicate WHERE`,
  `Cypher AND index scan`, `idempotent predicate`, `duplicate equality
  predicate`, and `BooleanSimplifier idempotent`. No matching issue was found.

## Code-scope evidence

The #4405 fix is commit `010db1bc3e60da73bdc3b71144f88ea7fb71a7a6`,
titled “simplify AND-true/OR-false boolean constants in Cypher WHERE clauses”.
Its regression test states that the specific expression
`base AND true OR false AND expr` should reduce to `base`. It does not implement
or test conjunction idempotence.

The #5107 fix is commit `faf899ba11f1f87119e414b6aa1bb4e3a661b5f3`,
which changes the legacy `MatchNodeStep` seed-MATCH index shortcut. The current
plans both say “Using Cost-Based Query Optimizer”, so this is a different path.

## Submission recommendation

Submit as a new issue and mention #4405 and #5107 in “Related issues”. If the
maintainer prefers one umbrella for all BooleanSimplifier identities, it can be
linked back to #4405, but the current evidence should not be discarded: it is a
new rule, current-version reproducer, unique-index access-path change, and
runtime-confirmed impact.
