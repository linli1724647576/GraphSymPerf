# `ORDER BY` before `LIMIT` and `count(*)` causes a large slowdown

I found that adding `ORDER BY` before a limited row count makes the query much
slower. The order changes which rows pass through `LIMIT`, but it cannot change
how many rows are counted.

Ladybug Version: 0.18.3
Operating System: macOS 27.0 arm64
Installation Method: `pip install ladybug==0.18.3`
API/Driver: Ladybug Python API 0.18.3 / Python 3.12.7

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`order_before_limited_count_symmetry`. The rule adds or removes an order that
cannot affect the final row count.

### Steps to reproduce

1. Create a node table and 500,000 nodes:

   ```cypher
   CREATE NODE TABLE N(id INT64, PRIMARY KEY(id));

   UNWIND range(1, 500000) AS i
   CREATE (:N {id: i});
   ```

2. Run query A:

   ```cypher
   MATCH (n:N)
   WITH n
   LIMIT 100000
   RETURN count(*) AS value;
   ```

3. Run query B, which only adds `ORDER BY n.id`:

   ```cypher
   MATCH (n:N)
   WITH n
   ORDER BY n.id
   LIMIT 100000
   RETURN count(*) AS value;
   ```

4. Run both queries several times after warm-up. `PROFILE` can be used
   separately to compare their plans.

### Expected behavior

Both queries should return `value = 100000`. More generally, ordering can
change the contents of a limited row set, but not its size when the only later
operation is `count(*)`. The optimizer should be able to avoid the unused
ordering work.

### Actual behavior

Both queries return `value = 100000`, but query B is much slower. I repeated
the test in two fresh Python processes with independent database files:

| Fresh process | Query A | Query B |
|---|---:|---:|
| 1 | 0.38 ms | 136.40 ms |
| 2 | 0.39 ms | 136.55 ms |

Each process used five warm-up runs followed by 20 runs of each query in mixed
order.

`PROFILE` shows that query B keeps a `TOP_K` step and reads all 500,000 `id`
values. Query A uses `LIMIT` without that sorting step.
