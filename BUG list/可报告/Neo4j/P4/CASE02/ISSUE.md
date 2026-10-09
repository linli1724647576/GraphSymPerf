# Missed optimization: COUNT greater than zero becomes a SemiApply before its false AND guard

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
MATCH (u:GspAnchor) WHERE ((((u.keep = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) AND (COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE coalesce(CASE WHEN (u.uid = u.uid) THEN (coalesce(gsp_leaf.mark, false) = true) ELSE null END, null) } > 0)) RETURN u.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **reference**; comparison status is **candidate**.

```cypher
MATCH (u:GspAnchor) WHERE CASE WHEN ((((u.keep = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) = false) THEN false ELSE ((((u.keep = true) AND (u.uid = u.uid)) OR (NOT (u.uid = u.uid))) AND (COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE coalesce(CASE WHEN (u.uid = u.uid) THEN (coalesce(gsp_leaf.mark, false) = true) ELSE null END, null) } > 0)) END RETURN u.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Input metadata note: `bindings.highCostBool.kind` retains the original builder role (`exists`); the actual input operator is recorded by `base.cypher` and `role_choices.high_operator=count`. The interpretation below follows the input and native PROFILE, without rewriting the raw record.

Recorded mechanism interpretation (provisional): The original plan places SemiApply/Expand below the cheap keep Filter. The CASE reference evaluates the decisive guard before the graph subquery and changes neither the GSP_EXPAND edges nor their properties. The input uses COUNT {...}>0, but the recorded native plan rewrites it to EXISTS/SemiApply; there is no EagerAggregation in this evidence. This is not an aggregation-specific root-cause claim. This is a reference comparison, not a successful symmetric-operand comparison. The two P4 cases share the observed late-SemiApply phenomenon.

### Expected behavior

For L=false, L AND H is already false regardless of H. CASE WHEN L=false THEN false ELSE L AND H END preserves TRUE/FALSE/NULL behavior and retains the same high-cost graph. The demonstrated false guard should prevent unnecessary high-side expansion. This expected improvement is supported by the same-graph CASE plan, rather than by assuming all equivalent syntax must have identical runtimes.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **1**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **86.090x**; median paired extra time: **166.432 ms**. Slow query: **A**. Bootstrap ratio interval: **[81.466, 97.457]**; self-control noise P99: **60.268 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE total DB Hits: base 800,014; transformed 19.

Independent A/B confirmation summary:

```json
{
  "slow_side": "a",
  "median_ratio": 86.08995562666016,
  "median_delta_ms": 166.4321874268353,
  "direction_consistency": 1.0,
  "ratio_ci_low": 81.46572591350267,
  "ratio_ci_high": 97.45702649144987,
  "noise_p99_ms": 60.26788199553261
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 15, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 15, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+------------------+----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| Operator         | Id | Details                                                                       | Estimated Rows | Rows   | DB Hits | Memory (Bytes) | Page Cache Hits/Misses | Indexes Used       |
+------------------+----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +ProduceResults  |  0 | `u.uid`                                                                       |              0 |      1 |       0 |              0 |                    0/0 |                    |
| |                +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Projection      |  1 | cache[u.uid] AS `u.uid`                                                       |              0 |      1 |       0 |                |                    0/0 |                    |
| |                +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Filter          |  2 | (NOT cache[u.uid] = cache[u.uid] OR u.keep = true)                            |              0 |      1 |       1 |                |                    0/0 |                    |
| |                +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Distinct        |  3 | u                                                                             |              2 |      1 |       0 |            320 |                    0/0 |                    |
| |                +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Union           |  4 |                                                                               |              2 |      1 |       0 |                |                    0/0 |                    |
| |\               +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +SemiApply     |  5 |                                                                               |              1 |      0 |       0 |                |                    0/0 |                    |
| | |\             +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | | +Filter      |  6 | CoerceToPredicate(coalesce(CASE                                               |              1 |      0 |       0 |                |                    0/0 |                    |
| | | |            |    |   WHEN cache[u.uid] = cache[u.uid] THEN coalesce(gsp_leaf.mark, false) = true |                |        |         |                |                        |                    |
| | | |            |    |   ELSE NULL                                                                   |                |        |         |                |                        |                    |
| | | |            |    | END, NULL)) AND gsp_leaf:GspLeaf                                              |                |        |         |                |                        |                    |
| | | |            +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | | +Expand(All) |  7 | (u)-[anon_0:GSP_EXPAND]->(gsp_leaf)                                           |              1 |      0 |       0 |                |                    0/0 |                    |
| | | |            +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | | +Argument    |  8 | u                                                                             |              1 |      0 |       0 |                |                    0/0 |                    |
| | |              +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Filter        |  9 | NOT cache[u.uid] = cache[u.uid]                                               |              1 |      0 |       0 |                |                    0/0 |                    |
| | |              +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +NodeIndexScan | 10 | RANGE INDEX u:GspAnchor(uid) WHERE uid IS NOT NULL, cache[u.uid]              |              1 |      2 |       3 |                |                    0/0 | gsp_cost_lookup: 1 |
| |                +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Filter          | 11 | cache[u.uid] = cache[u.uid]                                                   |              1 |      1 |       0 |                |                    0/0 |                    |
| |                +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +SemiApply       | 12 |                                                                               |              1 |      1 |       0 |                |                    0/0 |                    |
| |\               +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Filter        | 13 | CoerceToPredicate(coalesce(CASE                                               |              1 |      0 |  400002 |                |                    0/0 |                    |
| | |              |    |   WHEN cache[u.uid] = cache[u.uid] THEN coalesce(gsp_leaf.mark, false) = true |                |        |         |                |                        |                    |
| | |              |    |   ELSE NULL                                                                   |                |        |         |                |                        |                    |
| | |              |    | END, NULL)) AND gsp_leaf:GspLeaf                                              |                |        |         |                |                        |                    |
| | |              +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Expand(All)   | 14 | (u)-[anon_0:GSP_EXPAND]->(gsp_leaf)                                           |              1 | 400001 |  400005 |                |                    0/0 |                    |
| | |              +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| | +Argument      | 15 | u                                                                             |              1 |      2 |       0 |                |                    0/0 |                    |
| |                +----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +NodeIndexScan   | 16 | RANGE INDEX u:GspAnchor(uid) WHERE uid IS NOT NULL, cache[u.uid]              |              1 |      2 |       3 |                |                    0/0 | gsp_cost_lookup: 1 |
+------------------+----+-------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+

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
| +Filter          |  2 | CoerceToPredicate(CASE                                                                               |              8 |    1 |      16 |                |                    0/0 |
| |                |    |   WHEN ((NOT cache[u.uid] = cache[u.uid] OR u.keep = true) AND (cache[u.uid] = cache[u.uid] OR NOT c |                |      |         |                |                        |
| |                |    | ache[u.uid] = cache[u.uid])) = false THEN false                                                      |                |      |         |                |                        |
| |                |    |   ELSE (NOT u.uid = u.uid OR u.keep = true) AND (u.uid = u.uid OR NOT u.uid = u.uid) AND EXISTS {    |                |      |         |                |                        |
| |                |    | MATCH (u)-[`anon_0`:GSP_EXPAND]->(gsp_leaf)     WHERE gsp_leaf:GspLeaf AND coalesce(CASE     WHEN u. |                |      |         |                |                        |
| |                |    | uid = u.uid THEN coalesce(gsp_leaf.mark, false) IN [true]     ELSE NULL   END, NULL) }               |                |      |         |                |                        |
| |                |    | END)                                                                                                 |                |      |         |                |                        |
| |                +----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +NodeByLabelScan |  3 | u:GspAnchor                                                                                          |             10 |    2 |       3 |                |                    0/0 |
+------------------+----+------------------------------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+

Total database accesses: 19, total allocated memory: 64
```

Missed optimization summary: The original plan places SemiApply/Expand below the cheap keep Filter. The CASE reference evaluates the decisive guard before the graph subquery and changes neither the GSP_EXPAND edges nor their properties. The input uses COUNT {...}>0, but the recorded native plan rewrites it to EXISTS/SemiApply; there is no EagerAggregation in this evidence. This is not an aggregation-specific root-cause claim. This is a reference comparison, not a successful symmetric-operand comparison. The two P4 cases share the observed late-SemiApply phenomenon. Recorded native PROFILE total DB Hits: base 800,014; transformed 19. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
