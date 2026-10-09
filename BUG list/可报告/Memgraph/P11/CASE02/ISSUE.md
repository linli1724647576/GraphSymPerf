# Missed optimization: anchored MATCH scans a high branch before its empty low adjacency

I found a potential missed optimization opportunity: the selected indexed anchor has no GSP_LOW neighbor, but the high-first order traverses its large GSP_HIGH branch before discovering that the low join input is empty.

Status: locally reproduced performance candidate; developer assessment pending.

**Memgraph Version:** Memgraph/3.10.1  
**Operating System:** macOS 27.0; host build: 26A5421a; host kernel: Darwin 27.0.0; container: Ubuntu 24.04.4 LTS (Noble Numbat); container kernel: 6.12.54-linuxkit; recorded architecture: arm64  
**Installation Method:** Docker image `memgraph/memgraph@sha256:042c8642954a7bde971a9c7683c8b1360e7fcf129a5fae36d63f12449e7154aa`; CPU quota: 2; container memory: 1536 MiB  
**API/Driver:** Neo4j Python driver (Bolt) 5.28.4

### Steps to reproduce

The files referenced below are included in `case-package.zip`; relative links refer to the extracted archive contents.

1. Use an empty disposable database/graph of the recorded version. The attached native graph setup is [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt), identical to [setup.cypher](setup.cypher). Extract all attachments into one directory and run the API reproducer from that directory. It loads the same native statement list from [setup-statements.json](setup-statements.json), uses the attached [parameters.json](parameters.json), waits for the recorded index where required, and runs the complete queries.

```bash
python3 -m pip install neo4j==5.28.4
export GSP_URI='bolt://127.0.0.1:7687'
export GSP_DATABASE='memgraph'
python3 reproduce.py setup
python3 reproduce.py counts
python3 reproduce.py queries
python3 reproduce.py profile
```

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **80004 nodes**, **80002 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspAnchor {uid: 1}) MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (NOT (w.keep = false)) MATCH (u)-[:GSP_LOW]->(v:GspLow) RETURN u.uid, v.uid, w.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
MATCH (u:GspAnchor {uid: 1}) MATCH (u)-[:GSP_LOW]->(v:GspLow) MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (NOT (w.keep = false)) RETURN u.uid, v.uid, w.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The selected anchor has zero low matches and retains a successful high witness. A separate anchor retains a GSP_LOW edge, so the relationship type is present globally and cannot simply be folded away as nonexistent. The low-first order avoids the high expansion after checking the selected anchor's actual empty adjacency.

### Expected behavior

For the selected anchor, a zero-row low MATCH makes the independent anchored inner join empty. The plan can check that inexpensive adjacency before executing the growing high branch. This case has different optimal order from the eight-low-match P11 case; a fixed textual order does not account for both data states.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **0**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **14.320x**; median paired extra time: **7.795 ms**. Slow query: **A**. Bootstrap ratio interval: **[13.628, 16.906]**; self-control noise P99: **1.723 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "* Expand (u)-[anon4:GSP_LOW]->(v)", "actual_hits": 1}, {"operator": "* Expand (u)-[anon2:GSP_HIGH]->(w)", "actual_hits": 80002}], "b": [{"operator": "* Expand (u)-[anon4:GSP_HIGH]->(w)", "actual_hits": 1}, {"operator": "* Expand (u)-[anon2:GSP_LOW]->(v)", "actual_hits": 1}]}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "a",
  "median_ratio": 14.319914573620466,
  "median_delta_ms": 7.795187528245151,
  "direction_consistency": 1.0,
  "ratio_ci_low": 13.628095640276564,
  "ratio_ci_high": 16.905910631821822,
  "noise_p99_ms": 1.72325147781521
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+-------------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                        | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+-------------------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid, v.uid, w.uid}                 | 1           |   0.089037 %  |   0.008462 ms |
| * Filter (v :GspLow)                            | 1           |   0.010527 %  |   0.001000 ms |
| * Expand (u)-[anon4:GSP_LOW]->(v)               | 1           |   0.844759 %  |   0.080282 ms |
| * Filter (w :GspHigh), Generic {w}              | 2           |  79.985877 %  |   7.601491 ms |
| * Expand (u)-[anon2:GSP_HIGH]->(w)              | 80002       |  19.037782 %  |   1.809264 ms |
| * ScanAllByLabelProperties (u :GspAnchor {uid}) | 2           |   0.031141 %  |   0.002960 ms |
| * Once                                          | 2           |   0.000877 %  |   0.000083 ms |
+-------------------------------------------------+-------------+---------------+---------------+
```

Transformed PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+-------------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                        | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+-------------------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid, v.uid, w.uid}                 | 1           |   2.491103 %  |   0.002055 ms |
| * Filter (w :GspHigh), Generic {w}              | 1           |   1.067616 %  |   0.000881 ms |
| * Expand (u)-[anon4:GSP_HIGH]->(w)              | 1           |   0.762583 %  |   0.000629 ms |
| * Filter (v :GspLow)                            | 1           |   0.355872 %  |   0.000294 ms |
| * Expand (u)-[anon2:GSP_LOW]->(v)               | 1           |  93.594306 %  |   0.077215 ms |
| * ScanAllByLabelProperties (u :GspAnchor {uid}) | 2           |   1.677682 %  |   0.001384 ms |
| * Once                                          | 2           |   0.050839 %  |   0.000042 ms |
+-------------------------------------------------+-------------+---------------+---------------+
```

Missed optimization summary: The selected anchor has zero low matches and retains a successful high witness. A separate anchor retains a GSP_LOW edge, so the relationship type is present globally and cannot simply be folded away as nonexistent. The low-first order avoids the high expansion after checking the selected anchor's actual empty adjacency. Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "* Expand (u)-[anon4:GSP_LOW]->(v)", "actual_hits": 1}, {"operator": "* Expand (u)-[anon2:GSP_HIGH]->(w)", "actual_hits": 80002}], "b": [{"operator": "* Expand (u)-[anon4:GSP_HIGH]->(w)", "actual_hits": 1}, {"operator": "* Expand (u)-[anon2:GSP_LOW]->(v)", "actual_hits": 1}]}. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
