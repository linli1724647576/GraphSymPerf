# Integer division silently wraps for `Long.MIN_VALUE / -1` instead of reporting overflow

I found that dividing the minimum 64-bit integer by `-1` returns the same
negative value. The mathematical result is one greater than Neo4j's maximum
`INTEGER`, so it cannot be represented.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
API/Driver: Cypher 5 and Cypher 25 / Neo4j Python Driver 5.28.4

### Discovery provenance

GraphSymPerf found this with the scalar SYM rule `integer_division_negation`:
for an integer `x`, `x / -1` and `-x` must have the same value or the same
overflow outcome. The framework compared complete typed outcomes under Cypher
5 and Cypher 25 and replayed the mismatch six times in alternating A/B order
before this standalone reproducer was written.

### Steps to reproduce

Run:

```cypher
RETURN -9223372036854775808 / -1 AS value;
```

The attached `reproducer.py` also runs the parameterized form six times under
both Cypher 5 and Cypher 25.

### Expected behavior

The query should report an integer overflow because:

```text
-9223372036854775808 / -1 = 9223372036854775808
```

The result is outside the signed 64-bit `INTEGER` range. Neo4j already reports
`Neo.ClientError.Statement.ArithmeticError` for the corresponding overflowing
negation, addition, and multiplication operations.

### Actual behavior

Neo4j returns the wrapped value:

```text
value
-9223372036854775808
```

The attached reproducer produced the same result in two fresh Docker
processes. In each process, the parameterized query returned the wrapped value
in all six alternating runs for both Cypher 5 and Cypher 25. Ordinary integer
division such as `42 / -1` returned `-42`. The negation, addition, and
multiplication overflow controls raised `ArithmeticError`.

### Related reports and deduplication status

This is a deterministic affected operation, but it should currently be treated
as related evidence rather than a new issue family. neo4j/neo4j#13857 reports
the same `Long.MIN_VALUE` boundary through `abs()`, neo4j/neo4j#12726 covers the
corresponding negation boundary, and neo4j/neo4j#5889 established Neo4j's
general checked-arithmetic behavior. Submit separately only if maintainers want
missing overflow checks tracked per operator; otherwise add it to #13857.
