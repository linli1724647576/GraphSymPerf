# A constant singleton `UNWIND` makes an equivalent aggregation about 1.8x slower

I found that adding `UNWIND [1] AS x` to an aggregation makes the query about
1.8 times slower, even though the list contains exactly one value and `x` is
not used.

AgensGraph Versions: 2.16.0 and 2.17.0
Operating System: macOS 27.0 arm64
Installation Method: official Docker images
API/Driver: PostgreSQL wire protocol / Psycopg 3.3.4

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`singleton_unwind_symmetry`. The rule inserts a constant one-element
`UNWIND` and checks that the result stays the same.

### Steps to reproduce

1. Create a fresh graph:

   ```cypher
   CREATE GRAPH singleton_unwind_perf;
   SET graph_path = singleton_unwind_perf;
   ```

2. Create 100,000 nodes:

   ```cypher
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS a
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS b
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS c
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS d
   UNWIND [0,1,2,3,4,5,6,7,8,9] AS e
   WITH a * 10000 + b * 1000 + c * 100 + d * 10 + e + 1 AS id
   CREATE (:N {id: id, grp: id % 97});
   ```

3. Refresh the label statistics:

   ```sql
   ANALYZE singleton_unwind_perf.n;
   ```

4. Run query A:

   ```cypher
   MATCH (n:N)
   RETURN sum(n.grp) AS value;
   ```

5. Run query B, which only adds a constant singleton `UNWIND`:

   ```cypher
   MATCH (n:N)
   UNWIND [1] AS x
   RETURN sum(n.grp) AS value;
   ```

6. Run both forms with `EXPLAIN (ANALYZE, BUFFERS)` to inspect their plans.

### Expected behavior

Both queries should return `4799775`. `UNWIND [1]` emits exactly one row for
each input row, and `x` is unused, so it does not change the input of
`sum(n.grp)`. A constant singleton expansion should be removable or have only
small overhead.

### Actual behavior

Both queries return `4799775`, but query B is consistently slower. Each
AgensGraph 2.16 instance ran 12 alternating comparisons, and query B was
slower in all of them:

| Independent instance | Query A | Query B | Slowdown |
|---|---:|---:|---:|
| AgensGraph 2.16, instance 1 | 33.59 ms | 59.98 ms | 1.79x |
| AgensGraph 2.16, instance 2 | 36.58 ms | 64.38 ms | 1.76x |

Query A uses a sequential scan followed by an aggregate. Query B adds a
`ProjectSet` and a parallel `Gather` even though the constant list produces
one row per input row:

```text
Query A: Aggregate -> Seq Scan
Query B: Aggregate -> Gather -> ProjectSet -> Parallel Seq Scan
```

I also reproduced the same result and plan difference on AgensGraph 2.17.0.
The official 2.17 Docker image was amd64-only on this arm64 host, so that run
was used only to check the current version, not to compare absolute time with
the native 2.16 runs. Within the 2.17 run, query A took 64.28 ms and query B
took 138.14 ms.

Both 2.16 containers remained running without a restart or OOM event during
validation.
