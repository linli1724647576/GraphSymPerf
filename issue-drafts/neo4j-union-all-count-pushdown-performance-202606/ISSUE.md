# Counting `UNION ALL` rows scans both branches instead of pushing counts down

I found that counting the rows produced by two `UNION ALL` branches is much
slower than counting each branch first and adding the two counts. Both queries
return the same value, but the first form materializes one million rows.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
Cypher Version: 25

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`union_all_count_pushdown_symmetry`. The rule moves a count through
`UNION ALL` and replaces the outer count with the sum of the branch counts.

### Steps to reproduce

1. In an empty database, create 500,000 labeled nodes. Run this as an
   auto-commit query:

   ```cypher
   UNWIND range(1, 500000) AS i
   CALL (i) {
     CREATE (:N {id: i})
   } IN TRANSACTIONS OF 5000 ROWS;
   ```

2. Run query A:

   ```cypher
   CYPHER 25
   CALL () {
     MATCH (n:N)
     RETURN n AS item
     UNION ALL
     MATCH (m:N)
     RETURN m AS item
   }
   RETURN count(item) AS value;
   ```

3. Run query B, which counts each branch before `UNION ALL`:

   ```cypher
   CYPHER 25
   CALL () {
     MATCH (n:N)
     RETURN count(n) AS subtotal
     UNION ALL
     MATCH (m:N)
     RETURN count(m) AS subtotal
   }
   RETURN sum(subtotal) AS value;
   ```

4. Run both queries several times after warm-up. Use `PROFILE` separately to
   compare their plans.

### Expected behavior

Both queries should return `value = 1000000`. For `UNION ALL`, counting all
non-null branch rows is equivalent to adding the branch counts. The optimizer
should be able to push this count into the branches.

### Actual behavior

Both queries return `value = 1000000`, but query A is much slower. I repeated
the test in two fresh Neo4j processes:

| Fresh process | Query A | Query B |
|---|---:|---:|
| 1 | 76.71 ms | 4.81 ms |
| 2 | 70.40 ms | 4.58 ms |

Each process used five warm-up runs followed by 20 runs of each query in mixed
order. The table reports the median time from those runs.

`PROFILE` shows that query A uses two `NodeByLabelScan` operators and sends
1,000,000 rows through `Union` before counting them:

```text
ProduceResults
EagerAggregation
Union
NodeByLabelScan
NodeByLabelScan
```

Query B reads one count from each branch:

```text
ProduceResults
EagerAggregation
Union
NodeCountFromCountStore
NodeCountFromCountStore
```
