# Additional reproduction for Memgraph #4123

I reproduced the duplicate-row behavior on Memgraph 3.10.1 with an integer
property when a property index exists.

```cypher
CREATE INDEX ON :Item(key);
CREATE (:Item {id: 1, key: 42});
```

```cypher
MATCH (n:Item)
WHERE n.key IN [42]
RETURN n.id AS id;
```

Result:

```text
1
```

Repeating the same list element repeats the matching row:

```cypher
MATCH (n:Item)
WHERE n.key IN [42, 42]
RETURN n.id AS id;
```

Result:

```text
1
1
```

Three occurrences return the row three times. `EXPLAIN` contains:

```text
* ScanAllByLabelProperties (n :Item {key})
* Unwind
```

After dropping `:Item(key)`, the single-element and duplicate-element queries
both return the row once. A parameter list such as `$values = [42, 42]` also
returns the row once in this test.

Two fresh processes reproduced the same result. The raw records are:

- `results/duplicate-in-session1.json`
- `results/duplicate-in-session2.json`

This is supporting evidence for #4123, not a separate issue.
