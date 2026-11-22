# Pattern analysis

These cases use short-circuit and symmetry patterns to locate unnecessary work in equivalent graph queries. [Reported issues](readme.md#reported-issues)

## Boolean absorption (SC1, SC2)

FalkorDB changed the starting node of a traversal after Q2 in Listing 1 added a redundant conjunct. The inner `true OR ...` evaluates to true even if its other operand is null, so the conjunct leaves the original predicate unchanged. Both queries returned the same 256 rows on a graph with 128 nodes and 239 relationships. [FalkorDB #2081](https://github.com/FalkorDB/FalkorDB/issues/2081)

**Listing 1.** A true conjunct changes the traversal start in FalkorDB.

```cypher
// Q1
MATCH (n0)<-[r0:T2]-(n1)<-[r1:T5]-(n2:L4), (n3:L1), (n4)
WHERE r0.id > -1 AND r0.id <> r1.id
OPTIONAL MATCH (n0:L1)<-[]-(n1)<-[]-(n2)
WITH min('KeY') AS a0, n4, n0.k9 AS a1, r1.k60 AS a2, max('8C') AS a3
RETURN 3 AS a4;

// Q2
MATCH (n0)<-[r0:T2]-(n1)<-[r1:T5]-(n2:L4), (n3:L1), (n4)
WHERE (r0.id > -1 AND r0.id <> r1.id)
  AND NOT(NOT(true OR coalesce(n0.k9 = n0.k9, true)))
OPTIONAL MATCH (n0:L1)<-[]-(n1)<-[]-(n2)
WITH min('KeY') AS a0, n4, n0.k9 AS a1, r1.k60 AS a2, max('8C') AS a3
RETURN 3 AS a4;

// C-engine PROFILE, starting operator and records produced
// Q1: Node By Label Scan (n2:L4), 143,360 records
// Q2: All Node Scan (n0), 655,360 records
```

In the C engine (v4.20.4), the planner kept the reference to `n0.k9` in the added conjunct. The traversal heuristic treated the reference as a filter on `n0` and chose an unrestricted node scan. The scan counts in Listing 1 include repeated bindings of the same graph nodes. The Rust engine retained the labeled start for both queries. FalkorDB confirmed the fix in the 6.0.x release line. [Upstream reproduction and diagnosis](https://github.com/FalkorDB/FalkorDB/issues/2081#issuecomment-5389634047) · [Fix confirmation](https://github.com/FalkorDB/FalkorDB/issues/2081#issuecomment-6334924271)

## Branch selection and non-null prefixes (SC3, SC4)

LadybugDB planned a correlated count whose value could never reach the output. In Listing 2, the first argument to `COALESCE` is the non-null literal `0`. It fixes the result at the same value returned by Q2. [LadybugDB #1483](https://github.com/LadybugDB/ladybug/issues/1483)

**Listing 2.** A non-null literal fixes the return value before the count is needed.

```cypher
// Q1, median runtime 131.45 ms
MATCH (u:ProbeRoot {uid:1})
RETURN coalesce(0, COUNT {
    MATCH (u)-[:ProbeA]->(m:ProbeMid)-[:ProbeB]->(h:ProbeHub)-[:ProbeC]->(l:ProbeLeaf)
    WHERE m.uid <> l.uid
}) AS value;

// Q2, median runtime 0.18 ms
MATCH (u:ProbeRoot {uid:1})
RETURN 0 AS value;
```

The root reaches one hub through 1,500 middle nodes. Each route to the hub can extend to any of its 1,500 leaves. In LadybugDB 0.21.2, the final relationship scan and inequality filter in Q1 each emitted 2,250,000 tuples. The plan counted those tuples and joined the aggregate back to the root before returning zero. Q2 used a primary-key lookup and projected zero directly. The fix drops the trailing count at binding time, so the scan and aggregate stay out of the plan. [Native profiles](https://github.com/LadybugDB/ladybug/issues/1483) · [LadybugDB PR #1485](https://github.com/LadybugDB/ladybug/pull/1485)

## Empty-result propagation (SC5, SC8)

The `MATCH` clauses in Listing 3 target regions with two and 300,000 qualifying neighbors. Both read-only subqueries retain `LIMIT 0`, so the outer query returns `false` regardless of which region it targets. On ArcadeDB 26.10.2, the larger region still increased the median runtime from 4.42 ms to 174.53 ms. [ArcadeDB #11081](https://github.com/ArcadeData/arcadedb/issues/11081)

**Listing 3.** A read-only `EXISTS` subquery retains `LIMIT 0` as its matched region grows.

```cypher
// Q1
RETURN EXISTS {
  MATCH (r:GspRoot)-[:GSP_LOW]->(n:GspLow)
  WHERE n.keep = true
  RETURN 1 AS item
  LIMIT 0
} AS answer

// Q2
RETURN EXISTS {
  MATCH (r:GspRoot)-[:GSP_HIGH]->(n:GspHigh)
  WHERE n.keep = true
  RETURN 1 AS item
  LIMIT 0
} AS answer
```

Separate profiles place the extra time in the projection evaluating `EXISTS`, which took 1,554 μs in Q1 and 163,331 μs in Q2. With a zero limit, `LimitStep` pulled from its predecessor and drained the returned batch. That path let preceding writes finish. The fix lets the step return an empty stream without pulling input in a read-only statement. [Native profiles](https://github.com/ArcadeData/arcadedb/issues/11081) · [Pre-fix LimitStep](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/query/opencypher/executor/steps/LimitStep.java#L53-L65) · [ArcadeDB PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101)

## Bounded demand and existential witnesses (SC6, SC7)

ArcadeDB built a complete neighbor list for a Gremlin filter that needed only one match. In Listing 4, one relationship type reaches 600,000 qualifying neighbors and the other reaches three. The first neighbor suffices in either traversal. Both queries returned `{"c0":1}` with the integer binding `_gsp_p0=1`. [ArcadeDB #10308](https://github.com/ArcadeData/arcadedb/issues/10308)

**Listing 4.** The filter looks for a witness along either of two relationship types.

```gremlin
// Q1, median runtime 160.21 ms
g.inject(1).V().hasLabel('ScorRoot').has('id', P.eq(_gsp_p0))
    .as('_gsp_v0')
    .filter(__.select('_gsp_v0').outE('GSP_SC_HIGH').inV()
        .hasLabel('ScorTarget').as('_gsp_v1'))
    .project('c0')
    .by(__.select('_gsp_v0').coalesce(__.values('uid'), __.constant(null)));

// Q2, median runtime 6.02 ms
g.inject(1).V().hasLabel('ScorRoot').has('id', P.eq(_gsp_p0))
    .as('_gsp_v0')
    .filter(__.select('_gsp_v0').outE('GSP_SC_REF').inV()
        .hasLabel('ScorTarget').as('_gsp_v1'))
    .project('c0')
    .by(__.select('_gsp_v0').coalesce(__.values('uid'), __.constant(null)));
```

On ArcadeDB 26.10.2 with TinkerPop 3.8.2, `outE().inV()` became a `VertexStep` that emitted one vertex in each profile. In that build, `ArcadeVertex.vertices()` populated the full list before returning its iterator. The cost of obtaining the first match therefore depended on how many neighbors the provider prepared. The median paired Q1/Q2 runtime ratio was 26.66×. The fix retrieves neighbors as the traversal requests them, allowing the filter to stop after the first match. [Native profiles](https://github.com/ArcadeData/arcadedb/issues/10308) · [Pre-fix neighbor iterator](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeVertex.java#L284-L302) · [ArcadeDB PR #10505](https://github.com/ArcadeData/arcadedb/pull/10505)

## Equivalent expressions (SY1, SY2)

The order of two `AND` operands determined whether ArcadeDB could use a property test to avoid a correlated count. The root with `uid=1` had `keep=false` and 400,000 neighbors, none with `mark=true`. Only the root with `uid=2` satisfied both tests, so both queries in Listing 5 returned one row with `c0=2`. [ArcadeDB #9579](https://github.com/ArcadeData/arcadedb/issues/9579)

**Listing 5.** The same property test and correlated count appear in opposite orders.

```cypher
// Q1, median runtime 1.24 ms
MATCH (u:GspAnchor)
WHERE ((coalesce(u.keep, false) = true) AND
  (COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0))
RETURN u.uid AS c0;

// Q2, median runtime 226.69 ms
MATCH (u:GspAnchor)
WHERE ((COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0) AND
  (coalesce(u.keep, false) = true))
RETURN u.uid AS c0;
```

On ArcadeDB 26.10.1-SNAPSHOT, Q1 rejected the root with `uid=1` using its property value. Q2 searched its neighbors to establish that the count was zero, performing more than 800,000 record lookups according to the report. The fix preserved short-circuit evaluation and made `AND` and `OR` try an operand without subquery work first. [Execution evidence](https://github.com/ArcadeData/arcadedb/issues/9579) · [ArcadeDB PR #9583](https://github.com/ArcadeData/arcadedb/pull/9583)

## Endpoint and path orientation (SY3, SY4)

Reversing the written path in Listing 6 changed where Neo4j evaluated the predicate on its nodes. The path is undirected and has the same bound in both queries. The predicate tests every node for `keep=true`, and the projection keeps the endpoint order. Each query returned `(1, 3, 2)`. [Neo4j #14008](https://github.com/neo4j/neo4j/issues/14008)

**Listing 6.** Reversing a bounded path delays the same all-node predicate.

```cypher
// Q1, median runtime 2.70 ms
MATCH p = (u:GspLow {id: 1})-[:GSP_LINK*1..3]-(v:GspHigh)
WHERE all(x IN nodes(p) WHERE (x.keep = true))
RETURN u.uid, v.uid, length(p) AS hops;

// Q2, median runtime 530.10 ms
MATCH p = (v:GspHigh)-[:GSP_LINK*1..3]-(u:GspLow {id: 1})
WHERE all(x IN nodes(p) WHERE (x.keep = true))
RETURN u.uid, v.uid, length(p) AS hops;

// PROFILE, output rows from VarLengthExpand(All) and total DB Hits
// Q1: 2 rows, 18 DB Hits
// Q2: 600,003 rows, 2,700,020 DB Hits
```

On Neo4j Community 2026.09.0, both plans started at the same indexed node. Q1 checked the predicate during expansion and pruned 300,000 two-hop branches beyond a node with `keep=false`. Q2 expanded through that node and filtered the resulting paths afterward. Neo4j closed the report and identified 2026.10 as the expected fix release. [Native profiles](https://github.com/neo4j/neo4j/issues/14008) · [Upstream resolution](https://github.com/neo4j/neo4j/issues/14008#issuecomment-6190652273)

## Matching and branch order (SY5–SY8)

Swapping two `OPTIONAL MATCH` clauses made ArcadeDB repeat a lookup for the same bound root. In Listing 7, each branch reads `r` and binds its own neighbor. The root has two low neighbors and 10,000 high neighbors. Both orders produce the same 20,000 combinations and return `answer=20000`. [ArcadeDB #11083](https://github.com/ArcadeData/arcadedb/issues/11083)

**Listing 7.** Two optional clauses exchange positions while retaining their shared root.

```cypher
// Q1, median runtime 7.31 ms
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
RETURN count(h) AS answer

// Q2, median runtime 784.53 ms
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
RETURN count(h) AS answer
```

On ArcadeDB 26.10.2, the second optional clause's inner root step produced 2 records in Q1 and 10,000 in Q2. In Q2, every incoming row carried the same root, so each lookup scanned that root's relationships for the same two low neighbors. The fix lets a read-only optional clause reuse its previous matches when consecutive rows agree on the bindings it reads. Q2 can then look up the two neighbors once and emit them for every incoming row. [Native profiles](https://github.com/ArcadeData/arcadedb/issues/11083) · [Upstream diagnosis](https://github.com/ArcadeData/arcadedb/issues/11083#issuecomment-6561075709) · [ArcadeDB PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101)
