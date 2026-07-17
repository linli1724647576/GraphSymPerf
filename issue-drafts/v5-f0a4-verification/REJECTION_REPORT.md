# v5 strong-absolute candidate verification report

## Decision

Do **not** submit this candidate as a Neo4j performance issue.

The query pair is result-equivalent and shows a repeatable positive absolute
runtime delta, but it does not satisfy the fixed issue-grade absolute submission
gate. In particular, both the median ratio and its confidence-interval lower
bound are below the required thresholds. The staged funnel therefore stops at
the independent strict pre-screen; full two-session validation, reduction, and
issue packaging were not run.

## Candidate and provenance

- Historical run: `neo4j-pattern-order-1280-10x1000-20260717-v5`
- Historical pair hash: `f0a4e119b9b4cdef2a12d7d94fcfc988f2ef55d1c28ed565c220b661b257bc26`
- Rule: `pattern_order_symmetry`
- Historical graph: 1,280 nodes and 1,515 relationships
- Verification DBMS: Neo4j `2026.05.0`, isolated from the live v6 experiment
- Rebound verification candidate hash: `a55f2898050da9acb2f633e19eca187f5ccbf9cbff23463a7c48b71294772021`
- Historical A/A noise floor (p99): 4.11416 ms

The candidate hash changes because the independent verification binds a new
DBMS endpoint fingerprint. Consequently, this verification is not counted in
the formal v5 or v6 `validated_runtime_yield`.

## Historical Stage-2 evidence

The v5 Stage-2 wide-recall evidence was sufficient to promote the candidate to
validation, but was not issue-grade evidence:

| Metric | Value |
|---|---:|
| Median A | 13,891.195 ms |
| Median B | 11,903.633 ms |
| Median ratio | 1.13658x |
| Median delta | 1,609.424 ms |
| Ratio CI lower bound | 1.10308x |
| Delta CI lower bound | 1,392.020 ms |
| Direction consistency | 10/12 = 0.8333 |

This passed the Stage-2 **absolute wide-recall** gate. That gate deliberately
has lower recall thresholds and must not be confused with the final submission
gate.

## Independent strict pre-screen

The graph snapshot was reconstructed and verified on an isolated Neo4j
instance. Both queries completed and returned equivalent complete results. The
runtime harness then collected 12 balanced AB/BA pairs (24 timed executions).

| Metric | Observed | Required by absolute path | Result |
|---|---:|---:|---|
| Median A | 44,609.619 ms | - | - |
| Median B | 35,798.553 ms | - | - |
| Median ratio | 1.18768x | >= 1.5x | **fail** |
| Median delta | 5,568.911 ms | >= max(5 x noise, 50 ms) | pass |
| Ratio CI lower bound | 1.12455x | >= 1.2x | **fail** |
| Delta CI lower bound | 3,814.637 ms | > effective noise | pass |
| Direction consistency | 11/12 = 0.9167 | diagnostic | strong |
| Result equivalence | complete and equivalent | required | pass |

The absolute delta is large and its confidence interval is positive, but the
effect is only about 18.8% in ratio terms and its 95% bootstrap lower bound is
about 12.5%. The fixed absolute submission path intentionally still requires a
minimum 1.5x pooled ratio and 1.2x ratio-CI lower bound. This guards against
reporting large millisecond deltas caused by uniformly expensive, noisy
queries.

## Why no issue was written

The final policy is `strong relative path OR strong absolute path`, but the
absolute path is not ratio-free. This candidate fails two mandatory predicates
of that path. Running the more expensive two-session validation cannot repair a
failed strict pre-screen without new evidence, and reduction is only valid after
the original pair passes independent validation. Writing an issue now would
overstate Stage-2 recall evidence as independent issue-grade evidence.

## Evidence files

- `offline_audit.json`: historical provenance, graph integrity, isolated DBMS
  binding, historical Stage-2 samples, and noise floor.
- `screen_12_report.json`: independent equivalence result and all 12 balanced
  runtime pairs.
- `verify_candidate.py`: reproducible verification harness.

## Recommended classification

- `submit_eligible`: `false`
- `validation_stage`: `strict_precheck_rejected`
- `rejection_reasons`:
  - `absolute_pooled_ratio_below_1_5`
  - `absolute_ratio_ci_low_below_1_2`
- Count in v5/v6 `validated_runtime_yield`: **no**
