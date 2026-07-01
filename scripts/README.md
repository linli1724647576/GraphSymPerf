# Scripts

LDBC mini experiment entrypoint:

```bash
.venv/bin/python scripts/run_ldbc_mining.py --config config/graph_sym_perf.yaml --load --max-cases 20
```

GDsmith-backed experiment entrypoint:

```bash
GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE=1 \
.venv/bin/python scripts/run_gdsmith_mining.py --config config/graph_sym_perf.yaml --systems neo4j --max-cases 100 --seed 20260513
```

Cluster candidates from an existing run:

```bash
.venv/bin/python scripts/cluster_candidates.py --run-dir logs/run-20260701T000000Z
```

The main CLI exposes the same operation:

```bash
graph-sym-perf cluster --run-dir logs/run-20260701T000000Z
```

Export manual review candidates from an existing run:

```bash
.venv/bin/python scripts/export_review_candidates.py --run-dir logs/run-20260701T000000Z
```

The scripts record skipped targets in `preflight.json` and exit with code `2` if no real database target is ready.
