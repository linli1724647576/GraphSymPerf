# Bare double `NOT` acts as single `NOT` in an OpenCypher `WHERE` clause

I found that ArcadeDB compiles `NOT NOT p` as `NOT p` when the expression
appears in `WHERE`. The same double negation returns the correct value in a
projection.

- ArcadeDB Version: 26.8.1-SNAPSHOT
- Operating System: macOS 27.0 arm64
- Installation Method: Docker 29.1.2 / arcadedata/arcadedb:latest
- API/Driver: HTTP REST API / OpenCypher

### Steps to reproduce

1. Create a vertex type with a Boolean property:

   ```sql
   CREATE VERTEX TYPE BoolCase;
   CREATE PROPERTY BoolCase.id LONG;
   CREATE PROPERTY BoolCase.flag BOOLEAN;
   ```

2. Create one `true` value, one `false` value, and one missing value:

   ```cypher
   CREATE (:BoolCase {id: 1, flag: true}) RETURN count(*) AS c;
   CREATE (:BoolCase {id: 2, flag: false}) RETURN count(*) AS c;
   CREATE (:BoolCase {id: 3}) RETURN count(*) AS c;
   ```

3. Run query A:

   ```cypher
   MATCH (n:BoolCase)
   WHERE n.flag
   RETURN n.id AS id
   ORDER BY id;
   ```

4. Run query B, which applies double negation:

   ```cypher
   MATCH (n:BoolCase)
   WHERE NOT NOT n.flag
   RETURN n.id AS id
   ORDER BY id;
   ```

5. Run two controls:

   ```cypher
   MATCH (n:BoolCase)
   WHERE NOT (NOT n.flag)
   RETURN n.id AS id
   ORDER BY id;
   ```

   ```cypher
   MATCH (n:BoolCase)
   RETURN n.id AS id,
          n.flag AS flag,
          NOT NOT n.flag AS double_not
   ORDER BY id;
   ```

You can reproduce the issue without graph data:

```cypher
WITH false AS p
WHERE NOT NOT p
RETURN p;
```


### Expected behavior

Queries A and B should return `id = [1]`. Double negation preserves `true`,
`false`, and `null`. The constant query should return no rows because its
`WHERE` expression evaluates to `false`.

### Actual behavior

Query A returns `id = [1]`. Query B returns `id = [2]`, the vertex whose flag
is `false`. The constant query returns a row containing `p = false`.

The parenthesized `WHERE NOT (NOT n.flag)` control returns `id = [1]`. The
projection also evaluates all three input values as expected:

```text
id = 1: flag = true,  double_not = true
id = 2: flag = false, double_not = false
id = 3: flag = null,  double_not = null
```

The plans show that the failing `WHERE` form loses one `NOT`:

```text
Query A

+ Filter [predicate=n.flag, cost=4.50, rows=1]
  + NodeByLabelScan(n:BoolCase) [cost=3.00, rows=3]
```

```text
Query B

+ Filter [predicate=NOT n.flag, cost=4.50, rows=1]
  + NodeByLabelScan(n:BoolCase) [cost=3.00, rows=3]
```

I reproduced the result in two fresh databases and separate client processes.
Each process ran the failing query and every control five times. Both processes
returned the same ordered rows in all runs.
