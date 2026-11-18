# A redundant null test prevents the count-to-existence optimization

## Describe the situation

Hi, Apache TinkerPop developers,

Please consider these two queries:

```groovy
// q1
g.V(root.id()).filter(
    __.outE('HIGH').count().is(P.neq(null)).is(P.gt(0))
).constant(1)

// q2
g.V(root.id()).filter(
    __.outE('HIGH').count().is(P.gt(0))
).constant(1)
```

Both filters test whether the starting vertex has an outgoing `HIGH` edge. The extra `is(neq(null))` in q1 always succeeds because `count()` returns an integer. Both queries return one row.

## Which Apache TinkerPop versions are affected?

I reproduced this with TinkerGraph 3.8.2 (OpenJDK 11.0.32.1) and TinkerGraph 3.5.1 (OpenJDK 11.0.30), using Gremlin-Groovy and the default traversal strategies. The TinkerGraph measurements below ran in a fresh in-memory graph in a Gremlin Server JVM in Docker on macOS ARM64.

I also ran the same query pair with 300,000 outgoing edges from the root on JanusGraph 1.2.0 (TinkerPop 3.8.2, BerkeleyJE) and HugeGraph 1.7.0 (TinkerPop 3.5.1, RocksDB). The median JVM runtimes for q1 and q2 were **273.981 ms / 82.214 ms** on JanusGraph and **135.274 ms / 0.464 ms** on HugeGraph. Both profiles retained `CountGlobalStep` in q1 with 300,000 incoming edge traversers. In q2 the count step was removed.

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

The root has 300,000 outgoing `HIGH` edges.

For q1, I get one row, `1`. Median runtime on 3.8.2 is **16.320 ms**. On 3.5.1 it is **10.317 ms**.

For q2, I get one row, `1`. Median runtime on 3.8.2 is **3.432 ms**. On 3.5.1 it is **5.820 ms**.

I ran five warm-up pairs and 20 measured pairs in each version, alternating execution order. I timed full result consumption in the JVM and ran `profile()` and `explain()` separately.

TinkerGraph 3.8.2 `PROFILE` excerpt. Blank count fields follow the recorded profile.

```text
Step                                                               Count  Traversers       Time (ms)    % Dur
q1
TraversalFilterStep([VertexStep(OUT,[HIGH],edge...                     1           1         173.133    99.29
  VertexStep(OUT,[HIGH],edge)                                     300000      300000         137.252
  CountGlobalStep                                                      1           1          35.480

q2
TraversalFilterStep([VertexStep(OUT,[HIGH],edge)])                     1           1           6.838    98.85
  VertexStep(OUT,[HIGH],edge)                                                                  6.803
```

The count and both tests survive optimization in q1. In q2, TinkerGraph reduces the child to an edge traversal whose first result satisfies the filter.

## Expected performance

Please let `CountStrategy` recognize the positive count test through the redundant null check so q1 can stop at the first matching edge.

<!-- Provenance
Original report: BUG list/TinkerPop/Native/count-nonnull/BUG001.md
Existing evidence paths are written from the repository root. Historical relative paths use the original report location.

Local evidence
300,000-scale cross-provider evidence:
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/janusgraph-BUG001.json
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/hugegraph-BUG001.json
Earlier provider observations:
BUG list/evidence/legacy-review-20261031/hugegraph-2-result.json
Discovery history: BUG list/evidence/method-attribution-20261101.json
Native probe, October 9. Mechanism evidence and historical source review are in the archived provenance record.
Samples, complete profiles, explain output and expected results: BUG list/evidence/tinkergraph-reports-20261115/scale-300000/BUG001.json
Runner: BUG list/evidence/tinkergraph-reports-20261115/replay_reports.py
-->
