# Semantically equivalent first-MATCH pattern reorder causes a 25x runtime difference

I found that reordering two comma-separated patterns inside the first `MATCH` clause changes ordinary execution time by more than an order of magnitude, although both queries return the same complete result.

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
   -- relationships: 1358
   ```

3. Run query A:

   ```cypher
   MATCH (n0)-[r0 :T6]->(n1 :L1), (n3 :L5)
WHERE ('uv' CONTAINS '9uvWO')
MATCH (n3), (n3 :L5)
MATCH (n4 :L4)-[r2 :T5]->(n5 :L1)-[r3 :T1]->(n6), (n4)<-[r4 :T0]-(n7 :L4 :L2)-[r5 :T3]->(n8)
WHERE ((((((((n1.k7) OR (r4.k44)) AND ((r2.id) <> (r3.id))) AND ((r2.id) <> (r4.id))) AND ((r2.id) <> (r5.id))) AND ((r3.id) <> (r4.id))) AND ((r3.id) <> (r5.id))) AND ((r4.id) <> (r5.id)))
OPTIONAL MATCH (n4 :L4)<-[]-(n7 :L4)-[]->(n8), (n3)
WITH r3
ORDER BY (r3.k46) DESC
MATCH (n3 :L5), (n3)
WHERE ((n3.k36) < (r3.k47))
OPTIONAL MATCH (n0 :L2)-[]->(n1)<-[]-(n2)
RETURN (r3.k46) AS a0;
   ```

4. Run query B:

   ```cypher
   MATCH (n3 :L5), (n0)-[r0 :T6]->(n1 :L1)
WHERE ('uv' CONTAINS '9uvWO')
MATCH (n3), (n3 :L5)
MATCH (n4 :L4)-[r2 :T5]->(n5 :L1)-[r3 :T1]->(n6), (n4)<-[r4 :T0]-(n7 :L4 :L2)-[r5 :T3]->(n8)
WHERE ((((((((n1.k7) OR (r4.k44)) AND ((r2.id) <> (r3.id))) AND ((r2.id) <> (r4.id))) AND ((r2.id) <> (r5.id))) AND ((r3.id) <> (r4.id))) AND ((r3.id) <> (r5.id))) AND ((r4.id) <> (r5.id)))
OPTIONAL MATCH (n4 :L4)<-[]-(n7 :L4)-[]->(n8), (n3)
WITH r3
ORDER BY (r3.k46) DESC
MATCH (n3 :L5), (n3)
WHERE ((n3.k36) < (r3.k47))
OPTIONAL MATCH (n0 :L2)-[]->(n1)<-[]-(n2)
RETURN (r3.k46) AS a0;
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

The complete typed results were equivalent. The strict validation used two fresh sessions, each with five warmups and 20 balanced paired measurements.

| Metric | Query A | Query B / paired result |
|---|---:|---:|
| Median elapsed time | 33.739 ms | 1.361 ms |
| Median A/B ratio | - | 24.82700x |
| Median A-B delta | - | 32.080 ms |
| Ratio CI lower bound | - | 22.31071x |
| Delta CI lower bound | - | 31.295 ms |
| Direction consistency | - | 1.0000 |

Query A was slower in all 40 validation pairs. The two session ratios were 24.74841x and 24.84403x. A subsequent reducer run could analyze the query after the dependency-parser fix, but none of its 24 safe deletion proposals preserved the runtime symptom. Its final five-pair recheck of the unchanged query measured 20.30631x but only 17.836 ms absolute delta, below the sealed 26.384 ms noise floor; GraphSymPerf therefore marked reduction invalidated and did not promote this case as submit-ready.

### Related report

This has the same surface transformation as #13813. The graph, query, Neo4j
version, and ordinary-execution measurements here are different, so I am
including the relationship explicitly for duplicate/root-cause triage:
https://github.com/neo4j/neo4j/issues/13813
