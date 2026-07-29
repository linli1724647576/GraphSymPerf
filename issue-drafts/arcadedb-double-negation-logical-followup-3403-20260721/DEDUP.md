# Deduplication decision

Decision: **post a follow-up on #3403 first; do not immediately create a new
issue**.

#3403 reported that parentheses around `NOT (pattern)` changed results. Its fix,
commit `497efbd64c4b5f7cdef4a389144c475e213fa10e`, changed the ordering of
parenthesized-expression and graph-pattern recognition in
`parseBooleanExpression` and added pattern-predicate regression tests.

The current trigger is distinct in the following ways:

- ordinary scalar Boolean property or constant, with no pattern predicate;
- double negation `NOT NOT P`, not outer parentheses around `NOT (pattern)`;
- projection evaluation is correct, while `WHERE` loses one `NOT`;
- current plan renders `NOT n.flag`, directly exposing the lost operator;
- reproduced on the current image after the #3403 fix.

However, the symptom and parser area overlap enough that a new issue could be
closed as duplicate or incomplete fix coverage. The conservative path is:

1. post `FOLLOWUP_3403.md` and request reopen or maintainer guidance;
2. create a separate issue from `ISSUE.md` if the maintainer asks for separate tracking.

Searches on 2026-07-21 for `double negation WHERE`, `NOT NOT Cypher`, `WHERE
NOT NOT`, `extra parentheses NOT`, and `parenthesized predicate` found no issue
specific to the scalar double-NOT trigger.
