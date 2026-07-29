# A duplicate equality predicate changes NodeIndexSeek to NodeByLabelScan in OpenCypher

I found that adding the same equality predicate a second time with `AND` changes a unique-index lookup into a full label scan. Both queries return the same count, but the duplicate-predicate form slows down as the vertex count increases.

- ArcadeDB Version: 26.8.1-SNAPSHOT
- Operating System: macOS 27.0 arm64
- Installation Method: Docker 29.1.2 / arcadedata/arcadedb:latest
- API/Driver: HTTP REST API / OpenCypher for queries / SQL for schema setup

### Steps to reproduce

1. Create a vertex type with a unique index:

   ```sql
   CREATE VERTEX TYPE Bench;
   CREATE PROPERTY Bench.id LONG;
   CREATE INDEX ON Bench (id) UNIQUE;
   ```

2. Load 20,000 vertices. The verifier inserts 2,000 vertices per batch:

   ```cypher
   UNWIND range(1, 2000) AS i
   CREATE (:Bench {id: i})
   RETURN count(*) AS c;
   ```

   Repeat the statement with non-overlapping ranges through `20000`.

3. Run query A:

   ```cypher
   MATCH (n:Bench)
   WHERE n.id = 1
   RETURN count(*) AS c;
   ```

4. Run query B, which repeats the same predicate:

   ```cypher
   MATCH (n:Bench)
   WHERE (n.id = 1) AND (n.id = 1)
   RETURN count(*) AS c;
   ```

5. Compare the plans:

   ```cypher
   EXPLAIN MATCH (n:Bench)
   WHERE n.id = 1
   RETURN count(*) AS c;

   EXPLAIN MATCH (n:Bench)
   WHERE (n.id = 1) AND (n.id = 1)
   RETURN count(*) AS c;
   ```


### Expected behavior

Both queries should return `c = 1`. ArcadeDB should simplify `P AND P` to `P`
before index selection, or extract the equality constraint from the
conjunction and keep `NodeIndexSeek`.

### Actual behavior

Both queries return `c = 1`. Query A uses the unique index:

```text
+ Filter [predicate=n.id = 1, cost=17.00, rows=10]
  + NodeIndexSeek(n:Bench) [index=Bench[id], id=1, cost=7.00, rows=20]
```

Query B scans all `Bench` vertices:

```text
+ Filter [predicate=(n.id = 1 AND n.id = 1), cost=30000.00, rows=10000]
  + NodeByLabelScan(n:Bench) [cost=20000.00, rows=20000]
```

I reproduced the gap in two fresh databases and separate client processes:

```text
Session 1: A = 2.96 ms, B = 41.17 ms
Session 2: A = 2.39 ms, B = 32.27 ms
```

Query B is about 12 to 13 times slower. Both queries returned `c = 1` in each
run.
