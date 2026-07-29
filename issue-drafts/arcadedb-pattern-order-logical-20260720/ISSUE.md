# Reordering two comma-separated patterns on the same variable drops a label constraint

### ArcadeDB version

Observed on a freshly pulled `arcadedata/arcadedb:latest` image:

```text
ArcadeDB engine: 26.8.1-SNAPSHOT
image digest: sha256:0a7935789d65da3ae5e328c33fc56c3144115d324be14d561967afc65c552a86
image platform: linux/arm64
host: macOS 27.0 arm64
interface: HTTP OpenCypher API
```

### Description

Reordering two comma-separated node patterns that bind the same variable changes the result. If the labelled pattern appears first, the one labelled vertex is returned. If the bare pattern appears first, the result is empty.

The two forms express the same conjunction of constraints on `n`, so pattern order should not affect the result.

### Steps to reproduce

1. Create a fresh database and add one vertex of type `L5`:

   ```sql
   CREATE VERTEX TYPE L5;
   CREATE VERTEX L5;
   ```

2. Run query A:

   ```cypher
   MATCH (n:L5), (n)
   RETURN count(*) AS c;
   ```

3. Run query B, changing only the order of the two comma-separated patterns:

   ```cypher
   MATCH (n), (n:L5)
   RETURN count(*) AS c;
   ```

The attached `reproducer.py` repeats the pair in alternating `AB`/`BA` order, by default six times in each of three independently created databases.

```bash
python reproducer.py \
  --url http://127.0.0.1:2480 \
  --user root \
  --password '<password>' \
  --cleanup
```

### Expected behavior

Both queries should return:

```text
c = 1
```

Both patterns constrain the same variable `n`; `(n:L5), (n)` and `(n), (n:L5)` should therefore have the same match set.

### Actual behavior

ArcadeDB consistently returns:

```text
query A: c = 1
query B: c = 0
```

I reproduced this in three fresh databases, with six alternating executions per database. The result did not depend on execution order.

### Related issue

#5116 reported a large performance difference for the same general family of shared-variable pattern-order variants and was closed after planner reordering work. This report is a current wrong-result case on `26.8.1-SNAPSHOT`: the result set changes, and the reproducer has only one vertex. I could not find an existing issue with this minimal result mismatch.
