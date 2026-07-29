I can still reproduce a smaller form of this issue on the current `arcadedata/arcadedb:latest` image after the reported 26.7.2 fix.

### Current version

```text
ArcadeDB engine: 26.8.1-SNAPSHOT
image digest: sha256:0a7935789d65da3ae5e328c33fc56c3144115d324be14d561967afc65c552a86
image platform: linux/arm64
interface: HTTP OpenCypher API
```

### Minimized query pair

Fast form:

```cypher
WITH 1 AS x
MATCH (a)-[:T1]->(b), (c)-[:T4]->(d), (n)
RETURN count(*) AS c;
```

Slow reordered form:

```cypher
WITH 1 AS x
MATCH (a)-[:T1]->(b), (n), (c)-[:T4]->(d)
RETURN count(*) AS c;
```

Only the order of the second and third independent patterns changes. Both queries return the same complete result:

```text
c = 133120
```

The attached graph has 128 nodes and 185 relationships, including 26 `T1` relationships and 40 `T4` relationships. Thus the count is also independently checkable as `26 * 40 * 128 = 133120`.

### Measurements

I reloaded and verified the graph in two independent sessions. Each session used five warmups and 20 exactly balanced `AB`/`BA` paired ordinary executions; `PROFILE` was not used for timing.

| Metric | Session 1 | Session 2 | Pooled |
|---|---:|---:|---:|
| Query A median | 100.152 ms | 107.179 ms | - |
| Query B median | 888.916 ms | 873.084 ms | - |
| Paired ratio | 8.25605x | 7.85645x | 8.15255x |
| Ratio CI lower bound | 8.01672x | 7.30534x | 7.77419x |
| Paired delta | 784.561 ms | 759.717 ms | 764.608 ms |
| Delta CI lower bound | 664.954 ms | 707.742 ms | 715.539 ms |
| Direction consistency | 1.0000 | 1.0000 | 1.0000 |

The A/A and B/B same-query controls were at most 1.05954x. Complete typed results were equivalent in both sessions.

One useful boundary is that removing the leading `WITH 1 AS x` makes this large order-dependent gap disappear on the same graph. That suggests the 26.7.2 reordering fix may not cover the execution path entered after an existing input row/pipeline boundary.

Would you prefer this issue to be reopened, or should I file the minimized current-version case as a separate regression/fix-coverage issue? I can attach the graph setup, the two query files, the standalone balanced-timing reproducer, and the full two-session validation JSON.
