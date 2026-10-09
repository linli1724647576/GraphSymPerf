# Missed optimization: anchored MATCH order repeats high-side expansion for eight low matches

I found a potential missed optimization opportunity: the two anchored branches depend only on u and their own output variables. One MATCH order repeats the high expansion for each of eight low matches, whereas the equivalent order performs it once per anchor.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **200010 nodes**, **200009 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspAnchor {uid: 1}) MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (NOT (w.keep = false)) MATCH (u)-[:GSP_LOW]->(v:GspLow) RETURN u.uid, v.uid, w.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
MATCH (u:GspAnchor {uid: 1}) MATCH (u)-[:GSP_LOW]->(v:GspLow) MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (NOT (w.keep = false)) RETURN u.uid, v.uid, w.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The high branch has one surviving witness after filtering a large noise region. Evaluating it before the eight-match low branch preserves all eight result rows while avoiding high-side repetition. No DISTINCT or result limit is introduced.

### Expected behavior

The high branch does not refer to v, so its result for the same u need not be recomputed separately for every low-side v. The equivalent branch order demonstrates that the high expansion can be evaluated once and then joined with eight low matches while preserving result multiplicity.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **8**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **7.234x**; median paired extra time: **136.464 ms**. Slow query: **B**. Bootstrap ratio interval: **[7.145, 7.372]**; self-control noise P99: **15.936 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "* Expand (u)-[anon4:GSP_LOW]->(v)", "actual_hits": 9}, {"operator": "* Expand (u)-[anon2:GSP_HIGH]->(w)", "actual_hits": 200002}], "b": [{"operator": "* Expand (u)-[anon4:GSP_HIGH]->(w)", "actual_hits": 1600009}, {"operator": "* Expand (u)-[anon2:GSP_LOW]->(v)", "actual_hits": 9}]}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 7.234241403294761,
  "median_delta_ms": 136.46422838792205,
  "direction_consistency": 1.0,
  "ratio_ci_low": 7.145118047129742,
  "ratio_ci_high": 7.372335522759318,
  "noise_p99_ms": 15.935589128639547
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+-------------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                        | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+-------------------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid, v.uid, w.uid}                 | 9           |   0.044485 %  |   0.010378 ms |
| * Filter (v :GspLow)                            | 9           |   0.006610 %  |   0.001542 ms |
| * Expand (u)-[anon4:GSP_LOW]->(v)               | 9           |   0.785536 %  |   0.183268 ms |
| * Filter (w :GspHigh), Generic {w}              | 2           |  79.936936 %  |  18.649487 ms |
| * Expand (u)-[anon2:GSP_HIGH]->(w)              | 200002      |  19.198742 %  |   4.479115 ms |
| * ScanAllByLabelProperties (u :GspAnchor {uid}) | 2           |   0.027334 %  |   0.006377 ms |
| * Once                                          | 2           |   0.000357 %  |   0.000083 ms |
+-------------------------------------------------+-------------+---------------+---------------+
```

Transformed PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+-------------------------------------------------+-------------+---------------+----------------+
| OPERATOR                                        | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME  |
+-------------------------------------------------+-------------+---------------+----------------+
| * Produce {u.uid, v.uid, w.uid}                 | 9           |   0.008639 %  |   0.014959 ms  |
| * Filter (w :GspHigh), Generic {w}              | 9           |  78.792892 %  |  136.443813 ms |
| * Expand (u)-[anon4:GSP_HIGH]->(w)              | 1600009     |  21.073608 %  |  36.492675 ms  |
| * Filter (v :GspLow)                            | 9           |   0.002743 %  |   0.004750 ms  |
| * Expand (u)-[anon2:GSP_LOW]->(v)               | 9           |   0.117980 %  |   0.204303 ms  |
| * ScanAllByLabelProperties (u :GspAnchor {uid}) | 2           |   0.004139 %  |   0.007167 ms  |
| * Once                                          | 2           |   0.000000 %  |   0.000000 ms  |
+-------------------------------------------------+-------------+---------------+----------------+
```

Missed optimization summary: The high branch has one surviving witness after filtering a large noise region. Evaluating it before the eight-match low branch preserves all eight result rows while avoiding high-side repetition. No DISTINCT or result limit is introduced. Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "* Expand (u)-[anon4:GSP_LOW]->(v)", "actual_hits": 9}, {"operator": "* Expand (u)-[anon2:GSP_HIGH]->(w)", "actual_hits": 200002}], "b": [{"operator": "* Expand (u)-[anon4:GSP_HIGH]->(w)", "actual_hits": 1600009}, {"operator": "* Expand (u)-[anon2:GSP_LOW]->(v)", "actual_hits": 9}]}. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
