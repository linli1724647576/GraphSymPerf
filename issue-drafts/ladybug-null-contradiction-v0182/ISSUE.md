# NULL contradiction scans the node table instead of producing an empty result

LadybugDB does not simplify this contradiction to `false`:

```cypher
n.v IS NULL AND n.v IS NOT NULL
```

Both sides of the comparison below return `0`, but the contradiction scans the
node table and takes about five times as long as `WHERE false` on 5 million
nodes.

Ladybug Version: 0.18.2
Operating System: macOS 27.0 arm64
Installation Method: `pip install ladybug==0.18.2`
API/Driver: Ladybug Python API 0.18.2 / Python 3.12.7

### Steps to reproduce

Run the attached script in a disposable directory:

```bash
python3 -m pip install ladybug==0.18.2
python3 reproducer.py
```

The script creates this table and data set:

```cypher
CREATE NODE TABLE N(id INT64, v INT64, PRIMARY KEY(id));

UNWIND RANGE(1, 5000000) AS i
CREATE (:N {
  id: i,
  v: CASE WHEN i % 2 = 0 THEN NULL ELSE i END
});
```

Query A:

```cypher
MATCH (n:N)
WHERE n.v IS NULL AND n.v IS NOT NULL
RETURN count(*) AS c;
```

Query B:

```cypher
MATCH (n:N)
WHERE false
RETURN count(*) AS c;
```

### Expected behavior

`x IS NULL` and `x IS NOT NULL` cannot both be true, so the two predicates are
equivalent. Both queries should return `0`, and the optimizer should avoid the
node-table scan in query A.

### Actual behavior

Both queries return `0`. Two fresh runs of the attached script produced:

| Run | Query A | Query B | A / B | Extra time |
|---|---:|---:|---:|---:|
| 1 | 1.64 ms | 0.31 ms | 5.3x | 1.33 ms |
| 2 | 1.77 ms | 0.36 ms | 4.9x | 1.42 ms |

The exact timings vary by machine. The repeated difference comes from query A
scanning the property column while query B returns an empty result without a
node-table scan.

`EXPLAIN` for query A includes:

```text
SCAN_NODE_TABLE N (property n.v)
  FILTER IS_NULL(n.v)
    FILTER IS_NOT_NULL(n.v)
```

`EXPLAIN` for query B contains `EMPTY_RESULT` and no node-table scan.

### Related report

#669 reported a different boolean identity on LadybugDB 0.18.0. This smaller
reproducer uses `IS NULL AND IS NOT NULL` and still reproduces on 0.18.2 after
#671. The two cases may share an optimizer root cause:

https://github.com/LadybugDB/ladybug/issues/669
https://github.com/LadybugDB/ladybug/pull/671
