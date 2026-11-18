# Bounded reachability in match() expands from the high-degree endpoint

## Describe the situation

Hi, Apache TinkerPop developers,

Please consider these two queries:

```groovy
// q1
g.V(u.id()).match(
    __.as('u').out('Bind').as('t'),
    __.as('u').where(__.repeat(__.both('Walk')).emit().times(2).as('t'))
).select('u').constant(1)

// q2
g.V(u.id()).match(
    __.as('u').out('Bind').as('t'),
    __.as('t').where(__.repeat(__.both('Walk')).emit().times(2).as('u'))
).select('u').constant(1)
```

The first pattern binds the same two vertices in both queries. The second checks whether a walk of one or two `Walk` edges connects them. Each hop uses `both()`, so the existence condition holds in either direction. Both queries return one row.

## Which Apache TinkerPop versions are affected?

I reproduced this with TinkerGraph 3.8.2 (OpenJDK 11.0.32.1) and TinkerGraph 3.5.1 (OpenJDK 11.0.30), using Gremlin-Groovy and the default traversal strategies. The TinkerGraph measurements below ran in a fresh in-memory graph in a Gremlin Server JVM in Docker on macOS ARM64.

I also ran the same query pair with 300,000 leaves at the high-degree endpoint on JanusGraph 1.2.0 (TinkerPop 3.8.2, BerkeleyJE) and HugeGraph 1.7.0 (TinkerPop 3.5.1, RocksDB). The median JVM runtimes for q1 and q2 were **0.807 ms / 2,042.578 ms** on JanusGraph and **0.670 ms / 1,193.371 ms** on HugeGraph. Both providers returned one row in either direction. Inside `repeat()`, the JanusGraph vertex expansion produced 3 and 600,003 traversers, and the HugeGraph expansion produced 2 and 600,003.

## How to reproduce

Run this setup, then run q1 and q2 with the same traversal source. Consume the results using `toList()`.

```groovy
import org.apache.tinkerpop.gremlin.tinkergraph.structure.TinkerGraph

def tg = TinkerGraph.open()
def g = tg.traversal()
def u = g.addV('U').next()
def m = g.addV('Middle').next()
def t = g.addV('T').next()
u.addEdge('Bind', t)
u.addEdge('Walk', m)
m.addEdge('Walk', t)
300000.times { t.addEdge('Walk', g.addV('Leaf').next()) }
```

Two `Walk` edges connect the endpoints through `m`. Another 300,000 leaves are attached to `t`. A walk that takes the first hop from `t` to one of those leaves cannot reach `u` in its remaining hop.

For q1, I get one row, `1`. Median runtime on 3.8.2 is **0.328 ms**. On 3.5.1 it is **0.329 ms**.

For q2, I get one row, `1`. Median runtime on 3.8.2 is **371.455 ms**. On 3.5.1 it is **296.923 ms**.

I ran five warm-up pairs and 20 measured pairs in each version, alternating execution order. I timed full result consumption in the JVM and ran `profile()` and `explain()` separately.

TinkerGraph 3.8.2 `PROFILE` excerpt. Blank count fields follow the recorded profile.

```text
Step                                                               Count  Traversers       Time (ms)    % Dur
q1
    RepeatStep([VertexStep(BOTH,[Walk],vertex),...                     2           2           0.051
      VertexStep(BOTH,[Walk],vertex)                                   2           2           0.002

q2
    RepeatStep([VertexStep(BOTH,[Walk],vertex),...                600003      600003         513.606
      VertexStep(BOTH,[Walk],vertex)                              600003      600003         172.708
```

Starting at `u`, the walk reaches `t` after two expansions. Starting at `t`, the repeat also visits the leaves and walks back from them. The endpoint comparison sees the traversers after they leave `repeat()`.

## Expected performance

Please consider choosing the lower-expansion endpoint for a read-only bounded `both()` existence check between already bound vertices.

<!-- Provenance
Original report: BUG list/TinkerPop/SY4/BUG006.md
Existing evidence paths are written from the repository root. Historical relative paths use the original report location.

Local evidence
300,000-scale cross-provider evidence:
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/janusgraph-BUG006.json
BUG list/evidence/tinkergraph-reports-20261115/cross-provider-300000/hugegraph-BUG006.json
Earlier provider observations:
../../../results/gremlin-campaign-20261114/runs/gremlin-janusgraph/00189-SY4-3-100000
../../../results/gremlin-campaign-20261114/runs/gremlin-arcadedb/00189-SY4-3-100000
../../../results/gremlin-campaign-20261114/runs/gremlin-hugegraph-part10/00060-SY4-33-100000
Discovery history: BUG list/GREMLIN_CAMPAIGN_20261114.md
Current SY4 discovery. k=1 and k=3 controls and cross-provider profiles are in BUG list/evidence/candidate-audit-20261115/README.md
Samples, complete profiles, explain output and expected results: BUG list/evidence/tinkergraph-reports-20261115/scale-300000/BUG006.json
Runner: BUG list/evidence/tinkergraph-reports-20261115/replay_reports.py
-->
