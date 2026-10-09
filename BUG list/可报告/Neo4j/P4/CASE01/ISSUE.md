# Missed optimization: an EXISTS branch expands before its decisive false AND guard

I found a potential missed optimization opportunity: the original guarded AND query traverses the large branch even for an anchor whose cheap condition is false. A native CASE guard over the same GSP_EXPAND relations preserves the complete result and avoids that traversal.

Status: locally reproduced performance candidate; developer assessment pending.

**Neo4j Version:** Neo4j/2026.09.0  
**Operating System:** macOS 27.0; host build: 26A5421a; host kernel: Darwin 27.0.0; container: Debian GNU/Linux 13 (trixie), DEBIAN_VERSION_FULL=13.7; container kernel: 6.12.54-linuxkit; recorded architecture: arm64  
**Installation Method:** Docker image `neo4j@sha256:91fb0bf237c41b7b3dcbe84703aa0b82e0d7d067b16e1c8ab21f03fc679edf4e`; CPU quota: 2; container memory: 4096 MiB  
**API/Driver:** Neo4j Python driver (Bolt) 5.28.4

### Steps to reproduce

The files referenced below are included in `case-package.zip`; relative links refer to the extracted archive contents.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **400003 nodes**, **400002 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspAnchor) WHERE ((((coalesce(u.keep, false) = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) AND EXISTS { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE (((gsp_leaf.mark = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) }) RETURN u.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **reference**; comparison status is **candidate**.

```cypher
MATCH (u:GspAnchor) WHERE CASE WHEN ((((coalesce(u.keep, false) = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) = false) THEN false ELSE ((((coalesce(u.keep, false) = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) AND EXISTS { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE (((gsp_leaf.mark = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) }) END RETURN u.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The original plan places SemiApply/Expand below the cheap keep Filter. The CASE reference evaluates the decisive guard before the graph subquery and changes neither the GSP_EXPAND edges nor their properties. The input uses EXISTS. This is a reference comparison, not a successful symmetric-operand comparison. The two P4 cases share the observed late-SemiApply phenomenon.

### Expected behavior

For L=false, L AND H is already false regardless of H. CASE WHEN L=false THEN false ELSE L AND H END preserves TRUE/FALSE/NULL behavior and retains the same high-cost graph. The demonstrated false guard should prevent unnecessary high-side expansion. This expected improvement is supported by the same-graph CASE plan, rather than by assuming all equivalent syntax must have identical runtimes.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **1**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **113.172x**; median paired extra time: **152.293 ms**. Slow query: **A**. Bootstrap ratio interval: **[95.660, 123.086]**; self-control noise P99: **47.524 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE total DB Hits: base 800,014; transformed 21.

Independent A/B confirmation summary:

```json
{
  "slow_side": "a",
  "median_ratio": 113.17222129632559,
  "median_delta_ms": 152.29302120860666,
  "direction_consistency": 1.0,
  "ratio_ci_low": 95.66026997832452,
  "ratio_ci_high": 123.08636825886423,
  "noise_p99_ms": 47.524081163574
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+------------------+----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| Operator         | Id | Details                                                                        | Estimated Rows | Rows   | DB Hits | Memory (Bytes) | Page Cache Hits/Misses | Indexes Used       |
+------------------+----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +ProduceResults  |  0 | `u.uid`                                                                        |              1 |      1 |       0 |              0 |                    0/0 |                    |
| |                +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Projection      |  1 | cache[u.uid] AS `u.uid`                                                        |              1 |      1 |       0 |                |                    0/0 |                    |
| |                +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Filter          |  2 | (NOT cache[u.uid] = cache[u.uid] OR coalesce(u.keep, false) = true)            |              1 |      1 |       1 |                |                    0/0 |                    |
| |                +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Distinct        |  3 | u                                                                              |              2 |      1 |       0 |            320 |                    0/0 |                    |
| |                +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Union           |  4 |                                                                                |              2 |      1 |       0 |                |                    0/0 |                    |
| |\               +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +SemiApply     |  5 |                                                                                |              1 |      0 |       0 |                |                    0/0 |                    |
| | |\             +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | | +Filter      |  6 | (NOT cache[u.uid] = cache[u.uid] OR gsp_leaf.mark = true) AND gsp_leaf:GspLeaf |              1 |      0 |       0 |                |                    0/0 |                    |
| | | |            +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | | +Expand(All) |  7 | (u)-[anon_0:GSP_EXPAND]->(gsp_leaf)                                            |              7 |      0 |       0 |                |                    0/0 |                    |
| | | |            +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | | +Filter      |  8 | (cache[u.uid] = cache[u.uid] OR NOT cache[u.uid] = cache[u.uid])               |              0 |      0 |       0 |                |                    0/0 |                    |
| | | |            +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | | +Argument    |  9 | u                                                                              |              1 |      0 |       0 |                |                    0/0 |                    |
| | |              +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Filter        | 10 | NOT cache[u.uid] = cache[u.uid]                                                |              1 |      0 |       0 |                |                    0/0 |                    |
| | |              +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +NodeIndexScan | 11 | RANGE INDEX u:GspAnchor(uid) WHERE uid IS NOT NULL, cache[u.uid]               |              1 |      2 |       3 |                |                    0/0 | gsp_cost_lookup: 1 |
| |                +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Filter          | 12 | cache[u.uid] = cache[u.uid]                                                    |              1 |      1 |       0 |                |                    0/0 |                    |
| |                +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +SemiApply       | 13 |                                                                                |              1 |      1 |       0 |                |                    0/0 |                    |
| |\               +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Filter        | 14 | (NOT cache[u.uid] = cache[u.uid] OR gsp_leaf.mark = true) AND gsp_leaf:GspLeaf |              1 |      0 |  400002 |                |                    0/0 |                    |
| | |              +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Expand(All)   | 15 | (u)-[anon_0:GSP_EXPAND]->(gsp_leaf)                                            |              7 | 400001 |  400005 |                |                    0/0 |                    |
| | |              +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Filter        | 16 | (cache[u.uid] = cache[u.uid] OR NOT cache[u.uid] = cache[u.uid])               |              0 |      2 |       0 |                |                    0/0 |                    |
| | |              +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Argument      | 17 | u                                                                              |              1 |      2 |       0 |                |                    0/0 |                    |
| |                +----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +NodeIndexScan   | 18 | RANGE INDEX u:GspAnchor(uid) WHERE uid IS NOT NULL, cache[u.uid]               |              1 |      2 |       3 |                |                    0/0 | gsp_cost_lookup: 1 |
+------------------+----+--------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+

Total database accesses: 800014, total allocated memory: 384
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
| +ProduceResults  |  0 | `u.uid`                                                                                              |              8 |    1 |       0 |              0 |                    0/0 |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +Projection      |  1 | cache[u.uid] AS `u.uid`                                                                              |              8 |    1 |       0 |                |                    0/0 |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +Filter          |  2 | CoerceToPredicate(CASE                                                                               |              8 |    1 |      18 |                |                    0/0 |
| |                |    |   WHEN ((NOT cache[u.uid] = cache[u.uid] OR coalesce(u.keep, false) = true) AND (cache[u.uid] = cach |                |      |         |                |                        |
| |                |    | e[u.uid] OR NOT cache[u.uid] = cache[u.uid])) = false THEN false                                     |                |      |         |                |                        |
| |                |    |   ELSE (NOT u.uid = u.uid OR coalesce(u.keep, false) = true) AND (u.uid = u.uid OR NOT u.uid = u.uid |                |      |         |                |                        |
| |                |    | ) AND EXISTS {   MATCH (u)-[`anon_0`:GSP_EXPAND]->(gsp_leaf)     WHERE gsp_leaf:GspLeaf AND (NOT u.u |                |      |         |                |                        |
| |                |    | id = u.uid OR gsp_leaf.mark IN [true]) AND (u.uid = u.uid OR NOT u.uid = u.uid) }                    |                |      |         |                |                        |
| |                |    | END)                                                                                                 |                |      |         |                |                        |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +NodeByLabelScan |  3 | u:GspAnchor                                                                                          |             10 |    2 |       3 |                |                    0/0 |
+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+

Total database accesses: 21, total allocated memory: 64
```

Missed optimization summary: The original plan places SemiApply/Expand below the cheap keep Filter. The CASE reference evaluates the decisive guard before the graph subquery and changes neither the GSP_EXPAND edges nor their properties. The input uses EXISTS. This is a reference comparison, not a successful symmetric-operand comparison. The two P4 cases share the observed late-SemiApply phenomenon. Recorded native PROFILE total DB Hits: base 800,014; transformed 21. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
