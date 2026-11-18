# match() re-runs an independent pattern for every traverser of the other pattern

## Describe the situation

Hi, Apache TinkerPop developers,

Please consider these two queries:

```groovy
// q1
g.V(root.id()).match(
    __.as('r').out('HIGH').as('m'),
    __.as('r').out('LOW').has('mark', false).as('f')
).select('r').constant(1)

// q2
g.V(root.id()).match(
    __.as('r').out('LOW').has('mark', false).as('f'),
    __.as('r').out('HIGH').as('m')
).select('r').constant(1)
```

Neither pattern reads the other pattern's output label. The `Few` pattern has no matches, so both queries return no rows. In q1, the same lookup for `Few` is reached once per result of the `Many` pattern.

## Which Apache TinkerPop versions are affected?

I reproduced this with TinkerGraph 3.8.2 (OpenJDK 11.0.32.1) and TinkerGraph 3.5.1 (OpenJDK 11.0.30), using Gremlin-Groovy and the default traversal strategies. The TinkerGraph measurements below ran in a fresh in-memory graph in a Gremlin Server JVM in Docker on macOS ARM64.

I also ran the same query pair with 300,000 HIGH neighbors and one LOW neighbor at the shared root on JanusGraph 1.2.0 (TinkerPop 3.8.2, BerkeleyJE) and HugeGraph 1.7.0 (TinkerPop 3.5.1, RocksDB). The median JVM runtimes for q1 and q2 were **920.335 ms / 0.478 ms** on JanusGraph and **1,419.501 ms / 0.336 ms** on HugeGraph. The narrow pattern received 300,000 traversers in q1 and one in q2. Both queries returned no rows.

## How to reproduce

Run this setup, then run q1 and q2 with the same traversal source. Consume the results using `toList()`.

```groovy
import org.apache.tinkerpop.gremlin.tinkergraph.structure.TinkerGraph

def tg = TinkerGraph.open()
def g = tg.traversal()
def root = g.addV('Root').next()
int n = 300000
n.times { root.addEdge('HIGH', g.addV('Many').next()) }
root.addEdge('LOW', g.addV('Few').property('mark', true).next())
```

The root has 300,000 `HIGH` edges and one `LOW` edge. The `LOW` neighbor has `mark=true`, so the `mark=false` filter finds nothing.

For q1, I get no rows. Median runtime on 3.8.2 is **276.842 ms**.

For q2, I get no rows. Median runtime on 3.8.2 is **0.128 ms**.

On 3.5.1 the full 300,000-edge measurement request exceeded Gremlin Server's 120 s limit. With `n=1000`, both queries returned no rows and the medians were **2.407 ms** for q1 and **0.084 ms** for q2.

I ran five warm-up pairs and 20 measured pairs in each version, alternating execution order. I timed full result consumption in the JVM and ran `profile()` and `explain()` separately.

TinkerGraph 3.8.2 `PROFILE` excerpt. Blank count fields follow the recorded profile.

```text
Step                                                               Count  Traversers       Time (ms)    % Dur
q1
  MatchStartStep(r)                                                    1           1           0.008
  VertexStep(OUT,[HIGH],vertex)                                   300000      300000          48.792
  MatchStartStep(r)                                               300000      300000          84.209
  VertexStep(OUT,[LOW],vertex)                                    300000      300000          73.478
  HasStep([mark.eq(false)])                                                                   20.312

q2
  MatchStartStep(r)                                                    1           1           0.002
  VertexStep(OUT,[LOW],vertex)                                         1           1           0.002
  HasStep([mark.eq(false)])                                                                    0.002
  MatchStartStep(r)                                                                            0.000
  VertexStep(OUT,[HIGH],vertex)                                                                0.000
```

q1 passes 300,000 traversers with the same `r` binding into the `Few` pattern. In q2, that pattern receives one traverser and returns nothing, so the `Many` pattern is never reached.

For a nonempty control, I removed the `mark=false` filter from the `LOW` pattern in both queries. Both result lists contained 300,000 copies of `1`. On 3.8.2, the `LOW` step produced 300,000 traversers when placed second and one when placed first. The corresponding medians were **495.074 ms** and **277.114 ms**.

## Expected performance

Please consider avoiding repeated evaluation of a read-only `match()` pattern when all its input bindings are identical, including when the pattern returns no matches.

<!-- Provenance
Original report: BUG list/TinkerPop/SY5/BUG005.md
Existing evidence paths are written from the repository root. Historical relative paths use the original report location.

Local evidence
300,000-scale cross-provider evidence:
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/janusgraph-BUG005.json
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/hugegraph-BUG005.json
Earlier provider observations:
../../../results/gremlin-campaign-20261114/runs/gremlin-hugegraph-part6/00027-SY6-21-100000
../../../results/gremlin-campaign-20261114/runs/gremlin-janusgraph/00636-SY5-13-100000
../../../results/gremlin-campaign-20261114/runs/gremlin-arcadedb/01215-SY5-25-100000
Discovery history: BUG list/GREMLIN_CAMPAIGN_20261114.md
Current SY5/SY6 discovery. A nonempty control returns the full result list and is included in the evidence runner and JSON.
TinkerGraph 3.5.1 with 1,000 HIGH edges: BUG list/evidence/tinkergraph-reports-20261115/scale-300000/BUG005-351-small-control.json
Samples, complete profiles, explain output and expected results: BUG list/evidence/tinkergraph-reports-20261115/scale-300000/BUG005.json
Runner: BUG list/evidence/tinkergraph-reports-20261115/replay_reports.py
-->
