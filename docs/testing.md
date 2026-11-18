# Testing and validation

GraphSymPerf provides offline generation checks, database interface checks and query-pair experiments. Install the dependencies described in the [README](../README.md) and run the commands below from the repository directory. Database checks replace the configured experiment graph; select a dedicated database using the [backend setup guide](backends.md).

## Offline checks

Check Python syntax:

```sh
python -m compileall -q main.py replay.py executor.py cypher gremlin data_structures databases graph_generation scripts
```

Generate the controlled SC1 example:

```sh
python scripts/example_boolean_work_pool.py
```

The script constructs three query pairs with the same host, mutation site, anchor and background graph. Its assertions check the shared inputs and the pattern-specific regions. Output is written to `results/boolean-work-pool-example/`, including the queries, graphs and `comparison.json`.

## Generate a case

Use the default Cypher/Neo4j configuration. Retain the other fields in `config.yaml` and set:

```yaml
run:
  mode: generate
  case_seed: 24
  patterns: [SY4]
  output: results/generation-check
graph:
  background_nodes: 5
  background_relationships: 10
  high_units: 100
```

```sh
python main.py
```

Generation mode constructs the graph, checks query scope and renders both queries without opening a database connection. The run writes `summary.csv` and archives generated cases under the configured output directory. A case archive contains `query_a.cypher`, `query_b.cypher`, `graph.json` and `result.json`. Gremlin archives use the `.gremlin` extension.

## Check query results

Select a database and query language in `config.yaml`, then set `run.mode: check`:

```sh
python main.py
```

The executor loads each graph and compares the complete typed result multisets, including column names and row multiplicities. Matching results produce `equivalent`; differences produce `result_mismatch`. A sampled host with no compatible pattern site produces `skipped`.

## Check database interfaces

After configuring the selected database:

```sh
python scripts/check_interfaces.py
```

The script checks schema creation, loading, typed results, profiling, replay and backend-specific timeout or cancellation behavior. It supports Cypher on Memgraph, ArcadeDB, LadybugDB and FalkorDB, and Gremlin on ArcadeDB, JanusGraph and HugeGraph. For Neo4j, use generated result checks and archive replay.

Reports are written to `<run.output>/interfaces/<system>/report.json`. The [backend guide](backends.md) describes the configuration required by each adapter.

## Measure performance

Set `run.mode: experiment` and `run.save_all: true` to retain all completed cases:

```sh
python main.py
```

The executor compares results, alternates query order during paired measurements, confirms qualifying timing differences and collects native execution profiles. The [architecture guide](architecture.md#8-measurement-failures-and-persistence) defines the measurement thresholds and outcome fields.

Review a performance candidate using its archived inputs, complete results, timing samples and execution plans. Scale comparisons use the same query text and fixed graph structures while changing the pattern's expansion budget. Use native operator counters and plan structure to examine the additional work associated with the timing difference.

For fixed seeds and graph sizes, use the [campaign configuration](architecture.md#10-fixed-seed-campaigns) and run:

```sh
python scripts/run_campaign.py
```

The campaign writes `attempts.json` and `report.json`, including attempted cases, unsubmitted cases and the stopping reason.

## Replay an archive

Set `replay.case` to a case directory in `config.yaml`. Use `replay.check_only: true` for result checking or `false` for measurement:

```sh
python replay.py
```

Replay uses the saved graph, queries, parameters and measurement settings with the configured connection. The selected query language and database system must match the archive. The result is written to `replay-result.json` in the case directory.

## Update generated documentation

Issue summaries, README tables and the complete report index are generated from `bugs.csv`:

```sh
python scripts/update_readme.py
git diff --check
```

Maintain issue classifications, evidence links and local report paths in the registry. The [report guide](bug-reports.md) defines its fields and the report structure. Repeating the generation command with an unchanged registry produces identical documentation.
