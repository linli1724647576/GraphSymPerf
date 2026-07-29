# AgensGraph rule-family search summary

Search date: 2026-07-22

## Promoted candidates

| Rule | Type | Result | AgensGraph 2.16 evidence |
|---|---|---|---|
| `optional_null_rejection_symmetry` | SYM performance | Promoted | 2.13x-2.20x slower on two independent instances |
| `singleton_unwind_symmetry` | SYM performance | Promoted | 1.76x-1.79x slower on two independent instances |

Both candidates returned the expected result in every run. Each instance used
12 alternating A/B comparisons plus A/A and B/B controls. Both also reproduced
on the current AgensGraph 2.17.0 image; that image was run under amd64 emulation
and was used only as a current-version check.

## Strong results not promoted

The first screen also found large gaps for these transformations:

- exact zero-hop path versus a bound-node identity
- fixed one-hop relationship versus `*1..1`
- typed relationship pattern versus `type(r)` filtering

They were not promoted because the same rule families had already been used in
earlier database campaigns. A new scale or a different DBMS is not enough to
make them new issues.

## Added rule families that did not pass

The AgensGraph screen added constant projection, aggregate staging, distinct
staging, property-alias filtering, comparison commutation, count-property
filtering, minimum-versus-sort, bound-node matching, disjoint `CASE`, mixed
numeric `UNION`, and aggregate-input-order transformations. None produced a
logical mismatch or a large, stable performance gap in this screen.

The four earlier AgensGraph BOOL candidates were excluded before screening:

- `and_excluded_middle_or_null`
- `and_expr_null_partition`
- `and_case_true`
- `and_distributive_absorption`

They remain internal candidates and are not counted as new AgensGraph issues.

## Evidence files

- `additional-rule-screen-v216-n10000-20260722.jsonl`
- `new-rule-validation-instance-a-v216-20260722.json`
- `new-rule-validation-instance-b-v216-20260722.json`
- `new-rule-plancheck-v217-amd64-emulated-20260722.json`

No new logical issue passed validation in this round.
