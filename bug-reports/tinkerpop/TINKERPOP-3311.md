# A constant-false child filter still expands every neighbor

## Describe the situation

Hi, Apache TinkerPop developers,

Please consider these two queries:

```groovy
// q1
g.V(root.id()).out('HIGH').filter(
    __.constant(2).is(P.lt(1))
).constant(1)

// q2
g.V(root.id()).out('LOW').filter(
    __.constant(2).is(P.lt(1))
).constant(1)
```

Each query expands one edge label and then applies the same constant comparison, `2 < 1`. That comparison always rejects the input. Both queries return no rows.

## Which Apache TinkerPop versions are affected?

I reproduced this with TinkerGraph 3.8.2 (OpenJDK 11.0.32.1) and TinkerGraph 3.5.1 (OpenJDK 11.0.30), using Gremlin-Groovy and the default traversal strategies. The TinkerGraph measurements below ran in a fresh in-memory graph in a Gremlin Server JVM in Docker on macOS ARM64.

I also ran the same query pair with 300,000 HIGH neighbors and one LOW neighbor on JanusGraph 1.2.0 (TinkerPop 3.8.2, BerkeleyJE) and HugeGraph 1.7.0 (TinkerPop 3.5.1, RocksDB). The median JVM runtimes for q1 and q2 were **363.223 ms / 0.357 ms** on JanusGraph and **252.663 ms / 0.148 ms** on HugeGraph. Both providers emitted 300,000 and one neighbors into the constant-false filter in q1 and q2, respectively.

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
root.addEdge('LOW', g.addV('Leaf').property('mark', true).next())
```

The root has 300,000 neighbors under `HIGH` and one neighbor under `LOW`.

For q1, I get no rows. Median runtime on 3.8.2 is **125.833 ms**. On 3.5.1 it is **120.922 ms**.

For q2, I get no rows. Median runtime on 3.8.2 is **0.094 ms**. On 3.5.1 it is **0.090 ms**.

I ran five warm-up pairs and 20 measured pairs in each version, alternating execution order. I timed full result consumption in the JVM and ran `profile()` and `explain()` separately.

TinkerGraph 3.8.2 `PROFILE` excerpt. Blank count fields follow the recorded profile.

```text
Step                                                               Count  Traversers       Time (ms)    % Dur
q1
VertexStep(OUT,[HIGH],vertex)                                     300000      300000          26.867    10.95
TraversalFilterStep([ConstantStep(2), IsStep(lt...                                           218.394    89.02

q2
VertexStep(OUT,[LOW],vertex)                                           1           1           0.010    13.24
TraversalFilterStep([ConstantStep(2), IsStep(lt...                                             0.013    16.76
```

The final plans retain the constant comparison after the vertex expansion. The expansion supplies 300,000 inputs to the filter in q1 and one in q2.

## Expected performance

Please fold a pure constant-false child filter so the traversal can skip the preceding read-only expansion.

<!-- Provenance
Original report: BUG list/TinkerPop/SC5/BUG003.md
Existing evidence paths are written from the repository root. Historical relative paths use the original report location.

Local evidence
300,000-scale cross-provider evidence:
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/janusgraph-BUG003.json
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/hugegraph-BUG003.json
Earlier provider observations:
BUG list/evidence/legacy-review-20261031/janusgraph-8-result.json
Discovery history: BUG list/evidence/method-attribution-20261101.json
Originally found with the legacy constant-false template. Current SC5 uses a different rewrite.
Samples, complete profiles, explain output and expected results: BUG list/evidence/tinkergraph-reports-20261115/scale-300000/BUG003.json
Runner: BUG list/evidence/tinkergraph-reports-20261115/replay_reports.py
-->
