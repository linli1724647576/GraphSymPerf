# Missed optimization: reversing an undirected filtered path loses node-predicate pruning with a WHERE anchor

I found a potential missed optimization opportunity: reversing a bounded undirected path keeps the endpoint/length result unchanged but moves the node-local ALL predicate out of VarLengthExpand and into a later Filter, increasing traversal work.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **100004 nodes**, **100003 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH p = (u:GspLow)-[:GSP_LINK*1..3]-(v:GspHigh) WHERE ((u.id = 1) AND all(x IN nodes(p) WHERE (x.keep = true))) RETURN u.uid, v.uid, length(p) AS hops;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
MATCH p = (v:GspHigh)-[:GSP_LINK*1..3]-(u:GspLow) WHERE ((u.id = 1) AND all(x IN nodes(p) WHERE (x.keep = true))) RETURN u.uid, v.uid, length(p) AS hops;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The fast plan inlines ALL(x IN nodes(p) WHERE x.keep=true) into VarLengthExpand. The mirrored plan starts from the same indexed low endpoint but expands the failure-prefix noise region before filtering the reconstructed reverse path. This is predicate pushdown/pruning loss, not a measured switch to the high endpoint. Each path has at most four nodes; an ordinary within-path ALL evaluation-order difference cannot account for the number of materialized paths.

### Expected behavior

The mirrored path has the same nodes and length, and a pure node-local ALL predicate is invariant to reversal. Every added noise path passes through keep=false. The planner should be able to preserve the demonstrated early node-predicate pruning when the surface path orientation is reversed, so the failed prefix does not cause traversal of its growing fanout.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **1**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **36.753x**; median paired extra time: **50.072 ms**. Slow query: **B**. Bootstrap ratio interval: **[34.667, 38.028]**; self-control noise P99: **20.049 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded native PROFILE total DB Hits: base 18; transformed 450,020.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 36.752543343463245,
  "median_delta_ms": 50.072062644176185,
  "direction_consistency": 1.0,
  "ratio_ci_low": 34.66684012033603,
  "ratio_ci_high": 38.027667099137716,
  "noise_p99_ms": 20.049178579356507
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+-----------------------+----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+
| Operator              | Id | Details                                                                        | Estimated Rows | Rows | DB Hits | Memory (Bytes) | Page Cache Hits/Misses | Indexes Used       |
+-----------------------+----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+
| +ProduceResults       |  0 | `u.uid`, `v.uid`, hops                                                         |              1 |    1 |       0 |              0 |                    0/0 |                    |
| |                     +----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+
| +Projection           |  1 | cache[u.uid] AS `u.uid`, v.uid AS `v.uid`, length((u)-[anon_0*]-(v)) AS hops   |              1 |    1 |       1 |                |                    0/0 |                    |
| |                     +----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+
| +Filter               |  2 | v:GspHigh                                                                      |              1 |    1 |       2 |                |                    0/0 |                    |
| |                     +----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+
| +VarLengthExpand(All) |  3 | p = (u)-[anon_0:GSP_LINK*..3]-(v) WHERE all(x IN nodes(p) WHERE x.keep = true) |              3 |    2 |      12 |             56 |                    0/0 |                    |
| |                     +----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+
| +CacheProperties      |  4 | cache[u.uid]                                                                   |              1 |    1 |       1 |                |                    0/0 |                    |
| |                     +----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+
| +NodeIndexSeek        |  5 | RANGE INDEX u:GspLow(id) WHERE id = $autoint_0                                 |              1 |    1 |       2 |                |                    0/0 | gsp_cost_lookup: 1 |
+-----------------------+----+--------------------------------------------------------------------------------+----------------+------+---------+----------------+------------------------+--------------------+

Total database accesses: 18, total allocated memory: 120
```

Transformed PROFILE excerpt — Server-supplied Neo4j PROFILE string-representation, preserved verbatim.

```text
Cypher 25

Planner COST

Runtime SLOTTED

Runtime version 2026.09.0

+-----------------------+----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| Operator              | Id | Details                                                                      | Estimated Rows | Rows   | DB Hits | Memory (Bytes) | Page Cache Hits/Misses | Indexes Used       |
+-----------------------+----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +ProduceResults       |  0 | `u.uid`, `v.uid`, hops                                                       |              1 |      1 |       0 |              0 |                    0/0 |                    |
| |                     +----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Projection           |  1 | cache[u.uid] AS `u.uid`, v.uid AS `v.uid`, length((v)-[anon_0*]-(u)) AS hops |              1 |      1 |       1 |                |                    0/0 |                    |
| |                     +----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +Filter               |  2 | v:GspHigh AND all(x IN nodes((v)-[anon_0*]-(u)) WHERE x.keep = true)         |              1 |      1 |  250006 |                |                    0/0 |                    |
| |                     +----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +VarLengthExpand(All) |  3 | (u)-[anon_0:GSP_LINK*..3]-(v)                                                |              4 | 100003 |  200010 |        1600064 |                    0/0 |                    |
| |                     +----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +CacheProperties      |  4 | cache[u.uid]                                                                 |              1 |      1 |       1 |                |                    0/0 |                    |
| |                     +----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+
| +NodeIndexSeek        |  5 | RANGE INDEX u:GspLow(id) WHERE id = $autoint_0                               |              1 |      1 |       2 |                |                    0/0 | gsp_cost_lookup: 1 |
+-----------------------+----+------------------------------------------------------------------------------+----------------+--------+---------+----------------+------------------------+--------------------+

Total database accesses: 450020, total allocated memory: 1600128
```

Missed optimization summary: The fast plan inlines ALL(x IN nodes(p) WHERE x.keep=true) into VarLengthExpand. The mirrored plan starts from the same indexed low endpoint but expands the failure-prefix noise region before filtering the reconstructed reverse path. This is predicate pushdown/pruning loss, not a measured switch to the high endpoint. Each path has at most four nodes; an ordinary within-path ALL evaluation-order difference cannot account for the number of materialized paths. Recorded native PROFILE total DB Hits: base 18; transformed 450,020. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
