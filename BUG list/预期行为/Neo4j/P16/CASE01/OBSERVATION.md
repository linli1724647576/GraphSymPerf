# Expected evaluation behavior: P16 — ALL with an explicit failing element at the head versus the tail

I recorded this equivalent-query pair as an expected-behavior observation. The explicit zero makes ALL false in both list-group orders. Ordinary sequential quantifier evaluation can stop early when the zero is first but performs more predicate checks when it is last; the short reference also changes list-construction work. This construction is excluded from missed-optimization claims.

Status: expected behavior for this construction; not a bug report; do not submit as a bug.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **0 nodes**, **0 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
RETURN all(x IN ([0] + range(40000, 1, -1)) WHERE CASE WHEN (x = x) THEN coalesce((x <> 0), null) ELSE null END) AS ok;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **not_confirmed**.

```cypher
RETURN all(x IN (range(40000, 1, -1) + [0]) WHERE CASE WHEN (x = x) THEN coalesce((x <> 0), null) ELSE null END) AS ok;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The list-order transformation preserves the scalar false result, but it changes ordinary evaluation work. These are model-based evaluation-order explanations, not a captured count of individual ALL predicate calls. There is no graph traversal in this case.

### Expected behavior

Both queries should return the same complete scalar false result. A performance difference caused by failure-element position or list materialization is expected for this construction and is not sufficient evidence of an optimizer bug.

### Actual behavior

Recorded overall execution status: **not_confirmed**. Independent A/B status: **not_confirmed**. Result row count recorded by the runner: **1**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **4.194x**; median paired extra time: **2.052 ms**. Slow query: **B**. Bootstrap ratio interval: **[3.946, 4.637]**; self-control noise P99: **2.810 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE total DB Hits: base 0; transformed 0.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 4.194013832321808,
  "median_delta_ms": 2.0518958335742354,
  "direction_consistency": 1.0,
  "ratio_ci_low": 3.9456768881237188,
  "ratio_ci_high": 4.6368927268119275,
  "noise_p99_ms": 2.8098864620551436
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+-----------------+----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| Operator        | Id | Details                                                                     | Estimated Rows | Rows | DB Hits | Memory (Bytes) | Page Cache Hits/Misses |
+-----------------+----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +ProduceResults |  0 | ok                                                                          |              1 |    1 |       0 |              0 |                    0/0 |
| |               +----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +Projection     |  1 | all(x IN $autolist_1 + range($autoint_2, $autoint_3, $autoint_4) WHERE CASE |              1 |    1 |       0 |                |                    0/0 |
|                 |    |   WHEN x = x THEN coalesce(NOT x = $autoint_0, NULL)                        |                |      |         |                |                        |
|                 |    |   ELSE NULL                                                                 |                |      |         |                |                        |
|                 |    | END) AS ok                                                                  |                |      |         |                |                        |
+-----------------+----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+

Total database accesses: 0, total allocated memory: 64
```

Transformed PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+-----------------+----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| Operator        | Id | Details                                                                     | Estimated Rows | Rows | DB Hits | Memory (Bytes) | Page Cache Hits/Misses |
+-----------------+----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +ProduceResults |  0 | ok                                                                          |              1 |    1 |       0 |              0 |                    0/0 |
| |               +----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+
| +Projection     |  1 | all(x IN range($autoint_1, $autoint_2, $autoint_3) + $autolist_4 WHERE CASE |              1 |    1 |       0 |                |                    0/0 |
|                 |    |   WHEN x = x THEN coalesce(NOT x = $autoint_0, NULL)                        |                |      |         |                |                        |
|                 |    |   ELSE NULL                                                                 |                |      |         |                |                        |
|                 |    | END) AS ok                                                                  |                |      |         |                |                        |
+-----------------+----+-----------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+

Total database accesses: 0, total allocated memory: 64
```

Observation summary: The explicit zero makes ALL false in both list-group orders. Ordinary sequential quantifier evaluation can stop early when the zero is first but performs more predicate checks when it is last; the short reference also changes list-construction work. This construction is excluded from missed-optimization claims. This case is excluded from optimizer bug counts.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [reference.cypher](reference.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
