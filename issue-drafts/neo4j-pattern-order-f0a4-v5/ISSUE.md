# Semantically equivalent MATCH pattern reorder causes a repeatable multi-second runtime difference

I observed that reordering two independent node patterns inside the first `MATCH` clause changes ordinary execution time by several seconds, although both queries return the same complete result.

Neo4j Version: 2026.05.0 community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.05.0`
API/Driver: Bolt via Neo4j Python Driver 5.28.4

### Steps to reproduce

1. Load the attached graph setup file into an empty disposable database:

   ```bash
   cypher-shell -u neo4j -p <password> -d neo4j -f setup.cypher
   ```

2. Verify the graph size:

   ```cypher
   MATCH (n) RETURN count(n) AS nodes;
   -- nodes: 1280

   MATCH ()-[r]->() RETURN count(r) AS relationships;
   -- relationships: 1515
   ```

3. Run query A:

   ```cypher
   MATCH (n1 :L3), (n4 :L3), (n0 :L3)<-[r4 :T0]-(n5 :L1)
WHERE ((r4.id) > -1)
UNWIND [(n4.k22)] AS a0
OPTIONAL MATCH (n3)-[]->(n0)<-[]-(n5)
OPTIONAL MATCH (n0 :L3)<-[]-(n1 :L3)<-[]-(n2 :L3), (n6 :L2 :L0)
WHERE ((n4.k18) CONTAINS (n4.k18))
OPTIONAL MATCH (n7 :L4 :L2)-[r5 :T5]->(n1 :L3), (n6 :L2 :L0)
WHERE ((n2.k19) AND (r5.k63))
OPTIONAL MATCH (n6)
WITH n0, (n6.k12) AS a1, max('2Fc') AS a2, r4, a0
WHERE ((n0.k17) AND (n0.k17))
RETURN (r4.k34) AS a3;
   ```

4. Run query B:

   ```cypher
   MATCH (n4 :L3), (n1 :L3), (n0 :L3)<-[r4 :T0]-(n5 :L1)
WHERE ((r4.id) > -1)
UNWIND [(n4.k22)] AS a0
OPTIONAL MATCH (n3)-[]->(n0)<-[]-(n5)
OPTIONAL MATCH (n0 :L3)<-[]-(n1 :L3)<-[]-(n2 :L3), (n6 :L2 :L0)
WHERE ((n4.k18) CONTAINS (n4.k18))
OPTIONAL MATCH (n7 :L4 :L2)-[r5 :T5]->(n1 :L3), (n6 :L2 :L0)
WHERE ((n2.k19) AND (r5.k63))
OPTIONAL MATCH (n6)
WITH n0, (n6.k12) AS a1, max('2Fc') AS a2, r4, a0
WHERE ((n0.k17) AND (n0.k17))
RETURN (r4.k34) AS a3;
   ```

5. Compare ordinary execution time using balanced `AB`/`BA` order. Do not use
   `PROFILE` for the timing samples because profiling instrumentation changes
   the measured execution path.

### Expected behavior

The two queries only reorder comma-separated pattern atoms in the first
ordinary `MATCH` clause. The `WHERE` clause remains attached to the same
`MATCH`, and the reordered atoms introduce the same variables and graph
constraints. The queries should therefore return equivalent results without a
large and systematic runtime difference.

### Actual behavior

The complete typed results were equivalent. An isolated verification instance first executed each query for complete-result equivalence, then collected 12 balanced paired measurements.

| Metric | Query A | Query B / paired result |
|---|---:|---:|
| Median elapsed time | 44609.619 ms | 35798.553 ms |
| Median A/B ratio | - | 1.18768x |
| Median A-B delta | - | 5568.911 ms |
| Ratio CI lower bound | - | 1.12455x |
| Delta CI lower bound | - | 3814.637 ms |
| Direction consistency | - | 0.9167 |

Query A was slower in 11 of 12 pairs. The median absolute gap was large and its bootstrap confidence interval remained positive; the relative effect was 1.18768x.

### Related report

This has the same surface transformation as #13813. The graph, query, Neo4j
version, and ordinary-execution measurements here are different, so I am
including the relationship explicitly for duplicate/root-cause triage:
https://github.com/neo4j/neo4j/issues/13813
