# `ORDER BY` before `LIMIT` and `count(*)` keeps an unnecessary `Top`

I found that adding `ORDER BY` before a limited row count makes the query much
slower. The order changes which rows pass through `LIMIT`, but it cannot change
how many rows are counted.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
Cypher Version: 25

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`order_before_limited_count_symmetry`. The rule adds or removes an order that
cannot affect the final row count.

### Steps to reproduce

1. In an empty database, create 500,000 labeled nodes. Run this as an
   auto-commit query so that the subquery can commit in batches:

   ```cypher
   UNWIND range(1, 500000) AS i
   CALL (i) {
     CREATE (:N {id: i})
   } IN TRANSACTIONS OF 5000 ROWS;
   ```

2. Run query A:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   WITH n
   LIMIT 250000
   RETURN count(*) AS value;
   ```

3. Run query B, which only adds `ORDER BY n.id`:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   WITH n
   ORDER BY n.id
   LIMIT 250000
   RETURN count(*) AS value;
   ```

4. Run both queries several times after warm-up. Use `PROFILE` separately to
   compare their plans.

### Expected behavior

Both queries should return `value = 250000`. Ordering can change which 250,000
rows survive, but not the number of rows consumed by `count(*)`. The optimizer
should be able to avoid the unused ordering work.

### Actual behavior

Both queries return `value = 250000`, but query B is much slower. I repeated
the test in two fresh Neo4j processes:

| Fresh process | Query A | Query B |
|---|---:|---:|
| 1 | 25.45 ms | 261.71 ms |
| 2 | 14.09 ms | 175.65 ms |

Each process used five warm-up runs followed by 20 runs of each query in mixed
order. The table reports the median time from those runs.

`PROFILE` shows that query B reads all 500,000 nodes and keeps a `Top`
operator:

```text
ProduceResults
EagerAggregation
Top
Projection
NodeByLabelScan
```

Query A stops after 250,000 rows and has no sorting operator:

```text
ProduceResults
EagerAggregation
Limit
NodeByLabelScan
```
