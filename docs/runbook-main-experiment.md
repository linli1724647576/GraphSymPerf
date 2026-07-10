# Main experiment runbook

1. Confirm all seven DBMS versions, environment variables, disk capacity, and a clean GDsmith checkout.
2. Run `graphsymperf experiment doctor --config config/experiment.yaml`.
3. Verify `graphsymperf experiment plan --config config/experiment.yaml` prints 112 shards and 112,000 accepted pairs.
4. Run the formal matrix:

```bash
graphsymperf experiment run --config config/experiment.yaml
```

Use `experiment resume --run-id <run-id>` after an interruption. INCOMPLETE shards remain excluded from completed denominators. Validation, reducer and issue evidence are written under the new run ID; no old `runs/` or `logs/` artifact is read.
