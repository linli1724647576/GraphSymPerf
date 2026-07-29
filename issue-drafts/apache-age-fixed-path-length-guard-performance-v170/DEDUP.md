# Deduplication record

Checked on 2026-07-22 against the current `apache/age` issue tracker and the
local submitted-issue inventory.

## Verdict

No issue with the same trigger, symptom, version, and likely fix scope was
found.

## Closest upstream issues

- #2350 concerns `shortestPath()` using variable-length expansion instead of
  BFS. This candidate uses a fixed two-edge pattern and a redundant
  `length(p) = 2` filter.
- #195 concerns variable-length edge expansion as path length grows. This
  candidate does not use a variable-length relationship.
- #982 concerns basic undirected relationship matching and nested joins. This
  candidate is directed and the regression appears only after adding the
  path-length guard.
- #2428 concerns a contradictory NULL disjunct in a property predicate. It is
  a different rule family and filter expression.

Searches included `"length(p)" performance`, `"length(p) = 2"`,
`"fixed path" performance`, `"path length" filter`, and
`nodes(p) performance`.

## Previously submitted Apache AGE issues

The checked submitted issues cover NULL contradiction, property indexes,
shortest path/VLE, OPTIONAL MATCH namespace handling, single-node label
filtering, SET update caching, and relationship-type alternation. None adds a
constant length guard to a fixed path.
