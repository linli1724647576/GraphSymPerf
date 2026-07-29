# Deduplication note

Checked on 2026-07-22 against the local AgensGraph candidate ledger and the
upstream `skaiworldwide-oss/agensgraph` issue tracker.

Search terms included:

- `UNWIND performance`
- `UNWIND slow`
- `ProjectSet`
- `"UNWIND [1]"`
- `"execution time" Cypher`

No report with the same constant singleton `UNWIND` performance trigger
appeared in those searches. The closest upstream reports have different
symptoms:

- [#645](https://github.com/skaiworldwide-oss/agensgraph/issues/645) reports
  wrong or empty results when `UNWIND` expands a node property. This candidate
  returns the correct result and concerns the cost of a constant one-element
  list.
- [#730](https://github.com/skaiworldwide-oss/agensgraph/issues/730) contains
  redundant `UNWIND [1]` clauses in a larger control query, but reports a
  shared-memory allocation error in a different, simpler query. It does not
  report singleton-`UNWIND` performance.
- [#736](https://github.com/skaiworldwide-oss/agensgraph/issues/736) reports
  inconsistent row counts in a complex query combining `UNWIND`, optional
  patterns, and variable-length relationships. This candidate is a two-query
  aggregation pair with equal results.
- [#612](https://github.com/skaiworldwide-oss/agensgraph/issues/612) reports a
  sequence of write queries that ends with a JSONB size error after about 20
  seconds. It does not isolate a constant singleton expansion.

The four earlier local AgensGraph BOOL candidates use boolean-expression
rewrites. The earlier cross-database performance drafts use pattern order,
zero-hop paths, fixed-length paths, relationship-type filters, or aggregate
arguments. None inserts a constant singleton `UNWIND`.

This candidate is therefore related to existing `UNWIND` reports only at the
clause-name level. Its trigger, symptom, and likely fix scope are different.
