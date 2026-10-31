# GraphSymPerf

GraphSymPerf tests short-circuit and symmetry transformations in graph database management systems. It generates a symbolic query AST, inserts one compatible Pattern, constructs a graph with fixed witnesses and expandable regions, and compares two queries on that graph. Cypher targets are Neo4j, Memgraph, ArcadeDB, LadybugDB, and FalkorDB; Gremlin targets are ArcadeDB, JanusGraph, and HugeGraph.

<!-- BEGIN ISSUE SUMMARY -->
This README lists 33 submitted issue URLs: 21 fixed, 6 confirmed, 6 pending. Statuses are the recorded snapshot (2026-10-29), not a new upstream audit. Historical reports and merged issues are included; these totals are not unique root causes or bugs discovered by the current rewrite.
<!-- END ISSUE SUMMARY -->

The tables below are generated from [bugs.csv](bugs.csv), the maintained issue ledger. See the [code architecture](docs/architecture.md), [backend setup](docs/backends.md), and [validation record](docs/validation.md) for implementation details. [中文说明](README.zh-CN.md) · [中文架构文档](docs/architecture.zh-CN.md). This README follows the section order and table format of [ValScope](https://github.com/linli1724647576/ValScope/blob/e158a9d467a343f985427d04624fb44ac7895fa9/README.md).

<!-- BEGIN ISSUE TABLES -->
## Neo4j

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [13783](https://github.com/neo4j/neo4j/issues/13783) | fixed |
| 2 | [13866](https://github.com/neo4j/neo4j/issues/13866) | confirmed |
| 3 | [14008](https://github.com/neo4j/neo4j/issues/14008) | fixed |

## Memgraph

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [4208](https://github.com/memgraph/memgraph/issues/4208) | confirmed |
| 2 | [4456](https://github.com/memgraph/memgraph/issues/4456) | pending |
| 3 | [4457](https://github.com/memgraph/memgraph/issues/4457) | pending |
| 4 | [4458](https://github.com/memgraph/memgraph/issues/4458) | pending |
| 5 | [4459](https://github.com/memgraph/memgraph/issues/4459) | fixed |
| 6 | [5091](https://github.com/memgraph/memgraph/issues/5091) | pending |

## ArcadeDB

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [4405](https://github.com/ArcadeData/arcadedb/issues/4405) | fixed |
| 2 | [5116](https://github.com/ArcadeData/arcadedb/issues/5116) | fixed |
| 3 | [5117](https://github.com/ArcadeData/arcadedb/issues/5117) | fixed |
| 4 | [5360](https://github.com/ArcadeData/arcadedb/issues/5360) | fixed |
| 5 | [5362](https://github.com/ArcadeData/arcadedb/issues/5362) | fixed |
| 6 | [5363](https://github.com/ArcadeData/arcadedb/issues/5363) | fixed |
| 7 | [9579](https://github.com/ArcadeData/arcadedb/issues/9579) | fixed |
| 8 | [9580](https://github.com/ArcadeData/arcadedb/issues/9580) | fixed |
| 9 | [10307](https://github.com/ArcadeData/arcadedb/issues/10307) | fixed |
| 10 | [10308](https://github.com/ArcadeData/arcadedb/issues/10308) | fixed |

## LadybugDB

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [668](https://github.com/LadybugDB/ladybug/issues/668) | fixed |
| 2 | [669](https://github.com/LadybugDB/ladybug/issues/669) | fixed |
| 3 | [696](https://github.com/LadybugDB/ladybug/issues/696) | fixed |
| 4 | [697](https://github.com/LadybugDB/ladybug/issues/697) | fixed |
| 5 | [698](https://github.com/LadybugDB/ladybug/issues/698) | fixed |
| 6 | [720](https://github.com/LadybugDB/ladybug/issues/720) | fixed |
| 7 | [721](https://github.com/LadybugDB/ladybug/issues/721) | fixed |

## FalkorDB

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [2081](https://github.com/FalkorDB/FalkorDB/issues/2081) | fixed |
| 2 | [3227](https://github.com/FalkorDB/FalkorDB/issues/3227) | confirmed |
| 3 | [3228](https://github.com/FalkorDB/FalkorDB/issues/3228) | confirmed |
| 4 | [3315](https://github.com/FalkorDB/FalkorDB/issues/3315) | confirmed |

## Apache AGE

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [2480](https://github.com/apache/age/issues/2480) | confirmed |
| 2 | [2481](https://github.com/apache/age/issues/2481) | pending |

## JanusGraph

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [5113](https://github.com/JanusGraph/janusgraph/issues/5113) | pending |
<!-- END ISSUE TABLES -->

## How to Run

### 1. Environment Requirements
- Python 3.12; Ladybug CSV loading uses `csv.QUOTE_NOTNULL`.
- One configured database system:
  - Neo4j Community 2026.09.0, Cypher 25 (default configuration: port 7687).
  - Memgraph 3.14.0 (local example: port 27687).
  - ArcadeDB 26.10.2, Cypher or Gremlin (local example: port 22480).
  - LadybugDB 0.22.1, embedded pybind backend.
  - FalkorDB 6.0.3 (local example: port 16379).
  - JanusGraph 1.2.0 / TinkerPop 3.8.2, BerkeleyJE (local example: port 38182).
  - HugeGraph 1.7.0 / TinkerPop 3.5.1, RocksDB (local examples: ports 48080 and 48182).

### DBMS Connection Parameter Configuration Locations

Connection settings are read from the `database` section of [config.yaml](config.yaml). Password fields name environment variables; password values are not stored in case archives.

Specific configuration locations:

- **Main configuration file**: `config.yaml`
  - `language` selects `cypher` or `gremlin`.
  - `database` selects the adapter and its connection settings.
  - `generation`, `graph`, `run`, and `measurement` control experiments.
- **Database adapters**: `databases/`
  - `databases/__init__.py` selects the adapter.
  - Adapter capability dictionaries select compatible grammar and Pattern variants.
- **Server configuration**: `config/janusgraph/` and `config/hugegraph/`
  - These files configure the corresponding Gremlin services.
  - [Backend setup](docs/backends.md) provides complete connection blocks and startup commands.

### 2. Install Dependencies

Create a Python environment and install the pinned dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Main dependencies include:
- `neo4j==5.28.4`: Neo4j and Memgraph Bolt connections.
- `PyYAML==6.0.3`: configuration loading.
- `ladybug==0.22.1`: embedded LadybugDB.
- `falkordb==1.8.1` and `redis==8.1.0`: FalkorDB connections.

ArcadeDB, JanusGraph, and HugeGraph adapters use Python's standard HTTP libraries.

### 3. Running Parameter Configuration

Edit these fields in `config.yaml`, retaining the other configuration sections:

```yaml
language: cypher
run:
  mode: experiment
  seed: 0
  case_seed: null
  save_all: false
  rounds: 10
  minutes: 60
  output: results
  patterns: [S1, S2, S3, S4, S5, S6, S7, S8, Y1, Y2, Y3, Y4, Y5, Y6, Y7, Y8]
measurement:
  warmups: 3
  pairs: 6
  confirmation_warmups: 5
  confirmation_pairs: 20
  ratio: 5.0
  minimum_delta_ms: 100.0
  direction_consistency: 0.8
  timeout_seconds: 10
```

`generate` saves cases without opening a database connection. `check` compares complete results. `experiment` also measures performance and collects PROFILE. A fixed `case_seed` selects one seed and one round. Cypher S7 is enabled only for Neo4j. Gremlin supports S1–S6, S8, Y1, and Y2; configure that subset when selecting Gremlin.

### 4. Database Configuration

The default connection block targets a dedicated Neo4j database:

```yaml
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

Each executed case replaces the selected experiment graph. Q1 and Q2 share unchanged graph data and indexes. Select the corresponding connection block in [backend setup](docs/backends.md) when using another system.

### 5. Query Generation Configuration

Configure the random host and graph expansion in `config.yaml`:

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

`generation.nodes` is a grammar expansion budget. `high_units` is the total Pattern expansion budget, divided across anchors or regions; it is not the total number of graph nodes. The generator permits incompatible or unproductive random contexts. Such contexts are reported rather than repaired by a solver. [Architecture](docs/architecture.md) explains the grammar, scope rules, and graph recipes.

### 6. Execution Command

```bash
python main.py
```

For replay, set `replay.case` to an archive directory and `replay.check_only` to the required mode, then run:

```bash
python replay.py
```

For a fixed seed/scale campaign, add the `campaign` settings described in [architecture](docs/architecture.md#10-fixed-seed-campaigns), set `run.save_all: true` and a new output directory, then run:

```bash
python scripts/run_campaign.py
```

After editing the submitted-issue ledger, regenerate the README tables:

```bash
python scripts/update_readme.py
```

### 7. Output Description
- **`results/<run>/summary.csv`**:
  - Case IDs, Patterns, seeds, outcomes, graph sizes, and available timing summaries.
- **Archived case directories**:
  - `query_a.cypher` / `query_b.cypher`, or `.gremlin` equivalents.
  - `graph.json` — actual graph, schema, and requested indexes.
  - `result.json` — configuration, bindings, workload metadata, outcome, and timings.
  - `plans.json` — native PROFILE results and any recognized PROFILE failure.
  - `replay-result.json` — a new replay result when replay is requested.
- **Fixed campaign output**:
  - `attempts.json` — the complete fixed manifest, including unsubmitted cases.
  - `report.json` — stopping reason, counts, wall time, and client CPU/peak RSS.
- **Local evidence**:
  - `results/` is ignored by Git. The [validation record](docs/validation.md) distinguishes recorded local runs from shipped code and new verification.

Candidates, mismatches, query timeouts, recognized backend rejections, unexpected execution failures, and unconfirmed-stop cases are archived. Use `run.save_all: true` to retain ordinary cases too. An unconfirmed termination stops further requests without restarting the database.
