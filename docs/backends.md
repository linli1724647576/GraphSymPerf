# Backend setup

These deployment examples use the versions listed below. Run Docker commands from the repository root so mounted configuration files resolve. Replace the `database` block in `config.yaml` and retain the other sections. Use a dedicated experiment graph or database: each executed case replaces its data.

## Capability matrix

| System key | Version | Language | patterns | Relevant differences |
| --- | --- | --- | --- | --- |
| `neo4j` | Community 2026.09.0 | Cypher 25 | SC1–SC8, SY1–SY8 | Full expression subqueries, CALL, conditional queries |
| `memgraph` | 3.14.0 | Cypher | SC1–SC6, SC8, SY1–SY8 | No CALL inside expression subqueries; no subquery work in UNWIND |
| `arcadedb` | 26.10.2 | Cypher | SC1–SC6, SC8, SY1–SY8 | WITH drops unprojected imports; declared result columns |
| `ladybug` | 0.22.1, pybind | Cypher | SC1–SC6, SC8, SY1–SY8 | MATCH-body subqueries, native SC6, single labels, native CAST |
| `falkordb` | 6.0.3 | Cypher | SC1–SC6, SC8, SY1–SY8 | pattern-comprehension work items and native SC6 |
| `arcadedb` | 26.10.2 | Gremlin | SC1–SC8, SY1–SY8 | HTTP result envelope and tagged native cancellation |
| `janusgraph` | 1.2.0 / TP 3.8.2 | Gremlin | SC1–SC8, SY1–SY8 | BerkeleyJE, global typed keys, GraphSON 3 |
| `hugegraph` | 1.7.0 / TP 3.5.1 | Gremlin | SC1–SC8, SY1–SY8 | RocksDB, parent/child edge labels, REST batches |

Remove SC7 for non-Neo4j Cypher targets. For Gremlin set `language: gremlin`; all sixteen patterns are available. Gremlin SY3–SY8 use a TinkerPop `match()` step: SY5–SY8 exchange its two patterns (SY8 with `coalesce` and a sentinel in place of null extension), and SY3/SY4 rewrite a `where()` edge or bounded-walk filter from its other endpoint. The [issue ledger](../bugs.csv) also includes reports for systems outside this adapter list.

## Neo4j

```sh
docker run -d --name gsp-neo4j \
  -p 127.0.0.1:7687:7687 -p 127.0.0.1:7474:7474 \
  -e NEO4J_AUTH=neo4j/change-this-local-password \
  neo4j:2026.09.0
export GRAPHSYMPERF_NEO4J_PASSWORD='change-this-local-password'
```

```yaml
database:
  system: neo4j
  uri: bolt://127.0.0.1:7687
  user: neo4j
  password_env: GRAPHSYMPERF_NEO4J_PASSWORD
  name: neo4j
  batch_size: 5000
```

The adapter prefixes measured Cypher with `CYPHER 25`. It consumes records and the result summary before stopping timing. Requested indexes are awaited after loading.

## Memgraph

```sh
docker run -d --name gsp-memgraph --cpus 2 --memory 2g \
  -p 127.0.0.1:27687:7687 \
  memgraph/memgraph:3.14.0 \
  --query-execution-timeout-sec=0 \
  --storage-automatic-label-index-creation-enabled=false \
  --storage-automatic-edge-type-index-creation-enabled=false
```

```yaml
database:
  system: memgraph
  uri: bolt://127.0.0.1:27687
  user: ""
  password_env: null
  name: memgraph
  batch_size: 5000
```

The connection checks the global timeout, transactional storage mode, and automatic-index flags. Each load removes unrequested temporary loading indexes and runs `ANALYZE GRAPH`. Query-level CALL is supported; expression-subquery CALL and subquery-producing work inside UNWIND are excluded by capabilities. The adapter recognizes native timeouts from their error codes and messages.

## ArcadeDB

```sh
docker run -d --name gsp-arcadedb --cpus 2 --memory 3g \
  -p 127.0.0.1:22480:2480 \
  -e JAVA_OPTS='-Xms256m -Xmx1536m' \
  -e ARCADEDB_SETTINGS='-Darcadedb.server.rootPassword=change-this-local-password -Darcadedb.server.httpQueryMaxResultRows=0' \
  arcadedata/arcadedb:26.10.2
export GRAPHSYMPERF_ARCADEDB_PASSWORD='change-this-local-password'
```

```yaml
database:
  system: arcadedb
  url: http://127.0.0.1:22480
  user: root
  password_env: GRAPHSYMPERF_ARCADEDB_PASSWORD
  name: graphsymperf_arcadedb
  batch_size: 5000
  http_timeout_seconds: 60
```

Select either `language: cypher` or `language: gremlin`. The loader recreates only the named database, declares property types, loads graph data, and prepares requested indexes. HTTP requests use `limit: -1`; a response marked truncated is rejected.

The HTTP result format requires declared columns for empty results and expanded entity rows. Ordinary projection fields are checked against those declarations. Server type hints and native RIDs determine normalization.

Cypher uses the native database deadline. Gremlin traversal consumption uses a timer that requests tagged native cancellation through a separate connection. An unconfirmed cancellation stops the run. PROFILE preserves the query and stores native plan, text, and timing fields.

## LadybugDB

No server process is needed. Install the pinned package through `requirements.txt` and select its pybind backend through the adapter:

```yaml
database:
  system: ladybug
  directory: results/ladybug-work
  memory_mb: 256
  threads: 2
```

The adapter creates a temporary database below `directory` for each case and removes it on close. It declares node and relationship tables from `Graph.schema`, then uses COPY. Python 3.12 `csv.QUOTE_NOTNULL` distinguishes unquoted null fields from quoted empty strings.

Nodes have one label/table. UID primary keys provide the indexes used by this adapter. COUNT scalar work uses `CAST(value, "STRING")` and `CAST(value, "DOUBLE")`. Native query timeouts are separate from PROFILE failures.

## FalkorDB

```sh
docker run -d --name gsp-falkordb --cpus 2 --memory 2g \
  -p 127.0.0.1:16379:6379 falkordb/falkordb:6.0.3
docker exec gsp-falkordb redis-cli GRAPH.CONFIG SET RESULTSET_SIZE -1
docker exec gsp-falkordb redis-cli GRAPH.CONFIG SET TIMEOUT 0
```

```yaml
database:
  system: falkordb
  host: 127.0.0.1
  port: 16379
  name: graphsymperf_experiment
  batch_size: 5000
```

An optional `password_env` names the password variable. The adapter requires an unlimited result set and supplies explicit timeouts to ordinary queries and PROFILE. It replaces the named graph. Boolean work items use pattern comprehensions.

## JanusGraph

```sh
docker run -d --name gsp-janusgraph --cpus 2 --memory 3g \
  -p 127.0.0.1:38182:8182 \
  -e JAVA_OPTIONS='-Xms256m -Xmx1536m' \
  --mount "type=bind,source=$PWD/config/janusgraph/graph.properties,target=/etc/opt/janusgraph/gsp.properties,readonly" \
  --mount "type=bind,source=$PWD/config/janusgraph/gremlin-server.yaml,target=/etc/opt/janusgraph/gsp-server.yaml,readonly" \
  --entrypoint /opt/janusgraph/bin/janusgraph-server.sh \
  janusgraph/janusgraph:1.2.0 /etc/opt/janusgraph/gsp-server.yaml
```

```yaml
database:
  system: janusgraph
  url: http://127.0.0.1:38182
  name: gsp
  traversal_source: gsp_traversal
  graph_config: /etc/opt/janusgraph/gsp.properties
  batch_size: 1000
  http_timeout_seconds: 130
```

`graph_config` is a path inside the server. Each load drops/reopens the configured experiment graph, recreates schema, and registers graph/traversal bindings. SINGLE property keys and MULTI edges preserve scalar types and parallel relationships. Vertex batches return committed IDs for subsequent edge batches.

Fresh requested composite indexes must be ENABLED. The server configuration's default deadline covers management/loading; measured traversals set their own deadline. A recognized native timeout records unconfirmed termination and stops subsequent requests. PROFILE stores decoded TraversalMetrics and raw GraphSON.

## HugeGraph

```sh
docker run -d --name gsp-hugegraph --cpus 2 --memory 3g \
  -p 127.0.0.1:48080:8080 -p 127.0.0.1:48182:8182 \
  -e JAVA_OPTS='-Xms256m -Xmx1536m' \
  --mount "type=bind,source=$PWD/config/hugegraph/graph.properties,target=/hugegraph-server/conf/graphs/hugegraph.properties,readonly" \
  --mount "type=bind,source=$PWD/config/hugegraph/rest-server.properties,target=/hugegraph-server/conf/rest-server.properties,readonly" \
  --mount "type=bind,source=$PWD/config/hugegraph/gremlin-server.yaml,target=/hugegraph-server/conf/gremlin-server.yaml,readonly" \
  hugegraph/hugegraph:1.7.0
```

```yaml
database:
  system: hugegraph
  url: http://127.0.0.1:48182
  rest_url: http://127.0.0.1:48080
  graphspace: DEFAULT
  name: hugegraph
  graph_alias: DEFAULT-hugegraph
  traversal_source: __g_DEFAULT-hugegraph
  batch_size: 500
  http_timeout_seconds: 130
```

Native clear removes data, schema, and indexes for each case. REST batches return native vertex IDs for edge endpoints. Logical relationship types are parent labels; endpoint-specific children store edges. A shared `_gsp_eid` sort key preserves parallel edges across batches. Queries keep parent labels.

Label indexes are enabled. Requested property indexes must reach CREATED before loading. Recognized missing-index errors are archived as `backend_rejected`; no automatic index supplement is added. Gremlin queries and PROFILE use the direct Gremlin HTTP port. The archive records both server request IDs and client-generated IDs. Native timeout termination is unconfirmed, so the run stops.

## Verify the connection

After selecting a dedicated target in `config.yaml`, run:

```sh
python scripts/check_interfaces.py
```

The script checks loading, results, PROFILE and timeout/cancellation behavior for the non-Neo4j Cypher adapters and all three Gremlin targets. For Neo4j, use check mode with `run.save_all: true`, then replay the saved case. The [testing guide](testing.md) covers offline generation, result checks and experimental replay.
