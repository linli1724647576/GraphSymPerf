# `count(1)` scans all matched nodes instead of using the count store

I found that `count(1)` is much slower than `count(*)` for a label match.
Both expressions add one to the count for every matched row, but only
`count(*)` uses the count store.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
Cypher Version: 25

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`count_non_null_literal_symmetry`. The rule replaces `count(*)` with a count
over a non-null literal and compares the result and runtime.

### Steps to reproduce

1. In an empty database, create 1,000,000 labeled nodes. Run this as an
   auto-commit query so that the subquery can commit in batches:

   ```cypher
   UNWIND range(1, 1000000) AS i
   CALL (i) {
     CREATE (:N {id: i})
   } IN TRANSACTIONS OF 5000 ROWS;
   ```

2. Run query A:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   RETURN count(*) AS c;
   ```

3. Run query B, which counts the non-null literal `1` for every matched row:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   RETURN count(1) AS c;
   ```

4. As a control, run the equivalent bound-node form:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   RETURN count(n) AS c;
   ```

5. After two warmup executions, run A and B 12 times in alternating order.
   All three queries return `c = 1000000`.

6. Inspect the plans:

   ```cypher
   CYPHER 25
   EXPLAIN
   MATCH (n:N)
   RETURN count(*) AS c;

   CYPHER 25
   EXPLAIN
   MATCH (n:N)
   RETURN count(1) AS c;
   ```

### Expected behavior

The literal `1` is non-null, so `count(1)` contributes once for every input
row and returns the same value as `count(*)`. Neo4j should be able to use the
same count-store plan. The `count(n)` control already does so.

### Actual behavior

Two independent Neo4j processes each ran 12 alternating comparisons. Query B
was slower in every comparison:

| Process | Query A | Query B |
|---|---:|---:|
| 1 | 11.50 ms | 139.73 ms |
| 2 | 11.86 ms | 98.81 ms |

`PROFILE` for query A uses the count store and performs one database hit:

```text
ProduceResults
NodeCountFromCountStore
```

`PROFILE` for query B scans all 1,000,000 nodes and performs 1,000,001
database hits:

```text
ProduceResults
EagerAggregation
NodeByLabelScan
```

Both Docker containers remained running without a restart or OOM event during
validation.
