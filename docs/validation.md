# Validation record

This record distinguishes previously completed local evidence from verification performed during the repository audit on 2026-10-31. Raw results live below `results/`, which is ignored by Git. They are available in the working environment but are not included in a clean clone. Use the documented entrypoints and configurations to produce new evidence; do not interpret these tables as CI coverage or exhaustive grammar support.

## Accepted integration matrix

The integration run used seeds 0, 1, 2 and `high_units=100`. It planned 309 attempts across 103 Pattern/language/backend combinations:

- 205 complete equivalent pairs.
- 102 generation-stage skips.
- Two HugeGraph missing-index rejections, archived without adding indexes.
- Every combination had at least one equal pair.
- Eight real archive replays passed.

Local record: `results/integration/completion.json`. Ladybug UNWIND compatibility and ArcadeDB imported-variable scope were corrected during this stage; pre-fix errors remain separate from its final matrix.

## Workload-evidence acceptance

Six generated query pairs were run at 100 and 1,000 expansion units, yielding 12 equal timing cases and 24 native profiles. All timing results were `no_gap`. Case-specific plan reviews supplied examples of observed work, avoided work, unrelated host non-reachability, and insufficient metrics. The review is not a general runtime plan classifier.

Real controls checked check-only/mismatch behavior, missing-index rejection, experimental replay, and a native JanusGraph PROFILE deadline. A test-only 1 ms PROFILE deadline exercised stopping while ordinary queries retained the 10-second limit.

Local record: `results/workload-evidence/completion.json`.

## Fixed-seed calibration

The calibration used seeds 3–7, `high_units=1000`, and a 30-minute budget per language/backend target. No target exhausted the budget and no seed was replaced.

| Target | Planned | Attempted | Equal and measured | Skipped | Error | Unsubmitted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Cypher / Neo4j | 80 | 80 | 36 | 44 | 0 | 0 |
| Cypher / Memgraph | 75 | 73 | 31 | 41 | 1 | 2 |
| Cypher / ArcadeDB | 75 | 75 | 33 | 42 | 0 | 0 |
| Cypher / LadybugDB | 75 | 16 | 9 | 6 | 1 | 59 |
| Cypher / FalkorDB | 75 | 75 | 34 | 41 | 0 | 0 |
| Gremlin / ArcadeDB | 45 | 45 | 21 | 24 | 0 | 0 |
| Gremlin / JanusGraph | 45 | 45 | 21 | 24 | 0 | 0 |
| Gremlin / HugeGraph | 45 | 45 | 21 | 24 | 0 | 0 |
| Total | 515 | 454 | 206 | 246 | 2 | 61 |

There were 205 `no_gap` cases and one candidate. The observed errors were COUNT inside a Memgraph UNWIND expression and Ladybug's missing `toString` function. Their remaining manifest entries were not submitted. No result mismatch, query timeout, or missing-index rejection was observed in this attempted set.

Among 206 completed pairs, 149 had confirmed testing opportunity, nine were blocked by unrelated host conditions, and 48 remained uncertain. Both sides had sufficient observed/eliminated evidence in 52 pairs. Unknown cases are not automatically invalid. Eighty-one of 103 combinations produced equal measured cases; the fixed seeds did not fill all Pattern coverage gaps.

Local records: `results/calibration/report.md`, `summary.csv`, `coverage.csv`, and `completion.json`. The original ledger remains unchanged after subsequent compatibility fixes.

## Compatibility fixes after calibration

Memgraph now excludes subquery-producing work items inside UNWIND. Its original S3/seed-7 host has no remaining compatible site and correctly becomes inapplicable. Memgraph S3/seed-6 passed a fresh real comparison and both profiles with unchanged graph/query inputs.

Ladybug COUNT work uses native CAST to STRING or DOUBLE. Four native conversion checks and four expressions built through the actual work-item function passed. Its original S4/seed-4 case passed real equality and both profiles on the same graph as the archived failure.

An offline comparison preserved graphs, query text, parameters, and columns for 166 unaffected archives. These are offline comparisons, not additional DB executions. The 61 unsubmitted calibration entries were not backfilled.

Local record: `results/compatibility-review/completion.json`.

## Independently reproduced ArcadeDB candidate

Y4/seed-5 was replayed from frozen inputs at N=1,000 in a fresh instance of the same ArcadeDB 26.10.2 image/build with two CPUs and 3 GiB. The independent instance ID differed from calibration. Smaller N=100 and N=300 controls preserved query bytes, parameters, background, fixed witnesses, schema, and indexes. Only expanded branches changed.

| N | Phase reported | A median ms | B median ms | Ratio | Median paired difference ms | Outcome |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 100 | Discovery | 2.292 | 20.726 | 9.04 | 17.895 | no_gap |
| 300 | Confirmation | 5.354 | 137.441 | 25.67 | 130.665 | candidate |
| 1,000 | Independent confirmation | 17.827 | 1,337.838 | 75.05 | 1,317.952 | candidate |

B remained slower with 100% direction consistency. All three pairs returned equal empty results. Native path-step counters were 909/32,118, 2,709/276,318, and 9,009/3,021,018 respectively. These are reported operator counters, not distinct paths or storage accesses.

The review took 60.75 seconds including startup and shutdown, within its 600-second budget. N=300 and N=1,000 belong to the same candidate. Source-level attribution, duplicate checking against historical reports, and any external issue remain pending.

Local record: `results/candidate-review/completion.json`. Fresh N=1,000 measurements are in `frozen-1000/replay-result.json`; its `result.json` is the copied calibration input record.

## Repository audit and commit checks

The code audit found and corrected two recording defects:

1. `main.py` previously raised on an unconfirmed stop before writing the terminal summary row. It now archives and flushes the row before stopping or re-raising the execution failure.
2. The fixed campaign previously let Ctrl-C escape with `stop_reason=completed` and a `running` row. It now records interruption/cancellation details, marks unsubmitted entries with that reason, and exits 130.

Fresh real acceptance verified both changes. The JanusGraph test used a 1 ms native PROFILE deadline solely to reach the stop path and verified the saved `no_gap` CSV row. The ArcadeDB test interrupted an observed active generated Y4 query with SIGINT, retained the native terminated response, and verified that the next seed was unsubmitted. These controls are not performance findings.

The audit compiled 58 Python files and exercised 515 current offline Pattern/seed combinations: 231 generated and 284 were inapplicable. These were not DB executions. Both README issue tables regenerate without changes and contain all 40 ledger URLs. The local-link and ValScope heading/table checks passed, and 53 protected research files retained their byte hashes. Local audit evidence is under `results/repository-audit/`.

The README layout reference is ValScope commit `e158a9d467a343f985427d04624fb44ac7895fa9`: project introduction, per-database ID/Issue ID/Status tables, then How to Run with its configuration/install/generation/execution/output sections. Project-specific database names, languages, paths, and commands are adapted to this repository.
