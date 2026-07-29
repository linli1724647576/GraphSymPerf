# Deduplication notes

Checked against current Memgraph issues on 2026-07-21.

No issue was found for simplifying an exact `*0..0` path to its zero-hop node
identity.

The closest reports are:

- #2706: a Memgraph 3.0 versus 2.22 regression in a large query containing a
  `*1..6` path and complex sorting.
- #2707: a Memgraph 3.0 versus 2.19 regression in a large query containing
  `*..0` among many optional, write, and path operations.
- #2732: a complex regression containing a `*2..4` path.

This report targets one read-only mandatory `*0..0` pattern on Memgraph 3.10.1.
The expected fix can specialize the zero-hop boundary without addressing the
joins, sorting, writes, or nonzero path expansion in those reports.

The local submitted-issue inventory contains only Memgraph #4208. The earlier
unpublished Memgraph drafts do not cover variable-length path boundaries.

Searches used:

- `0..0 path performance`
- `zero-hop path`
- `zero length path performance`
- `ExpandVariable 0`
- `variable-length 0..0`
