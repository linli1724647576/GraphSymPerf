# Equivalent NULL contradiction remains about 5x slower than `WHERE false`

I found that a simple NULL-test contradiction is consistently slower than the
equivalent constant-false predicate on a large node table, although both
queries return the same complete result.

Ladybug Version: 0.18.2
Operating System: macOS 27.0 arm64
Installation Method: `pip install ladybug==0.18.2`
API/Driver: Ladybug Python API 0.18.2 / Python 3.12.7

### Steps to reproduce

1. Run the attached `reproducer.py` in a disposable directory:

   ```bash
   python3 -m pip install ladybug==0.18.2
   python3 reproducer.py
   ```

   The script creates this schema and data set:

   ```cypher
   CREATE NODE TABLE N(id INT64, v INT64, PRIMARY KEY(id));

   UNWIND RANGE(1, 5000000) AS i
   CREATE (:N {
     id: i,
     v: CASE WHEN i % 2 = 0 THEN NULL ELSE i END
   });
   ```

2. Run query A:

   ```cypher
   MATCH (n:N)
   WHERE n.v IS NULL AND n.v IS NOT NULL
   RETURN count(*) AS c;
   ```

3. Run query B:

   ```cypher
   MATCH (n:N)
   WHERE false
   RETURN count(*) AS c;
   ```

4. Compare ordinary execution time with balanced `AB`/`BA` order. `PROFILE`
   is not used for the timing samples.

### Expected behavior

Under Cypher three-valued logic, `x IS NULL` and `x IS NOT NULL` cannot both
be true. Therefore, in a `WHERE` clause,

```text
x IS NULL AND x IS NOT NULL
```

is equivalent to `false`. I would expect the contradiction to be simplified,
or at least not to retain a systematic multi-fold runtime difference from the
constant-false formulation.

### Actual behavior

Both queries returned `c = 0`. A fresh rerun of the attached reproducer in two
independent OS processes measured 5.2915x / 1.3288 ms and 4.9400x / 1.4151 ms.

A separate stricter validation pass used two fresh OS processes and two
independent database files. Each session performed six alternating complete-
result equivalence checks, five warmups, and 20 exactly balanced paired
measurements. Both 200-sample A/A calibrations passed.

| Metric | Query A | Query B / paired result |
|---|---:|---:|
| Session 1 median | 1.4860 ms | 0.2209 ms |
| Session 2 median | 1.5294 ms | 0.2445 ms |
| Session ratios | - | 6.5667x / 6.3137x |
| Pooled median ratio | - | 6.4456x |
| Ratio CI lower bound | - | 6.0867x |
| Pooled median delta | - | 1.2547 ms |
| Delta CI lower bound | - | 1.2374 ms |
| Effective A/A and same-query noise floor | - | 0.3377 ms |
| Direction consistency | - | 1.0000 |

The effect also remained above the same strong-ratio gate after reducing the
data set to 2,000,000 nodes: pooled ratio 3.5350x, ratio CI lower bound
3.3579x, and both session ratios above 3.4x. At 1,000,000 nodes it fell below
the reporting threshold, so 2,000,000 is the smallest validated scale in this
reduction pass.

### Plan observation

This is explanatory evidence only; all timing above used ordinary execution.
On the same database, `EXPLAIN` for query A contains:

```text
SCAN_NODE_TABLE N (property n.v)
  FILTER IS_NULL(n.v)
    FILTER IS_NOT_NULL(n.v)
```

while query B contains `EMPTY_RESULT` and no node-table scan.

### Related report

#669 reported an excluded-middle expression on 0.18.0. This reproducer uses a
different identity (`IS NULL AND IS NOT NULL`), a minimal query, and Ladybug
0.18.2 after #671. I am mentioning the relationship explicitly for duplicate
or shared-root-cause triage:
https://github.com/LadybugDB/ladybug/issues/669
