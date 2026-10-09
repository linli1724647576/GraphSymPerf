# Negative test case: P15 — Comparison of nullable CASE condition and branch dual

I found no confirmed candidate for this independent A/B comparison under the recorded measurement settings. The complete typed result multisets and checked construction facts agree; this tested input did not produce a confirmed performance candidate under the recorded gates.

Status: no_candidate_this_round; not a bug report; do not submit as a bug.

**Neo4j Version:** Neo4j/2026.09.0  
**Operating System:** macOS 27.0; host build: 26A5421a; host kernel: Darwin 27.0.0; container: Debian GNU/Linux 13 (trixie), DEBIAN_VERSION_FULL=13.7; container kernel: 6.12.54-linuxkit; recorded architecture: arm64  
**Installation Method:** Docker image `neo4j@sha256:91fb0bf237c41b7b3dcbe84703aa0b82e0d7d067b16e1c8ab21f03fc679edf4e`; CPU quota: 2; container memory: 4096 MiB  
**API/Driver:** Neo4j Python driver (Bolt) 5.28.4

### Steps to reproduce

1. Use an empty disposable database/graph of the recorded version. The attached native graph setup is [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt), identical to [setup.cypher](setup.cypher). Extract all attachments into one directory and run the API reproducer from that directory. It loads the same native statement list from [setup-statements.json](setup-statements.json), uses the attached [parameters.json](parameters.json), waits for the recorded index where required, and runs the complete queries.

```bash
python3 -m pip install neo4j==5.28.4
export GSP_URI='bolt://127.0.0.1:7687'
export GSP_DATABASE='neo4j'
export GSP_USER='neo4j'
read -s GSP_PASSWORD; export GSP_PASSWORD
python3 reproduce.py setup
python3 reproduce.py counts
python3 reproduce.py queries
python3 reproduce.py profile
```

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **40004 nodes**, **40002 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspAnchor) RETURN u.uid, CASE WHEN coalesce(((u.keep = true) OR (NOT (u.uid = u.uid))), null) THEN true ELSE EXISTS { MATCH (u)-[:GSP_EXPAND]->(x:GspLeaf) WHERE (((NOT (x.mark = false)) OR (NOT (u.uid = u.uid))) OR (NOT (u.uid = u.uid))) } END AS value;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **no_candidate**.

```cypher
MATCH (u:GspAnchor) RETURN u.uid, CASE WHEN (NOT coalesce(coalesce(((u.keep = true) OR (NOT (u.uid = u.uid))), null), false)) THEN EXISTS { MATCH (u)-[:GSP_EXPAND]->(x:GspLeaf) WHERE (((NOT (x.mark = false)) OR (NOT (u.uid = u.uid))) OR (NOT (u.uid = u.uid))) } ELSE true END AS value;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): No missed-optimization mechanism is asserted for this measured control case.

### Expected behavior

Complementing the CASE condition while swapping its branches must preserve the original NULL-to-ELSE behavior as well as ordinary TRUE/FALSE cases. The complete output values and multiplicities must agree. This input did not reach the configured performance candidate gate.

### Actual behavior

Recorded overall execution status: **no_candidate**. Independent A/B status: **no_candidate**. Result row count recorded by the runner: **3**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **1.037x**; median paired extra time: **0.027 ms**. Slow query: **A**. Bootstrap ratio interval: **[not recorded, not recorded]**; self-control noise P99: **not recorded ms**. The independent A/B record contains **0 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE total DB Hits: base 16; transformed 16.

Independent A/B discovery-only summary:

```json
{
  "slow_side": "a",
  "median_ratio": 1.036845828030466,
  "median_delta_ms": 0.02722896169871092,
  "direction_consistency": 0.8
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| Operator         | Id | Details                                                                                              | Estimated Rows | Rows | DB Hits | Memory (Bytes) | Page Cache Hits/Misses |
+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +ProduceResults  |  0 | `u.uid`, value                                                                                       |             10 |    3 |       0 |              0 |                    0/0 |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +Projection      |  1 | cache[u.uid] AS `u.uid`,                                                                             |             10 |    3 |      12 |                |                    0/0 |
| |                |    | CASE                                                                                                 |                |      |         |                |                        |
| |                |    |   WHEN coalesce(NOT cache[u.uid] = cache[u.uid] OR u.keep = true, NULL) THEN true                    |                |      |         |                |                        |
| |                |    |   ELSE EXISTS {   MATCH (u)-[`anon_0`:GSP_EXPAND]->(x)     WHERE x:GspLeaf AND (NOT x.mark IN [false |                |      |         |                |                        |
| |                |    | ] OR NOT u.uid = u.uid) }                                                                            |                |      |         |                |                        |
| |                |    | END AS value                                                                                         |                |      |         |                |                        |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +NodeByLabelScan |  5 | u:GspAnchor                                                                                          |             10 |    3 |       4 |                |                    0/0 |
+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+

Total database accesses: 16, total allocated memory: 64
```

Transformed PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| Operator         | Id | Details                                                                                              | Estimated Rows | Rows | DB Hits | Memory (Bytes) | Page Cache Hits/Misses |
+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +ProduceResults  |  0 | `u.uid`, value                                                                                       |             10 |    3 |       0 |              0 |                    0/0 |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +Projection      |  1 | cache[u.uid] AS `u.uid`,                                                                             |             10 |    3 |      12 |                |                    0/0 |
| |                |    | CASE                                                                                                 |                |      |         |                |                        |
| |                |    |   WHEN NOT coalesce(coalesce(NOT cache[u.uid] = cache[u.uid] OR u.keep = true, NULL), false) THEN EX |                |      |         |                |                        |
| |                |    | ISTS {   MATCH (u)-[`anon_0`:GSP_EXPAND]->(x)     WHERE x:GspLeaf AND (NOT x.mark IN [false] OR NOT  |                |      |         |                |                        |
| |                |    | u.uid = u.uid) }                                                                                     |                |      |         |                |                        |
| |                |    |   ELSE true                                                                                          |                |      |         |                |                        |
| |                |    | END AS value                                                                                         |                |      |         |                |                        |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +NodeByLabelScan |  5 | u:GspAnchor                                                                                          |             10 |    3 |       4 |                |                    0/0 |
+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+

Total database accesses: 16, total allocated memory: 64
```

Test outcome summary: The complete typed result multisets and checked construction facts agree; this tested input did not produce a confirmed performance candidate under the recorded gates. No developer Issue is claimed for this case.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [reference.cypher](reference.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
