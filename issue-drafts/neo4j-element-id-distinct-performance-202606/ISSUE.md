# `DISTINCT elementId(n)` adds unnecessary duplicate removal

I found that adding `DISTINCT` to `elementId(n)` makes the query much slower.
Neo4j documents that element IDs are unique among nodes and relationships in a
transaction, so a node scan cannot produce duplicate node element IDs.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
Cypher Version: 25

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`element_id_distinct_symmetry`. The rule adds or removes `DISTINCT` from a
projection whose values are already unique.

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
   MATCH (n:N)
   WITH elementId(n) AS id
   RETURN count(id) AS value;
   ```

3. Run query B, which only adds `DISTINCT`:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   WITH DISTINCT elementId(n) AS id
   RETURN count(id) AS value;
   ```

4. Run both queries several times after warm-up. Use `PROFILE` separately to
   compare their plans.

### Expected behavior

Both queries should return `value = 500000`. Because `elementId(n)` is unique
for every scanned node in the transaction, `DISTINCT` cannot remove any row.
The optimizer should be able to avoid duplicate removal in this case.

### Actual behavior

Both queries return `value = 500000`, but query B is slower. I repeated the
test in two fresh Neo4j processes:

| Fresh process | Query A | Query B |
|---|---:|---:|
| 1 | 67.71 ms | 231.26 ms |
| 2 | 71.97 ms | 196.56 ms |

Each process used five warm-up runs followed by 20 runs of each query in mixed
order. The table reports the median time from those runs.

`PROFILE` shows that query B keeps a `Distinct` operator for all 500,000 rows:

```text
ProduceResults
EagerAggregation
Distinct
NodeByLabelScan
```

Query A has no duplicate-removal operator:

```text
ProduceResults
EagerAggregation
Projection
NodeByLabelScan
```

Neo4j documents the uniqueness guarantee for `elementId()` here:
https://neo4j.com/docs/cypher-manual/current/functions/scalar/#functions-elementid
