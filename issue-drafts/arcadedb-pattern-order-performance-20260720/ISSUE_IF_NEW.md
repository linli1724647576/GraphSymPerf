# Pattern-order performance gap persists after a leading WITH on 26.8.1-SNAPSHOT

### ArcadeDB version

```text
ArcadeDB engine: 26.8.1-SNAPSHOT
Docker image: arcadedata/arcadedb:latest
image digest: sha256:0a7935789d65da3ae5e328c33fc56c3144115d324be14d561967afc65c552a86
image platform: linux/arm64
host: macOS 27.0 arm64
interface: HTTP OpenCypher API
```

### Description

Reordering two independent comma-separated patterns after a leading `WITH` changes ordinary execution time by about 8x, although both queries return the same complete result. This reproduces on `26.8.1-SNAPSHOT`, after the independent-pattern reordering fix reported in #5117 for 26.7.2.

### Steps to reproduce

1. Load the attached `setup.cypher` into a fresh database. It creates 128 nodes and 185 relationships, including 26 `T1` and 40 `T4` relationships.

2. Run query A:

   ```cypher
   WITH 1 AS x
   MATCH (a)-[:T1]->(b), (c)-[:T4]->(d), (n)
   RETURN count(*) AS c;
   ```

3. Run query B, changing only the order of the second and third patterns:

   ```cypher
   WITH 1 AS x
   MATCH (a)-[:T1]->(b), (n), (c)-[:T4]->(d)
   RETURN count(*) AS c;
   ```

The attached `reproducer.py` creates a fresh database, loads the setup, checks the query results, performs five warmups, and collects 20 balanced `AB`/`BA` pairs using ordinary execution.

### Expected behavior

Both queries represent the same natural join of three independent pattern match sets and should have comparable execution time. Both should return:

```text
c = 133120
```

### Actual behavior

Both queries return `c = 133120`, but Query B is consistently slower.

| Metric | Result |
|---|---:|
| Validated paired ratio | 8.15255x |
| Ratio CI lower bound | 7.77419x |
| Validated paired delta | 764.608 ms |
| Delta CI lower bound | 715.539 ms |
| Direction consistency | 1.0000 |

The validation used two independent graph reset/reload/verification sessions, each with five warmups and 20 exactly balanced paired executions. A/A and B/B controls were at most 1.05954x, and complete typed results were equivalent.

Removing the leading `WITH 1 AS x` makes the large order-dependent gap disappear on the same graph. This suggests that the current pattern reordering may not apply on the execution path entered after an existing input row or pipeline boundary.

### Related issue

#5117 described the same broad independent-pattern order family and was closed as fixed in 26.7.2. This report uses a smaller current-version reproducer whose trigger specifically includes a leading `WITH`.
