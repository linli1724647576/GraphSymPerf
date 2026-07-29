# `sum()` over the same INTEGER rows changes when only their `ORDER BY` direction changes

I found that `sum()` returns different values for the same three `INTEGER`
rows when only their input order is changed. One order returns the exact sum
`1`, while the other returns `0.0`.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
API/Driver: Cypher 5 and Cypher 25 / Neo4j Python Driver 5.28.4

### Discovery provenance

GraphSymPerf found this with the SYM rule `aggregate_order_symmetry`: an exact
aggregate over the same complete integer multiset must be invariant under input
permutation. Approximate floating-point and `NaN` cases were excluded from the
logical-candidate count. The framework compared complete typed results and
replayed the mismatch six times in alternating A/B order before this standalone
reproducer was written.

### Steps to reproduce

1. Sum the rows in ascending order:

   ```cypher
   UNWIND [9223372036854775807, 1, -9223372036854775807] AS x
   WITH x ORDER BY x ASC
   RETURN sum(x) AS sum_value, valueType(sum(x)) AS sum_type;
   ```

2. Run the same query with only `ASC` changed to `DESC`:

   ```cypher
   UNWIND [9223372036854775807, 1, -9223372036854775807] AS x
   WITH x ORDER BY x DESC
   RETURN sum(x) AS sum_value, valueType(sum(x)) AS sum_type;
   ```

The attached `reproducer.py` uses a parameterized list and runs both orders six
times under Cypher 5 and Cypher 25.

### Expected behavior

Both queries should return `sum_value = 1`. They aggregate the same three
`INTEGER` values, and the exact final sum is within the signed 64-bit range.
Changing only the row order should not change the sum.

### Actual behavior

Ascending order returns the exact integer result:

```text
sum_value, sum_type
1, "INTEGER NOT NULL"
```

Descending order returns a different floating-point result:

```text
sum_value, sum_type
0.0, "FLOAT NOT NULL"
```

The attached reproducer produced the same result in two fresh Docker processes
under both Cypher 5 and Cypher 25. A smaller control using `[7, 1, -7]`
returned the integer sum `1` in both orders.

The type change suggests that the descending order encounters an intermediate
integer overflow and switches to floating-point accumulation, losing the final
unit. This is only an observation from the returned values; the report does
not rely on that implementation hypothesis.

### Related overflow reports

This may share an overflow-policy root cause with neo4j/neo4j#5889 and the
`Long.MIN_VALUE` reports neo4j/neo4j#12726 and neo4j/neo4j#13857. The observable
contract here is narrower and aggregation-specific: the same exact `INTEGER`
multiset produces different values and result types solely because its row
order changes.
