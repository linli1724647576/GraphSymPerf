# Conditional `count` does not use an indexable predicate

I found that counting a conditional expression is much slower than moving the
same condition into `WHERE`. Both queries count the same node, but the
conditional form scans every node instead of using the available index.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
Cypher Version: 25

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`conditional_count_filter_symmetry`. The rule moves a nullable condition from
the argument of `count` into `WHERE`.

### Steps to reproduce

1. In an empty database, create 500,000 labeled nodes and an index. Run the
   first statement as an auto-commit query:

   ```cypher
   UNWIND range(1, 500000) AS i
   CALL (i) {
     CREATE (:N {id: i})
   } IN TRANSACTIONS OF 5000 ROWS;

   CREATE RANGE INDEX n_id IF NOT EXISTS FOR (n:N) ON (n.id);
   CALL db.awaitIndexes(300);
   ```

2. Run query A:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   WHERE n.id = 250000
   RETURN count(*) AS value;
   ```

3. Run query B, which expresses the same filtered count inside the aggregate:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   RETURN count(CASE WHEN n.id = 250000 THEN 1 END) AS value;
   ```

4. Run both queries several times after warm-up. Use `PROFILE` separately to
   compare their plans.

### Expected behavior

Both queries should return `value = 1`. `count` ignores null values, so the
`CASE` expression contributes one only for nodes satisfying the same indexed
predicate. The optimizer could move this condition before the aggregate and
use the index.

### Actual behavior

Both queries return `value = 1`, but query B is much slower. I repeated the
test in two fresh Neo4j processes:

| Fresh process | Query A | Query B |
|---|---:|---:|
| 1 | 5.15 ms | 141.94 ms |
| 2 | 3.88 ms | 96.43 ms |

Each process used five warm-up runs followed by 20 runs of each query in mixed
order. The table reports the median time from those runs.

`PROFILE` shows that query A uses `NodeIndexSeek` and performs two database
hits. Query B uses `NodeByLabelScan` and performs 1,000,001 database hits.

```text
Query A: NodeIndexSeek
Query B: NodeByLabelScan -> EagerAggregation
```
