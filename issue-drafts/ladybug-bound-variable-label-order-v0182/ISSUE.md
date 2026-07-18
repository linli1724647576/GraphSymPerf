# Reordering comma-separated `MATCH` patterns can ignore a label constraint on a previously bound variable

I found that changing only the order of two comma-separated patterns changes
the result. When an unlabelled node pattern binds a variable first, a label on
the same variable in a later relationship pattern appears not to be enforced.

Ladybug Version: 0.18.2
Operating System: macOS 27.0 arm64
Installation Method: `pip install ladybug==0.18.2`
API/Driver: Ladybug Python API 0.18.2 / Python 3.12.7

### Steps to reproduce

1. Create two source node tables, one empty, one target node table, and one
   relationship from `L1` to `L3`:

   ```cypher
   CREATE NODE TABLE L1(id INT64, PRIMARY KEY(id));
   CREATE NODE TABLE L2(id INT64, PRIMARY KEY(id));
   CREATE NODE TABLE L3(id INT64, PRIMARY KEY(id));
   CREATE REL TABLE T5(FROM L1 TO L3);

   CREATE (:L1 {id: 1});
   CREATE (:L3 {id: 2});
   MATCH (a:L1), (b:L3)
   WHERE a.id = 1 AND b.id = 2
   CREATE (a)-[:T5]->(b);
   ```

   `L2` intentionally remains empty.

2. Run query A:

   ```cypher
   MATCH (n0:L3)<-[:T5]-(n1:L2), (n1)
   RETURN count(*) AS c;
   ```

3. Run query B, which only reverses the order of the two comma-separated
   patterns:

   ```cypher
   MATCH (n1), (n0:L3)<-[:T5]-(n1:L2)
   RETURN count(*) AS c;
   ```

The attached `reproducer.py` creates the database and runs both queries six
times in alternating order.

### Expected behavior

Both queries should return `c = 0`. In both forms, `n1` is constrained by
`(n1:L2)`, but `L2` is empty. Reordering comma-separated patterns should not
change the result.

### Actual behavior

Query A consistently returns `c = 0`, while query B consistently returns
`c = 1`. The result was reproduced in two fresh OS processes using independent
database files, with six alternating executions per process.

As a differential semantic check, Neo4j 2026.05.0 returned `c = 0` for both
query forms on the same graph structure.

The `c = 1` result corresponds to the `L1` source node of `T5`, suggesting that
the later `:L2` constraint is ignored after the earlier unlabelled `(n1)` has
bound the variable.

This report is a correctness issue discovered while screening pattern-order
transformations; it is not being counted as a performance finding.
