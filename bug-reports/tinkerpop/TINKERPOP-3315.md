# A wrapped vertex filter misses early rejection

## Describe the situation

Hi, Apache TinkerPop developers,

Please consider these two queries:

```groovy
// q1
g.V(root.id()).filter(
    __.out('HIGH').has('mark', false)
).filter(__.has('anchor', 0)).constant(1)

// q2
g.V(root.id()).filter(
    __.out('HIGH').has('mark', false)
).has('anchor', 0).constant(1)
```

q1 wraps the final `has()` in `filter()`. Both queries have the same two conditions and return no rows.

## Which Apache TinkerPop versions are affected?

I reproduced this with TinkerGraph 3.8.2 (OpenJDK 11.0.32.1) and TinkerGraph 3.5.1 (OpenJDK 11.0.30), using Gremlin-Groovy and the default traversal strategies. The TinkerGraph measurements below ran in a fresh in-memory graph in a Gremlin Server JVM in Docker on macOS ARM64.

I also ran the same query pair with 300,000 outgoing edges from the root on JanusGraph 1.2.0 (TinkerPop 3.8.2, BerkeleyJE) and HugeGraph 1.7.0 (TinkerPop 3.5.1, RocksDB). The median JVM runtimes for q1 and q2 were **1,563.066 ms / 0.527 ms** on JanusGraph and **310.543 ms / 0.150 ms** on HugeGraph. On JanusGraph, q1 emitted 300,000 neighbors from the expansion; q2 did not reach that expansion. On HugeGraph, the folded vertex filter was active only in q1.

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

The selected root has `anchor=1` and 300,000 outgoing `HIGH` edges. Every neighbor has `mark=true`.

For q1, I get no rows. Median runtime on 3.8.2 is **36.089 ms**. On 3.5.1 it is **31.251 ms**.

For q2, I get no rows. Median runtime on 3.8.2 is **0.134 ms**. On 3.5.1 it is **0.075 ms**.

I ran five warm-up pairs and 20 measured pairs in each version, alternating execution order. I timed full result consumption in the JVM and ran `profile()` and `explain()` separately.

TinkerGraph 3.8.2 `PROFILE` excerpt. Blank count fields follow the recorded profile.

```text
Step                                                               Count  Traversers       Time (ms)    % Dur
q1
TinkerGraphStep(vertex,[0])                                            1           1           0.039     0.04
TraversalFilterStep([VertexStep(OUT,[HIGH],vert...                                            98.264    99.67
  VertexStep(OUT,[HIGH],vertex)                                   300000      300000          50.825
HasStep([anchor.eq(0)])                                                                        0.041     0.04

q2
TinkerGraphStep(vertex,[0],[anchor.eq(0)])                                                     0.054    48.36
TraversalFilterStep([VertexStep(OUT,[HIGH],vert...                                             0.016    14.95
  VertexStep(OUT,[HIGH],vertex)                                                                0.000
```

In q1, the late `HasStep` remains after the traversal filter when the wrapper is inlined. The traversal filter enumerates the neighbors. In q2, the anchor condition is already part of the starting lookup, so there is no input to expand.

## Expected performance

Please make the wrapped vertex predicate available to filter ranking so it can reject the root before expanding its neighbors.

<!-- Provenance
Original report: BUG list/TinkerPop/Native/filter-wrapper/BUG002.md
Existing evidence paths are written from the repository root. Historical relative paths use the original report location.

Local evidence
300,000-scale cross-provider evidence:
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/janusgraph-BUG002.json
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/hugegraph-BUG002.json
Earlier provider observations:
BUG list/evidence/legacy-review-20261031/janusgraph-6-result.json
BUG list/evidence/legacy-review-20261031/hugegraph-3-result.json
Discovery history: BUG list/evidence/method-attribution-20261101.json
Native probe, October 9. Mechanism evidence and historical source review are in the archived provenance record.
Samples, complete profiles, explain output and expected results: BUG list/evidence/tinkergraph-reports-20261115/scale-300000/BUG002.json
Runner: BUG list/evidence/tinkergraph-reports-20261115/replay_reports.py
-->
