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

Candidate replay resets the target Neo4j database. Use it only with a disposable database and set `GRAPHSYMPERF_REPLAY_ALLOW_DESTRUCTIVE=1` explicitly.

Additional helper entrypoints are documented in [scripts/README.md](/Users/sakuraq/Documents/科研实习/GraphSymPerf/scripts/README.md).

## Validation

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall src tests
.venv/bin/python scripts/run_gdsmith_mining.py --config config/graph_sym_perf.yaml --systems neo4j --max-cases 1 --run-id no-env-check
```
