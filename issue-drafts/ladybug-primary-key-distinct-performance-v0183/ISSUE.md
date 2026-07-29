# `WITH DISTINCT` on a primary-key property causes a large slowdown

I found that adding `DISTINCT` to a projection of a primary-key property makes
the query much slower, even though the primary key is already unique. Both
queries below return the same count.

Ladybug Version: 0.18.3
Operating System: macOS 27.0 arm64
Installation Method: `pip install ladybug==0.18.3`
API/Driver: Ladybug Python API 0.18.3 / Python 3.12.7

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`primary_key_distinct_symmetry`. The rule adds or removes a redundant
`DISTINCT` on a primary-key property.

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
   WITH n.id AS id
   RETURN count(id) AS value;
   ```

3. Run query B, which only adds `DISTINCT`:

   ```cypher
   MATCH (n:N)
   WITH DISTINCT n.id AS id
   RETURN count(id) AS value;
   ```

4. Run both queries several times after warm-up. `PROFILE` can be used
   separately to compare their plans.

### Expected behavior

Both queries should return `value = 500000`. Because `N.id` is the primary key,
its values are already unique. The optimizer should be able to avoid duplicate
removal in query B.

### Actual behavior

Both queries return `value = 500000`, but query B is much slower. I repeated
the test in two fresh Python processes with independent database files:

| Fresh process | Query A | Query B |
|---|---:|---:|
| 1 | 0.46 ms | 19.27 ms |
| 2 | 0.40 ms | 18.57 ms |

Each process used five warm-up runs followed by 20 runs of each query in mixed
order.

`PROFILE` for query B contains an additional aggregation grouped by `id` and a
`FLATTEN` step. Query A has neither step.
