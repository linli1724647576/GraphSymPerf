# An exact `*0..0` path scans all nodes and runs `VarLengthExpand`

I found that an exact zero-hop path is much slower than the equivalent node match. The path cannot traverse a relationship, but Neo4j scans the label and runs `VarLengthExpand(All)`.

- Neo4j Version: 2026.06.0 Community
- Operating System: macOS 27.0 arm64
- Installation Method: Docker image `neo4j:2026.06.0`
- Cypher Version: 25


### Steps to reproduce

1. In an empty database, create 100,000 nodes and a property index. Do not create any relationships:

   ```cypher
   UNWIND range(1, 100000) AS i
   CREATE (:N {id: i});

   CREATE RANGE INDEX n_id IF NOT EXISTS
   FOR (n:N) ON (n.id);

   CALL db.awaitIndexes(300);
   ```

1. Verify that the exact zero-hop form never binds different endpoints:

   ```cypher
   CYPHER 25
   MATCH (n:N)-[:R*0..0]->(m:N)
   WHERE n <> m
   RETURN count(*) AS mismatches;
   ```

   The query should return `mismatches = 0`.

2. Run query A:

   ```cypher
   CYPHER 25
   MATCH (n:N)
   RETURN count(*) AS c;
   ```

3. Run query B, which expresses the same rows through an exact zero-hop path:

   ```cypher
   CYPHER 25
   MATCH (n:N)-[:R*0..0]->(m:N)
   RETURN count(*) AS c;
   ```

4. After two warmup executions, run A and B 12 times in alternating order. Both queries return `c = 100000`.

1. Inspect both execution plans:

   ```cypher
   CYPHER 25
   EXPLAIN
   MATCH (n:N)
   RETURN count(*) AS c;

   CYPHER 25
   EXPLAIN
   MATCH (n:N)-[:R*0..0]->(m:N)
   RETURN count(*) AS c;
   ```

### Expected behavior

An exact `*0..0` path binds `m` to the same node as `n` and cannot inspect a relationship. Neo4j should handle this bound as a node match and avoid variable-length expansion work.

### Actual behavior

Two independent Neo4j processes each compared all 100,000 endpoint rows and ran 12 alternating timing pairs. Query B was slower in every pair:

| Process | Query A | Query B |
|---|---:|---:|
| 1 | 5.82 ms | 87.65 ms |
| 2 | 4.48 ms | 67.50 ms |

`EXPLAIN` for query A uses the node count store:

```text
ProduceResults
NodeCountFromCountStore
```

`EXPLAIN` for query B scans the label and runs a variable-length expansion:

```text
ProduceResults
EagerAggregation
Filter
VarLengthExpand(All)
NodeByLabelScan
```
