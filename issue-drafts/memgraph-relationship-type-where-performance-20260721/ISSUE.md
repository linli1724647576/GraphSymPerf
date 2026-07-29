# Moving a relationship type to `WHERE type(r)` causes a large slowdown

I found that moving a relationship type constraint from the pattern to an
equivalent `WHERE type(r)` predicate makes the query much slower. Both forms
return the same relationships.

Memgraph Version: 3.10.1
Operating System: macOS 27.0 arm64
Installation Method: Docker image `memgraph/memgraph:latest`

### Steps to reproduce

1. Create the indexes and nodes:

```cypher
CREATE INDEX ON :A(id);
CREATE INDEX ON :B(id);

UNWIND range(0, 9999) AS i
CREATE (:A {id: i}), (:B {id: i});
```

2. Create one `LINK` relationship for each `:A` node. Run the following
statement in batches, changing the `a.id` range by 1,000 each time until the
last range is `a.id >= 9000 AND a.id < 10000`:

```cypher
MATCH (a:A)
WHERE a.id >= 0 AND a.id < 1000
MATCH (b:B {id: a.id})
CREATE (a)-[:LINK]->(b);
```

3. Create 50 `NOISE` relationships for each `:A` node. Run the following
statement in batches, changing the `a.id` range by 1,000 each time until the
last range is `a.id >= 9000 AND a.id < 10000`:

```cypher
MATCH (a:A)
WHERE a.id >= 0 AND a.id < 1000
UNWIND range(1, 50) AS offset
MATCH (b:B {id: (a.id + offset) % 10000})
CREATE (a)-[:NOISE]->(b);
```

4. Compare the complete relationship lists:

```cypher
MATCH (a:A)-[r:LINK]->(b:B)
RETURN a.id AS start_id, b.id AS end_id, type(r) AS relationship_type
ORDER BY start_id, end_id, relationship_type;

MATCH (a:A)-[r]->(b:B)
WHERE type(r) = 'LINK'
RETURN a.id AS start_id, b.id AS end_id, type(r) AS relationship_type
ORDER BY start_id, end_id, relationship_type;
```

5. Run the following count queries several times in alternating order.

Query A puts the relationship type in the pattern:

```cypher
MATCH (a:A)-[r:LINK]->(b:B)
RETURN count(r) AS c;
```

Query B moves the same constraint to `WHERE`:

```cypher
MATCH (a:A)-[r]->(b:B)
WHERE type(r) = 'LINK'
RETURN count(r) AS c;
```

6. Inspect both execution plans:

```cypher
EXPLAIN MATCH (a:A)-[r:LINK]->(b:B)
RETURN count(r) AS c;

EXPLAIN MATCH (a:A)-[r]->(b:B)
WHERE type(r) = 'LINK'
RETURN count(r) AS c;
```

### Expected behavior

Both queries select the same directed `LINK` relationships. Memgraph should
apply the `type(r) = 'LINK'` predicate during relationship expansion, so the
two forms should have comparable execution time.

### Actual behavior

Both queries return `c = 10000`, and their complete relationship lists are
equal. Two fresh runs produced:

| Run | Query A | Query B |
|---|---:|---:|
| 1 | 9.16 ms | 103.47 ms |
| 2 | 7.86 ms | 92.91 ms |

`EXPLAIN` for query A expands only `LINK` relationships:

```text
* Expand (a)-[r:LINK]->(b)
```

`EXPLAIN` for query B expands every relationship and filters afterward:

```text
* Filter (b :B), Generic {r}
* Expand (a)-[r]->(b)
```

With no `NOISE` relationships, the difference is small. Adding unrelated
relationship types increases the time for query B while query A remains close
to its original time.
