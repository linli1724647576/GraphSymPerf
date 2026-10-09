# Negative test case: P9 — Comparison of WHERE placement

I found no confirmed candidate for this independent A/B comparison under the recorded measurement settings. The complete typed result multisets and checked construction facts agree; this tested input did not produce a confirmed performance candidate under the recorded gates.

Status: no_candidate_this_round; not a bug report; do not submit as a bug.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **20002 nodes**, **10001 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspLow) MATCH (u)-[:GSP_LINK]->(v:GspHigh) WHERE (u.id = 1) RETURN u.uid, v.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **no_candidate**.

```cypher
MATCH (u:GspLow) WHERE (u.id = 1) MATCH (u)-[:GSP_LINK]->(v:GspHigh) RETURN u.uid, v.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): No missed-optimization mechanism is asserted for this measured control case.

### Expected behavior

The transformation should preserve the complete result multiset, including duplicates and NULL values. This control case documents the tested workload and observed result; it does not establish the absence of optimization defects in other graph states.

### Actual behavior

Recorded overall execution status: **no_candidate**. Independent A/B status: **no_candidate**. Result row count recorded by the runner: **1**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **1.069x**; median paired extra time: **0.032 ms**. Slow query: **B**. Bootstrap ratio interval: **[not recorded, not recorded]**; self-control noise P99: **not recorded ms**. The independent A/B record contains **0 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "* Expand (u)-[anon2:GSP_LINK]->(v)", "actual_hits": 2}], "b": [{"operator": "* Expand (u)-[anon2:GSP_LINK]->(v)", "actual_hits": 2}]}.

Independent A/B discovery-only summary:

```json
{
  "slow_side": "b",
  "median_ratio": 1.0694771677423889,
  "median_delta_ms": 0.03249966539442539,
  "direction_consistency": 0.6
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+---------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                    | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+---------------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid, v.uid}                    | 2           |  32.258065 %  |   0.002809 ms |
| * Filter (v :GspHigh)                       | 2           |  17.741935 %  |   0.001545 ms |
| * Expand (u)-[anon2:GSP_LINK]->(v)          | 2           |  28.494624 %  |   0.002481 ms |
| * ScanAllByLabelProperties (u :GspLow {id}) | 2           |  21.505376 %  |   0.001873 ms |
| * Once                                      | 2           |   0.000000 %  |   0.000000 ms |
+---------------------------------------------+-------------+---------------+---------------+
```

Transformed PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+---------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                    | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+---------------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid, v.uid}                    | 2           |  21.428571 %  |   0.001080 ms |
| * Filter (v :GspHigh)                       | 2           |  10.714286 %  |   0.000540 ms |
| * Expand (u)-[anon2:GSP_LINK]->(v)          | 2           |  29.464286 %  |   0.001486 ms |
| * ScanAllByLabelProperties (u :GspLow {id}) | 2           |  37.500000 %  |   0.001891 ms |
| * Once                                      | 2           |   0.892857 %  |   0.000045 ms |
+---------------------------------------------+-------------+---------------+---------------+
```

Test outcome summary: The complete typed result multisets and checked construction facts agree; this tested input did not produce a confirmed performance candidate under the recorded gates. No developer Issue is claimed for this case.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
