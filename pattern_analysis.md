# Pattern analysis

The following cases show how short-circuit and symmetry patterns expose unnecessary work in graph databases. Each analysis identifies the query change, the resulting execution difference and the reported optimization.

- [SC1: A redundant true guard causes an unrestricted node scan](#sc1-a-redundant-true-guard-causes-an-unrestricted-node-scan)
- [SC2: A false guard fails to skip a large neighbor count](#sc2-a-false-guard-fails-to-skip-a-large-neighbor-count)
- [SC4: COALESCE evaluates a COUNT after its result is fixed](#sc4-coalesce-evaluates-a-count-after-its-result-is-fixed)
- [SC5: LIMIT 0 still pulls graph data](#sc5-limit-0-still-pulls-graph-data)
- [SC6: LIMIT 3 arrives after CALL and UNION materialization](#sc6-limit-3-arrives-after-call-and-union-materialization)
- [SC7: An existence test waits for the entire neighbor list](#sc7-an-existence-test-waits-for-the-entire-neighbor-list)
- [SC8: A NULL contradiction fails to eliminate a scan](#sc8-a-null-contradiction-fails-to-eliminate-a-scan)
- [SY1: Swapping AND operands changes COUNT evaluation](#sy1-swapping-and-operands-changes-count-evaluation)
- [SY2: A redundant OR branch repeats a label scan](#sy2-a-redundant-or-branch-repeats-a-label-scan)
- [SY4: Path reversal changes early rejection](#sy4-path-reversal-changes-early-rejection)
- [SY5: Independent MATCH order repeats a traversal](#sy5-independent-match-order-repeats-a-traversal)
- [SY6: A shared node is bound before its labels are checked](#sy6-a-shared-node-is-bound-before-its-labels-are-checked)
- [SY7: Repeated roots rerun a later OPTIONAL MATCH](#sy7-repeated-roots-rerun-a-later-optional-match)
- [SY8: Reusing optional matches preserves all combinations](#sy8-reusing-optional-matches-preserves-all-combinations)

## SC1: A redundant true guard causes an unrestricted node scan

FalkorDB [#2081](https://github.com/FalkorDB/FalkorDB/issues/2081) showed how an irrelevant Boolean expression could change the starting node of a traversal. The added conjunct, `NOT(NOT(true OR expr))`, is true even when `expr` is null. It therefore leaves the original predicate unchanged. Nevertheless, its reference to `n0.k9` made the planner choose an unrestricted scan of `n0` instead of a label scan of `n2:L4`. Listing 1 shows the changed part of the queries; their optional matching and aggregation suffix was identical.

**Listing 1.** A true guard leaves a node-property reference in the transformed predicate.

```cypher
// Base query excerpt
MATCH (n0)<-[r0 :T2]-(n1)<-[r1 :T5]-(n2 :L4), (n3 :L1), (n4)
WHERE (((r0.id) > -1) AND ((r0.id) <> (r1.id)))

// Transformed query excerpt
MATCH (n0)<-[r0 :T2]-(n1)<-[r1 :T5]-(n2 :L4), (n3 :L1), (n4)
WHERE ((((r0.id) > -1) AND ((r0.id) <> (r1.id))))
  AND NOT(NOT(true OR (coalesce(n0.k9 = n0.k9, true))))
```

The graph contained 128 vertices and 239 relationships, and both queries returned the same 256 records. In the contributor's reproduction on the C engine, version 4.20.4, the anchor scan produced 143,360 records for the base query and 655,360 for the transformed query. These were intermediate records, including repeated bindings of the same graph nodes. The added filter reported 179,200 output records. The [upstream diagnosis](https://github.com/FalkorDB/FalkorDB/issues/2081#issuecomment-5389634047) identified the cause: the traversal heuristic treated the surviving property reference as a filter on `n0` and selected it as the starting node.

```text
Base C-engine anchor:
  Node By Label Scan | (n2:L4) | Records produced: 143360

Transformed C-engine anchor:
  All Node Scan | (n0) | Records produced: 655360
```

Eliminating the true conjunct removes that reference before anchor selection. The contributor reported identical plans on Rust `main`, with the labeled starting node retained for both queries. A [subsequent reply](https://github.com/FalkorDB/FalkorDB/issues/2081#issuecomment-6334924271) confirmed the fix in the 6.0.x release line and recommended 6.0.2.

## SC2: A false guard fails to skip a large neighbor count

Counting 400,000 neighbors was unnecessary for the anchor whose `keep` property was false in ArcadeDB [#9579](https://github.com/ArcadeData/arcadedb/issues/9579). The graph had two anchors. Anchor 1 had `keep=false`, and none of its 400,000 neighbors passed `mark=true`. Anchor 2 had `keep=true` and one qualifying neighbor. Both queries in Listing 2 therefore returned only `c0=2`. Checking `keep` first rejected anchor 1 immediately; evaluating its count first required examining all of those neighbors to establish a zero count.

**Listing 2.** The property test precedes or follows the correlated count.

```cypher
// Q1: guard first
MATCH (u:GspAnchor)
WHERE ((coalesce(u.keep, false) = true) AND
  (COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0))
RETURN u.uid AS c0;

// Q2: COUNT first
MATCH (u:GspAnchor)
WHERE ((COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0) AND
  (coalesce(u.keep, false) = true))
RETURN u.uid AS c0;
```

On ArcadeDB 26.10.1-SNAPSHOT, the ordinary runtime medians were **1.24 ms** for Q1 and **226.69 ms** for Q2. The median of the paired Q2/Q1 ratios was **191.63×**. Separate native profiler windows recorded 10 record lookups for Q1 and 800,411 for Q2, including cached lookups. The one-row output in both plans did not reveal the work performed inside the count.

The evaluator originally followed the written operand order. Merged [PR #9583](https://github.com/ArcadeData/arcadedb/pull/9583) made `AND` and `OR` try an operand without a subquery first. For anchor 1, the property test could then determine the result before counting began. The [maintainer confirmed](https://github.com/ArcadeData/arcadedb/issues/9579#issuecomment-6088262423) the repair in 26.11.1.

## SC4: COALESCE evaluates a COUNT after its result is fixed

The first argument `0` already determined the result of `COALESCE` in LadybugDB [#1483](https://github.com/LadybugDB/ladybug/issues/1483), but the engine still executed the later `COUNT`. The graph connected one root to 1,500 middle nodes, each leading to the same hub. The hub connected to 1,500 leaves. This produced 1,500 × 1,500 = 2,250,000 three-hop paths, all satisfying the identifier inequality. Both queries in Listing 3 returned one integer `value=0`; only Q1 contained the unused traversal.

**Listing 3.** A non-NULL constant makes the following COUNT unnecessary.

```cypher
// Q1
MATCH (u:ProbeRoot {uid:1})
RETURN coalesce(0, COUNT {
    MATCH (u)-[:ProbeA]->(m:ProbeMid)-[:ProbeB]->(h:ProbeHub)-[:ProbeC]->(l:ProbeLeaf)
    WHERE m.uid <> l.uid
}) AS value;

// Q2
MATCH (u:ProbeRoot {uid:1})
RETURN 0 AS value;
```

On LadybugDB 0.21.2, Q1 and Q2 had ordinary runtime medians of **131.45 ms** and **0.18 ms**. The median paired Q1/Q2 ratio was **744.88×**. Separately collected profiles showed 2,250,000 output tuples at Q1's `ProbeC` scan and inequality filter. The aggregate count was then joined back to the root before the projection returned zero. Q2 instead used a primary-key scan followed by a constant projection.

```text
PROFILE excerpt; NumOutputTuples
Q1:
FILTER[17]              2250000  NOT_EQUALS(m.uid)
SCAN_REL_TABLE[7]      2250000  Tables: ProbeC, Direction: (h)-[]->(l)
AGGREGATE_SCAN[20]           1  COUNT_STAR(), Group By: u._ID
Q2:
PRIMARY_KEY_SCAN_NODE_TABLE[0]  1  Key: 1, Alias: u, Index: HASH
```

Merged [PR #1485](https://github.com/LadybugDB/ladybug/pull/1485) removed `COALESCE` and `IFNULL` arguments after the first non-NULL literal during binding. Folding the remaining expression to a literal prevented the unused traversal and aggregate from entering the execution plan.

## SC5: LIMIT 0 still pulls graph data

A zero limit should stop a read-only producer before it reads graph data. In ArcadeDB [#11081](https://github.com/ArcadeData/arcadedb/issues/11081), however, an `EXISTS` subquery consumed its input despite `LIMIT 0`. The root had two `GSP_LOW` neighbors and 300,000 `GSP_HIGH` neighbors, all satisfying `keep=true`. Both subqueries in Listing 4 returned no rows, so both outer queries returned the single Boolean `answer=false`. Changing the matched region could not change that result.

**Listing 4.** The zero limit is unchanged while the matched region grows.

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

On ArcadeDB 26.10.2, ordinary runtime medians were **4.42 ms** for Q1 and **174.53 ms** for Q2. The ratio of their unrounded medians was **39.49×**. In the measured build, [`LimitStep`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/query/opencypher/executor/steps/LimitStep.java#L61-L69) still pulled its previous step and drained the returned batch. Separate profiles attributed 1,554 μs to Q1's projection evaluating `EXISTS`, compared with 163,331 μs in Q2.

```text
Native PROFILE excerpt; operator durations in microseconds
Q1: PROJECT RETURN answer (1,554μs, 1 rows)
Q2: PROJECT RETURN answer (163,331μs, 1 rows)
```

The drain was needed for writes preceding a zero limit, but the same path also handled these read-only subqueries. Merged [PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101) passed the statement's read-only status to `LimitStep`. It could then return an empty result immediately without pulling upstream input. The [maintainer identified](https://github.com/ArcadeData/arcadedb/issues/11081#issuecomment-6561074526) 26.11.1 as the fix version.

## SC6: LIMIT 3 arrives after CALL and UNION materialization

Three witness rows already satisfied `LIMIT 3`, but ArcadeDB [#11082](https://github.com/ArcadeData/arcadedb/issues/11082) still materialized the other branch of a read-only `CALL`. In Listing 5, the first `UNION ALL` branch returned three copies of the integer `1`. The second branch matched two low-region leaves in Q1 or 300,000 high-region leaves in Q2. Since every branch projected the same constant, both complete queries returned three copies of `answer=1`.

**Listing 5.** A shared witness branch satisfies the outer limit.

```cypher
// Q1
CALL () {
  MATCH (r:GspRoot)-[:GSP_WITNESS]->(w:GspWitness)
  WHERE w.keep = true
  RETURN 1 AS item
  UNION ALL
  MATCH (r:GspRoot)-[:GSP_LOW]->(n:GspLow)
  WHERE n.keep = true
  RETURN 1 AS item
}
RETURN item AS answer
LIMIT 3

// Q2
CALL () {
  MATCH (r:GspRoot)-[:GSP_WITNESS]->(w:GspWitness)
  WHERE w.keep = true
  RETURN 1 AS item
  UNION ALL
  MATCH (r:GspRoot)-[:GSP_HIGH]->(n:GspHigh)
  WHERE n.keep = true
  RETURN 1 AS item
}
RETURN item AS answer
LIMIT 3
```

On ArcadeDB 26.10.2, ordinary runtime medians were **7.46 ms** for Q1 and **318.71 ms** for Q2. Their unrounded medians gave a **42.75×** ratio. The [seeded UNION implementation](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/query/opencypher/executor/CypherExecutionPlan.java#L531-L571) first collected all branch results into a list. [`SubqueryStep`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/query/opencypher/executor/steps/SubqueryStep.java#L432-L453) collected that result again. The outer limit therefore acted only after the expensive branch had finished. Separate profiles placed the additional cost inside `CALL SUBQUERY`.

```text
Native PROFILE excerpt; operator durations in microseconds
Q1:
+ CALL SUBQUERY { ... } (3,794μs)
+ LIMIT 3
+ FINAL PROJECTION [answer] (0μs, 3 rows)
Q2:
+ CALL SUBQUERY { ... } (355,794μs)
+ LIMIT 3
+ FINAL PROJECTION [answer] (1μs, 3 rows)
```

Merged [PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101) streamed read-only `CALL` results and their seeded `UNION` branches. The [maintainer reported](https://github.com/ArcadeData/arcadedb/issues/11082#issuecomment-6561075113) that the three witness rows then satisfied the limit without starting the 300,000-row branch, with the fix assigned to 26.11.1.

## SC7: An existence test waits for the entire neighbor list

A Gremlin `filter(child)` can retain its input as soon as the child produces one result. ArcadeDB [#10308](https://github.com/ArcadeData/arcadedb/issues/10308) showed why that was insufficient when the provider built an entire neighbor list before returning its iterator. The indexed root had 600,000 `GSP_SC_HIGH` neighbors and three `GSP_SC_REF` neighbors, all labeled `ScorTarget`. Either child in Listing 6 could therefore succeed on its first neighbor. With `_gsp_p0=1`, both complete queries returned the single row `c0=1`.

**Listing 6.** Only the relationship type read by the existence test changes.

```gremlin
// Q1: high-region existence test
g.inject(1).V().hasLabel('ScorRoot').has('id', P.eq(_gsp_p0))
    .as('_gsp_v0')
    .filter(__.select('_gsp_v0').outE('GSP_SC_HIGH').inV()
        .hasLabel('ScorTarget').as('_gsp_v1'))
    .project('c0')
    .by(__.select('_gsp_v0').coalesce(__.values('uid'), __.constant(null)));

// Q2: reference-region existence test
g.inject(1).V().hasLabel('ScorRoot').has('id', P.eq(_gsp_p0))
    .as('_gsp_v0')
    .filter(__.select('_gsp_v0').outE('GSP_SC_REF').inV()
        .hasLabel('ScorTarget').as('_gsp_v1'))
    .project('c0')
    .by(__.select('_gsp_v0').coalesce(__.values('uid'), __.constant(null)));
```

On ArcadeDB 26.10.2 with TinkerPop 3.8.2, ordinary runtime medians were **160.21 ms** for Q1 and **6.02 ms** for Q2. The median paired Q1/Q2 ratio was **26.66×**. Both profiles used the same root index, and each optimized `VertexStep` emitted only one element. However, [`ArcadeVertex.vertices()`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeVertex.java#L284-L302) filled an `ArrayList` before returning its iterator. The source and graph together imply 600,000 neighbor wrappers in Q1 and three in Q2, even though only the first result was consumed.

```text
Native PROFILE excerpt; cost and totalCost in nanoseconds
Q1:
VertexStep(OUT,[GSP_SC_HIGH],vertex)
  elements=1, traversers=1, cost=230370209, totalCost=230370209
Q2:
VertexStep(OUT,[GSP_SC_REF],vertex)
  elements=1, traversers=1, cost=6785417, totalCost=6785417
```

Merged [PR #10505](https://github.com/ArcadeData/arcadedb/pull/10505) made neighbor lookup and wrapping occur as the iterator was consumed. The producer could then yield its first match without constructing the unused remainder. The [maintainer confirmed](https://github.com/ArcadeData/arcadedb/issues/10308#issuecomment-6320211012) the fix for 26.11.1.

## SC8: A NULL contradiction fails to eliminate a scan

LadybugDB [#698](https://github.com/LadybugDB/ladybug/issues/698) retained a node scan for a predicate that could never be true. The test used version 0.18.2 and five million `N` nodes, with `v` null on even identifiers and non-null on odd identifiers. `v IS NULL` and `v IS NOT NULL` always have opposite Boolean values. Consequently, both queries in Listing 7 supplied an empty input to `count(*)` and returned one row containing `c=0`.

**Listing 7.** Two ways to express an empty aggregate input.

```cypher
// Q1: discover the empty input by evaluating node predicates.
MATCH (n:N)
WHERE n.v IS NULL AND n.v IS NOT NULL
RETURN count(*) AS c;

// Q2: expose the empty input directly.
MATCH (n:N)
WHERE false
RETURN count(*) AS c;
```

Two reported runs measured **1.64/0.31 ms** and **1.77/0.36 ms** for Q1/Q2, an approximately fivefold difference. `EXPLAIN` retained `SCAN_NODE_TABLE` and property filters in Q1. Q2 instead contained `EMPTY_RESULT` and no node scan. Folding the contradiction could therefore remove the scan while retaining the aggregate that returned zero.

```text
Q1, EXPLAIN excerpt                 Q2, EXPLAIN excerpt
SCAN_NODE_TABLE N (property n.v)    EMPTY_RESULT
  FILTER IS_NULL(n.v)
    FILTER IS_NOT_NULL(n.v)
```

Merged [PR #703](https://github.com/LadybugDB/ladybug/pull/703) added Boolean folding that simplified child expressions before their parents and recognized complementary null tests. Filters that folded to false or null were replaced with `EmptyResult`, eliminating the unnecessary input scan.

## SY1: Swapping AND operands changes COUNT evaluation

Swapping two `AND` operands changed whether ArcadeDB [#9579](https://github.com/ArcadeData/arcadedb/issues/9579) evaluated an expensive correlated count. The graph had two anchors: anchor 1 had `keep=false` and 400,000 neighbors with `mark=false`; anchor 2 had `keep=true` and one neighbor with `mark=true`. Q1 and Q2 in Listing 8 contain the same expressions and return the same row `c0=2`. Only the operand order changes.

**Listing 8.** Exchanging the AND operands changes which test can end evaluation first.

```cypher
// Q1: property test first
MATCH (u:GspAnchor)
WHERE ((coalesce(u.keep, false) = true) AND
  (COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0))
RETURN u.uid AS c0;

// Q2: COUNT first
MATCH (u:GspAnchor)
WHERE ((COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0) AND
  (coalesce(u.keep, false) = true))
RETURN u.uid AS c0;
```

For anchor 1, Q1 ends the conjunction after the property test. Q2 instead exhausts 400,000 neighbors to obtain `COUNT=0`. That false count test then ends the conjunction without reading `keep`. Anchor 2 passes both predicates in either order.

On ArcadeDB 26.10.1-SNAPSHOT, ordinary runtime medians were **1.24 ms** and **226.69 ms**, with a median paired Q2/Q1 ratio of **191.63×**. Native profiler windows recorded 10 versus 800,411 record lookups, including cached lookups. The cost difference arose from choosing which unchanged operand to evaluate first.

Merged [PR #9583](https://github.com/ArcadeData/arcadedb/pull/9583) classified operands by whether they could execute a subquery or pattern match, then prioritized operands without that work. This retained short-circuit evaluation while removing the expensive count from rejected roots. The [maintainer confirmed](https://github.com/ArcadeData/arcadedb/issues/9579#issuecomment-6088262423) the repair in 26.11.1.

## SY2: A redundant OR branch repeats a label scan

A redundant disjunct made Neo4j [#13783](https://github.com/neo4j/neo4j/issues/13783) repeat an independent label scan. The reproduction used Neo4j Community 2026.04.0, 128 nodes and 95 relationships; both queries returned the same 20 rows. Listing 9 shows their shared matching components and the changed predicate. The added branch mentions `n0`, but `false AND E` is false regardless of that node. The remaining matching, projection and return clauses were unchanged.

**Listing 9.** The false branch adds a reference to an otherwise independent component.

```cypher
// Shared MATCH clause; WHERE excerpts follow.
MATCH (n0:L5), (n1:L5:L0:L2), (n4)<-[r2:T3]-(n5:L3)

// Q1: base predicate
WHERE r2.id > -1

// Q2: equivalent predicate with a redundant disjunct
WHERE (r2.id > -1)
   OR (false AND ((elementId(n0) = elementId(n0))
                 OR (size(labels(n0)) >= 0)))
```

Retaining the `n0` reference introduced a syntactic dependency between its component and the predicate on `r2`. In Q1, the `n1` label intersection belonged to an inner Cartesian product with `n0`. In Q2, it moved to the outer Cartesian product, whose other input filtered combinations of `n0` and the relationship matches. The label intersection then produced 20 rows rather than one. Its DB Hits increased from 39 to 780, while total database accesses increased from 230 to 971. Both queries still returned 20 rows.

```text
PROFILE excerpt                         Q1       Q2
IntersectionNodeByLabelsScan rows         1       20
IntersectionNodeByLabelsScan DB Hits     39      780
Total database accesses                 230      971
Final result rows                        20       20
```

Discarding the false branch also removes its unnecessary node reference before planning. A [Neo4j developer replied](https://github.com/neo4j/neo4j/issues/13783#issuecomment-4741603460) that the case should be simplified starting in 2026.07 and closed the issue.

## SY4: Path reversal changes early rejection

Reversing a path made Neo4j [#14008](https://github.com/neo4j/neo4j/issues/14008) continue through a node that already invalidated the path. The graph contained one valid two-hop path and a `keep=false` node with 300,000 two-hop branches behind it. The queries in Listing 10 preserved the endpoint identities, relationship type, length bound and all-node predicate. Both returned `(1,3,2)`. Both plans started at the same indexed `GspLow` node; the difference was when they rejected the false node.

**Listing 10.** Reversing the written path leaves its node condition unchanged.

```cypher
// Q1
MATCH p = (u:GspLow {id: 1})-[:GSP_LINK*1..3]-(v:GspHigh)
WHERE all(x IN nodes(p) WHERE (x.keep = true))
RETURN u.uid, v.uid, length(p) AS hops;

// Q2
MATCH p = (v:GspHigh)-[:GSP_LINK*1..3]-(u:GspLow {id: 1})
WHERE all(x IN nodes(p) WHERE (x.keep = true))
RETURN u.uid, v.uid, length(p) AS hops;
```

On Neo4j Community 2026.09.0 with Cypher 25, the recorded median runtimes were **2.70 ms** for Q1 and **530.10 ms** for Q2. The median paired Q2/Q1 ratio was **199.66×**. Q1 evaluated the node predicate inside `VarLengthExpand(All)` and stopped at the false node. Q2 expanded through it and applied the predicate afterward. Expansion output increased from two rows to 600,003, and total DB Hits increased from 18 to 2,700,020. [Native profiles](https://github.com/neo4j/neo4j/issues/14008) showed the same indexed start in both plans.

```text
Q1: NodeIndexSeek -> VarLengthExpand(All, node predicate) -> Filter
    expansion rows: 2; expansion DB Hits: 12; total DB Hits: 18
Q2: NodeIndexSeek -> VarLengthExpand(All) -> Filter(all-node predicate)
    expansion rows: 600003; expansion DB Hits: 1200010; total DB Hits: 2700020
```

Applying the predicate during expansion avoids every branch behind the false node in either orientation. Neo4j closed the issue, with a [contributor stating](https://github.com/neo4j/neo4j/issues/14008#issuecomment-6190652273) that it should be fixed in 2026.10.

## SY5: Independent MATCH order repeats a traversal

Putting an independent node match before a path traversal made ArcadeDB [#5117](https://github.com/ArcadeData/arcadedb/issues/5117) repeat the traversal. The changed `MATCH` in Listing 11 combines a two-edge path through `n3`, `n2` and `n1` with the separate node `n4:L3`. Neither component reads variables introduced by the other. Their matches therefore form the same combinations in either order. Only these comma-separated components change position; the outer scans, constant lists and result projection remain unchanged.

**Listing 11.** The independent node and path components exchange positions.

```cypher
// Q1
PROFILE
MATCH (n0)
WITH n0
UNWIND [1868710224] AS a0
UNWIND [517248469, -1472958097, 1525636158] AS a1
MATCH (n1 :L1)<-[r0 :T4]-(n2 :L0 :L1)<-[r1 :T1]-(n3),
      (n4 :L3)
WHERE (((r1.k43) > -2019129302) AND ((r0.id) <> (r1.id)))
WITH a0, (r0.k57) AS a2
ORDER BY a0
RETURN 0 AS a3;

// Q2
PROFILE
MATCH (n0)
WITH n0
UNWIND [1868710224] AS a0
UNWIND [517248469, -1472958097, 1525636158] AS a1
MATCH (n4 :L3),
      (n1 :L1)<-[r0 :T4]-(n2 :L0 :L1)<-[r1 :T1]-(n3)
WHERE (((r1.k43) > -2019129302) AND ((r0.id) <> (r1.id)))
WITH a0, (r0.k57) AS a2
ORDER BY a0
RETURN 0 AS a3;
```

The report used a graph with 128 nodes and 67 relationships and Docker image `arcadedata/arcadedb:latest`. The two formulations returned the same rows, with reported median runtimes of **11.206 ms** and **374.813 ms**. Q2 took approximately **33.4 times** as long as Q1. The maintainer explained that queries containing the multi-label node bypassed the cost-based planner and followed the written component order. In Q2, every `L3` match restarted the expensive path traversal. In Q1, each path match was combined with the `L3` matches.

```text
Maintainer-described loop structure:
Q1: path traversal -> combine each path match with L3 matches
Q2: L3 matches -> execute the path traversal again for each L3 match
```

ArcadeDB fixed this execution path by reordering independent patterns, putting the selective traversal before the node component. The [maintainer reported](https://github.com/ArcadeData/arcadedb/issues/5117#issuecomment-4918235682) the fix in `main` and 26.7.2, with both formulations reaching the faster execution time and retaining their results.

## SY6: A shared node is bound before its labels are checked

ArcadeDB [#5116](https://github.com/ArcadeData/arcadedb/issues/5116) bound a repeated node variable too broadly when its bare occurrence appeared first. In Listing 12, `(n0:L1:L5)` and `(n0)` denote the same node. Both queries require the two labels and the same Boolean predicate, while `(n1)` is independent. Q1 applies the label restriction while binding `n0`. Q2 first encounters bare `(n0)` and reaches the labeled occurrence later. The subsequent optional clauses and aggregate are identical.

**Listing 12.** The labeled and bare occurrences of the shared variable exchange positions.

```cypher
// Q1
PROFILE
MATCH (n0 :L1 :L5),
      (n0),
      (n1)
WHERE ((n0.k9) OR (n0.k8))
OPTIONAL MATCH (n2 :L5)-[r0 :T1]->(n3 :L3 :L0), (n1)
WHERE ('b' ENDS WITH '0ZIeb')
OPTIONAL MATCH (n1)
RETURN max('h') AS a0, (n0.k36) AS a1, (r0.k52) AS a2;

// Q2
PROFILE
MATCH (n1),
      (n0),
      (n0 :L1 :L5)
WHERE ((n0.k9) OR (n0.k8))
OPTIONAL MATCH (n2 :L5)-[r0 :T1]->(n3 :L3 :L0), (n1)
WHERE ('b' ENDS WITH '0ZIeb')
OPTIONAL MATCH (n1)
RETURN max('h') AS a0, (n0.k36) AS a1, (r0.k52) AS a2;
```

On the reported graph of 128 nodes and 113 relationships, Q1 and Q2 returned the same rows. With Docker image `arcadedata/arcadedb:latest`, their reported median runtimes were **2.925 ms** and **47.497 ms**, a **16.238×** ratio. The maintainer traced the additional work to binding bare `n0` by scanning all nodes, then checking `:L1:L5` afterward. Starting with the labeled occurrence reduced the candidate bindings before later matches multiplied the rows.

```text
Maintainer-described binding order:
Q1: bind n0 with L1:L5 -> reuse n0 -> combine with n1
Q2: bind n1 -> scan nodes for n0 -> apply L1:L5 to bound n0
```

The repair reordered single-node patterns by selectivity on the step-by-step execution path, evaluating the restrictive labeled occurrence first. The [maintainer confirmed](https://github.com/ArcadeData/arcadedb/issues/5116#issuecomment-4920515762) the change in `main` and stated that the result set was preserved.

## SY7: Repeated roots rerun a later OPTIONAL MATCH

ArcadeDB [#11083](https://github.com/ArcadeData/arcadedb/issues/11083) repeatedly ran a later match even though its input root never changed. The indexed root had two `GSP_LOW` neighbors and 10,000 `GSP_HIGH` neighbors. Both orders in Listing 13 produced 20,000 combined rows and returned `answer=20000`. In Q2, however, the high branch first produced 10,000 rows. Each row had a different `h` but the same `r`, and the later low lookup read only `r`.

**Listing 13.** The high branch repeats the root supplied to the later low lookup.

```cypher
// Q1: selective stage first.
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
RETURN count(h) AS answer

// Q2: expansive stage first.
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
RETURN count(h) AS answer
```

On ArcadeDB 26.10.2, median runtimes were **7.31 ms** for Q1 and **784.53 ms** for Q2, a **107.36×** ratio from the unrounded medians. Native profiling recorded two entries at the later branch's inner root step in Q1 and 10,000 in Q2. Every row from the high branch supplied the same root to the low lookup. Repeating that lookup added matching work while leaving the final 20,000-row combination unchanged.

```text
PROFILE excerpt                    Q1          Q2
Later branch                       GSP_HIGH    GSP_LOW
Inner root-step records            2           10,000
Rows before aggregation            20,000      20,000
```

Merged [PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101) reused optional matches for consecutive rows with identical values for the bindings the clause reads. The [maintainer's diagnosis](https://github.com/ArcadeData/arcadedb/issues/11083#issuecomment-6561075709) identified the repeated low lookup as the expensive operation. Reusing its result avoided repeated adjacency access while still emitting its matches for every incoming row.

## SY8: Reusing optional matches preserves all combinations

Swapping two independent optional branches exposed repeated matching in ArcadeDB [#11083](https://github.com/ArcadeData/arcadedb/issues/11083). The indexed root had two `GSP_LOW` neighbors and 10,000 `GSP_HIGH` neighbors. Both branches in Listing 14 read only the already-bound root `r`; neither uses the other's neighbor variable. Exchanging them preserves all 20,000 low/high combinations, and both queries returned `answer=20000`.

**Listing 14.** Independent OPTIONAL MATCH branches exchange positions while preserving their combinations.

```cypher
// Q1: low branch first
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
RETURN count(h) AS answer

// Q2: high branch first
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
RETURN count(h) AS answer
```

On ArcadeDB 26.10.2, median runtimes were **7.31 ms** for Q1 and **784.53 ms** for Q2, a **107.36×** ratio from the unrounded medians. Native profiling recorded two entries at the later branch's inner root step in Q1 and 10,000 in Q2. In Q2, every high-neighbor row triggered the same low lookup for `r`.

The low lookup can be evaluated once for the root. Emitting its two matches for each of the 10,000 incoming rows preserves all 20,000 combinations while skipping repeated adjacency searches. The aggregate still counts one high neighbor for every low/high pair, so its answer remains `20000`. If an optional branch has no match, reuse must retain its input with null values for that branch.

[PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101) implemented this reuse for read-only optional clauses with identical consecutive input bindings. The [maintainer assigned](https://github.com/ArcadeData/arcadedb/issues/11083#issuecomment-6561075709) the fix to 26.11.1.
