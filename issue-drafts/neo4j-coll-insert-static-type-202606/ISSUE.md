# `coll.insert()` returns a list at runtime but is statically typed as `ANY` when passed to list functions

I found that `coll.insert()` returns a `LIST` when used directly, but the
Cypher 25 static type checker rejects the same expression when it is passed to
`size()`, `head()`, or another list function.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
API/Driver: Cypher 25 / Neo4j Python Driver 5.28.4

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`list_insert_composition`: replacing a literal list with the equivalent list
produced by `coll.insert()` must preserve the result of a pure list consumer.
The rule requires an in-bounds insertion index and pure literal inputs. The
framework compared complete typed outcomes and replayed the mismatch six times
in alternating A/B order before this standalone reproducer was written.

### Steps to reproduce

First, run `coll.insert()` directly:

```cypher
CYPHER 25
RETURN coll.insert([1, 2, 3], 1, 9) AS value,
       valueType(coll.insert([1, 2, 3], 1, 9)) AS value_type;
```

It returns `[1, 9, 2, 3]` with type
`LIST<INTEGER NOT NULL> NOT NULL`.

Then pass the same expression to documented list consumers:

```cypher
CYPHER 25
RETURN size(coll.insert([1, 2, 3], 1, 9)) AS value;
```

```cypher
CYPHER 25
RETURN head(coll.insert([1, 2, 3], 1, 9)) AS value;
```

```cypher
CYPHER 25
RETURN coll.remove(coll.insert([1, 2, 3], 1, 9), 1) AS value;
```

The attached `reproducer.py` runs all three queries six times in alternating
order and also checks adjacent `coll.*` composition controls.

### Expected behavior

The queries should return `4`, `1`, and `[1, 2, 3]`, respectively. The Cypher
manual and `SHOW FUNCTIONS` both declare the return type of `coll.insert()` as
`LIST<ANY>`, which is a valid input to `size()`, `head()`, and `coll.remove()`.

### Actual behavior

All three queries are rejected before execution. For example, `size()` fails
with:

```text
Neo.ClientError.Statement.SyntaxError
Type mismatch: expected String, Vector or List<T> but was Any
```

The direct query still returns a list and `valueType()` reports a concrete
`LIST` type. The same list-consuming compositions work when `coll.insert()` is
replaced by `coll.remove()`, `coll.flatten()`, `coll.distinct()`, or
`coll.sort()`.

The result was reproduced in two fresh Neo4j 2026.06.0 Docker processes, with
six alternating executions per process.
