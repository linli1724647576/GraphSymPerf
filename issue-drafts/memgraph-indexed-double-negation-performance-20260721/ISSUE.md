# Double negation prevents an indexed equality from using the property index

I found that adding a double negation to an indexed equality makes the query
much slower. Both queries return the same rows, but the double-negated form
scans all nodes instead of using the property index.

Memgraph Version: 3.10.1
Operating System: macOS 27.0 arm64
Installation Method: Docker image `memgraph/memgraph:latest`

### Steps to reproduce

1. Create the property index:

```cypher
CREATE INDEX ON :Item(key);
```

2. Create 500,000 `:Item` nodes. Run the following statement in batches,
changing the range by 5,000 each time until the last range is
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
WHERE NOT NOT (n.key = 42)
RETURN n.id AS id
ORDER BY id;
```

5. Run the following count queries several times in alternating order.

Query A:

```cypher
MATCH (n:Item)
WHERE n.key = 42
RETURN count(*) AS c;
```

Query B adds only `NOT NOT`:

```cypher
MATCH (n:Item)
WHERE NOT NOT (n.key = 42)
RETURN count(*) AS c;
```

6. Inspect both execution plans:

```cypher
EXPLAIN MATCH (n:Item)
WHERE n.key = 42
RETURN count(*) AS c;

EXPLAIN MATCH (n:Item)
WHERE NOT NOT (n.key = 42)
RETURN count(*) AS c;
```

### Expected behavior

Double negation preserves the result of a boolean expression, including
`null`. Both queries should use the `:Item(key)` index and have comparable
execution time.

### Actual behavior

Both queries return `c = 500`, and their complete matching ID lists are
equal. Two fresh runs produced:

| Run | Query A | Query B |
|---|---:|---:|
| 1 | 1.53 ms | 100.12 ms |
| 2 | 1.43 ms | 105.85 ms |

`EXPLAIN` for query A uses the property index:

```text
* ScanAllByLabelProperties (n :Item {key})
```

`EXPLAIN` for query B scans all nodes:

```text
* Filter (n :Item), Generic {n}
* ScanAll (n)
```
