# A direct Boolean property predicate does not use its index in OpenCypher

I found that `WHERE n.flag` scans every vertex even when `flag` has an index.
Writing the equivalent predicate as `WHERE n.flag = true` uses the index. Both
queries return the same count, but the direct Boolean form becomes much slower
as the number of vertices grows.

ArcadeDB Version: 26.8.1-SNAPSHOT (build `5de4d02b67a6fbbc07cd243866600cad8172129e`)
Operating System: macOS 27.0 arm64
Installation Method: Docker 29.1.2 / `arcadedata/arcadedb:latest` (digest `sha256:d96360569a515d341800783009f653f413a5ee9b7fa0de74954f68e4cc8e7787`)
API/Driver: ArcadeDB HTTP API / OpenCypher for queries / SQL for schema setup

### Steps to reproduce

1. Create a vertex type and an index on a Boolean property:

   ```sql
   CREATE VERTEX TYPE Bench;
   CREATE PROPERTY Bench.id LONG;
   CREATE PROPERTY Bench.flag BOOLEAN;
   CREATE INDEX ON Bench (flag) NOTUNIQUE;
   ```

2. Create 300,000 vertices, with one `true` value:

   ```cypher
   UNWIND range(1, 300000) AS i
   CREATE (:Bench {
     id: i,
     flag: CASE WHEN i = 1 THEN true ELSE false END
   })
   RETURN count(*) AS c;
   ```

3. Run query A:

   ```cypher
   MATCH (n:Bench)
   WHERE n.flag = true
   RETURN count(*) AS c;
   ```

4. Run query B:

   ```cypher
   MATCH (n:Bench)
   WHERE n.flag
   RETURN count(*) AS c;
   ```

5. Compare the plans:

   ```cypher
   EXPLAIN
   MATCH (n:Bench)
   WHERE n.flag = true
   RETURN count(*) AS c;

   EXPLAIN
   MATCH (n:Bench)
   WHERE n.flag
   RETURN count(*) AS c;
   ```

### Expected behavior

Both queries should return `c = 1`. For a Boolean property in a `WHERE`
clause, `n.flag` and `n.flag = true` select the same rows. The direct form
should be able to use the index on `flag`.

### Actual behavior

Both queries return `c = 1`, but their plans differ.

Query A uses the Boolean-property index:

```text
+ Filter [predicate=n.flag = true]
  + NodeIndexSeek(n:Bench) [index=Bench[flag], flag=true]
```

Query B scans all `Bench` vertices:

```text
+ Filter [predicate=n.flag]
  + NodeByLabelScan(n:Bench) [rows=300000]
```

I reproduced the difference in two fresh databases:

```text
Session 1: query A = 2.80 ms, query B = 249.06 ms
Session 2: query A = 4.05 ms, query B = 246.04 ms
```

The complete results were identical in every run.
