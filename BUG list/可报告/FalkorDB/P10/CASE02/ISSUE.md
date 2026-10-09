# Missed optimization: independent MATCH order scans the high branch before an empty indexed input

I found a potential missed optimization opportunity: swapping two independent MATCH clauses preserves an empty complete result, but one order scans the large high branch before the indexed low branch is found empty.

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

2. Verify the graph size against [graph-counts.json](graph-counts.json) when available. Recorded graph size: **250003 nodes**, **200001 relationships**. The native setup differs from [setup.raw.cypher](setup.raw.cypher) only in the database's index syntax; [setup-statements.json](setup-statements.json) preserves statement boundaries.

3. Run the base query, saved as [base.cypher](base.cypher):

```cypher
MATCH (a:GspLow) WHERE (a.id = 1) MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (NOT (c.keep = false)) RETURN a.uid, b.uid, c.uid;
```

4. Run the transformed query, saved as [transformed.cypher](transformed.cypher). The independently recorded pair kind is **symmetry**; comparison status is **candidate**.

```cypher
MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (NOT (c.keep = false)) MATCH (a:GspLow) WHERE (a.id = 1) RETURN a.uid, b.uid, c.uid;
```

The attached [cost-facts.json](cost-facts.json) and [expected-results.json](expected-results.json) preserve this input's checked construction facts and complete constructed expectation. Any separate reference query is preserved as `reference.cypher`; the figures below describe the independent A/B pair, not the top-level winning comparison.

Recorded mechanism interpretation (provisional): The low node label and indexed property exist, but id=1 matches zero nodes. The high branch retains a real successful witness. The fast order reaches the empty indexed input first and performs no high-side traversal; the slow order scans the high region before joining it with the zero-row low input.

### Expected behavior

Once the indexed low input produces zero rows, the independent inner join cannot return any rows. A plan that checks that inexpensive input first should avoid executing the large high branch. The attached high witness ensures the result is data-dependent rather than a globally impossible high branch.

### Actual behavior

Recorded overall execution status: **candidate**. Independent A/B status: **candidate**. Result row count recorded by the runner: **0**. The runner validates complete typed result multisets and constructed expectations on successful ordinary-query measurements; the raw record is attached as [result.json](result.json).

Median paired slow/fast ratio: **21.500x**; median paired extra time: **8.407 ms**. Slow query: **B**. Bootstrap ratio interval: **[17.887, 21.985]**; self-control noise P99: **4.082 ms**. The independent A/B record contains **2 confirmation sessions**; paired samples and A/A, B/B controls are preserved in `result.json`.

Recorded PROFILE work evidence: Recorded GRAPH.PROFILE produced-record counts and diagnostic operation times: {"a": ["Node By Index Scan | (a:GspLow) | Records produced: 0, Execution time: 0.001750 ms", "Conditional Traverse | (c)->(b:GspHigh) | Records produced: 0, Execution time: 0.000000 ms", "Node By Label Scan | (c:GspTarget) | Records produced: 0, Execution time: 0.000000 ms"], "b": ["Conditional Traverse | (b)->(b) | Records produced: 1, Execution time: 0.026834 ms", "Node By Label Scan | (c:GspTarget) | Records produced: 200001, Execution time: 7.304270 ms", "Node By Index Scan | (a:GspLow) | Records produced: 0, Execution time: 0.002209 ms"]}.

Independent A/B confirmation summary:

```json
{
  "slow_side": "b",
  "median_ratio": 21.499675746547723,
  "median_delta_ms": 8.406937355175614,
  "direction_consistency": 1.0,
  "ratio_ci_low": 17.88737188699048,
  "ratio_ci_high": 21.98479926305086,
  "noise_p99_ms": 4.081659992225465
}
```

Ordinary-query measurement settings: `{"bootstrap_samples": 10000, "ci_low": 2.0, "confirmation_pairs": 20, "confirmation_sessions": 2, "confirmation_warmups": 10, "control_pairs": 20, "direction_consistency": 0.9, "discovery_pairs": 10, "discovery_warmups": 10, "max_rows": 100000, "ratio": 3.0, "screen_ratio": 2.0, "timeout_seconds": 15}`. Timing evidence comes from the ordinary-query samples in `result.json`; PROFILE timings are diagnostic and are not the performance oracle. Missing report evidence: `none`. Full provenance is in [evidence.json](evidence.json).

Base PROFILE excerpt — Server-returned GRAPH.PROFILE operator lines, preserved verbatim.

```text
Results | Records produced: 0, Execution time: 0.000082 ms
    Project | Records produced: 0, Execution time: 0.000126 ms
        Cartesian Product | Records produced: 0, Execution time: 0.000083 ms
            Node By Index Scan | (a:GspLow) | Records produced: 0, Execution time: 0.001750 ms
            Conditional Traverse | (c)->(b:GspHigh) | Records produced: 0, Execution time: 0.000000 ms
                Filter | Records produced: 0, Execution time: 0.000000 ms
                    Node By Label Scan | (c:GspTarget) | Records produced: 0, Execution time: 0.000000 ms
```

Transformed PROFILE excerpt — Server-returned GRAPH.PROFILE operator lines, preserved verbatim.

```text
Results | Records produced: 0, Execution time: 0.000084 ms
    Project | Records produced: 0, Execution time: 0.000083 ms
        Cartesian Product | Records produced: 0, Execution time: 0.000499 ms
            Conditional Traverse | (b)->(b) | Records produced: 1, Execution time: 0.026834 ms
                Filter | Records produced: 1, Execution time: 11.974188 ms
                    Node By Label Scan | (c:GspTarget) | Records produced: 200001, Execution time: 7.304270 ms
            Node By Index Scan | (a:GspLow) | Records produced: 0, Execution time: 0.002209 ms
```

Missed optimization summary: The low node label and indexed property exist, but id=1 matches zero nodes. The high branch retains a real successful witness. The fast order reaches the empty indexed input first and performs no high-side traversal; the slow order scans the high region before joining it with the zero-row low input. Recorded GRAPH.PROFILE produced-record counts and diagnostic operation times: {"a": ["Node By Index Scan | (a:GspLow) | Records produced: 0, Execution time: 0.001750 ms", "Conditional Traverse | (c)->(b:GspHigh) | Records produced: 0, Execution time: 0.000000 ms", "Node By Label Scan | (c:GspTarget) | Records produced: 0, Execution time: 0.000000 ms"], "b": ["Conditional Traverse | (b)->(b) | Records produced: 1, Execution time: 0.026834 ms", "Node By Label Scan | (c:GspTarget) | Records produced: 200001, Execution time: 7.304270 ms", "Node By Index Scan | (a:GspLow) | Records produced: 0, Execution time: 0.002209 ms"]}. Developer assessment and root-cause consolidation remain open.

Attachments: [reproducer_graph.cypher.txt](reproducer_graph.cypher.txt) · [setup.cypher](setup.cypher) · [base.cypher](base.cypher) · [transformed.cypher](transformed.cypher) · [result.json](result.json) · [profile.json](profile.json) · [profile-base.txt](profile-base.txt) · [profile-transformed.txt](profile-transformed.txt) · [environment.json](environment.json) · [evidence.json](evidence.json) · [reproduce.py](reproduce.py)
