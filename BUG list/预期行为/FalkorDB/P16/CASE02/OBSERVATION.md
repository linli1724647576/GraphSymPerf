# Expected evaluation behavior: P16 — ALL with an explicit failing element at the head versus the tail

I recorded this equivalent-query pair as an expected-behavior observation. The explicit zero makes ALL false in both list-group orders. Ordinary sequential quantifier evaluation can stop early when the zero is first but performs more predicate checks when it is last; the short reference also changes list-construction work. This construction is excluded from missed-optimization claims.

Status: expected behavior for this construction; not a bug report; do not submit as a bug.

**FalkorDB Version:** 4.18.8 (`MODULE LIST`: graph ver=41808)  
**Operating System:** macOS 27.0; host build: 26A5421a; host kernel: Darwin 27.0.0; container: Debian GNU/Linux 13 (trixie), DEBIAN_VERSION_FULL=13.5; container kernel: 6.12.54-linuxkit; recorded architecture: arm64  
**Installation Method:** Docker image `falkordb/falkordb-server@sha256:d1c8bbc77bc36d5912f726801dad3753f1a0957b4f1d4f7625ae87faedd49918`; CPU quota: 2; container memory: 1536 MiB  
**API/Driver:** FalkorDB Python client 1.7.1

### Steps to reproduce

1. Use an empty disposable database/graph of the recorded version. The attached native graph setup is [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt), identical to [setup.cypher](setup.cypher). Extract all attachments into one directory and run the API reproducer from that directory. It loads the same native statement list from [setup-statements.json](setup-statements.json), uses the attached [parameters.json](parameters.json), waits for the recorded index where required, and runs the complete queries.

```bash
python3 -m pip install falkordb==1.7.1 redis==8.1.0
export GSP_URI='redis://127.0.0.1:6379'
export GSP_DATABASE='gsp_reproducer'
python3 reproduce.py setup
python3 reproduce.py counts
python3 reproduce.py queries
python3 reproduce.py profile
```

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **0 nodes**, **0 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
RETURN all(x IN ([0] + range(1, 40000)) WHERE coalesce(CASE WHEN (x = x) THEN (x <> 0) ELSE null END, null)) AS ok;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
RETURN all(x IN (range(1, 40000) + [0]) WHERE coalesce(CASE WHEN (x = x) THEN (x <> 0) ELSE null END, null)) AS ok;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The list-order transformation preserves the scalar false result, but it changes ordinary evaluation work. These are model-based evaluation-order explanations, not a captured count of individual ALL predicate calls. There is no graph traversal in this case.

### Expected behavior

Both queries should return the same complete scalar false result. A performance difference caused by failure-element position or list materialization is expected for this construction and is not sufficient evidence of an optimizer bug.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **1**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **6.004x**; median paired extra time: **2.327 ms**. Slow query: **B**. Bootstrap ratio interval: **[5.597, 6.564]**; self-control noise P99: **0.835 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded GRAPH.PROFILE produced-record counts and diagnostic operation times: {"a": [], "b": []}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 6.004148271142379,
  "median_delta_ms": 2.3266873322427273,
  "direction_consistency": 1.0,
  "ratio_ci_low": 5.59735841972793,
  "ratio_ci_high": 6.564033545294116,
  "noise_p99_ms": 0.8347654901444902
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-returned GRAPH.PROFILE operator lines, preserved verbatim.

```text
Results | Records produced: 1, Execution time: 0.000209 ms
    Project | Records produced: 1, Execution time: 0.001000 ms
```

Transformed PROFILE excerpt — Server-returned GRAPH.PROFILE operator lines, preserved verbatim.

```text
Results | Records produced: 1, Execution time: 0.000208 ms
    Project | Records produced: 1, Execution time: 2.658042 ms
```

Observation summary: The explicit zero makes ALL false in both list-group orders. Ordinary sequential quantifier evaluation can stop early when the zero is first but performs more predicate checks when it is last; the short reference also changes list-construction work. This construction is excluded from missed-optimization claims. This case is excluded from optimizer bug counts.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [reference.cypher](reference.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
