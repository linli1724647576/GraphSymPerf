# A label predicate in `WHERE` scans unrelated vertex types in OpenCypher

I found that moving one node label from the `MATCH` pattern to an equivalent
`WHERE` predicate changes the execution path. The pattern form uses the
cost-based optimizer and scans the matching type. The predicate form uses
traditional execution, and its runtime grows with the number of vertices in
unrelated types.

- ArcadeDB Version: 26.8.1-SNAPSHOT
- Operating System: macOS 27.0 arm64
- Installation Method: Docker 29.1.2 / arcadedata/arcadedb:latest
- API/Driver: HTTP REST API / OpenCypher

### Steps to reproduce

1. Create a parent type, one child type, and an unrelated type:

   ```sql
   CREATE VERTEX TYPE Parent;
   CREATE PROPERTY Parent.id LONG;
   CREATE VERTEX TYPE Child EXTENDS Parent;
   CREATE VERTEX TYPE Other;
   CREATE PROPERTY Other.id LONG;
   ```

2. Create one `Parent`, one `Child`, and 300,000 unrelated `Other` vertices.
   The verifier inserts `Other` vertices in 5,000-row batches:

   ```cypher
   CREATE (:Parent {id: 1}) RETURN count(*) AS c;
   CREATE (:Child {id: 2}) RETURN count(*) AS c;

   UNWIND range(1, 5000) AS i
   CREATE (:Other {id: i})
   RETURN count(*) AS c;
   ```

   Repeat the last statement with non-overlapping ranges through `300000`.

3. Run query A, with the label in the node pattern:

   ```cypher
   MATCH (n:Parent)
   RETURN n.id AS id
   ORDER BY id;
   ```

4. Run query B, with the same label in `WHERE`:

   ```cypher
   MATCH (n)
   WHERE n:Parent
   RETURN n.id AS id
   ORDER BY id;
   ```

5. Compare the plans:

   ```cypher
   EXPLAIN MATCH (n:Parent)
   RETURN n.id AS id
   ORDER BY id;

   EXPLAIN MATCH (n)
   WHERE n:Parent
   RETURN n.id AS id
   ORDER BY id;
   ```

The attached `verify.py` performs the batched load, runs both queries, and
compares the complete ordered results.

### Expected behavior

Both queries should return `id = [1, 2]` and restrict execution to the `Parent`
type hierarchy. Adding `Other` vertices should not increase the runtime of
query B.

### Actual behavior

Both queries return `id = [1, 2]`, including the inherited `Child` vertex.
Their plans differ:

```text
Query A

Using Cost-Based Query Optimizer

+ NodeByLabelScan(n:Parent) [cost=1.00, rows=1]
```

```text
Query B

Using Traditional Execution (Non-Optimized)

Reason: Query pattern not yet supported by optimizer
Execution will use step-by-step interpretation
```

I reproduced the gap in two fresh databases and separate client processes:

```text
Session 1: A = 2.21 ms, B = 155.09 ms
Session 2: A = 3.13 ms, B = 156.55 ms
```

Query B is about 50 to 70 times slower. Both queries returned the same ordered
rows in each run.

The test framework generated this pair from `label_where_symmetry`, which
replaces `MATCH (n:Label)` with `MATCH (n) WHERE n:Label` for one static label.
I checked the complete results before writing this report.

I found no ArcadeDB issue for this direct label-predicate optimizer fallback.
#5226 covers a label and edge-type collision inside `CALL`; the queries above
use top-level `MATCH`, contain no type-name collision, and return the correct
rows.
