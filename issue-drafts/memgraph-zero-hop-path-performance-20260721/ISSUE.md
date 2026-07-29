# An exact zero-hop path performs unnecessary variable-length expansion work

I found that an exact zero-hop path is about twice as slow as the equivalent
node match. The path cannot traverse a relationship, but Memgraph still uses
`ExpandVariable`.

Memgraph Version: 3.10.1
Operating System: macOS 27.0 arm64
Installation Method: Docker image `memgraph/memgraph:latest`

### Steps to reproduce

1. Create 1,000,000 `:Item` nodes. Run the following statement in batches,
changing the range by 5,000 each time until the last range is
`995000, 999999`:

```cypher
UNWIND range(0, 4999) AS i
CREATE (:Item {id: i});
```

2. Create 100 unrelated `:Other` nodes. Do not create any relationships:

```cypher
UNWIND range(1, 100) AS i
CREATE (:Other {id: i});
```

3. Verify that the zero-hop form returns only equal endpoints:

```cypher
MATCH (n:Item)-[:UNUSED*0..0]->(m:Item)
WHERE n.id <> m.id
RETURN count(*) AS mismatches;
```

The result should be `mismatches = 0`.

4. Run the following count queries several times in alternating order.

Query A matches each `:Item` node:

```cypher
MATCH (n:Item)
RETURN count(*) AS c;
```

Query B expresses the same rows through an exact zero-hop path:

```cypher
MATCH (n:Item)-[:UNUSED*0..0]->(m:Item)
RETURN count(*) AS c;
```

5. Inspect both execution plans:

```cypher
EXPLAIN MATCH (n:Item)
RETURN count(*) AS c;

EXPLAIN MATCH (n:Item)-[:UNUSED*0..0]->(m:Item)
RETURN count(*) AS c;
```

### Expected behavior

An exact `*0..0` path does not traverse a relationship. It binds `m` to the
same node as `n`, so Memgraph can execute this case as a node match without a
variable-length expansion.

### Actual behavior

Both queries return `c = 1000000`, and all endpoint rows are equal. Two fresh
runs produced:

| Run | Query A | Query B |
|---|---:|---:|
| 1 | 61.18 ms | 129.66 ms |
| 2 | 50.57 ms | 102.96 ms |

`EXPLAIN` for query A scans the nodes:

```text
* Filter (n :Item)
* ScanAll (n)
```

`EXPLAIN` for query B adds an `ExpandVariable` operator even though the upper
and lower path bounds are both zero:

```text
* Filter (m :Item)
* ExpandVariable (n)-[anon1:UNUSED]->(m)
* Filter (n :Item)
* ScanAll (n)
```
