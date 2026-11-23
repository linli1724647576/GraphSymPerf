# Pattern Analysis

To demonstrate how GraphSymPerf's short-circuit (`SC1`–`SC8`) and symmetry (`SY1`–`SY8`) patterns uncover missed optimizations and optimizer instabilities across graph DBMSs, we present 13 representative case studies drawn from our submitted public issue reports ([Reported issues](readme.md#reported-issues)). The selected cases span eight graph database engines—Neo4j, Memgraph, LadybugDB, FalkorDB, ArcadeDB, TinkerGraph, JanusGraph, and HugeGraph—covering both Cypher and Gremlin, with 10 fixed and 3 confirmed reports.

## Overview of Case Studies

| Listing | Pattern Group | System (Version) | Lang. | Issue | Fast → Slow (Factor) | Status / Fix | Root-Cause Class |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `SC1`, `SC2` | Neo4j (2026.04.0) | Cypher | [#13783](https://github.com/neo4j/neo4j/issues/13783) | 230 → 971 DB Hits (`4.2×`) | Fixed (`2026.07`) | Boolean simplification & plan placement |
| 2 | `SC1`, `SC2` | Memgraph (3.10.1) | Cypher | [#4459](https://github.com/memgraph/memgraph/issues/4459) | 1.43 → 105.85 ms (`74.0×`) | Fixed ([PR #4594](https://github.com/memgraph/memgraph/pull/4594)) | Double negation hides property index |
| 3 | `SC3`, `SC4` | ArcadeDB (26.10.1-SNAP) | Cypher | [#9580](https://github.com/ArcadeData/arcadedb/issues/9580) | 0.98 → 219.82 ms (`224.3×`) | Fixed ([PR #9583](https://github.com/ArcadeData/arcadedb/pull/9583)) | Runtime eager `COALESCE` evaluation |
| 4 | `SC3`, `SC4` | LadybugDB (0.21.2) | Cypher | [#1483](https://github.com/LadybugDB/ladybug/issues/1483) | 0.18 → 131.45 ms (`~745×`) | Fixed ([PR #1485](https://github.com/LadybugDB/ladybug/pull/1485)) | Binder fails to drop trailing `COALESCE` args |
| 5 | `SC5`, `SC8` | ArcadeDB (26.10.2) | Cypher | [#11081](https://github.com/ArcadeData/arcadedb/issues/11081) | 4.42 → 174.53 ms (`39.5×`) | Fixed ([PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101)) | Read-only `LIMIT 0` drains upstream input |
| 6 | `SC5`, `SC8` | TinkerGraph (3.8.2) / JanusGraph / HugeGraph | Gremlin | [TINKERPOP-3311](https://issues.apache.org/jira/browse/TINKERPOP-3311) | 0.094 → 125.83 ms (`1,339×`) | Confirmed | Constant-false filter fails to prune expansion |
| 7 | `SC6`, `SC7` | ArcadeDB (26.10.2) | Gremlin | [#10308](https://github.com/ArcadeData/arcadedb/issues/10308) | 6.02 → 160.21 ms (`26.7×` paired) | Fixed ([PR #10505](https://github.com/ArcadeData/arcadedb/pull/10505)) | Eager neighbor list materialization |
| 8 | `SC6`, `SC7` | ArcadeDB (26.10.2) | Cypher | [#11082](https://github.com/ArcadeData/arcadedb/issues/11082) | 7.46 → 318.71 ms (`42.8×`) | Fixed ([PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101)) | Eager `CALL UNION ALL` ignores outer `LIMIT` |
| 9 | `SY1`, `SY2` | ArcadeDB (26.10.1-SNAP) | Cypher | [#9579](https://github.com/ArcadeData/arcadedb/issues/9579) | 1.24 → 226.69 ms (`182.8×`) | Fixed ([PR #9583](https://github.com/ArcadeData/arcadedb/pull/9583)) | Syntax-order `AND` evaluation |
| 10 | `SY3`, `SY4` | Neo4j (2026.09.0) | Cypher | [#14008](https://github.com/neo4j/neo4j/issues/14008) | 2.70 → 530.10 ms (`196.3×`) | Fixed (`2026.10`) | Path orientation blocks predicate pushdown |
| 11 | `SY3`, `SY4` | FalkorDB (6.0.3) | Cypher | [#3637](https://github.com/FalkorDB/FalkorDB/issues/3637) | 6.45 → 4,521.40 ms (`701.0×`) | Confirmed (`edge`) | Path orientation drops index scan |
| 12 | `SY5`–`SY8` | ArcadeDB (26.10.2) | Cypher | [#11083](https://github.com/ArcadeData/arcadedb/issues/11083) | 7.31 → 784.53 ms (`107.4×`) | Fixed ([PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101)) | Repeated `OPTIONAL MATCH` over identical anchor |
| 13 | `SY5`–`SY8` | FalkorDB (4.18.8) | Cypher | [#3228](https://github.com/FalkorDB/FalkorDB/issues/3228) | 0.54 → 124.16 ms (`229.9×`) | Confirmed (`4.20`/`6.0`) | Clause order delays empty index lookup |

---

## Boolean Absorption and Simplification (`SC1`, `SC2`)

Boolean absorption patterns (disjunctive `OR` with a decisive truth value and conjunctive `AND` with a decisive falsehood) embed logically redundant boolean branches whose elimination should leave the physical query plan unchanged. Even when the redundant subexpression is inexpensive to evaluate on its own, failing to simplify it during logical rewriting can disrupt join ordering, operator placement, or index selection.

**Missed Disjunctive Absorption in Neo4j.** Listing 1 illustrates a missed optimization in Neo4j 2026.04.0 Community ([Neo4j #13783](https://github.com/neo4j/neo4j/issues/13783)). On a graph with 128 nodes and 95 relationships, `Q2` appends the always-false disjunct `OR (false AND ((elementId(n0) = elementId(n0)) OR (size(labels(n0)) >= 0)))` to the first `WHERE` predicate `r2.id > -1`. Under three-valued logic, `false AND E` evaluates to `false` whenever `E` is non-erroring and boolean, so the disjunct should be simplified away at compile time. Both queries return the same 20 rows. However, as shown in the `PROFILE` excerpt, Neo4j retains an extra `Filter` operator referencing both `r2.id` and `n0`, which forces `IntersectionNodeByLabelsScan(n1:L0&L2&L5)` above the `n0` Cartesian product instead of underneath it. Consequently, that scan executes 20 times instead of once—increasing its work from 1 row (39 DB Hits) to 20 rows (780 DB Hits) and inflating total database accesses from 230 to 971 (`4.22×`). Neo4j developers confirmed the issue and scheduled simplification for release `2026.07` ([Upstream confirmation](https://github.com/neo4j/neo4j/issues/13783)).

**Listing 1 (Neo4j #13783).** An always-false disjunct escapes simplification, retaining an extra `Filter` and inflating label-scan DB Hits by `4.22×`.

```cypher
// Graph: 128 nodes, 95 relationships. Both queries return 20 rows.

// Q1 (baseline): 230 total DB Hits
PROFILE
MATCH (n0:L5), (n1:L5:L0:L2), (n4)<-[r2:T3]-(n5:L3)
WHERE r2.id > -1
MATCH (n4)<-[]-(n5), (n5:L3)-[r3:T3]->(n6:L1)-[r4:T3]->(n7:L0)
WHERE n5.k24 AND n0.k38 AND r3.id <> r4.id
WITH r4 WHERE r4.k60 >= r4.k60
RETURN r4.k56 AS a1;

// Q2 (redundant disjunct): 971 total DB Hits (4.22x)
PROFILE
MATCH (n0:L5), (n1:L5:L0:L2), (n4)<-[r2:T3]-(n5:L3)
WHERE (r2.id > -1)
  OR (false AND ((elementId(n0) = elementId(n0)) OR (size(labels(n0)) >= 0)))
MATCH (n4)<-[]-(n5), (n5:L3)-[r3:T3]->(n6:L1)-[r4:T3]->(n7:L0)
WHERE n5.k24 AND n0.k38 AND r3.id <> r4.id
WITH r4 WHERE r4.k60 >= r4.k60
RETURN r4.k56 AS a1;

// Native PROFILE excerpt (Neo4j 2026.04.0 Community):
// Q1: CartesianProduct -> IntersectionNodeByLabelsScan(n1:L0&L2&L5) [1 row, 39 DB Hits]
//     Total DB Hits: 230
// Q2: CartesianProduct -> IntersectionNodeByLabelsScan(n1:L0&L2&L5) [20 rows, 780 DB Hits]
//     + extra Filter((cache[r2.id] > $autoint_0 OR size(labels(n0)) >= ... OR elementId(n0) = ...))
//     Total DB Hits: 971
```

**Opaque Double Negation Defeating Index Selection in Memgraph.** A closely related boolean-simplification gap arises when redundant negation wraps an indexed predicate. Listing 2 shows a bug uncovered in Memgraph 3.10.1 ([Memgraph #4459](https://github.com/memgraph/memgraph/issues/4459)). The graph contains 500,000 `:Item` nodes with `key = i % 1000` (indexed via `CREATE INDEX ON :Item(key)`) plus 100 `:Item` nodes lacking `key` to verify three-valued `null` preservation. Both queries return `c = 500` and identical sorted node IDs. Because `NOT NOT (n.key = 42)` preserves `true`, `false`, and `null` identically, the optimizer should strip the double negation prior to index analysis. Instead, Memgraph's index analyzer stopped at the outer `NOT` and treated the entire predicate as a generic filter, replacing `ScanAllByLabelProperties` with a full `ScanAll` over all 500,100 nodes. Across two fresh alternating runs, runtime degraded from `1.53 ms` / `1.43 ms` to `100.12 ms` / `105.85 ms` (`65.4×`–`74.0×`). Memgraph fixed the issue in [PR #4594](https://github.com/memgraph/memgraph/pull/4594) by allowing index analysis to see through double negation.

**Listing 2 (Memgraph #4459).** Wrapping an indexed equality in `NOT NOT` degrades an index lookup into a full graph scan (`65.4×`–`74.0×` slower).

```cypher
// Graph: 500,000 :Item nodes with key = i % 1000 (indexed on :Item(key)) + 100 :Item nodes without key.
// Both queries return c = 500.

// Q1 (baseline): 1.53 ms / 1.43 ms across two fresh runs
MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c;

// Q2 (double negation): 100.12 ms / 105.85 ms (65.4x - 74.0x slower)
MATCH (n:Item) WHERE NOT NOT (n.key = 42) RETURN count(*) AS c;

// Native EXPLAIN excerpt (Memgraph 3.10.1):
// Q1: * ScanAllByLabelProperties (n :Item {key})
// Q2: * Filter (n :Item), Generic {n}
//     * ScanAll (n)
```

---

## Branch Selection and Non-Null Prefixes (`SC3`, `SC4`)

Conditional expressions and non-null prefix selectors (`CASE` and `COALESCE`) determine their output as soon as the first matching branch or non-null argument is reached. When a subsequent branch contains an expensive graph traversal or subquery (such as a correlated `COUNT { ... }`), eager evaluation incurs cost proportional to the unreachable subgraph. Comparing Listings 3 and 4 illustrates how two graph engines exhibited the same `COALESCE` bug yet resolved it at different architectural layers: execution-time lazy evaluation in ArcadeDB versus bind-time argument pruning in LadybugDB.

**Eager `COALESCE` Argument Evaluation in ArcadeDB.** Listing 3 shows a missed optimization in ArcadeDB 26.10.1-SNAPSHOT ([ArcadeDB #9580](https://github.com/ArcadeData/arcadedb/issues/9580)). The indexed root `ScorRoot {id: 1}` (backed by an `LSM_TREE` index `gsp_cost_lookup`) has 400,000 outgoing `GSP_SC_HIGH` edges to `ScorTarget` nodes with `mark = true`. In `Q1`, the second argument of `coalesce(null, 0, COUNT { ... })` is already the non-null constant `0`, making the trailing `COUNT` subquery unreachable. `Q2` provides the constant-folded baseline `RETURN 0 AS c0`. Both queries return one row with `c0 = 0`, yet `Q1` takes `219.82 ms` versus `0.98 ms` for `Q2` (`224.3×`). Native `PROFILE` confirms that both plans use the same `NodeIndexSeek(u:ScorRoot)`, isolating the entire overhead to `ProjectReturnStep` (`241,817 μs` vs. `1 μs`), with the report recording over 800,000 record lookups. ArcadeDB fixed this in [PR #9583](https://github.com/ArcadeData/arcadedb/pull/9583) (released in `26.11.1`) by evaluating `COALESCE` arguments lazily in order and stopping at the first non-null value ([Upstream confirmation](https://github.com/ArcadeData/arcadedb/issues/9580#issuecomment-6088262088)).

**Listing 3 (ArcadeDB #9580).** Eager evaluation of a trailing `COUNT` subquery after a non-null constant in `COALESCE` causes a `224.3×` slowdown.

```cypher
// Graph: 1 indexed :ScorRoot(id=1) with 400,000 :GSP_SC_HIGH neighbors (mark=true) and 3 :GSP_SC_REF neighbors.
// Both queries return c0 = 0.

// Q1 (high-cost): median runtime 219.82 ms (224.3x slower)
MATCH (u:ScorRoot {id: 1})
RETURN coalesce(null, 0, COUNT {
  MATCH (u)-[:GSP_SC_HIGH]->(v:ScorTarget) WHERE (v.mark = true)
}) AS c0;

// Q2 (baseline): median runtime 0.98 ms
MATCH (u:ScorRoot {id: 1})
RETURN 0 AS c0;

// Native PROFILE excerpt (ArcadeDB 26.10.1-SNAPSHOT):
// Q1: NodeIndexSeek(u:ScorRoot) -> ProjectReturnStep: 241,817 μs, 1 row (>800,000 record lookups)
// Q2: NodeIndexSeek(u:ScorRoot) -> ProjectReturnStep:       1 μs, 1 row
```

**Three-Hop Path Explosion Behind `COALESCE` in LadybugDB.** Listing 4 demonstrates that even a small graph can trigger severe degradation when the dominated subquery involves multi-hop path expansion ([LadybugDB #1483](https://github.com/LadybugDB/ladybug/issues/1483)). On LadybugDB 0.21.2, the test graph contains only 3,002 nodes and 4,500 edges arranged in a three-hop diamond (`ProbeRoot` → 1,500 `ProbeMid` → 1 `ProbeHub` → 1,500 `ProbeLeaf`), yielding `1,500 × 1,500 = 2,250,000` qualifying paths where `m.uid <> l.uid`. Although the first `COALESCE` argument is the non-null literal `0`, LadybugDB plans and executes the entire three-hop traversal, filters all 2,250,000 tuples, computes `COUNT_STAR()`, and hash-joins the aggregate back to `u` before projecting `0`. Median runtime increases from `0.18 ms` (`Q2`) to `131.45 ms` (`Q1`, reported as `~745×` from unrounded medians). In contrast to ArcadeDB's runtime short-circuit fix, LadybugDB resolved this issue at bind time in [PR #1485](https://github.com/LadybugDB/ladybug/pull/1485), dropping all `COALESCE` arguments following a non-NULL literal before physical planning so the subquery plan is never generated.

**Listing 4 (LadybugDB #1483).** A compact 3,002-node diamond graph induces 2,250,000 unnecessary three-hop path evaluations inside `COALESCE` (`~745×` slower).

```cypher
// Graph: 3,002 nodes, 4,500 edges (1 ProbeRoot -> 1,500 ProbeMid -> 1 ProbeHub -> 1,500 ProbeLeaf; 2.25M 3-hop paths).
// Both queries return value = 0.

// Q1 (high-cost): median runtime 131.45 ms (~745x slower as reported)
MATCH (u:ProbeRoot {uid: 1})
RETURN coalesce(0, COUNT {
  MATCH (u)-[:ProbeA]->(m:ProbeMid)-[:ProbeB]->(h:ProbeHub)-[:ProbeC]->(l:ProbeLeaf)
  WHERE m.uid <> l.uid
}) AS value;

// Q2 (baseline): median runtime 0.18 ms
MATCH (u:ProbeRoot {uid: 1})
RETURN 0 AS value;

// Native PROFILE excerpt (LadybugDB 0.21.2, NumOutputTuples):
// Q1: SCAN_REL_TABLE[ProbeC] (2,250,000) -> FILTER[m.uid <> l.uid] (2,250,000)
//     -> AGGREGATE_SCAN[COUNT_STAR()] (1) -> HASH_JOIN_BUILD/PROBE (1) -> PROJECTION (1)
// Q2: PRIMARY_KEY_SCAN_NODE_TABLE[ProbeRoot, Key: 1] (1) -> PROJECTION (1)
```

---

## Empty-Result Propagation (`SC5`, `SC8`)

Empty-result propagation patterns (zero-row demand via `LIMIT 0` and unsatisfiable predicates or empty inputs) construct queries whose output is guaranteed empty independent of an upstream expansion. An optimizer that recognizes read-only empty subtrees should eliminate the preceding traversal entirely rather than materializing and discarding its rows.

**Read-Only `EXISTS { ... LIMIT 0 }` Draining Upstream Matches in ArcadeDB.** Listing 5 exposes a missed optimization in ArcadeDB 26.10.2 ([ArcadeDB #11081](https://github.com/ArcadeData/arcadedb/issues/11081)). The graph contains one root (`GspRoot {uid: 0}`) connected to 2 `GspLow` leaves and 300,000 `GspHigh` leaves (all with `keep = true`). Both queries evaluate a read-only `EXISTS` subquery terminated by `LIMIT 0`, which must produce an empty stream and therefore return `answer = false`. Switching only the matched region from `GSP_LOW` to `GSP_HIGH` raises the median runtime from `4.42 ms` (`Q1`) to `174.53 ms` (`Q2`, `39.49×`), with separate `PROFILE` runs attributing `1,554 μs` vs. `163,331 μs` to `PROJECT RETURN answer`. Source inspection of pre-fix [`LimitStep.java`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/query/opencypher/executor/steps/LimitStep.java#L53-L65) reveals a classic graph-query read/write coupling issue: `LimitStep` unconditionally pulled and drained its upstream batch when `limit == 0` so that any preceding mutating clause (`CREATE`, `SET`, `DELETE`) would still execute its side effects. In [PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101) (shipping in `26.11.1`), ArcadeDB wired `statement.isReadOnly()` into `LimitStep` so read-only `LIMIT 0` returns immediately without reading input, making `Q2` match `Q1` while preserving write semantics ([Upstream confirmation](https://github.com/ArcadeData/arcadedb/issues/11081#issuecomment-6561074526)).

**Listing 5 (ArcadeDB #11081).** A read-only `EXISTS` subquery with `LIMIT 0` drains 300,000 upstream matches (`39.49×` slower).

```cypher
// Graph: 1 :GspRoot(uid=0) with 2 :GSP_LOW leaves and 300,000 :GSP_HIGH leaves (all keep=true).
// Both queries return answer = false.

// Q1 (baseline): median runtime 4.42 ms
RETURN EXISTS {
  MATCH (r:GspRoot)-[:GSP_LOW]->(n:GspLow)
  WHERE n.keep = true
  RETURN 1 AS item
  LIMIT 0
} AS answer

// Q2 (high-cost): median runtime 174.53 ms (39.49x slower)
RETURN EXISTS {
  MATCH (r:GspRoot)-[:GSP_HIGH]->(n:GspHigh)
  WHERE n.keep = true
  RETURN 1 AS item
  LIMIT 0
} AS answer

// Native PROFILE excerpt (ArcadeDB 26.10.2):
// Q1: PROJECT RETURN answer (  1,554 μs, 1 row) | Execution Time:   2.051 ms
// Q2: PROJECT RETURN answer (163,331 μs, 1 row) | Execution Time: 163.407 ms
// Post-fix (PR #11101): read-only LIMIT 0 returns immediately without pulling input (Q2 matches Q1).
```

**Constant-False Filter Failing to Prune Traversal Across Three Gremlin Engines.** Listing 6 presents a missed optimization confirmed across the Apache TinkerPop Gremlin ecosystem ([TINKERPOP-3311](https://issues.apache.org/jira/browse/TINKERPOP-3311)). Starting from a root with 300,000 `HIGH` neighbors and 1 `LOW` neighbor, both traversals apply the pure constant-false child filter `.filter(__.constant(2).is(P.lt(1)))`, which rejects every traverser regardless of graph state and produces an empty result. Because TinkerPop's default traversal strategies do not fold constant-false child traversals or prune preceding read-only expansions, every tested Gremlin engine expands all 300,000 `HIGH` neighbors in `Q1` before discarding them one by one: median JVM runtimes for `Q1` vs. `Q2` are `125.833 ms` vs. `0.094 ms` (`1,339×`) on TinkerGraph 3.8.2 (`120.922 ms` vs. `0.090 ms` on 3.5.1), `363.223 ms` vs. `0.357 ms` (`1,017×`) on JanusGraph 1.2.0 (BerkeleyJE), and `252.663 ms` vs. `0.148 ms` (`1,707×`) on HugeGraph 1.7.0 (RocksDB). In triage, a TinkerPop maintainer confirmed that identifying filter contradictions and rewriting them to `discard()` is a reasonable general strategy, while noting the architectural division between TinkerPop's schema-agnostic traversal strategies and provider-level query optimization.

**Listing 6 (TINKERPOP-3311).** A constant-false Gremlin child filter still expands 300,000 neighbors across TinkerGraph (`1,339×`), JanusGraph (`1,017×`), and HugeGraph (`1,707×`).

```gremlin
// Graph: 1 :Root vertex with 300,000 :HIGH neighbors and 1 :LOW neighbor. Both queries return empty.

// Q1 (high-cost): 125.833 ms (TinkerGraph 3.8.2) | 363.223 ms (JanusGraph 1.2.0) | 252.663 ms (HugeGraph 1.7.0)
g.V(root.id()).out('HIGH').filter(__.constant(2).is(P.lt(1))).constant(1)

// Q2 (baseline):    0.094 ms (TinkerGraph 3.8.2) |   0.357 ms (JanusGraph 1.2.0) |   0.148 ms (HugeGraph 1.7.0)
g.V(root.id()).out('LOW').filter(__.constant(2).is(P.lt(1))).constant(1)

// Native PROFILE excerpt (TinkerGraph 3.8.2):
// Q1: VertexStep(OUT,[HIGH],vertex)                   300,000 traversers   26.867 ms (10.95%)
//     TraversalFilterStep([ConstantStep(2), lt(1)])         0 traversers  218.394 ms (89.02%)
// Q2: VertexStep(OUT,[LOW],vertex)                          1 traverser     0.010 ms (13.24%)
//     TraversalFilterStep([ConstantStep(2), lt(1)])         0 traversers    0.013 ms (16.76%)
```

---

## Bounded Demand and Existential Witnesses (`SC6`, `SC7`)

Bounded-demand (`LIMIT k` with a witness prefix) and existential-witness (`EXISTS` / `filter()` needing only a single match) patterns test whether a graph engine preserves lazy, pull-based streaming across storage adapters and subquery boundaries. Eager buffering at any operator boundary turns an `O(k)` or `O(1)` witness lookup into an `O(d)` full-degree or full-branch scan.

**Eager Neighbor Materialization Beneath a Gremlin Existence Filter in ArcadeDB.** Listing 7 reveals a cross-layer execution bottleneck in ArcadeDB 26.10.2 with TinkerPop 3.8.2 ([ArcadeDB #10308](https://github.com/ArcadeData/arcadedb/issues/10308)). With parameter `_gsp_p0 = 1`, the indexed `ScorRoot` has 600,000 outgoing `GSP_SC_HIGH` edges and 3 outgoing `GSP_SC_REF` edges, all targeting `:ScorTarget`. Both Gremlin queries test existence via `.filter(...)`, which requires only a single witness and returns `{"c0": 1}`. Although native `PROFILE` confirms that both plans use the same index (`ArcadeFilterByIndexStep`) and that `VertexStep` emits exactly one vertex in each query, `Q1` takes `160.21 ms` versus `6.02 ms` for `Q2` (a `26.66×` median paired ratio), with `VertexStep` accounting for `230.37 ms` vs. `6.79 ms` in separate `PROFILE` runs. The root cause lies in an abstraction mismatch between TinkerPop's lazy pull pipeline and ArcadeDB's storage adapter: pre-fix [`ArcadeVertex.vertices()`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeVertex.java#L284-L302) eagerly copied all matching neighbors into an `ArrayList` before returning an iterator to `VertexStep`. ArcadeDB fixed this in [PR #10505](https://github.com/ArcadeData/arcadedb/pull/10505) (shipping in `26.11.1`) by streaming `out()`/`in()`/`both()` and edge accessors lazily so existential filters terminate at the first witness ([Upstream confirmation](https://github.com/ArcadeData/arcadedb/issues/10308#issuecomment-6320211012)).

**Listing 7 (ArcadeDB #10308).** Eager `ArrayList` materialization in `ArcadeVertex.vertices()` defeats Gremlin's existential short-circuit (`26.66×` paired slowdown).

```gremlin
// Graph: 1 indexed :ScorRoot(id=1) with 600,000 :GSP_SC_HIGH and 3 :GSP_SC_REF :ScorTarget neighbors.
// Parameter: {_gsp_p0: 1}. Both queries return {"c0": 1}.

// Q1 (high-cost): median runtime 160.21 ms (26.66x paired median ratio)
g.inject(1).V().hasLabel('ScorRoot').has('id', P.eq(_gsp_p0))
    .as('_gsp_v0')
    .filter(__.select('_gsp_v0').outE('GSP_SC_HIGH').inV()
        .hasLabel('ScorTarget').as('_gsp_v1'))
    .project('c0')
    .by(__.select('_gsp_v0').coalesce(__.values('uid'), __.constant(null)));

// Q2 (baseline): median runtime 6.02 ms
g.inject(1).V().hasLabel('ScorRoot').has('id', P.eq(_gsp_p0))
    .as('_gsp_v0')
    .filter(__.select('_gsp_v0').outE('GSP_SC_REF').inV()
        .hasLabel('ScorTarget').as('_gsp_v1'))
    .project('c0')
    .by(__.select('_gsp_v0').coalesce(__.values('uid'), __.constant(null)));

// Native Gremlin PROFILE excerpt (ArcadeDB 26.10.2 / TinkerPop 3.8.2):
// Q1: VertexStep(OUT,[GSP_SC_HIGH],vertex)  elements=1, traversers=1, cost=230,370,209 ns (230.37 ms)
// Q2: VertexStep(OUT,[GSP_SC_REF],vertex)   elements=1, traversers=1, cost=  6,785,417 ns (  6.79 ms)
```

**Eager `CALL UNION ALL` Buffering Ignoring Outer `LIMIT 3` in ArcadeDB.** Listing 8 demonstrates a bounded-demand bug in Cypher subquery execution on ArcadeDB 26.10.2 ([ArcadeDB #11082](https://github.com/ArcadeData/arcadedb/issues/11082)). Using the same root-and-leaf graph as Listing 5 (1 root, 3 `GSP_WITNESS`, 2 `GSP_LOW`, and 300,000 `GSP_HIGH` leaves, all with `keep = true`), the first branch of the uncorrelated `CALL () { ... UNION ALL ... }` already supplies the three rows required by the outer `LIMIT 3`, and every branch projects the constant `1`. Yet replacing the second branch from `GSP_LOW` (`Q1`) to `GSP_HIGH` (`Q2`) increases median runtime from `7.46 ms` to `318.71 ms` (`42.75×`), with `CALL SUBQUERY` rising from `3,794 μs` to `355,794 μs`. Before the fix, ArcadeDB materialized the entire `CALL` body—running every `UNION ALL` branch to completion—before yielding its first row. In [PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101), read-only `CALL` and `UNION` steps were converted to demand-driven streaming so `Q2` stops after the three witness rows without starting the 300,000-row branch ([Upstream confirmation](https://github.com/ArcadeData/arcadedb/issues/11082#issuecomment-6561075113)).

**Listing 8 (ArcadeDB #11082).** Materializing both `UNION ALL` branches inside a read-only `CALL` prevents early termination under `LIMIT 3` (`42.75×` slower).

```cypher
// Graph: 1 :GspRoot(uid=0) with 3 :GSP_WITNESS, 2 :GSP_LOW, and 300,000 :GSP_HIGH leaves (all keep=true).
// Both queries return three rows of answer = 1.

// Q1 (baseline): median runtime 7.46 ms
CALL () {
  MATCH (r:GspRoot)-[:GSP_WITNESS]->(w:GspWitness) WHERE w.keep = true RETURN 1 AS item
  UNION ALL
  MATCH (r:GspRoot)-[:GSP_LOW]->(n:GspLow)         WHERE n.keep = true RETURN 1 AS item
}
RETURN item AS answer LIMIT 3

// Q2 (high-cost): median runtime 318.71 ms (42.75x slower)
CALL () {
  MATCH (r:GspRoot)-[:GSP_WITNESS]->(w:GspWitness) WHERE w.keep = true RETURN 1 AS item
  UNION ALL
  MATCH (r:GspRoot)-[:GSP_HIGH]->(n:GspHigh)      WHERE n.keep = true RETURN 1 AS item
}
RETURN item AS answer LIMIT 3

// Native PROFILE excerpt (ArcadeDB 26.10.2):
// Q1: CALL SUBQUERY { ... } (  3,794 μs) -> LIMIT 3 | Execution Time:   4.093 ms
// Q2: CALL SUBQUERY { ... } (355,794 μs) -> LIMIT 3 | Execution Time: 355.921 ms
```

---

## Equivalent Expressions (`SY1`, `SY2`)

Equivalent-expression symmetry patterns commute pure boolean conjuncts (`AND`) and disjuncts (`OR`) to test whether a query engine reorders operands by estimated evaluation cost rather than blindly following syntactic left-to-right order.

**Cost-Blind `AND` Operand Ordering in ArcadeDB.** Listing 9 presents a symmetry violation in ArcadeDB 26.10.1-SNAPSHOT ([ArcadeDB #9579](https://github.com/ArcadeData/arcadedb/issues/9579)). The graph contains two `:GspAnchor` nodes: `uid = 1` has `keep = false` and 400,000 outgoing `GSP_EXPAND` edges to leaves with `mark = false`, while `uid = 2` has `keep = true` and one matching leaf (`mark = true`). Both queries return `c0 = 2`. Swapping the local property predicate `coalesce(u.keep, false) = true` and the correlated `COUNT { ... } > 0` predicate degrades median runtime from `1.24 ms` (`Q1`) to `226.69 ms` (`Q2`, `182.8×`; `0.515 ms` vs. `254.992 ms` in separate `PROFILE` runs). Because ArcadeDB evaluated `AND` strictly in written order without cost-based reordering, `Q2` executed the 400,000-neighbor `COUNT` subquery for `uid = 1` (performing over 800,000 record lookups) before checking `u.keep = false`. In [PR #9583](https://github.com/ArcadeData/arcadedb/pull/9583) (released in `26.11.1`), ArcadeDB retained short-circuit semantics while scheduling subquery-free operands before subquery-bearing operands in both `AND` and `OR` expressions ([Upstream confirmation](https://github.com/ArcadeData/arcadedb/issues/9579#issuecomment-6088262423)).

**Listing 9 (ArcadeDB #9579).** Commuting a local property test and a correlated `COUNT` subquery under `AND` induces a `182.8×` slowdown.

```cypher
// Graph: :GspAnchor(uid=1, keep=false) with 400,000 :GSP_EXPAND leaves (mark=false);
//        :GspAnchor(uid=2, keep=true)  with 1 :GSP_EXPAND leaf (mark=true). Both return c0 = 2.

// Q1 (fast): median runtime 1.24 ms
MATCH (u:GspAnchor)
WHERE ((coalesce(u.keep, false) = true) AND
  (COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0))
RETURN u.uid AS c0;

// Q2 (slow): median runtime 226.69 ms (182.8x slower)
MATCH (u:GspAnchor)
WHERE ((COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf)
    WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0) AND
  (coalesce(u.keep, false) = true))
RETURN u.uid AS c0;

// Native PROFILE excerpt (ArcadeDB 26.10.1-SNAPSHOT):
// Q1: NodeByLabelScan(u:GspAnchor) -> Execution Time:   0.515 ms (rejects uid=1 via u.keep)
// Q2: NodeByLabelScan(u:GspAnchor) -> Execution Time: 254.992 ms (>800,000 record lookups for uid=1)
```

---

## Endpoint and Path Orientation (`SY3`, `SY4`)

Unlike relational joins, property-graph queries specify undirected single-hop and bounded variable-length path patterns whose syntactic left-to-right orientation should not alter physical optimization when the projected columns and order-insensitive path predicates are preserved. Listings 10 and 11 reveal two distinct graph-planner failures triggered by reversing an undirected variable-length path: failure to push an `all(x IN nodes(p) ...)` predicate into traversal in Neo4j, and failure to select an indexed starting endpoint in FalkorDB.

**Path Reversal Blocking Traversal-Time Predicate Pushdown in Neo4j.** Listing 10 shows a path-orientation bug in Neo4j 2026.09.0 Community (COST planner, SLOTTED runtime) ([Neo4j #14008](https://github.com/neo4j/neo4j/issues/14008)). The graph has 600,004 nodes and 600,003 relationships: an indexed node `u:GspLow {id: 1, keep: true}` connects via one valid two-hop path to `v:GspHigh {uid: 3, keep: true}`, and via an adjacent blocked node `b:GspBad {keep: false}` to 300,000 two-hop branches. Both queries match the same undirected `*1..3` pattern under `all(x IN nodes(p) WHERE (x.keep = true))` and return `(1, 3, 2)`. Reversing the written endpoints from `(u)-[*]-(v)` to `(v)-[*]-(u)` increases median runtime from `2.70 ms` (`Q1`) to `530.10 ms` (`Q2`, `196.3×`). Remarkably, the `PROFILE` plans show that Neo4j selects the *same* indexed starting point `NodeIndexSeek(u:GspLow(id))` in both queries. However, only in `Q1` does the planner inline `all(x IN nodes(p) WHERE x.keep = true)` into `VarLengthExpand(All)`, pruning `GspBad` immediately (`2` rows, `18` total DB Hits). In `Q2`, the reversed path representation prevents pushdown, leaving the predicate in a post-expansion `Filter` after expanding all 300,000 unreachable branches (`600,003` rows, `2,700,020` total DB Hits—a `150,001×` blowup in DB Hits). Neo4j confirmed and closed the issue for release `2026.10` ([Upstream resolution](https://github.com/neo4j/neo4j/issues/14008#issuecomment-6190652273)).

**Listing 10 (Neo4j #14008).** Reversing a bounded undirected path prevents predicate pushdown into `VarLengthExpand(All)`, inflating DB Hits from 18 to 2,700,020 (`196.3×` runtime slowdown).

```cypher
// Graph: 600,004 nodes, 600,003 relationships; index on :GspLow(id).
// u(id=1, keep=true) has 1 valid 2-hop path to v(uid=3) and 300,000 2-hop branches behind GspBad(keep=false).
// Both queries return (u.uid=1, v.uid=3, hops=2).

// Q1 (fast): median runtime 2.70 ms
MATCH p = (u:GspLow {id: 1})-[:GSP_LINK*1..3]-(v:GspHigh)
WHERE all(x IN nodes(p) WHERE (x.keep = true))
RETURN u.uid, v.uid, length(p) AS hops;

// Q2 (slow): median runtime 530.10 ms (196.3x slower)
MATCH p = (v:GspHigh)-[:GSP_LINK*1..3]-(u:GspLow {id: 1})
WHERE all(x IN nodes(p) WHERE (x.keep = true))
RETURN u.uid, v.uid, length(p) AS hops;

// Native PROFILE excerpt (Neo4j 2026.09.0 Community):
// Q1: NodeIndexSeek(u:GspLow(id)) [1 row, 2 hits]
//     -> VarLengthExpand(All) WHERE all(x IN nodes(p) WHERE x.keep = true) [2 rows, 12 hits]
//     -> Filter(v:GspHigh) [1 row, 2 hits]                              | Total DB Hits:        18
// Q2: NodeIndexSeek(u:GspLow(id)) [1 row, 2 hits]
//     -> VarLengthExpand(All) (u)-[:GSP_LINK*..3]-(v) [600,003 rows, 1,200,010 hits]
//     -> Filter(v:GspHigh AND all(...)) [1 row, 1,500,006 hits]         | Total DB Hits: 2,700,020
```

**Path Reversal Dropping Index-Scan Selection in FalkorDB.** Listing 11 shows a distinct path-orientation planner failure on FalkorDB 6.0.3 (Rust engine) ([FalkorDB #3637](https://github.com/FalkorDB/FalkorDB/issues/3637)). The graph has one indexed root `r:GspRoot {uid: 1, keep: true}` (index on `:GspRoot(uid)`), one direct valid leaf `t:GspLeaf {uid: 3, keep: true}`, and 10,000 additional leaves behind a blocked node `b:GspBlocked {keep: false}`. Both queries return `(1, 3, 1)`, yet reversing the undirected `*1..2` pattern causes median runtime to jump from `6.45 ms` (`Q1`) to `4,521.40 ms` (`Q2`, `701.0×`). As the `GRAPH.PROFILE` excerpts confirm, `Q1` seeds traversal via `Node By Index Scan (r:GspRoot)` (1 record; `2.25 ms` traversal), whereas `Q2` seeds traversal from a full `Node By Label Scan (t:GspLeaf)` (10,001 records; `4,513.84 ms` traversal). Although both traversal operators emit 10,001 records before the post-filter, starting from 10,001 leaves forces each leaf to explore into the high-degree blocked hub rather than traversing outward once from `r`. Inspection of [`select_scan_node.rs`](https://github.com/FalkorDB/FalkorDB/blob/c937aa4fafa9a0e9ab4afb007885be4fa30f1629/graph/src/planner/optimizer/select_scan_node.rs#L540-L549) shows that FalkorDB's scan-node selection pass skips bidirectional relationships, making scan selection depend on syntactic endpoint order. A control experiment removing the `ALL` predicate preserved both the `Index Scan` vs. `Label Scan` divergence and a `40–62 ms` vs. `4.56–4.57 s` gap, isolating endpoint selection from path-predicate pruning. FalkorDB maintainers reproduced and confirmed the bug on the Rust `edge` branch ([Upstream reproduction](https://github.com/FalkorDB/FalkorDB/issues/3637#issuecomment-6559886627)).

**Listing 11 (FalkorDB #3637).** Reversing a single undirected variable-length path drops the root index scan in favor of a 10,001-node label scan (`701.0×` slower).

```cypher
// Graph: 1 indexed :GspRoot(uid=1, keep=true), 1 direct :GspLeaf(uid=3), and 10,000 :GspLeaf behind :GspBlocked(keep=false).
// Both queries return (root_uid=1, leaf_uid=3, hops=1).

// Q1 (fast): median runtime 6.45 ms
MATCH p=(r:GspRoot {uid:1})-[:GSP_PATH*1..2]-(t:GspLeaf)
WHERE all(n IN nodes(p) WHERE n.keep=true)
RETURN r.uid AS root_uid, t.uid AS leaf_uid, length(p) AS hops;

// Q2 (slow): median runtime 4,521.40 ms (701.0x slower)
MATCH p=(t:GspLeaf)-[:GSP_PATH*1..2]-(r:GspRoot {uid:1})
WHERE all(n IN nodes(p) WHERE n.keep=true)
RETURN r.uid AS root_uid, t.uid AS leaf_uid, length(p) AS hops;

// Native GRAPH.PROFILE excerpt (FalkorDB 6.0.3):
// Q1: Node By Index Scan (r:GspRoot) [1 row, 0.006 ms]
//     -> Conditional Variable Length Traverse [10,001 rows,    2.247 ms] -> Filter [1 row, 4.344 ms]
// Q2: Node By Label Scan (t:GspLeaf) [10,001 rows, 0.225 ms]
//     -> Conditional Variable Length Traverse [10,001 rows, 4513.837 ms] -> Filter [1 row, 4.028 ms]
// Control without ALL: 40-62 ms (Q1) vs. 4.56-4.57 s (Q2), preserving the index-scan vs. label-scan gap.
```

---

## Matching and Branch Order (`SY5`–`SY8`)

Clause-order symmetry patterns commute independent mandatory `MATCH` clauses, shared-anchor branches, chained patterns, and independent `OPTIONAL MATCH` clauses. Because multi-clause graph queries decompose naturally into composable pattern fragments, sensitivity to clause order exposes both missing join/Cartesian-product reordering and redundant per-row re-evaluation.

**Quadratic Re-Evaluation of Swapped `OPTIONAL MATCH` Clauses in ArcadeDB.** Listing 12 illustrates a clause-order performance bug in ArcadeDB 26.10.2 ([ArcadeDB #11083](https://github.com/ArcadeData/arcadedb/issues/11083)). A single indexed root `r:GspRoot {uid: 0}` connects to 2 `GspLow` leaves and 10,000 `GspHigh` leaves. The two `OPTIONAL MATCH` clauses depend solely on the mandatory binding `r` and introduce disjoint variables (`l` and `h`), so commuting them preserves both null-extension semantics and multiset cardinality (`answer = 20000`). Yet `Q2` takes `784.53 ms` compared to `7.31 ms` for `Q1` (`107.36×`). As shown in `PROFILE`, the first `OPTIONAL MATCH` in `Q2` expands `r` into 10,000 rows carrying the exact same `r`, and [`OptionalMatchStep`](https://github.com/ArcadeData/arcadedb/blob/e854692736b48971f92dff78a1e727bc570bb6a2/engine/src/main/java/com/arcadedb/query/opencypher/executor/steps/OptionalMatchStep.java#L225-L241) re-executes `(r)-[:GSP_LOW]->(l)` for all 10,000 incoming rows. As ArcadeDB's maintainer diagnosed, each lookup scans all relationships of `r` to locate the 2 `GSP_LOW` edges, causing runtime to scale quadratically as `rows × relationships` (reaching ~33 seconds at 100,000 leaves). Rather than relying solely on cost-based clause reordering, ArcadeDB fixed this in [PR #11101](https://github.com/ArcadeData/arcadedb/pull/11101) by memoizing matches across consecutive rows in read-only `OPTIONAL MATCH` whenever the read bindings are unchanged, reducing `Q2` to a single low-branch lookup matching `Q1`'s latency ([Upstream diagnosis](https://github.com/ArcadeData/arcadedb/issues/11083#issuecomment-6561075709)).

**Listing 12 (ArcadeDB #11083).** Swapping independent `OPTIONAL MATCH` clauses triggers repeated relationship scans over the same bound root (`107.36×` slower; quadratic scaling).

```cypher
// Graph: 1 indexed :GspRoot(uid=0) with 2 :GSP_LOW leaves and 10,000 :GSP_HIGH leaves.
// Both queries return answer = 20000.

// Q1 (fast): median runtime 7.31 ms
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
RETURN count(h) AS answer

// Q2 (slow): median runtime 784.53 ms (107.36x slower; ~33 s at 100k leaves per maintainer)
MATCH (r:GspRoot {uid:0})
OPTIONAL MATCH (r)-[:GSP_HIGH]->(h:GspHigh)
OPTIONAL MATCH (r)-[:GSP_LOW]->(l:GspLow)
RETURN count(h) AS answer

// Native PROFILE excerpt (ArcadeDB 26.10.2):
// Q1: OPTIONAL MATCH (l) [2 rows] -> OPTIONAL MATCH (h) [inner MATCH NODE (r):      2 rows] | Execution Time:  13.010 ms
// Q2: OPTIONAL MATCH (h) [10,000] -> OPTIONAL MATCH (l) [inner MATCH NODE (r): 10,000 rows] | Execution Time: 999.674 ms
// Post-fix (PR #11101): read-only OptionalMatchStep reuses matches for identical bindings (Q2 matches Q1).
```

**Cartesian-Product Clause Order Scanning Millions of Nodes Before an Empty Index Lookup in FalkorDB.** Listing 13 exposes a clause-order bug in FalkorDB 4.18.8 ([FalkorDB #3228](https://github.com/FalkorDB/FalkorDB/issues/3228)). The graph has an operational index on `:GspLow(id)` containing one node with `id = 0`, alongside an independent component of 750,001 `GspHigh` nodes and 3,000,001 `GspTarget` nodes (only one with `keep = true`). Because no `GspLow` node has `id = 1`, the Cartesian product of the two independent `MATCH` clauses is empty in either order. In `Q1`, `Cartesian Product` evaluates `Node By Index Scan (a:GspLow)` first, observes 0 records, and skips the target branch completely (`0.54 ms`). In `Q2`, reversing the two clauses causes FalkorDB to scan all 3,000,001 `:GspTarget` nodes and apply `Filter(c.keep = true)` before consulting the empty index, inflating median runtime to `124.16 ms` (`229.9×`). Automated upstream triage reproduced the issue on both FalkorDB `4.20.7` (`0.23 ms` vs. `273.55 ms`) and `6.0.2` (where the planner scanned all 3,000,001 targets in *both* orders, taking `126.78 ms` and `115.72 ms`), confirming that the underlying Cartesian-product ordering optimization remains absent ([Upstream triage](https://github.com/FalkorDB/FalkorDB/issues/3228#issuecomment-6241680950)).

**Listing 13 (FalkorDB #3228).** Reordering two independent `MATCH` clauses scans 3,000,001 target nodes before checking an empty index (`229.9×` slower).

```cypher
// Graph: 1 :GspLow(id=0) with index on :GspLow(id); 750,001 :GspHigh -> 3,000,001 :GspTarget (1 with keep=true).
// Both queries return empty.

// Q1 (fast): median runtime 0.54 ms
MATCH (a:GspLow) WHERE (a.id = 1)
MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (c.keep = true)
RETURN a.uid, b.uid, c.uid;

// Q2 (slow): median runtime 124.16 ms (229.9x slower)
MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (c.keep = true)
MATCH (a:GspLow) WHERE (a.id = 1)
RETURN a.uid, b.uid, c.uid;

// Native GRAPH.PROFILE excerpt (FalkorDB 4.18.8):
// Q1: Cartesian Product [0]
//       |- Node By Index Scan (a:GspLow)    [0 records]
//       |- Node By Label Scan (c:GspTarget) [0 records]   (short-circuited)
// Q2: Cartesian Product [0]
//       |- Conditional Traverse [1] <- Filter [1] <- Node By Label Scan (c:GspTarget) [3,000,001 records]
//       |- Node By Index Scan (a:GspLow)    [0 records]
```
