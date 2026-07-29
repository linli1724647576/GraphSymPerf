# A redundant `length(p) = 2` check makes a fixed two-hop path about 11x slower

I found that adding `WHERE length(p) = 2` to a fixed two-hop path makes the
query about 11 times slower. The condition cannot remove any row because the
pattern already contains exactly two relationships. Both queries return the
same count.

Apache AGE Version: 1.7.0
PostgreSQL Version: 18.1
Operating System: macOS 27.0 arm64
Installation Method: official `apache/age` Docker image
API/Driver: PostgreSQL wire protocol / Psycopg 3.3.4

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`fixed_path_length_guard_symmetry`. The rule adds a path-length condition that
is already guaranteed by a fixed-length pattern.

### Steps to reproduce

1. Load AGE and create a fresh graph:

   ```sql
   LOAD 'age';
   SET search_path = ag_catalog, "$user", public;
   SELECT ag_catalog.create_graph('fixed_path_length_perf');
   ```

2. Create 20,000 independent two-hop paths:

   ```sql
   SELECT * FROM ag_catalog.cypher('fixed_path_length_perf', $$
     UNWIND range(1, 20000) AS i
     CREATE (:X {id: i})-[:P {id: i}]->(:Y {id: i})-[:Q {id: i}]->(:Z {id: i})
     RETURN count(*) AS created
   $$) AS (created agtype);
   ```

3. Refresh the label statistics:

   ```sql
   ANALYZE fixed_path_length_perf."X";
   ANALYZE fixed_path_length_perf."Y";
   ANALYZE fixed_path_length_perf."Z";
   ANALYZE fixed_path_length_perf."P";
   ANALYZE fixed_path_length_perf."Q";
   ```

4. Run query A:

   ```sql
   SELECT * FROM ag_catalog.cypher('fixed_path_length_perf', $$
     MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z)
     RETURN count(*) AS c
   $$) AS (c agtype);
   ```

5. Run query B, which only adds a condition already guaranteed by the
   pattern:

   ```sql
   SELECT * FROM ag_catalog.cypher('fixed_path_length_perf', $$
     MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z)
     WHERE length(p) = 2
     RETURN count(*) AS c
   $$) AS (c agtype);
   ```

6. Prefix both queries with `EXPLAIN (ANALYZE, BUFFERS)` to inspect their
   plans.

### Expected behavior

Both queries should return `c = 20000`. Since the fixed pattern already proves
that every `p` has length 2, query B should not need to build and measure the
path for every matched row.

### Actual behavior

Both queries return `c = 20000`, but query B is consistently slower. Each
independent instance ran 12 alternating comparisons, and query B was slower in
all of them:

| Independent instance | Query A | Query B | Slowdown |
|---|---:|---:|---:|
| AGE 1.7.0, instance 1 | 16.94 ms | 195.88 ms | 11.57x |
| AGE 1.7.0, instance 2 | 17.18 ms | 192.34 ms | 11.19x |

The slow plan contains this additional per-row filter:

```text
Join Filter: (age_length(_agtype_build_path(...)) = '2'::agtype)
```

The fast plan has no corresponding path construction or length filter. Both
containers remained running without a restart or OOM event during validation.
