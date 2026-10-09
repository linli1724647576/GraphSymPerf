# Missed optimization: independent OPTIONAL MATCH order repeats high-side traversal

I found a potential missed optimization opportunity: reordering two independent OPTIONAL branches preserves all ten rows, including the two NULL-extended rows, but one order recomputes the expensive high branch for the eight low matches of the same anchor.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **200014 nodes**, **200011 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspAnchor) OPTIONAL MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (w.keep = true) OPTIONAL MATCH (u)-[:GSP_LOW]->(v:GspLow) RETURN u.uid, v.uid, w.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
MATCH (u:GspAnchor) OPTIONAL MATCH (u)-[:GSP_LOW]->(v:GspLow) OPTIONAL MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (w.keep = true) RETURN u.uid, v.uid, w.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The high branch depends on u and w, not v. Its filtered result can be evaluated once per u before joining the low branch. The other anchors retain low-only and high-only matches, so the evidence preserves both OPTIONAL NULL extensions rather than deleting them or using DISTINCT.

### Expected behavior

The independent OPTIONAL branches should preserve their complete multiplicities and NULL extensions under reordering. The demonstrated high-first order avoids repeated high traversal for an unchanged u; that work can be avoided without removing any of the ten result rows.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **10**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **5.633x**; median paired extra time: **132.860 ms**. Slow query: **B**. Bootstrap ratio interval: **[5.400, 5.759]**; self-control noise P99: **7.992 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "| * Expand (u)-[anon4:GSP_LOW]->(v)", "actual_hits": 12}, {"operator": "| * Expand (u)-[anon2:GSP_HIGH]->(w)", "actual_hits": 200005}], "b": [{"operator": "| * Expand (u)-[anon4:GSP_HIGH]->(w)", "actual_hits": 1600019}, {"operator": "| * Expand (u)-[anon2:GSP_LOW]->(v)", "actual_hits": 12}]}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 5.6325735268007655,
  "median_delta_ms": 132.8597709070891,
  "direction_consistency": 1.0,
  "ratio_ci_low": 5.399570584585701,
  "ratio_ci_high": 5.7588939546301345,
  "noise_p99_ms": 7.992013930343084
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+--------------------------------------+-------------+---------------+---------------+
| OPERATOR                             | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME |
+--------------------------------------+-------------+---------------+---------------+
| * Produce {u.uid, v.uid, w.uid}      | 11          |   0.015487 %  |   0.004292 ms |
| * Optional                           | 11          |   0.009623 %  |   0.002667 ms |
| |\                                   |             |               |               |
| | * Filter (v :GspLow)               | 12          |   0.008120 %  |   0.002250 ms |
| | * Expand (u)-[anon4:GSP_LOW]->(v)  | 12          |   0.631828 %  |   0.175103 ms |
| | * Once                             | 6           |   0.000000 %  |   0.000000 ms |
| * Optional                           | 4           |   0.051875 %  |   0.014377 ms |
| |\                                   |             |               |               |
| | * Filter (w :GspHigh), {w.keep}    | 5           |  60.191353 %  |  16.681281 ms |
| | * Expand (u)-[anon2:GSP_HIGH]->(w) | 200005      |  15.712408 %  |   4.354498 ms |
| | * Once                             | 6           |   0.000301 %  |   0.000083 ms |
| * Filter (u :GspAnchor)              | 4           |  10.105044 %  |   2.800487 ms |
| * ScanAll (u)                        | 200015      |  13.273509 %  |   3.678587 ms |
| * Once                               | 2           |   0.000451 %  |   0.000125 ms |
+--------------------------------------+-------------+---------------+---------------+
```

Transformed PROFILE excerpt — Table rendered from recorded Memgraph PROFILE rows; not captured console stdout.

```text
+--------------------------------------+-------------+---------------+----------------+
| OPERATOR                             | ACTUAL HITS | RELATIVE TIME | ABSOLUTE TIME  |
+--------------------------------------+-------------+---------------+----------------+
| * Produce {u.uid, v.uid, w.uid}      | 11          |   0.009558 %  |   0.017627 ms  |
| * Optional                           | 11          |   0.013942 %  |   0.025711 ms  |
| |\                                   |             |               |                |
| | * Filter (w :GspHigh), {w.keep}    | 19          |  76.099199 %  |  140.337704 ms |
| | * Expand (u)-[anon4:GSP_HIGH]->(w) | 1600019     |  19.919933 %  |  36.735178 ms  |
| | * Once                             | 20          |   0.000136 %  |   0.000250 ms  |
| * Optional                           | 11          |   0.004203 %  |   0.007751 ms  |
| |\                                   |             |               |                |
| | * Filter (v :GspLow)               | 12          |   0.002599 %  |   0.004792 ms  |
| | * Expand (u)-[anon2:GSP_LOW]->(v)  | 12          |   0.099559 %  |   0.183601 ms  |
| | * Once                             | 6           |   0.000045 %  |   0.000083 ms  |
| * Filter (u :GspAnchor)              | 4           |   1.588764 %  |   2.929905 ms  |
| * ScanAll (u)                        | 200015      |   2.262041 %  |   4.171524 ms  |
| * Once                               | 2           |   0.000023 %  |   0.000042 ms  |
+--------------------------------------+-------------+---------------+----------------+
```

Missed optimization summary: The high branch depends on u and w, not v. Its filtered result can be evaluated once per u before joining the low branch. The other anchors retain low-only and high-only matches, so the evidence preserves both OPTIONAL NULL extensions rather than deleting them or using DISTINCT. Recorded native PROFILE Expand ACTUAL HITS (operator pulls, not Neo4j DB Hits): {"a": [{"operator": "| * Expand (u)-[anon4:GSP_LOW]->(v)", "actual_hits": 12}, {"operator": "| * Expand (u)-[anon2:GSP_HIGH]->(w)", "actual_hits": 200005}], "b": [{"operator": "| * Expand (u)-[anon4:GSP_HIGH]->(w)", "actual_hits": 1600019}, {"operator": "| * Expand (u)-[anon2:GSP_LOW]->(v)", "actual_hits": 12}]}. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
