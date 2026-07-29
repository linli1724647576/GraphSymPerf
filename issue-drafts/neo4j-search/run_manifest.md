# Neo4j 2026.06 candidate-screen run manifest

Date: 2026-07-19

## Inputs

- `screen_experimental_rules_20260719.py`
- `screen_scalar_boundaries_20260719.py`
- Neo4j Community 2026.06.0 in two independent Docker processes
- GraphSymPerf typed-result comparator from the current
  `codex/runtime-first-full` worktree at HEAD `189618c`

## Commands

The rule screens were run with the project `src` directory on `PYTHONPATH`, a
Neo4j 2026.06.0 Bolt endpoint, and the password supplied only through the
`NEO4J_PASSWORD` environment variable:

```text
PYTHONPATH=src python3 issue-drafts/neo4j-search/screen_experimental_rules_20260719.py --nodes 1000 --replay-rounds 6 --warmups 1 --repetitions 4
PYTHONPATH=src python3 issue-drafts/neo4j-search/screen_scalar_boundaries_20260719.py --rounds 6
```

Each candidate's `reproducer.py` was then run against both independent Bolt
endpoints. A non-zero assertion exit is the intended red signal: the scripts
assert the documented or metamorphic expected behavior and print the observed
Neo4j result before failing.

## Results

- Experimental screen: 122 cases; 3 eligible deterministic logical leads;
  8 ineligible approximate/NaN observations.
- Scalar screen: 64 cases; 1 eligible deterministic logical lead;
  5 ineligible temporal-canonicalization observations.
- Focused verification: all four reproducers reached their candidate-specific
  assertions on both Neo4j processes; import, authentication, restart, and OOM
  failures were excluded from the accepted evidence.

## Known limitations

- The raw JSONL screen output was retained only as current-session temporary
  evidence; the durable, review-oriented result is `CANDIDATE_SUMMARY.md` plus
  the four focused reproducer scripts.
- The regex candidate depends on the interpretation of Cypher's Unicode
  character contract relative to Java regular-expression behavior.
- The division candidate is not counted as an independent issue family.
