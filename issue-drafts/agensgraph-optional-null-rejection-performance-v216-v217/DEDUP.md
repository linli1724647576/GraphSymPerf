# Deduplication note

Checked on 2026-07-22 against the local AgensGraph candidate ledger and the
upstream `skaiworldwide-oss/agensgraph` issue tracker.

Search terms included:

- `"OPTIONAL MATCH" performance`
- `OPTIONAL slow`
- `"Hash Left Join"`
- `"Rows Removed by Filter"`
- `"IS NOT NULL"`

No report with the same null-rejecting `OPTIONAL MATCH` trigger appeared in
those searches. The closest upstream reports use different triggers or have a
different symptom:

- [#803](https://github.com/skaiworldwide-oss/agensgraph/issues/803) reports
  `[null]` instead of `[]` when `collect(...)` follows an unmatched
  `OPTIONAL MATCH`. It is a result-correctness issue about aggregation, not a
  performance issue caused by rejecting unmatched rows.
- [#736](https://github.com/skaiworldwide-oss/agensgraph/issues/736) reports
  inconsistent row counts in a complex query containing several optional
  patterns, `UNWIND`, and variable-length relationships. It does not use the
  required-match versus null-rejected-optional transformation.
- [#642](https://github.com/skaiworldwide-oss/agensgraph/issues/642) reports a
  performance regression when an anonymous endpoint causes extra label scans.
  Both queries in this candidate bind the same endpoint variables, and the
  trigger is outer-join elimination rather than anonymous vertices.
- [#493](https://github.com/skaiworldwide-oss/agensgraph/issues/493) reports a
  variable-length path that runs indefinitely. This candidate has one fixed
  relationship and completes normally.

The four earlier local AgensGraph BOOL candidates use boolean-expression
rewrites. The earlier cross-database performance drafts use pattern order,
zero-hop paths, fixed-length paths, relationship-type filters, or aggregate
arguments. None applies a null-rejecting optional-match transformation.

The 2.17.0 release notes mention general planner improvements for
`IS [NOT] NULL`, but the candidate still reproduces on 2.17.0. The current
evidence therefore indicates a separate, still-open optimization case.
