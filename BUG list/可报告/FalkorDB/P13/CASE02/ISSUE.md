# Missed optimization: independent OPTIONAL MATCH order repeats high-side traversal

I found a potential missed optimization opportunity: reordering two independent OPTIONAL branches preserves all ten rows, including the two NULL-extended rows, but one order recomputes the expensive high branch for the eight low matches of the same anchor.

Status: locally reproduced performance candidate; developer assessment pending.

**FalkorDB Version:** 4.18.8 (`MODULE LIST`: graph ver=41808)  
**Operating System:** macOS 27.0; host build: 26A5421a; host kernel: Darwin 27.0.0; container: Debian GNU/Linux 13 (trixie), DEBIAN_VERSION_FULL=13.5; container kernel: 6.12.54-linuxkit; recorded architecture: arm64  
**Installation Method:** Docker image `falkordb/falkordb-server@sha256:d1c8bbc77bc36d5912f726801dad3753f1a0957b4f1d4f7625ae87faedd49918`; CPU quota: 2; container memory: 1536 MiB  
**API/Driver:** FalkorDB Python client 1.7.1

### Steps to reproduce

The files referenced below are included in `case-package.zip`; relative links refer to the extracted archive contents.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **200014 nodes**, **200011 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (u:GspAnchor) OPTIONAL MATCH (u)-[:GSP_LOW]->(v:GspLow) OPTIONAL MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (coalesce(w.keep, false) = true) RETURN u.uid, v.uid, w.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
MATCH (u:GspAnchor) OPTIONAL MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (coalesce(w.keep, false) = true) OPTIONAL MATCH (u)-[:GSP_LOW]->(v:GspLow) RETURN u.uid, v.uid, w.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The high branch depends on u and w, not v. Its filtered result can be evaluated once per u before joining the low branch. The other anchors retain low-only and high-only matches, so the evidence preserves both OPTIONAL NULL extensions rather than deleting them or using DISTINCT.

### Expected behavior

The independent OPTIONAL branches should preserve their complete multiplicities and NULL extensions under reordering. The demonstrated high-first order avoids repeated high traversal for an unchanged u; that work can be avoided without removing any of the ten result rows.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **10**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **7.591x**; median paired extra time: **94.500 ms**. Slow query: **A**. Bootstrap ratio interval: **[7.539, 7.669]**; self-control noise P99: **8.502 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded GRAPH.PROFILE produced-record counts and diagnostic operation times: {"a": ["Optional Conditional Traverse | (u)->(v:GspLow) | Records produced: 10, Execution time: 0.041789 ms", "Node By Label Scan | (u:GspAnchor) | Records produced: 3, Execution time: 0.000459 ms", "Conditional Traverse | (u)->(w:GspHigh) | Records produced: 1600009, Execution time: 64.960411 ms"], "b": ["Optional Conditional Traverse | (u)->(v:GspLow) | Records produced: 10, Execution time: 0.035829 ms", "Node By Label Scan | (u:GspAnchor) | Records produced: 3, Execution time: 0.000418 ms", "Conditional Traverse | (u)->(w:GspHigh) | Records produced: 200002, Execution time: 7.749813 ms"]}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "a",
  "median_ratio": 7.591141543866003,
  "median_delta_ms": 94.49993749149144,
  "direction_consistency": 1.0,
  "ratio_ci_low": 7.539128716481269,
  "ratio_ci_high": 7.668947767217917,
  "noise_p99_ms": 8.502259082160885
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-returned GRAPH.PROFILE operator lines, preserved verbatim.

```text
Results | Records produced: 10, Execution time: 0.001041 ms
    Project | Records produced: 10, Execution time: 0.004624 ms
        Apply | Records produced: 10, Execution time: 0.016001 ms
            Optional Conditional Traverse | (u)->(v:GspLow) | Records produced: 10, Execution time: 0.041789 ms
                Node By Label Scan | (u:GspAnchor) | Records produced: 3, Execution time: 0.000459 ms
            Optional | Records produced: 10, Execution time: 0.001503 ms
                Filter | Records produced: 9, Execution time: 94.868922 ms
                    Conditional Traverse | (u)->(w:GspHigh) | Records produced: 1600009, Execution time: 64.960411 ms
                        Argument | Records produced: 10, Execution time: 0.000832 ms
```

Transformed PROFILE excerpt — Server-returned GRAPH.PROFILE operator lines, preserved verbatim.

```text
Results | Records produced: 10, Execution time: 0.000628 ms
    Project | Records produced: 10, Execution time: 0.002376 ms
        Optional Conditional Traverse | (u)->(v:GspLow) | Records produced: 10, Execution time: 0.035829 ms
            Apply | Records produced: 3, Execution time: 0.003293 ms
                Node By Label Scan | (u:GspAnchor) | Records produced: 3, Execution time: 0.000418 ms
                Optional | Records produced: 3, Execution time: 0.000290 ms
                    Filter | Records produced: 2, Execution time: 11.838644 ms
                        Conditional Traverse | (u)->(w:GspHigh) | Records produced: 200002, Execution time: 7.749813 ms
                            Argument | Records produced: 3, Execution time: 0.000168 ms
```

Missed optimization summary: The high branch depends on u and w, not v. Its filtered result can be evaluated once per u before joining the low branch. The other anchors retain low-only and high-only matches, so the evidence preserves both OPTIONAL NULL extensions rather than deleting them or using DISTINCT. Recorded GRAPH.PROFILE produced-record counts and diagnostic operation times: {"a": ["Optional Conditional Traverse | (u)->(v:GspLow) | Records produced: 10, Execution time: 0.041789 ms", "Node By Label Scan | (u:GspAnchor) | Records produced: 3, Execution time: 0.000459 ms", "Conditional Traverse | (u)->(w:GspHigh) | Records produced: 1600009, Execution time: 64.960411 ms"], "b": ["Optional Conditional Traverse | (u)->(v:GspLow) | Records produced: 10, Execution time: 0.035829 ms", "Node By Label Scan | (u:GspAnchor) | Records produced: 3, Execution time: 0.000418 ms", "Conditional Traverse | (u)->(w:GspHigh) | Records produced: 200002, Execution time: 7.749813 ms"]}. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
