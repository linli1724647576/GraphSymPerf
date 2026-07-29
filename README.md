# GraphSymPerf

GraphSymPerf detects graph-DBMS performance anomalies through certificate-carrying graph-state symmetries and an independent runtime-first validator. GDsmith generates the base graph; GraphSymPerf constructs two involutively isomorphic graph components modulo the reserved replay `id` and derives graph-pattern symmetry pairs from their topology.

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

The formal path excludes Boolean redundancy and general-purpose equivalent-query rewriting. Its current method and feasibility boundaries are documented in [the current implementation outline](doc/method-outline-current-implementation.md).
