# GraphSymPerf

GraphSymPerf tests graph database performance using short-circuit and symmetry patterns. It generates query pairs and graph data, checks complete query results, and compares execution times and native execution plans.

Cypher targets include Neo4j, Memgraph, ArcadeDB, LadybugDB and FalkorDB. Gremlin targets include ArcadeDB, JanusGraph and HugeGraph. Representative cases are described in [Pattern Analysis](pattern_analysis.md).

## Reported issues

The tables list 48 submitted reports: 25 fixed, 10 confirmed and 13 pending.

### Apache AGE

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [2480](https://github.com/apache/age/issues/2480) | Confirmed |
| 2 | [2481](https://github.com/apache/age/issues/2481) | Pending |

### ArcadeDB

| ID | Issue | Status |
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

### FalkorDB

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [2081](https://github.com/FalkorDB/FalkorDB/issues/2081) | Fixed |
| 2 | [3227](https://github.com/FalkorDB/FalkorDB/issues/3227) | Confirmed |
| 3 | [3228](https://github.com/FalkorDB/FalkorDB/issues/3228) | Confirmed |
| 4 | [3315](https://github.com/FalkorDB/FalkorDB/issues/3315) | Confirmed |
| 5 | [3637](https://github.com/FalkorDB/FalkorDB/issues/3637) | Confirmed |

### HugeGraph

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [3479](https://github.com/apache/hugegraph/issues/3479) | Pending |
| 2 | [3480](https://github.com/apache/hugegraph/issues/3480) | Pending |

### JanusGraph

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [5113](https://github.com/JanusGraph/janusgraph/issues/5113) | Pending |
| 2 | [5148](https://github.com/JanusGraph/janusgraph/issues/5148) | Pending |
| 3 | [5147](https://github.com/JanusGraph/janusgraph/issues/5147) | Pending |

### LadybugDB

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [668](https://github.com/LadybugDB/ladybug/issues/668) | Fixed |
| 2 | [669](https://github.com/LadybugDB/ladybug/issues/669) | Fixed |
| 3 | [696](https://github.com/LadybugDB/ladybug/issues/696) | Fixed |
| 4 | [697](https://github.com/LadybugDB/ladybug/issues/697) | Fixed |
| 5 | [698](https://github.com/LadybugDB/ladybug/issues/698) | Fixed |
| 6 | [720](https://github.com/LadybugDB/ladybug/issues/720) | Fixed |
| 7 | [721](https://github.com/LadybugDB/ladybug/issues/721) | Fixed |
| 8 | [1483](https://github.com/LadybugDB/ladybug/issues/1483) | Fixed |

### Memgraph

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [4208](https://github.com/memgraph/memgraph/issues/4208) | Confirmed |
| 2 | [4456](https://github.com/memgraph/memgraph/issues/4456) | Pending |
| 3 | [4457](https://github.com/memgraph/memgraph/issues/4457) | Pending |
| 4 | [4458](https://github.com/memgraph/memgraph/issues/4458) | Pending |
| 5 | [4459](https://github.com/memgraph/memgraph/issues/4459) | Fixed |
| 6 | [5091](https://github.com/memgraph/memgraph/issues/5091) | Pending |

### Neo4j

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [13783](https://github.com/neo4j/neo4j/issues/13783) | Fixed |
| 2 | [13866](https://github.com/neo4j/neo4j/issues/13866) | Confirmed |
| 3 | [14008](https://github.com/neo4j/neo4j/issues/14008) | Fixed |

### TinkerGraph (Apache TinkerPop)

| ID | Issue | Status |
| --- | --- | --- |
| 1 | [TINKERPOP-3316](https://issues.apache.org/jira/browse/TINKERPOP-3316) | Pending |
| 2 | [TINKERPOP-3315](https://issues.apache.org/jira/browse/TINKERPOP-3315) | Pending |
| 3 | [TINKERPOP-3311](https://issues.apache.org/jira/browse/TINKERPOP-3311) | Confirmed |
| 4 | [TINKERPOP-3312](https://issues.apache.org/jira/browse/TINKERPOP-3312) | Pending |
| 5 | [TINKERPOP-3313](https://issues.apache.org/jira/browse/TINKERPOP-3313) | Confirmed |
| 6 | [TINKERPOP-3314](https://issues.apache.org/jira/browse/TINKERPOP-3314) | Confirmed |

## How to Run

### 1. Install dependencies

Use Python 3.12. From the repository directory, run:

```sh
cd src
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the remaining commands from `src/`. The programs read [config.yaml](src/config.yaml) from the current working directory.

### 2. Set up a database

The default configuration uses Neo4j. For a local Docker instance:

```sh
docker run -d --name gsp-neo4j \
  -p 127.0.0.1:7687:7687 \
  -e NEO4J_AUTH=neo4j/change-this-local-password \
  neo4j:2026.09.0
export GRAPHSYMPERF_NEO4J_PASSWORD='change-this-local-password'
```

Use a dedicated experiment database. Each executed or replayed case replaces the configured graph data.

For other systems, configure `language` and `database` using the corresponding [adapter](src/databases/). Server configuration files for JanusGraph and HugeGraph are provided in [src/config/](src/config/). Password fields such as `password_env` specify environment variable names.

### 3. Configure and run

Edit [config.yaml](src/config.yaml), retaining its existing sections. `run` controls patterns, seeds and duration; `generation` and `graph` control query and graph construction; `measurement` controls warmups, samples, thresholds and query timeouts.

| `run.mode` | Behavior |
| --- | --- |
| `generate` | Generate and archive cases without connecting to a database |
| `check` | Load each graph and compare complete query results |
| `experiment` | Check results, measure performance and collect execution plans |

Once the selected database is ready, run:

```sh
python main.py
```

For a reproducible generated case, choose one pattern and set `run.case_seed` to a nonnegative integer. Use `run.mode: generate` to archive the case without connecting to a database.

For non-Neo4j Cypher targets, remove SC7 from `run.patterns`. Gremlin runs require `language: gremlin`; all sixteen patterns are available.

An offline example illustrates three SC1 query constructions:

```sh
python scripts/example_boolean_work_pool.py
```

The example writes query pairs and graph data. Use `main.py` in `generate` mode to create complete archives for replay.

### 4. Replay a case

Set `replay.case` to a case directory containing `result.json`, `graph.json` and the two query files. Select the same query language and database system as the archive. Set `replay.check_only: true` for result checking or `false` for measurement, then run:

```sh
python replay.py
```

Replay uses the archived graph, queries and measurement settings with the current database connection. To reproduce a reported issue, use the data setup and affected database version specified in its linked report.

### 5. Output

Runs are saved under `src/results/` by default:

- `summary.csv`: case identifiers, patterns, seeds, outcomes and timing summaries.
- Case archives: query pairs, `graph.json`, `result.json` and available execution plans in `plans.json`.
- `replay-result.json`: the result of an archive replay.

Set `run.save_all: true` to retain all completed cases.
