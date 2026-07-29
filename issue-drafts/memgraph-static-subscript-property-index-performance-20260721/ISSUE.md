# Static subscript property access does not use a property index

I found that two equivalent forms of static property access use different
execution paths. Dot access uses the property index, while subscript access
with the literal key `'key'` scans all nodes.

Memgraph Version: 3.10.1
Operating System: macOS 27.0 arm64
Installation Method: Docker image `memgraph/memgraph:latest`

### Steps to reproduce

1. Create the property index:

```cypher
CREATE INDEX ON :Item(key);
```

2. Create 500,000 `:Item` nodes with `key`. Run the following statement in
batches, changing the range by 5,000 each time until the last range is
`495000, 499999`:

```cypher
UNWIND range(0, 4999) AS i
CREATE (:Item {id: i, key: i % 1000});
```

3. Create 100 `:Item` nodes without `key`:

```cypher
UNWIND range(500000, 500099) AS i
CREATE (:Item {id: i});
```

4. Compare the complete matching ID lists:

```cypher
MATCH (n:Item)
WHERE n.key = 42
RETURN n.id AS id
ORDER BY id;

MATCH (n:Item)
WHERE n['key'] = 42
RETURN n.id AS id
ORDER BY id;
```

5. Run the following count queries several times in alternating order.

Query A uses dot access:

```cypher
MATCH (n:Item)
WHERE n.key = 42
RETURN count(*) AS c;
```

Query B uses the same static property name with subscript access:

```cypher
MATCH (n:Item)
WHERE n['key'] = 42
RETURN count(*) AS c;
```

6. Inspect both execution plans:

```cypher
EXPLAIN MATCH (n:Item)
WHERE n.key = 42
RETURN count(*) AS c;

EXPLAIN MATCH (n:Item)
WHERE n['key'] = 42
RETURN count(*) AS c;
```

### Expected behavior

Both queries should return the same nodes. Memgraph can resolve the literal
key `'key'` before planning and use the existing `:Item(key)` index.

### Actual behavior

Both queries return `c = 500`, and their complete matching ID lists are equal.
Two fresh runs produced:

| Run | Query A | Query B |
|---|---:|---:|
| 1 | 0.98 ms | 94.26 ms |
| 2 | 0.82 ms | 96.46 ms |

`EXPLAIN` for query A uses the property index:

```text
* ScanAllByLabelProperties (n :Item {key})
```

`EXPLAIN` for query B scans all nodes:

```text
* Filter (n :Item), Generic {n}
* ScanAll (n)
```

In a separate control without the property index, both forms scanned the graph.
The subscript form remained slower, but the difference dropped from about 100
times to about 2 times.
