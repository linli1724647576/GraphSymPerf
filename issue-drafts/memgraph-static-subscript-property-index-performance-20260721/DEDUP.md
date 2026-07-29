# Deduplication notes

Checked against current Memgraph issues on 2026-07-21.

No issue was found for a literal subscript property key causing a property
index lookup to become a full node scan.

This report has a different trigger and fix scope from:

- #3199: an `OR` filter does not use an index.
- #2583 and #2665: `IN` predicates and label property index selection.
- #4208: a generated contradictory Boolean branch changes the plan.

The local submitted-issue inventory contains only Memgraph #4208. The two
earlier unpublished Memgraph drafts cover double negation and relationship type
placement, not property access syntax.

Searches used:

- `subscript property index`
- `bracket notation property`
- `dynamic property lookup`
- `property lookup full scan`
