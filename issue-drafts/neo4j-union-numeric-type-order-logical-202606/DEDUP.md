# Deduplication record

Checked: 2026-07-22

Candidate: swapping read-only `UNION` branches containing the equal numeric
values `1` and `1.0` changes a later division result from `0` to `0.5`.

## Searches

The Neo4j issue tracker was searched in both open and closed issues with these
term groups:

- `UNION integer float`
- `UNION numeric type`
- `UNION duplicate numeric type`
- `UNION branch order result`
- `DISTINCT integer float order`
- `valueType UNION`
- `integer division UNION`
- `1.0 UNION DISTINCT`

No report with the same trigger and symptom was found.

## Nearest existing reports

- [#13863](https://github.com/neo4j/neo4j/issues/13863) and
  [#13862](https://github.com/neo4j/neo4j/issues/13862) concern internal runtime
  errors from `NEXT + UNION + ANY SHORTEST` with imported graph variables. This
  candidate uses only constants and returns two different ordinary values.
- [#13643](https://github.com/neo4j/neo4j/issues/13643) and
  [#13630](https://github.com/neo4j/neo4j/issues/13630) compare variable-length
  paths with fixed-length `UNION ALL` branches inside an optional subquery.
  They do not involve duplicate removal or mixed numeric representations.
- [#13580](https://github.com/neo4j/neo4j/issues/13580) reports different graph
  rows after adding `DISTINCT` to a complex query containing updates. The new
  candidate is read-only, graph-free, and is triggered by swapping equal
  integer and float inputs.
- [#13864](https://github.com/neo4j/neo4j/issues/13864) and
  [#13865](https://github.com/neo4j/neo4j/issues/13865) are the previously
  submitted `count(1)` and exact zero-hop performance reports. Their triggers,
  symptoms, and likely fix scopes are unrelated.

## Same-family manifestations

The following standalone `DISTINCT` pair also changes a later division result:

```cypher
UNWIND [1, 1.0] AS x
WITH DISTINCT x
RETURN x / 2 AS value;
```

```cypher
UNWIND [1.0, 1] AS x
WITH DISTINCT x
RETURN x / 2 AS value;
```

Nested lists and maps containing equal integer/float values show the same
representative-order behavior. These are treated as supporting manifestations
of one duplicate-removal issue, not as separate issues.

## Conclusion

This is not a duplicate of the checked upstream or previously submitted
issues. Submit one logical issue for the numeric duplicate-removal family.
