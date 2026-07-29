# A redundant `type(r) = 'P'` check makes a statically typed relationship query about 6x slower

I found that adding `WHERE type(r) = 'P'` to a relationship already matched as
`[r:P]` makes the query about six times slower. The condition is always true,
and both queries return the same count.

Apache AGE Version: 1.7.0
PostgreSQL Version: 18.1
Operating System: macOS 27.0 arm64
Installation Method: official `apache/age` Docker image
API/Driver: PostgreSQL wire protocol / Psycopg 3.3.4

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`typed_relationship_type_guard_symmetry`. The rule adds a relationship-type
condition already guaranteed by the static type in the pattern.

### Steps to reproduce

1. Load AGE and create a fresh graph:

   ```sql
   LOAD 'age';
   SET search_path = ag_catalog, "$user", public;
   SELECT ag_catalog.create_graph('relationship_type_guard_perf');
   ```

2. Create 20,000 relationships of type `P`:

   ```sql
   SELECT * FROM ag_catalog.cypher('relationship_type_guard_perf', $$
     UNWIND range(1, 20000) AS i
     CREATE (:X {id: i})-[:P {id: i}]->(:Y {id: i})
     RETURN count(*) AS created
   $$) AS (created agtype);
   ```

3. Refresh the label statistics:

   ```sql
   ANALYZE relationship_type_guard_perf."X";
   ANALYZE relationship_type_guard_perf."Y";
   ANALYZE relationship_type_guard_perf."P";
   ```

4. Run query A:

   ```sql
   SELECT * FROM ag_catalog.cypher('relationship_type_guard_perf', $$
     MATCH (:X)-[r:P]->(:Y)
     RETURN count(*) AS c
   $$) AS (c agtype);
   ```

5. Run query B, which only adds a condition already guaranteed by `[r:P]`:

   ```sql
   SELECT * FROM ag_catalog.cypher('relationship_type_guard_perf', $$
     MATCH (:X)-[r:P]->(:Y)
     WHERE type(r) = 'P'
     RETURN count(*) AS c
   $$) AS (c agtype);
   ```

6. Prefix both queries with `EXPLAIN (ANALYZE, BUFFERS)` to inspect their
   plans.

### Expected behavior

Both queries should return `c = 20000`. Since `[r:P]` already limits `r` to
type `P`, query B should not need to rebuild each relationship value and check
its type again.

### Actual behavior

Both queries return `c = 20000`, but query B is consistently slower. Each
independent instance ran 12 alternating comparisons, and query B was slower in
all of them:

| Independent instance | Query A | Query B | Slowdown |
|---|---:|---:|---:|
| AGE 1.7.0, instance 1 | 9.43 ms | 53.86 ms | 5.71x |
| AGE 1.7.0, instance 2 | 8.13 ms | 52.45 ms | 6.45x |

The slow plan applies this filter while scanning the `P` relationship table:

```text
Filter: (age_type(_agtype_build_edge(...)) = '"P"'::agtype)
```

The fast plan scans the same `P` table without that filter. Both containers
remained running without a restart or OOM event during validation.
