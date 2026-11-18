# and()/or() evaluate an expensive child before a cheap has() child

## Describe the situation

Hi, Apache TinkerPop developers,

Please consider these two queries:

```groovy
// q1
g.V(root.id()).or(
    __.out('HIGH').has('mark', false),
    __.has('anchor', 1)
).constant(1)

// q2
g.V(root.id()).or(
    __.has('anchor', 1),
    __.out('HIGH').has('mark', false)
).constant(1)
```

Both queries return one row because the root satisfies `has('anchor',1)`. The two children of `or()` appear in opposite orders. q1 reaches the decisive property check after visiting the neighbors.

## Which Apache TinkerPop versions are affected?

I reproduced this with TinkerGraph 3.8.2 (OpenJDK 11.0.32.1) and TinkerGraph 3.5.1 (OpenJDK 11.0.30), using Gremlin-Groovy and the default traversal strategies. The TinkerGraph measurements below ran in a fresh in-memory graph in a Gremlin Server JVM in Docker on macOS ARM64.

I also ran the same query pair with 300,000 HIGH neighbors, using the `or()` pair above on JanusGraph 1.2.0 (TinkerPop 3.8.2, BerkeleyJE) and HugeGraph 1.7.0 (TinkerPop 3.5.1, RocksDB). The median JVM runtimes for q1 and q2 were **1,574.205 ms / 0.471 ms** on JanusGraph and **309.590 ms / 0.155 ms** on HugeGraph. The JanusGraph profile recorded 300,000 neighbors from the expensive child in q1. The HugeGraph profile also showed that the expensive child ran only in q1.

## How to reproduce

Run this setup, then run q1 and q2 with the same traversal source. Consume the results using `toList()`.

```groovy
import org.apache.tinkerpop.gremlin.tinkergraph.structure.TinkerGraph

def tg = TinkerGraph.open()
def g = tg.traversal()
def root = g.addV('Root').property('anchor', 1).next()
300000.times {
    root.addEdge('HIGH', g.addV('Leaf').property('mark', true).next())
}
```

The single starting vertex has `anchor=1`. Its 300,000 `HIGH` neighbors all have `mark=true`, so the traversal looking for `mark=false` finds nothing.

For q1, I get one row, `1`. Median runtime on 3.8.2 is **37.723 ms**. On 3.5.1 it is **35.637 ms**.

For q2, I get one row, `1`. Median runtime on 3.8.2 is **0.129 ms**. On 3.5.1 it is **0.092 ms**.

I ran five warm-up pairs and 20 measured pairs in each version, alternating execution order. I timed full result consumption in the JVM and ran `profile()` and `explain()` separately.

TinkerGraph 3.8.2 `PROFILE` excerpt. Blank count fields follow the recorded profile.

```text
Step                                                               Count  Traversers       Time (ms)    % Dur
q1
OrStep([[VertexStep(OUT,[HIGH],vertex), HasStep...                     1           1          68.274    99.85
  VertexStep(OUT,[HIGH],vertex)                                   300000      300000          32.409
  HasStep([anchor.eq(1)])                                                                      0.003

q2
OrStep([[HasStep([anchor.eq(1)])], [VertexStep(...                     1           1           0.008    15.26
  HasStep([anchor.eq(1)])                                                                      0.001
  VertexStep(OUT,[HIGH],vertex)                                                                0.000
```

The optimized `OrStep` preserves the child order. The edge traversal runs in q1. The successful property check ends the evaluation in q2 before the edge traversal runs.

I also ran the two child orders with `and()` and `has('anchor',0)` on the same graph. Both results were empty. On 3.8.2, the adjacency-first form produced 300,000 traversers in the edge step and took **41.581 ms**. The property-first form skipped that step and took **0.093 ms**.

## Expected performance

Please consider ranking these side-effect-free children so the cheaper property check can run before the adjacency traversal.

<!-- Provenance
Original report: BUG list/TinkerPop/SY2/BUG004.md
Existing evidence paths are written from the repository root. Historical relative paths use the original report location.

Local evidence
300,000-scale cross-provider evidence:
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/janusgraph-BUG004.json
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/hugegraph-BUG004.json
Earlier provider observations:
BUG004/01098-SY2-22-100000
BUG004/00078-SY1-22-100000
BUG004/00228-SY1-4-100000
Discovery history: BUG list/GREMLIN_CAMPAIGN_20261114.md
Current SY1/SY2 discovery. Standalone and() control is included in the evidence runner and JSON.
Samples, complete profiles, explain output and expected results: BUG list/evidence/tinkergraph-reports-20261115/scale-300000/BUG004.json
Runner: BUG list/evidence/tinkergraph-reports-20261115/replay_reports.py
-->
