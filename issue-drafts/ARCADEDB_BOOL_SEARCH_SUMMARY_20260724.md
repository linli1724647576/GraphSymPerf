# ArcadeDB BOOL rule-family search summary

## Promoted drafts

| Rule | Type | Current-build result | Draft |
|---|---|---|---|
| `de_morgan_predicate` | Performance | Equivalent result; `NodeIndexSeek` becomes `NodeByLabelScan`; validated in two fresh databases | `arcadedb-de-morgan-index-performance-20260724/` |
| `boolean_property_truth` | Performance | Equivalent result; Boolean-property index is not used by the direct predicate; validated in two fresh databases | `arcadedb-boolean-property-index-performance-20260724/` |
| `case_true_selection` | Performance | Equivalent result; `CASE` form changes the indexed lookup to a label scan; validated in two fresh databases | `arcadedb-case-true-index-performance-20260724/` |

All three were reproduced on ArcadeDB 26.8.1-SNAPSHOT build
`5de4d02b67a6fbbc07cd243866600cad8172129e`, pulled on 2026-07-24 as Docker
digest
`sha256:d96360569a515d341800783009f653f413a5ee9b7fa0de74954f68e4cc8e7787`.

## Validated but held

`negated_inequality` also passed two fresh-database runtime validations:

```cypher
MATCH (n:Bench)
WHERE n.id = 1
RETURN count(*) AS c;

MATCH (n:Bench)
WHERE NOT (n.id <> 1)
RETURN count(*) AS c;
```

The transformed query changed `NodeIndexSeek` to `NodeByLabelScan`. It is held
instead of being included in the first three drafts because its likely
normalization path overlaps the selected De Morgan candidate. It remains a
useful follow-up if the maintainer asks for smaller `NOT` examples.

## Screened without promotion

- `null_predicate_negation`: equivalent results, no significant slowdown.
- `numeric_equality_range`: equivalent results, both forms retained fast index
  access.
- `singleton_in`: equivalent results, both forms retained fast index access.
- `and_over_or_distribution`: equivalent results, no large runtime difference.
- `predicate_equal_true`: strong screening result, held because it overlaps the
  direct Boolean-property normalization theme.
- `coalesce_false_selection`: strong screening result, held because it is a
  generic expression wrapper and less direct than the selected three.

No logical mismatch survived the truth-table and graph-result checks in this
round.
