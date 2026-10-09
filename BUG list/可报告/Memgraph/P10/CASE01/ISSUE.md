# Missed optimization: independent MATCH order scans the high branch before an empty indexed input

I found a potential missed optimization opportunity: swapping two independent MATCH clauses preserves an empty complete result, but one order scans the large high branch before the indexed low branch is found empty.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **100003 nodes**, **80001 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (a:GspLow {id: 1}) MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (NOT (c.keep = false)) RETURN a.uid, b.uid, c.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (NOT (c.keep = false)) MATCH (a:GspLow {id: 1}) RETURN a.uid, b.uid, c.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The low node label and indexed property exist, but id=1 matches zero nodes. The high branch retains a real successful witness. The fast order reaches the empty indexed input first and performs no high-side traversal; the slow order scans the high region before joining it with the zero-row low input.

### Expected behavior

Once the indexed low input produces zero rows, the independent inner join cannot return any rows. A plan that checks that inexpensive input first should avoid executing the large high branch. The attached high witness ensures the result is data-dependent rather than a globally impossible high branch.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **0**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **22.339x**; median paired extra time: **13.402 ms**. Slow query: **B**. Bootstrap ratio interval: **[19.418, 25.087]**; self-control noise P99: **1.508 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [], "b": [{"operator": "* Expand (b)-[anon1:GSP_LINK]->(c)", "actual_hits": 80002}]}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 22.339211244976482,
  "median_delta_ms": 13.401791569776833,
  "direction_consistency": 1.0,
  "ratio_ci_low": 19.41837858111277,
  "ratio_ci_high": 25.08700968542266,
  "noise_p99_ms": 1.5076111117377875
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+---------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                    | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+---------------------------------------------+-------------+---------------+---------------+
| * Produce {a.uid, b.uid, c.uid}             | 1           |  89.068826 %  |   0.037595 ms |
| * Cartesian                                 | 1           |   5.364372 %  |   0.002264 ms |
| * ScanAllByLabelProperties (a :GspLow {id}) | 1           |   5.465587 %  |   0.002307 ms |
| * Once                                      | 2           |   0.101215 %  |   0.000043 ms |
+---------------------------------------------+-------------+---------------+---------------+
```

Transformed PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+-----------------------------------------------+-------------+---------------+---------------+
| OPERATOR                                      | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+-----------------------------------------------+-------------+---------------+---------------+
| * Produce {a.uid, b.uid, c.uid}               | 1           |   0.005642 %  |   0.000875 ms |
| * Cartesian                                   | 1           |   0.083820 %  |   0.013001 ms |
| |\                                            |             |               |               |
| | * ScanAllByLabelProperties (a :GspLow {id}) | 1           |   0.018806 %  |   0.002917 ms |
| | * Once                                      | 2           |   0.000269 %  |   0.000042 ms |
| * Filter (c :GspTarget), Generic {c}          | 2           |  45.289568 %  |   7.024676 ms |
| * Expand (b)-[anon1:GSP_LINK]->(c)            | 80002       |  22.843050 %  |   3.543090 ms |
| * Filter (b :GspHigh)                         | 20002       |   9.814710 %  |   1.522319 ms |
| * ScanAll (b)                                 | 100004      |  21.943868 %  |   3.403622 ms |
| * Once                                        | 2           |   0.000269 %  |   0.000042 ms |
+-----------------------------------------------+-------------+---------------+---------------+
```

Missed optimization summary: The low node label and indexed property exist, but id=1 matches zero nodes. The high branch retains a real successful witness. The fast order reaches the empty indexed input first and performs no high-side traversal; the slow order scans the high region before joining it with the zero-row low input. Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [], "b": [{"operator": "* Expand (b)-[anon1:GSP_LINK]->(c)", "actual_hits": 80002}]}. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
