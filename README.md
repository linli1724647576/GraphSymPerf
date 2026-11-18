# GraphSymPerf

[中文](README.zh-CN.md) · [Documentation](docs/README.md) · [Bug reports](docs/bug-reports.md)

GraphSymPerf tests graph database performance using short-circuit and symmetry transformations. It generates a seed query as an abstract syntax tree (AST), applies a compatible test pattern, and builds a graph for the resulting query pair. It compares complete query results, measures execution times, and collects execution plans to investigate performance differences.

Cypher adapters support Neo4j, Memgraph, ArcadeDB, LadybugDB, and FalkorDB. Gremlin adapters support ArcadeDB, JanusGraph, and HugeGraph. The [pattern catalog](docs/pattern-catalog.en.md) describes the eight short-circuit patterns (SC1–SC8) and eight symmetry patterns (SY1–SY8).

<!-- BEGIN ISSUE SUMMARY -->
The tables below list 48 submitted reports: 25 fixed, 10 confirmed, and 13 pending. Status review dates: 2026-11-18. See the [report index](docs/bug-reports.md) for the complete registry, status evidence, and reproduction reports.
<!-- END ISSUE SUMMARY -->

<!-- BEGIN ISSUE TABLES -->
## Neo4j

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [13783](https://github.com/neo4j/neo4j/issues/13783) | Fixed |
| 2 | [13866](https://github.com/neo4j/neo4j/issues/13866) | Confirmed |
| 3 | [14008](https://github.com/neo4j/neo4j/issues/14008) | Fixed |

## Memgraph

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [4208](https://github.com/memgraph/memgraph/issues/4208) | Confirmed |
| 2 | [4456](https://github.com/memgraph/memgraph/issues/4456) | Pending |
| 3 | [4457](https://github.com/memgraph/memgraph/issues/4457) | Pending |
| 4 | [4458](https://github.com/memgraph/memgraph/issues/4458) | Pending |
| 5 | [4459](https://github.com/memgraph/memgraph/issues/4459) | Fixed |
| 6 | [5091](https://github.com/memgraph/memgraph/issues/5091) | Pending |

## ArcadeDB

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [4405](https://github.com/ArcadeData/arcadedb/issues/4405) | Fixed |
| 2 | [5116](https://github.com/ArcadeData/arcadedb/issues/5116) | Fixed |
| 3 | [5117](https://github.com/ArcadeData/arcadedb/issues/5117) | Fixed |
| 4 | [5360](https://github.com/ArcadeData/arcadedb/issues/5360) | Fixed |
| 5 | [5362](https://github.com/ArcadeData/arcadedb/issues/5362) | Fixed |
| 6 | [5363](https://github.com/ArcadeData/arcadedb/issues/5363) | Fixed |
| 7 | [9579](https://github.com/ArcadeData/arcadedb/issues/9579) | Fixed |
| 8 | [9580](https://github.com/ArcadeData/arcadedb/issues/9580) | Fixed |
| 9 | [10307](https://github.com/ArcadeData/arcadedb/issues/10307) | Fixed |
| 10 | [10308](https://github.com/ArcadeData/arcadedb/issues/10308) | Fixed |
| 11 | [11081](https://github.com/ArcadeData/arcadedb/issues/11081) | Fixed |
| 12 | [11082](https://github.com/ArcadeData/arcadedb/issues/11082) | Fixed |
| 13 | [11083](https://github.com/ArcadeData/arcadedb/issues/11083) | Fixed |

## LadybugDB

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [668](https://github.com/LadybugDB/ladybug/issues/668) | Fixed |
| 2 | [669](https://github.com/LadybugDB/ladybug/issues/669) | Fixed |
| 3 | [696](https://github.com/LadybugDB/ladybug/issues/696) | Fixed |
| 4 | [697](https://github.com/LadybugDB/ladybug/issues/697) | Fixed |
| 5 | [698](https://github.com/LadybugDB/ladybug/issues/698) | Fixed |
| 6 | [720](https://github.com/LadybugDB/ladybug/issues/720) | Fixed |
| 7 | [721](https://github.com/LadybugDB/ladybug/issues/721) | Fixed |
| 8 | [1483](https://github.com/LadybugDB/ladybug/issues/1483) | Fixed |

## FalkorDB

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [2081](https://github.com/FalkorDB/FalkorDB/issues/2081) | Fixed |
| 2 | [3227](https://github.com/FalkorDB/FalkorDB/issues/3227) | Confirmed |
| 3 | [3228](https://github.com/FalkorDB/FalkorDB/issues/3228) | Confirmed |
| 4 | [3315](https://github.com/FalkorDB/FalkorDB/issues/3315) | Confirmed |
| 5 | [3637](https://github.com/FalkorDB/FalkorDB/issues/3637) | Confirmed |

## Apache AGE

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [2480](https://github.com/apache/age/issues/2480) | Confirmed |
| 2 | [2481](https://github.com/apache/age/issues/2481) | Pending |

## JanusGraph

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [5113](https://github.com/JanusGraph/janusgraph/issues/5113) | Pending |
| 2 | [5148](https://github.com/JanusGraph/janusgraph/issues/5148) | Pending |
| 3 | [5147](https://github.com/JanusGraph/janusgraph/issues/5147) | Pending |

## TinkerPop

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [TINKERPOP-3316](https://issues.apache.org/jira/browse/TINKERPOP-3316) | Pending |
| 2 | [TINKERPOP-3315](https://issues.apache.org/jira/browse/TINKERPOP-3315) | Pending |
| 3 | [TINKERPOP-3311](https://issues.apache.org/jira/browse/TINKERPOP-3311) | Confirmed |
| 4 | [TINKERPOP-3312](https://issues.apache.org/jira/browse/TINKERPOP-3312) | Pending |
| 5 | [TINKERPOP-3313](https://issues.apache.org/jira/browse/TINKERPOP-3313) | Confirmed |
| 6 | [TINKERPOP-3314](https://issues.apache.org/jira/browse/TINKERPOP-3314) | Confirmed |

## HugeGraph

| ID | Issue ID | Status |
| --- | --- | --- |
| 1 | [3479](https://github.com/apache/hugegraph/issues/3479) | Pending |
| 2 | [3480](https://github.com/apache/hugegraph/issues/3480) | Pending |
<!-- END ISSUE TABLES -->

## How to Run

### 1. Environment Requirements

Use Python 3.12 and one database from the following configuration examples. Each executed case replaces the selected experiment graph; use a dedicated database.

| Database | Version used in the setup examples | Language |
| --- | --- | --- |
| Neo4j | Community 2026.09.0 | Cypher 25 |
| Memgraph | 3.14.0 | Cypher |
| ArcadeDB | 26.10.2 | Cypher, Gremlin |
| LadybugDB | 0.22.1 | Cypher |
| FalkorDB | 6.0.3 | Cypher |
| JanusGraph | 1.2.0 / TinkerPop 3.8.2 | Gremlin |
| HugeGraph | 1.7.0 / TinkerPop 3.5.1 | Gremlin |

Server startup commands and connection settings are in [Backend setup](docs/backends.md). LadybugDB runs inside the Python process.

### DBMS Connection Parameter Configuration Locations

All run settings are read from [config.yaml](config.yaml) in the current working directory.

| Section | Purpose |
| --- | --- |
| `language`, `database` | Query language, adapter, and connection settings |
| `run` | Run mode, seeds, patterns, duration, and output directory |
| `generation`, `graph` | Query generation and graph size |
| `measurement` | Warmups, paired measurements, thresholds, and query timeout |
| `replay` | Archived case and replay mode |

Password fields such as `password_env` name environment variables. The `config/janusgraph/` and `config/hugegraph/` directories contain server configuration files.

### 2. Install Dependencies

Run from the repository directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

[requirements.txt](requirements.txt) pins the database drivers and configuration parser.

### 3. Running Parameter Configuration

Edit the `run` and `measurement` sections of `config.yaml`:

```yaml
run:
  mode: experiment
  seed: 0
  case_seed: null
  save_all: false
  rounds: 10
  minutes: 60
  output: results
  patterns: [SC1, SC2, SC3, SC4, SC5, SC6, SC7, SC8, SY1, SY2, SY3, SY4, SY5, SY6, SY7, SY8]
measurement:
  warmups: 3
  pairs: 6
  confirmation_warmups: 5
  confirmation_pairs: 20
  ratio: 5.0
  minimum_delta_ms: 100.0
  direction_consistency: 0.8
  scale_factor: 10
  growth_ratio: 3.0
  timeout_seconds: 10
```

| Mode | Behavior |
| --- | --- |
| `generate` | Generate and archive cases without connecting to a database |
| `check` | Load each graph and compare complete query results |
| `experiment` | Check results, measure execution times, and collect execution plans |

`seed` controls the run schedule. Set `case_seed` to try every selected pattern once with the same fixed seed. Each ordinary round attempts every selected pattern. The time budget is checked before starting each case; an active case can finish after that budget.

A timing candidate must meet all three thresholds: runtime ratio, median paired difference, and direction consistency. It then undergoes confirmation with fresh samples. With `scale_factor` enabled, `no_gap` pairs are also tested at a smaller graph size for growth in the timing difference. Set `scale_factor: null` to disable this step. [Architecture](docs/architecture.md) defines the measurements and outcomes.

### 4. Database Configuration

The default configuration uses Neo4j:

```yaml
language: cypher
database:
  system: neo4j
  uri: bolt://127.0.0.1:7687
  user: neo4j
  password_env: GRAPHSYMPERF_NEO4J_PASSWORD
  name: neo4j
  batch_size: 5000
```

```bash
export GRAPHSYMPERF_NEO4J_PASSWORD='your-local-experiment-password'
```

For another database, use its connection block from [Backend setup](docs/backends.md). Set `language: gremlin` for Gremlin runs. Cypher SC7 is available on Neo4j; remove it from `run.patterns` for other Cypher targets. All sixteen patterns are available for Gremlin.

### 5. Query Generation Configuration

```yaml
generation:
  clauses: 4
  nodes: 100
  expression_depth: 3
  subquery_depth: 3
  conditional_queries: true
graph:
  background_nodes: 50
  background_relationships: 100
  high_units: 100000
  indexes: true
```

`generation.nodes` sets the AST expansion budget. `graph.high_units` sets the graph expansion budget for the selected pattern, distributed across its anchors or regions. The resulting graph also includes the background graph and fixed structures required by the pattern. A sampled query with no compatible pattern position is recorded as `skipped`.

### 6. Execution Command

```bash
python main.py
```

To replay an archived case, set `replay.case` to its directory and `replay.check_only` to `true` for result checking or `false` for a performance run:

```bash
python replay.py
```

For fixed seed and graph-size campaigns, follow the [campaign configuration](docs/architecture.md#10-fixed-seed-campaigns), then run:

```bash
python scripts/run_campaign.py
```

After updating [bugs.csv](bugs.csv), regenerate the issue summaries and tables in both READMEs:

```bash
python scripts/update_readme.py
```

### 7. Output Description

| Output | Contents |
| --- | --- |
| `results/<run>/summary.csv` | Case IDs, patterns, seeds, outcomes, graph sizes, and timing summaries |
| `query_a.cypher`, `query_b.cypher` | Archived query pair; Gremlin cases use `.gremlin` |
| `graph.json` | Graph data, schema, and requested indexes |
| `result.json` | Configuration, database version, query parameters, outcome, and measurements |
| `plans.json` | Execution plans and operator summaries |
| `plans-small.json` | Smaller-graph confirmation plans, when collected |
| `replay-result.json` | Result of the most recent replay |
| `attempts.json`, `report.json` | Fixed campaign manifest and completion summary |

Generated cases, performance candidates, result mismatches, timeouts, backend rejections, and execution failures are archived. Set `run.save_all: true` to retain ordinary completed cases as well. A run stops if query termination cannot be confirmed.

The `results/` directory holds local output and is excluded from Git. Verification procedures are described in [Testing and validation](docs/testing.md). Formal reproduction reports are in [bug-reports/](bug-reports/README.md).
