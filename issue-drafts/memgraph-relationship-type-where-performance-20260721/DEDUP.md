# Deduplication notes
Checked against current Memgraph issues on 2026-07-21.

- #2164 concerns an unnamed relationship and an edge property index on
  Memgraph 2.18. This candidate keeps `r`, uses no edge property index, and
  tests `WHERE type(r) = 'LINK'`.
- #2177 concerns wrong path results with edge type property indexes. This
  candidate returns correct results and tests ordinary relationship expansion.
- #662 requested edge property indexes. This candidate uses only a relationship
  type.

Searches for `type(r) index`, `relationship type WHERE index`, `edge type
WHERE type`, and `type relationship ScanAllByEdgeType` found no issue with
the same trigger.

Links:

- https://github.com/memgraph/memgraph/issues/2164
- https://github.com/memgraph/memgraph/issues/2177
- https://github.com/memgraph/memgraph/issues/662
