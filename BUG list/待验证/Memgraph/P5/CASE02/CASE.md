# Evidence pending or inconclusive: P5 — OR operand order changes existential traversal but remains below the configured runtime gate

I recorded this query pair, but the required evidence or mechanism assessment is incomplete. The recheck shows an independent A/B median ratio of about 2.35, below the configured 3.0 candidate threshold. PROFILE shows different expansion work, but this case requires further assessment of evaluation-order expectations and optimizer opportunity before it is reported as a bug.

Status: needs_review; not a bug report; do not submit as a bug.

**Memgraph Version:** Memgraph/3.10.1  
**Operating System:** macOS 27.0; host build: 26A5421a; host kernel: Darwin 27.0.0; container: Ubuntu 24.04.4 LTS (Noble Numbat); container kernel: 6.12.54-linuxkit; recorded architecture: arm64  
**Installation Method:** Docker image `memgraph/memgraph@sha256:042c8642954a7bde971a9c7683c8b1360e7fcf129a5fae36d63f12449e7154aa`; CPU quota: 2; container memory: 1536 MiB  
**API/Driver:** Neo4j Python driver (Bolt) 5.28.4

### Steps to reproduce

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **40003 nodes**, **40002 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspAnchor) WHERE ((coalesce((coalesce(u.keep, false) = true), null) AND (u.uid = u.uid)) OR EXISTS { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE ((NOT (NOT (coalesce(gsp_leaf.mark, false) = true))) AND (u.uid = u.uid)) }) RETURN u.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **not_confirmed**.

```cypher
MATCH (u:GspAnchor) WHERE (EXISTS { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE ((NOT (NOT (coalesce(gsp_leaf.mark, false) = true))) AND (u.uid = u.uid)) } OR (coalesce((coalesce(u.keep, false) = true), null) AND (u.uid = u.uid))) RETURN u.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The recorded Expand ACTUAL HITS differ between the operand orders. The complete result is unchanged, but the formal confirmation gate did not pass. A Boolean short-circuit order effect alone is insufficient to establish a missed optimization.

### Expected behavior

The transformation should preserve the complete result multiset, including duplicates and NULL values. This control case documents the tested workload and observed result; it does not establish the absence of optimization defects in other graph states.

### Actual behavior

Recorded overall execution status: **not_confirmed**. Independent A/B status: **not_confirmed**. Result row count recorded by the runner: **2**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **2.352x**; median paired extra time: **4.092 ms**. Slow query: **B**. Bootstrap ratio interval: **[2.286, 2.379]**; self-control noise P99: **2.058 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "| * Expand (u)-[anon1:GSP_EXPAND]->(gsp_leaf)", "actual_hits": 1}], "b": [{"operator": "| * Expand (u)-[anon1:GSP_EXPAND]->(gsp_leaf)", "actual_hits": 40002}]}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 2.352384496314894,
  "median_delta_ms": 4.0915419813245535,
  "direction_consistency": 1.0,
  "ratio_ci_low": 2.286137363501982,
  "ratio_ci_high": 2.379063022473013,
  "noise_p99_ms": 2.057612466160207
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+----------------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                           | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+----------------------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid}                                  | 3           |   0.441777 %  |   0.015339 ms |
| * Filter (u :GspAnchor), Generic {u}               | 3           |  33.252101 %  |   1.154582 ms |
| |\                                                 |             |               |               |
| | * EvaluatePatternFilter                          | 40003       |  32.516206 %  |   1.129031 ms |
| |\                                                 |             |               |               |
| | * Limit                                          | 1           |   0.039616 %  |   0.001376 ms |
| | * Filter (gsp_leaf :GspLeaf), Generic {gsp_leaf} | 1           |   0.048019 %  |   0.001667 ms |
| | * Expand (u)-[anon1:GSP_EXPAND]->(gsp_leaf)      | 1           |   0.058824 %  |   0.002042 ms |
| | * Filter {u.uid}                                 | 1           |   0.010804 %  |   0.000375 ms |
| | * Once                                           | 1           |   0.000000 %  |   0.000000 ms |
| * ScanAll (u)                                      | 40004       |  33.631453 %  |   1.167754 ms |
| * Once                                             | 2           |   0.001200 %  |   0.000042 ms |
+----------------------------------------------------+-------------+---------------+---------------+
```

Transformed PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+----------------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                           | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+----------------------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid}                                  | 3           |   0.031793 %  |   0.002625 ms |
| * Filter (u :GspAnchor), Generic {u}               | 3           |  11.715012 %  |   0.967392 ms |
| |\                                                 |             |               |               |
| | * EvaluatePatternFilter                          | 40003       |  11.153334 %  |   0.921010 ms |
| |\                                                 |             |               |               |
| | * Limit                                          | 2           |   0.052989 %  |   0.004376 ms |
| | * Filter (gsp_leaf :GspLeaf), Generic {gsp_leaf} | 2           |  51.490240 %  |   4.251914 ms |
| | * Expand (u)-[anon1:GSP_EXPAND]->(gsp_leaf)      | 40002       |  11.324916 %  |   0.935178 ms |
| | * Filter {u.uid}                                 | 3           |   0.022205 %  |   0.001834 ms |
| | * Once                                           | 3           |   0.000000 %  |   0.000000 ms |
| * ScanAll (u)                                      | 40004       |  14.209512 %  |   1.173380 ms |
| * Once                                             | 2           |   0.000000 %  |   0.000000 ms |
+----------------------------------------------------+-------------+---------------+---------------+
```

Test outcome summary: The recheck shows an independent A/B median ratio of about 2.35, below the configured 3.0 candidate threshold. PROFILE shows different expansion work, but this case requires further assessment of evaluation-order expectations and optimizer opportunity before it is reported as a bug. No developer Issue is claimed for this case.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [reference.cypher](reference.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
