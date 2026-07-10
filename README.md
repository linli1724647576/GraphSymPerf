# GraphSymPerf

GraphSymPerf detects graph-DBMS performance anomalies through GDsmith-generated, safety-certified equivalent query pairs and an independent runtime-first validator.

## Formal experiment

```bash
graphsymperf experiment doctor --config config/experiment.yaml
graphsymperf experiment plan --config config/experiment.yaml
graphsymperf experiment run --config config/experiment.yaml
```

The only formal configuration is `config/experiment.yaml`: seven GDBMSs, sixteen shards per system and 1,000 accepted query pairs per shard. A formal run requires a clean GDsmith checkout and all required DBMS environment variables. Resume and status use immutable run IDs:

```bash
graphsymperf experiment resume --run-id <run-id>
graphsymperf experiment status --run-id <run-id>
```

Runtime candidates are independently validated, reduced, and exported only after `REDUCED_VALID` plus a CORE SafetyCertificate. See [validation](docs/validation.md), [reducer](docs/reducer.md), [issue lifecycle](docs/issue-lifecycle.md), and [operations](docs/operations.md).
