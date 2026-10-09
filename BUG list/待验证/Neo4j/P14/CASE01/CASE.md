# Negative test case: P14 — Comparison of UNION ALL branch order

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **40002 nodes**, **0 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (a:GspLow) RETURN a.uid AS uid UNION ALL MATCH (b:GspHigh) WHERE (NOT (b.keep = false)) RETURN b.uid AS uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **no_candidate**.

```cypher
MATCH (b:GspHigh) WHERE (NOT (b.keep = false)) RETURN b.uid AS uid UNION ALL MATCH (a:GspLow) RETURN a.uid AS uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): No missed-optimization mechanism is asserted for this measured control case.

### Expected behavior

UNION ALL branch reordering should preserve all branch rows and duplicates. With no LIMIT or decisive failure guard, finding a low-branch row does not eliminate the high branch's required work. This case is a control and does not assert that either complete branch is globally avoidable.

### Actual behavior

Recorded overall execution status: **no_candidate**. Independent A/B status: **no_candidate**. Result row count recorded by the runner: **2**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **1.023x**; median paired extra time: **0.128 ms**. Slow query: **B**. Bootstrap ratio interval: **[not recorded, not recorded]**; self-control noise P99: **not recorded ms**. The independent A/B record contains **0 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE total DB Hits: base 80,007; transformed 80,007.

Independent A/B discovery-only summary:

```json
{
  "slow_side": "b",
  "median_ratio": 1.0232339633667336,
  "median_delta_ms": 0.12814556248486042,
  "direction_consistency": 0.5
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+--------------------+----+--------------------+----------------+-------+---------+----------------+------------------------+
| Operator           | Id | Details            | Estimated Rows | Rows  | DB Hits | Memory (Bytes) | Page Cache Hits/Misses |
+--------------------+----+--------------------+----------------+-------+---------+----------------+------------------------+
| +ProduceResults    |  0 | uid                |          19010 |     2 |       0 |              0 |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +Union             |  1 |                    |          19010 |     2 |       0 |                |                    0/0 |
| |\                 +----+--------------------+----------------+-------+---------+----------------+------------------------+
| | +Projection      |  2 | uid                |          19000 |     1 |       0 |                |                    0/0 |
| | |                +----+--------------------+----------------+-------+---------+----------------+------------------------+
| | +Projection      |  3 | b.uid AS uid       |          19000 |     1 |       1 |                |                    0/0 |
| | |                +----+--------------------+----------------+-------+---------+----------------+------------------------+
| | +Filter          |  4 | NOT b.keep = false |          19000 |     1 |   40001 |                |                    0/0 |
| | |                +----+--------------------+----------------+-------+---------+----------------+------------------------+
| | +NodeByLabelScan |  5 | b:GspHigh          |          40001 | 40001 |   40002 |                |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +Projection        |  6 | uid                |             10 |     1 |       0 |                |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +Projection        |  7 | a.uid AS uid       |             10 |     1 |       1 |                |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +NodeByLabelScan   |  8 | a:GspLow           |             10 |     1 |       2 |                |                    0/0 |
+--------------------+----+--------------------+----------------+-------+---------+----------------+------------------------+

Total database accesses: 80007, total allocated memory: 64
```

Transformed PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+--------------------+----+--------------------+----------------+-------+---------+----------------+------------------------+
| Operator           | Id | Details            | Estimated Rows | Rows  | DB Hits | Memory (Bytes) | Page Cache Hits/Misses |
+--------------------+----+--------------------+----------------+-------+---------+----------------+------------------------+
| +ProduceResults    |  0 | uid                |          19010 |     2 |       0 |              0 |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +Union             |  1 |                    |          19010 |     2 |       0 |                |                    0/0 |
| |\                 +----+--------------------+----------------+-------+---------+----------------+------------------------+
| | +Projection      |  2 | uid                |             10 |     1 |       0 |                |                    0/0 |
| | |                +----+--------------------+----------------+-------+---------+----------------+------------------------+
| | +Projection      |  3 | a.uid AS uid       |             10 |     1 |       1 |                |                    0/0 |
| | |                +----+--------------------+----------------+-------+---------+----------------+------------------------+
| | +NodeByLabelScan |  4 | a:GspLow           |             10 |     1 |       2 |                |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +Projection        |  5 | uid                |          19000 |     1 |       0 |                |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +Projection        |  6 | b.uid AS uid       |          19000 |     1 |       1 |                |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +Filter            |  7 | NOT b.keep = false |          19000 |     1 |   40001 |                |                    0/0 |
| |                  +----+--------------------+----------------+-------+---------+----------------+------------------------+
| +NodeByLabelScan   |  8 | b:GspHigh          |          40001 | 40001 |   40002 |                |                    0/0 |
+--------------------+----+--------------------+----------------+-------+---------+----------------+------------------------+

Total database accesses: 80007, total allocated memory: 64
```

Test outcome summary: The complete typed result multisets and checked construction facts agree; this tested input did not produce a confirmed performance candidate under the recorded gates. No developer Issue is claimed for this case.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
