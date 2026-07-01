# GraphSymPerf

GraphSymPerf is a research prototype for detecting symmetry-violation performance bugs in graph database management systems.

The v1 workflow uses GDsmith to generate Cypher graph workloads, constructs conservative symmetric query families, validates result equivalence on a real database, then compares `EXPLAIN` and `PROFILE` evidence across the fastest and slowest equivalent variants.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[neo4j,test]"
```

## LDBC Mini Mining

Use the deterministic built-in LDBC mini dataset when you want a non-destructive local workflow before switching to GDsmith.

```bash
export GRAPHSYMPERF_ALLOW_LOAD=1

.venv/bin/python scripts/run_ldbc_mining.py --config config/graph_sym_perf.yaml --systems auto --load --max-cases 20
```

## Real GDsmith Mining

Use a dedicated Neo4j database because GDsmith can rebuild the target graph.

```bash
export GRAPHSYMPERF_NEO4J_URI=bolt://localhost:7687
export GRAPHSYMPERF_NEO4J_USER=neo4j
export GRAPHSYMPERF_NEO4J_PASSWORD=password
export GRAPHSYMPERF_GDSMITH_JAR=/path/to/GDsmith.jar
export GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE=1

.venv/bin/python scripts/run_gdsmith_mining.py --config config/graph_sym_perf.yaml --systems neo4j --max-cases 500 --seed 20260522
```

The run writes `summary.json`, `cases.jsonl`, `symmetry_violations.json`, `review_candidates.md`, query files, equivalence records, plan diffs, and PROFILE summaries under `logs/{run_id}`.

Newer runs also write:

- `pmc_summary.json` / `pmc_summary.md`
- `pmc_evidence.jsonl`
- `feedback_summary.json`
- `rule_stats.json`
- `clustered_summary.json` / `clustered_summary.md`
- `candidate_signatures.jsonl`
- `issue_families.json`
- `unique_issues.json`
- `duplicate_groups.json`

Candidate replay resets the target Neo4j database. Use it only with a disposable database and set `GRAPHSYMPERF_REPLAY_ALLOW_DESTRUCTIVE=1` explicitly.

Additional helper entrypoints are documented in [scripts/README.md](/Users/sakuraq/Documents/科研实习/GraphSymPerf/scripts/README.md).

## Runtime Overrides

The CLI now supports runtime overrides for feedback-guided transform selection and family restriction:

```bash
graph-sym-perf run-gdsmith-mining \
  --config config/graph_sym_perf.yaml \
  --systems neo4j \
  --max-cases 100 \
  --feedback \
  --max-rules-per-case 4 \
  --transform-family symmetry
```

To force the bool-redundancy family on the deterministic LDBC loop:

```bash
graph-sym-perf run-ldbc-mining \
  --config config/graph_sym_perf.yaml \
  --systems auto \
  --max-cases 20 \
  --transform-family bool_redundancy
```

Cluster an existing run directory from the main CLI:

```bash
graph-sym-perf cluster --run-dir logs/<run_id>
```

## Local Multi-DB Stack

This repository now includes a local five-database stack for deterministic smoke runs:

- Neo4j
- Memgraph
- FalkorDB
- ArcadeDB
- Apache AGE

The local environment file is loaded automatically from `.env`. A tracked template is available at [.env.example](/Users/sakuraq/Documents/科研实习/GraphSymPerf/.env.example).

Start or stop the local stack with:

```bash
./scripts/start_local_graphdbs.sh
./scripts/stop_local_graphdbs.sh
```

Verify that all five adapters are locally reachable:

```bash
graph-sym-perf doctor --config config/experiments/local_multidb_smoke.yaml --strict
```

Run a real end-to-end smoke experiment across all five systems:

```bash
graph-sym-perf experiment run \
  --manifest config/experiments/local_multidb_smoke.yaml \
  --shard 0 \
  --num-shards 1
```

The smoke manifest writes artifacts under `runs/local_multidb_smoke_2026_07/shard_000`.

## Experiment Manifests

The repository now includes YAML-driven experiment presets under [config/experiments](/Users/sakuraq/Documents/科研实习/GraphSymPerf/config/experiments).

Preflight a manifest before uploading or launching long server runs:

```bash
graph-sym-perf doctor --config config/experiments/main_effectiveness.yaml
```

Run one shard of a resumable experiment:

```bash
graph-sym-perf experiment run \
  --manifest config/experiments/main_effectiveness.yaml \
  --shard 0 \
  --num-shards 16
```

Aggregate shard outputs after the full sweep finishes:

```bash
graph-sym-perf experiment aggregate --run-root runs/main_effectiveness_2026_01
```

Rank top representatives and replay them:

```bash
graph-sym-perf triage --run-dir runs/main_effectiveness_2026_01 --top 100

graph-sym-perf validate \
  --triage runs/main_effectiveness_2026_01/triage_top100.json \
  --runtime-repeats 15 \
  --reduce
```

Notes:

- Current `source.kind: gdsmith` experiment manifests support `neo4j` targets only, matching the existing GDsmith integration.
- For local multi-database validation, use `config/experiments/local_multidb_smoke.yaml`, which runs the deterministic mini-LDBC loader instead of GDsmith.
- `config/experiments/replay_known.yaml` is a scaffold. Populate `config/experiments/replay_known_cases.jsonl` with real replayable `QueryCase` records before running known-issue replay.
- Shards currently execute serially within one process; large-scale execution is expected to come from launching many shards in parallel on the server.

## Artifact Semantics

- `pmc_summary.*` summarizes plan-level metamorphic coverage over all dynamically equivalent cases.
- `feedback_summary.json` explains which transform rules accrued reward and therefore move earlier in later cases when feedback is enabled.
- `candidate_signatures.jsonl` records stable per-candidate signatures for issue-family clustering and duplicate suppression.
- `clustered_summary.*` reports issue-family counts, unique issues, duplicate groups, and representatives chosen by support strength and ratio evidence.

## Validation

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall src tests
.venv/bin/python scripts/run_gdsmith_mining.py --config config/graph_sym_perf.yaml --systems neo4j --max-cases 1 --run-id no-env-check
```
