# Repeating a bound node in a comma-separated `OPTIONAL MATCH` doubles the result cardinality

I found that adding a redundant node pattern for an already bound variable to
an `OPTIONAL MATCH` changes the result cardinality. On the minimal graph below,
the additional `(n3)` changes `count(*)` from 2 to 4.

Ladybug Version: 0.18.2
Operating System: macOS 27.0 arm64
Installation Method: `pip install ladybug==0.18.2`
API/Driver: Ladybug Python API 0.18.2 / Python 3.12.7

### Steps to reproduce

1. Create two nodes and one relationship:

   ```cypher
   CREATE NODE TABLE L1(id INT64, k11 INT64, PRIMARY KEY(id));
   CREATE NODE TABLE X(id INT64, PRIMARY KEY(id));
   CREATE REL TABLE T1(FROM X TO L1);

   CREATE (:L1 {id: 1, k11: 1});
   CREATE (:X {id: 2});
   MATCH (a:X), (b:L1)
   CREATE (a)-[:T1]->(b);
   ```

2. Run query A:

   ```cypher
   MATCH (n0:L1), (n3)
   OPTIONAL MATCH (n6)-[:T1]->(n0)
   WHERE n0.k11 = 1
   RETURN count(*) AS c;
   ```

3. Run query B, which only repeats the already bound variable `n3` in the
   comma-separated optional pattern:

   ```cypher
   MATCH (n0:L1), (n3)
   OPTIONAL MATCH (n6)-[:T1]->(n0), (n3)
   WHERE n0.k11 = 1
   RETURN count(*) AS c;
   ```

The attached `reproducer.py` creates the database and runs both queries six
times in alternating order.

### Expected behavior

Both queries should return `c = 2`. The first `MATCH` produces two rows because
`n3` ranges over the two nodes. In query B, `(n3)` refers to the variable that
is already bound in each input row, so it should act as an identity constraint
and should not introduce new bindings.

### Actual behavior

Query A consistently returns `c = 2`, while query B consistently returns
`c = 4`. The result was reproduced in two fresh OS processes using independent
database files, with six alternating executions per process.

Removing the redundant `(n3)` restores the expected cardinality. The observed
factor of two equals the number of nodes in this graph, suggesting that `n3`
is scanned and rebound instead of being correlated with its existing binding.

This report is a correctness issue discovered while reducing an equivalence
failure; it is not being counted as a performance finding.
