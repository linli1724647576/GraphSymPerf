# Swapping read-only `UNION` branches changes a later numeric result

I found that swapping two read-only `UNION` branches changes a later division
result. Neo4j removes `1` and `1.0` as duplicates, but the surviving numeric
type depends on which branch appears first.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
API/Driver: Cypher 25 / Neo4j Python Driver 5.28.4

### Discovery provenance

GraphSymPerf found this with the SYM rule `union_branch_order_symmetry`:
reordering the branches of a read-only set union should not change a later
calculation. The framework compared complete typed results and reproduced the
mismatch six times in alternating A/B order in each of two fresh Neo4j
processes.

### Steps to reproduce

No graph data is required.

1. Run query A:

   ```cypher
   CYPHER 25
   CALL () {
     RETURN 1 AS x
     UNION
     RETURN 1.0 AS x
   }
   RETURN x / 2 AS value;
   ```

2. Run query B, which only swaps the two `UNION` branches:

   ```cypher
   CYPHER 25
   CALL () {
     RETURN 1.0 AS x
     UNION
     RETURN 1 AS x
   }
   RETURN x / 2 AS value;
   ```

3. As a control, verify that Neo4j treats the two input values as equal:

   ```cypher
   CYPHER 25
   RETURN 1 = 1.0 AS equal;
   ```

   This returns `true`. The inner `UNION` also returns one row in both branch
   orders.

### Expected behavior

Queries A and B should return the same value. They are read-only and differ
only in branch order. `UNION` removes the two numerically equal rows as
duplicates, so the representation retained by duplicate removal should not
make a later calculation branch-order-dependent.

### Actual behavior

Query A returns:

```text
value
0
```

Query B returns:

```text
value
0.5
```

Inspecting the value after `UNION` shows the observable type difference:

| Query | `x` | `valueType(x)` | `x / 2` |
|---|---:|---|---:|
| A | `1` | `INTEGER NOT NULL` | `0` |
| B | `1.0` | `FLOAT NOT NULL` | `0.5` |

Both the slotted and pipelined runtimes reproduce the same `0` versus `0.5`
result. `EXPLAIN` contains `Union`, followed by `Distinct`, followed by the
division `Projection`. This is consistent with duplicate removal retaining the
first equal representation, but the report does not rely on that
implementation hypothesis.

The following controls do not produce a mismatch:

- replacing `UNION` with `UNION ALL`;
- converting both branches to `FLOAT` explicitly;
- using unequal values, such as `1` and `2.0`.

The same behavior is also present under Cypher 5.
