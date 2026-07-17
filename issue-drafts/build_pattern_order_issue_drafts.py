#!/usr/bin/env python3
"""Build reviewer-facing issue drafts from durable pattern-order artifacts."""

from __future__ import annotations

import json
import re
from collections import Counter
from statistics import median
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RELATED_ISSUE = "https://github.com/neo4j/neo4j/issues/13813"
ENVIRONMENT = {
    "neo4j_version": "2026.05.0 community",
    "operating_system": "macOS 27.0 arm64",
    "installation_method": "Docker image `neo4j:2026.05.0`",
    "api_driver": "Bolt via Neo4j Python Driver 5.28.4",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _setup_text(corpus: dict[str, Any]) -> str:
    statements = corpus["graph_statements"]
    return "\n".join(
        statement.rstrip().rstrip(";") + ";" for statement in statements
    )


def _query_text(query: str) -> str:
    normalized = query.strip().rstrip(";")
    clause_pattern = re.compile(
        r"\s+(OPTIONAL MATCH|ORDER BY|MATCH|WHERE|UNWIND|WITH|RETURN)\s+"
    )
    return clause_pattern.sub(lambda match: f"\n{match.group(1)} ", normalized) + ";"


def _issue_body(
    *,
    title: str,
    intro: str,
    query_a: str,
    query_b: str,
    nodes: int,
    relationships: int,
    measurement: str,
    metrics: dict[str, float],
    actual_note: str,
) -> str:
    return f"""# {title}

{intro}

Neo4j Version: {ENVIRONMENT['neo4j_version']}
Operating System: {ENVIRONMENT['operating_system']}
Installation Method: {ENVIRONMENT['installation_method']}
API/Driver: {ENVIRONMENT['api_driver']}

### Steps to reproduce

1. Load the attached graph setup file into an empty disposable database:

   ```bash
   cypher-shell -u neo4j -p <password> -d neo4j -f setup.cypher
   ```

2. Verify the graph size:

   ```cypher
   MATCH (n) RETURN count(n) AS nodes;
   -- nodes: {nodes}

   MATCH ()-[r]->() RETURN count(r) AS relationships;
   -- relationships: {relationships}
   ```

3. Run query A:

   ```cypher
   {_query_text(query_a)}
   ```

4. Run query B:

   ```cypher
   {_query_text(query_b)}
   ```

5. Compare ordinary execution time using balanced `AB`/`BA` order. Do not use
   `PROFILE` for the timing samples because profiling instrumentation changes
   the measured execution path.

### Expected behavior

The two queries only reorder comma-separated pattern atoms in the first
ordinary `MATCH` clause. The `WHERE` clause remains attached to the same
`MATCH`, and the reordered atoms introduce the same variables and graph
constraints. The queries should therefore return equivalent results without a
large and systematic runtime difference.

### Actual behavior

The complete typed results were equivalent. {measurement}

| Metric | Query A | Query B / paired result |
|---|---:|---:|
| Median elapsed time | {metrics['median_a_ms']:.3f} ms | {metrics['median_b_ms']:.3f} ms |
| Median A/B ratio | - | {metrics['median_ratio']:.5f}x |
| Median A-B delta | - | {metrics['median_delta_ms']:.3f} ms |
| Ratio CI lower bound | - | {metrics['ci_ratio_low']:.5f}x |
| Delta CI lower bound | - | {metrics['ci_delta_low_ms']:.3f} ms |
| Direction consistency | - | {metrics['direction_consistency']:.4f} |

{actual_note}

### Related report

This has the same surface transformation as #{13813}. The graph, query, Neo4j
version, and ordinary-execution measurements here are different, so I am
including the relationship explicitly for duplicate/root-cause triage:
{RELATED_ISSUE}
"""


def _build_f989() -> None:
    entry = ROOT / (
        "runs/neo4j-pattern-order-1280-10x1000-20260717-v6/entries/"
        "p0-neo4j-discovery-pattern_order_symmetry-1280-1-9abbaaa0150b"
    )
    candidate_path = entry / (
        "candidates/f9891d29092edfaa5bd39b26cf3552f32194026c4b6ed919f74b08c5a3dec994/"
        "candidate.json"
    )
    candidate = _load(candidate_path)
    corpus = _load(entry / "generation_corpus.json")
    validation_path = entry / f"validation/{candidate['pair_hash']}.json"
    validation = _load(validation_path)
    report = validation["report"]
    reduction_path = (
        ROOT
        / "issue-drafts/f989-reduction-verification/reduction_report-20260717.json"
    )
    reduction = _load(reduction_path)
    reduction_report = reduction["reduction"]
    samples = [
        sample
        for session in report["sessions"]
        for sample in session["evidence"]["paired_samples"]
    ]
    evidence = {
        "median_a_ms": median(sample["a_ms"] for sample in samples),
        "median_b_ms": median(sample["b_ms"] for sample in samples),
        "median_ratio": report["validated_ratio"],
        "median_delta_ms": report["pooled_delta_ms"],
        "ci_ratio_low": report["pooled_ratio_ci_low"],
        "ci_delta_low_ms": report["pooled_delta_ci_low_ms"],
        "direction_consistency": min(
            session["evidence"]["direction_consistency"]
            for session in report["sessions"]
        ),
    }
    validation_summary = {
        "stage": report["stage"],
        "submission_path": report["submission_path"],
        "validated_ratio": report["validated_ratio"],
        "pooled_delta_ms": report["pooled_delta_ms"],
        "pooled_ratio_ci_low": report["pooled_ratio_ci_low"],
        "pooled_delta_ci_low_ms": report["pooled_delta_ci_low_ms"],
        "sessions": [
            {
                "session_id": session["session_id"],
                "median_ratio": session["evidence"]["median_ratio"],
                "median_delta_ms": session["evidence"]["median_delta_ms"],
                "ci_ratio_low": session["evidence"]["ci_ratio_low"],
                "ci_delta_low_ms": session["evidence"]["ci_delta_low_ms"],
                "direction_consistency": session["evidence"]["direction_consistency"],
            }
            for session in report["sessions"]
        ],
    }
    reduction_summary = {
        "status": reduction_report["status"],
        "stop_reason": reduction_report["stop_reason"],
        "accepted_step_count": len(reduction_report["accepted_steps"]),
        "rejected_step_count": len(reduction_report["rejected_steps"]),
        "rejection_reasons": dict(
            Counter(step["reason"] for step in reduction_report["rejected_steps"])
        ),
        "final_evidence": reduction_report["final_evidence"],
        "final_validation_reached": reduction_report["final_validation"] is not None,
    }
    destination = ROOT / "issue-drafts/neo4j-pattern-order-f9891d"

    summary = {
        "draft_status": "validation_passed_but_reduction_invalidated_not_submit_ready",
        "likely_duplicate_of": RELATED_ISSUE,
        "source_run": "neo4j-pattern-order-1280-10x1000-20260717-v6",
        "formal_run_status": "provenance_invalidated_not_counted_in_yield",
        "pair_hash": candidate["pair_hash"],
        "rule_id": candidate["entry_identity"]["rule_id"],
        "graph_manifest": candidate["graph_manifest"],
        "stage2_decision": candidate["stage2_decision"],
        "stage2_evidence": candidate["stage2_evidence"],
        "independent_validation": validation_summary,
        "live_reduction_verification": reduction_summary,
        "blocking_gates": [
            "no_reduction_accepted",
            "final_fast_runtime_delta_within_noise",
            "reduced_case_validation_not_reached",
            "likely_duplicate_of_neo4j_13813",
            "source_run_provenance_invalidated",
        ],
        "source_candidate": str(candidate_path.relative_to(ROOT)),
        "source_validation": str(validation_path.relative_to(ROOT)),
        "source_reduction_verification": str(reduction_path.relative_to(ROOT)),
    }
    body = _issue_body(
        title=(
            "Semantically equivalent first-MATCH pattern reorder causes a "
            "25x runtime difference"
        ),
        intro=(
            "I found that reordering two comma-separated patterns inside the "
            "first `MATCH` clause changes ordinary execution time by more than "
            "an order of magnitude, although both queries return the same "
            "complete result."
        ),
        query_a=candidate["query_a"],
        query_b=candidate["query_b"],
        nodes=candidate["graph_manifest"]["actual_nodes"],
        relationships=candidate["graph_manifest"]["actual_relationships"],
        measurement=(
            "The strict validation used two fresh sessions, each with five "
            "warmups and 20 balanced paired measurements."
        ),
        metrics=evidence,
        actual_note=(
            "Query A was slower in all 40 validation pairs. The two session "
            "ratios were 24.74841x and 24.84403x. A subsequent reducer run "
            "could analyze the query after the dependency-parser fix, but none "
            "of its 24 safe deletion proposals preserved the runtime symptom. "
            "Its final five-pair recheck of the unchanged query measured "
            "20.30631x but only 17.836 ms absolute delta, below the sealed "
            "26.384 ms noise floor; GraphSymPerf therefore marked reduction "
            "invalidated and did not promote this case as submit-ready."
        ),
    )
    readme = f"""# Draft status: independently validated, but not submit-ready

This directory contains a GitHub-issue-style draft requested for candidate
`{candidate['pair_hash']}`.

The original full query passed strict independent validation in two fresh
sessions with 20 pairs per session: pooled ratio `24.82700x`, pooled delta
`32.080 ms`, ratio confidence-interval lower bound `22.31071x`, and delta
confidence-interval lower bound `31.295 ms`.

However, the live reducer accepted none of 24 safe deletion proposals. Its
final five-pair recheck of the unchanged query still measured `20.30631x`, but
the absolute delta (`17.836 ms`) was below the sealed noise floor
(`26.384 ms`). The reducer therefore returned `reduction_invalidated` with
`final_runtime_failed`; reduced-case validation was not reached.

In addition, the source v6 run was provenance-invalidated after files were
created in the repository while the run was active. This validation artifact
must not be counted in formal v6 `validated_runtime_yield`.

The transformation also matches the already reported Neo4j issue #13813, so
the default disposition is a non-submittable follow-up reproducer unless a
fresh clean-manifest run produces a reduced, independently revalidated case and
deduplication establishes a distinct root-cause family.

Files:

- `ISSUE.md`: copy-editable developer report in the format of prior reports.
- `setup.cypher`: complete 1,280-node graph attachment.
- `query_a.cypher` and `query_b.cypher`: the measured query pair.
- `evidence_summary.json`: source-bound validation, reduction, provenance, and
  deduplication blockers.
"""

    _write(destination / "ISSUE.md", body)
    _write(destination / "README.md", readme)
    _write(destination / "setup.cypher", _setup_text(corpus))
    _write(destination / "query_a.cypher", _query_text(candidate["query_a"]))
    _write(destination / "query_b.cypher", _query_text(candidate["query_b"]))
    _write(
        destination / "evidence_summary.json",
        json.dumps(summary, indent=2, sort_keys=True),
    )


def _build_f0a4() -> None:
    entry = ROOT / (
        "runs/neo4j-pattern-order-1280-10x1000-20260717-v5/entries/"
        "p0-neo4j-discovery-pattern_order_symmetry-1280-1-9abbaaa0150b"
    )
    candidate_path = entry / (
        "candidates/f0a4e119b9b4cdef2a12d7d94fcfc988f2ef55d1c28ed565c220b661b257bc26/"
        "candidate.json"
    )
    candidate = _load(candidate_path)
    corpus = _load(entry / "generation_corpus.json")
    verification_path = (
        ROOT / "issue-drafts/v5-f0a4-verification/screen_12_report.json"
    )
    verification = _load(verification_path)
    evidence = verification["runtime_evidence"]
    destination = ROOT / "issue-drafts/neo4j-pattern-order-f0a4-v5"

    summary = {
        "draft_status": "strict_precheck_rejected_not_submit_ready",
        "likely_duplicate_of": RELATED_ISSUE,
        "source_run": "neo4j-pattern-order-1280-10x1000-20260717-v5",
        "pair_hash": candidate["pair_hash"],
        "rebound_verification_candidate_hash": verification["provenance"][
            "rebound_candidate_hash"
        ],
        "rule_id": candidate["entry_identity"]["rule_id"],
        "graph_manifest": candidate["graph_manifest"],
        "historical_stage2_evidence": candidate["stage2_evidence"],
        "independent_strict_precheck": verification,
        "failed_submission_predicates": [
            "median_ratio_1.18768_below_1.5",
            "ratio_ci_low_1.12455_below_1.2",
        ],
        "source_candidate": str(candidate_path.relative_to(ROOT)),
        "source_verification": str(verification_path.relative_to(ROOT)),
    }
    body = _issue_body(
        title=(
            "Semantically equivalent MATCH pattern reorder causes a "
            "repeatable multi-second runtime difference"
        ),
        intro=(
            "I observed that reordering two independent node patterns inside "
            "the first `MATCH` clause changes ordinary execution time by "
            "several seconds, although both queries return the same complete "
            "result."
        ),
        query_a=candidate["query_a"],
        query_b=candidate["query_b"],
        nodes=candidate["graph_manifest"]["actual_nodes"],
        relationships=candidate["graph_manifest"]["actual_relationships"],
        measurement=(
            "An isolated verification instance first executed each query for "
            "complete-result equivalence, then collected 12 balanced paired "
            "measurements."
        ),
        metrics=evidence,
        actual_note=(
            "Query A was slower in 11 of 12 pairs. The median absolute gap was "
            "large and its bootstrap confidence interval remained positive; "
            "the relative effect was 1.18768x."
        ),
    )
    readme = f"""# Draft status: rejected by the current submission policy

This directory contains a GitHub-issue-style draft requested for historical
candidate `{candidate['pair_hash']}`.

The independent 12-pair pre-screen confirmed equivalent complete results and a
positive multi-second absolute delta. However, it failed two mandatory
predicates of the configured strong-absolute submission path:

- median ratio `1.18768x` is below `1.5x`;
- ratio confidence-interval lower bound `1.12455x` is below `1.2x`.

Accordingly, this draft must not be counted as `submit_eligible`, a validation
pass, or `validated_runtime_yield`. It is supplied as a copy-editable report of
the observation because the user explicitly requested an issue-format draft.

The transformation also matches Neo4j #13813 and should be treated as likely
supplemental evidence for that existing issue family, not a distinct discovery.

Files:

- `ISSUE.md`: copy-editable developer report in the format of prior reports.
- `setup.cypher`: complete 1,280-node graph attachment.
- `query_a.cypher` and `query_b.cypher`: the independently screened query pair.
- `evidence_summary.json`: historical and independent evidence plus rejection.
"""

    _write(destination / "ISSUE.md", body)
    _write(destination / "README.md", readme)
    _write(destination / "setup.cypher", _setup_text(corpus))
    _write(destination / "query_a.cypher", _query_text(candidate["query_a"]))
    _write(destination / "query_b.cypher", _query_text(candidate["query_b"]))
    _write(
        destination / "evidence_summary.json",
        json.dumps(summary, indent=2, sort_keys=True),
    )


def main() -> None:
    _build_f989()
    _build_f0a4()


if __name__ == "__main__":
    main()
