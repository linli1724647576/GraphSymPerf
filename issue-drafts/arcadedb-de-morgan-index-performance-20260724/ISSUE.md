# A De Morgan-equivalent WHERE predicate changes an index seek to a full label scan

I found that rewriting an indexed conjunction with De Morgan's law changes an
index lookup into a full label scan. Both queries return the same count, but the
De Morgan form becomes much slower as the number of vertices grows.

ArcadeDB Version: 26.8.1-SNAPSHOT (build `5de4d02b67a6fbbc07cd243866600cad8172129e`)
Operating System: macOS 27.0 arm64
Installation Method: Docker 29.1.2 / `arcadedata/arcadedb:latest` (digest `sha256:d96360569a515d341800783009f653f413a5ee9b7fa0de74954f68e4cc8e7787`)
API/Driver: ArcadeDB HTTP API / OpenCypher for queries / SQL for schema setup

### Steps to reproduce

1. Create a vertex type and a unique index:

   ```sql
   CREATE VERTEX TYPE Bench;
   CREATE PROPERTY Bench.id LONG;
   CREATE INDEX ON Bench (id) UNIQUE;
   ```

2. Create 300,000 vertices:

   ```cypher
   UNWIND range(1, 300000) AS i
   CREATE (:Bench {id: i})
   RETURN count(*) AS c;
   ```

3. Run query A:

   ```cypher
   MATCH (n:Bench)
   WHERE n.id = 1 AND n.id <= 1
   RETURN count(*) AS c;
   ```

4. Run query B:

   ```cypher
   MATCH (n:Bench)
   WHERE NOT (NOT (n.id = 1) OR NOT (n.id <= 1))
   RETURN count(*) AS c;
   ```

5. Compare the plans:

   ```cypher
   EXPLAIN
   MATCH (n:Bench)
   WHERE n.id = 1 AND n.id <= 1
   RETURN count(*) AS c;

   EXPLAIN
   MATCH (n:Bench)
   WHERE NOT (NOT (n.id = 1) OR NOT (n.id <= 1))
   RETURN count(*) AS c;
   ```

### Expected behavior

Both queries should return `c = 1`. Query B is the De Morgan form of query A:
`NOT (NOT P OR NOT Q)` is equivalent to `P AND Q`, including under Cypher's
three-valued Boolean semantics. The equivalent form should retain an indexed
access path.

### Actual behavior

Both queries return `c = 1`, but their plans differ.

Query A uses the unique index:

```text
+ Filter [predicate=(n.id = 1 AND n.id <= 1)]
  + NodeIndexSeek(n:Bench) [index=Bench[id], id=1]
```

Query B scans all `Bench` vertices:

```text
+ Filter [predicate=NOT (NOT n.id = 1 OR NOT n.id <= 1)]
  + NodeByLabelScan(n:Bench) [rows=300000]
```

I reproduced the difference in two fresh databases:

```text
Session 1: query A = 3.70 ms, query B = 350.08 ms
Session 2: query A = 2.96 ms, query B = 304.56 ms
```

The complete results were identical in every run.
