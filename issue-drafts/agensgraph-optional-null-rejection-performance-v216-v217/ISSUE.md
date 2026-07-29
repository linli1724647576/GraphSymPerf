# `OPTIONAL MATCH` followed by `IS NOT NULL` is about 2x slower than the equivalent `MATCH`

I found that an `OPTIONAL MATCH` followed by a null-rejecting filter is about
twice as slow as the equivalent required `MATCH`. Both queries return the same
20,000 rows, but the optional form first preserves 100,000 unmatched rows and
then removes them.

AgensGraph Versions: 2.16.0 and 2.17.0
Operating System: macOS 27.0 arm64
Installation Method: official Docker images
API/Driver: PostgreSQL wire protocol / Psycopg 3.3.4

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`optional_null_rejection_symmetry`. The rule compares a required pattern with
an optional form whose unmatched rows are removed by `IS NOT NULL`.

### Steps to reproduce

1. Create a fresh graph:

   ```cypher
   CREATE GRAPH optional_null_perf;
   SET graph_path = optional_null_perf;
   ```

2. Create 20,000 matched `RA` nodes, `RB` nodes, and `R` relationships:

   ```cypher
   UNWIND [0,1] AS batch
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS a
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS b
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS c
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS d
   WITH batch * 10000 + a * 1000 + b * 100 + c * 10 + d + 1 AS id
   CREATE (:RA {id: id})-[:R {id: id}]->(:RB {id: id});
   ```

3. Create 100,000 additional `RA` nodes without an outgoing `R` relationship:

   ```cypher
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS batch
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS a
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS b
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS c
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS d
   WITH 20000 + batch * 10000 + a * 1000 + b * 100 + c * 10 + d + 1 AS id
   CREATE (:RA {id: id});
   ```

4. Refresh the label statistics:

   ```sql
   ANALYZE optional_null_perf.ra;
   ANALYZE optional_null_perf.rb;
   ANALYZE optional_null_perf.r;
   ```

5. Run query A:

   ```cypher
   MATCH (n:RA)-[:R]->(m:RB)
   RETURN count(*) AS c;
   ```

6. Run query B:

   ```cypher
   MATCH (n:RA)
   OPTIONAL MATCH (n)-[:R]->(m:RB)
   WITH n, m
   WHERE m IS NOT NULL
   RETURN count(*) AS c;
   ```

7. Run both forms with `EXPLAIN (ANALYZE, BUFFERS)` to inspect their plans.

### Expected behavior

Both queries should return `c = 20000`. Query B removes every row for which
the optional pattern did not bind `m`, so it has the same surviving rows as
query A. It should be possible to plan the null-rejecting optional pattern as
a required match, or otherwise avoid material work for rows that will be
removed immediately.

### Actual behavior

Both queries return `c = 20000`, but query B is consistently slower. Each
AgensGraph 2.16 instance ran 12 alternating comparisons, and query B was
slower in all of them:

| Independent instance | Query A | Query B | Slowdown |
|---|---:|---:|---:|
| AgensGraph 2.16, instance 1 | 10.62 ms | 23.38 ms | 2.20x |
| AgensGraph 2.16, instance 2 | 12.17 ms | 25.94 ms | 2.13x |

The main plan difference is:

```text
Query A: Hash Join
Query B: Hash Left Join
         Rows Removed by Filter: 100000
```

I also reproduced the result and plan difference on AgensGraph 2.17.0. The
official 2.17 Docker image was amd64-only on this arm64 host, so that run was
used only to check the current version, not to compare absolute time with the
native 2.16 runs. Within the 2.17 run, query A took 14.89 ms and query B took
31.96 ms.

Both 2.16 containers remained running without a restart or OOM event during
validation.
