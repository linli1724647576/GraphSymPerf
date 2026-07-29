I found a scalar-Boolean coverage gap on the current `26.8.1-SNAPSHOT` after
#3403 / commit
[`497efbd`](https://github.com/ArcadeData/arcadedb/commit/497efbd64c4b5f7cdef4a389144c475e213fa10e).

#3403 tested parentheses around `NOT (pattern)` pattern predicates. The current
case contains no graph pattern predicate: a bare double `NOT` in `WHERE` is
compiled as a single `NOT`.

Minimal reproducer:

```cypher
WITH false AS p
WHERE NOT NOT p
RETURN p;
```

Expected: no rows, because double negation of `false` is `false`.

Actual:

```text
p = false
```

The symmetric constant case is also wrong:

```cypher
WITH true AS p
WHERE NOT NOT p
RETURN p;
```

Expected `p = true`; actual result is empty.

A graph-backed three-valued reproducer makes the lost `NOT` visible:

```cypher
CREATE (:BoolCase {id: 1, flag: true});
CREATE (:BoolCase {id: 2, flag: false});
CREATE (:BoolCase {id: 3});

MATCH (n:BoolCase)
WHERE n.flag
RETURN n.id AS id ORDER BY id;
-- [1]

MATCH (n:BoolCase)
WHERE NOT NOT n.flag
RETURN n.id AS id ORDER BY id;
-- actual: [2], expected: [1]
```

The plan for the second query contains only one negation:

```text
+ Filter [predicate=NOT n.flag, cost=4.50, rows=1]
  + NodeByLabelScan(n:BoolCase) [cost=3.00, rows=3]
```

Controls:

```cypher
-- Correct in WHERE when explicitly nested
MATCH (n:BoolCase)
WHERE NOT (NOT n.flag)
RETURN n.id AS id ORDER BY id;
-- [1]

-- Correct in projection, including the bare form
MATCH (n:BoolCase)
RETURN n.id, n.flag, NOT NOT n.flag AS double_not
ORDER BY n.id;
-- true -> true, false -> false, null -> null
```

I reproduced all results five times in each of two fresh databases and separate
client processes. Both sessions produced the same complete ordered results.

Environment:

- ArcadeDB `26.8.1-SNAPSHOT`
- build `792bdff3378f10bf3fafe136b04a7437e2ba6c4b`
- image ID `sha256:0a7935789d65da3ae5e328c33fc56c3144115d324be14d561967afc65c552a86`
- HTTP API, `language=opencypher`

Would you prefer reopening #3403 for this scalar `WHERE` coverage gap, or a
separate issue? I can open a separate report if that is easier to track.
