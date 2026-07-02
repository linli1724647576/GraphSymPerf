from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.core import ConfirmationConfig, ExecutionResult, TargetSystem
from graph_sym_perf.experiment.triage import _reducer_enabled
from graph_sym_perf.reducer import reduce_candidate


class FakeReducerAdapter:
    target = TargetSystem.NEO4J

    def execute(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
        del parameters, query_kind, max_rows, timeout_seconds
        self._ensure_valid(query)
        return ExecutionResult(
            system=self.target,
            query_kind="execute",
            rows=({"count": 1},),
            elapsed_ms=1.0,
            summary={},
        )

    def profile(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
        del parameters, max_rows, timeout_seconds
        self._ensure_valid(query)
        slow = "slow_marker" in query
        return ExecutionResult(
            system=self.target,
            query_kind=query_kind,
            rows=({"count": 1},),
            elapsed_ms=60.0 if slow else 10.0,
            summary={
                "profile": {
                    "operator_type": "NodeByLabelScan",
                    "arguments": {
                        "Rows": 600.0 if slow else 100.0,
                        "DbHits": 600.0 if slow else 100.0,
                    },
                    "children": [],
                }
            },
        )

    def _ensure_valid(self, query: str) -> None:
        if "MATCH" not in query or query.lstrip().upper().startswith("WHERE"):
            raise ValueError(f"invalid fake Cypher: {query}")


def test_reduce_candidate_writes_validated_query_artifacts(tmp_path: Path) -> None:
    summary = reduce_candidate(
        adapter=FakeReducerAdapter(),
        case_id="case001",
        case_dir=tmp_path,
        fast_query="MATCH (n) WHERE n.keep = true RETURN n ORDER BY n.id",
        slow_query="MATCH (n) WHERE n.keep = true AND n.slow_marker = true RETURN n ORDER BY n.id",
        parameters={},
        confirmation_config=ConfirmationConfig(min_latency_ratio=2.0, min_latency_delta_ms=5.0, min_db_hits_ratio=2.0, min_rows_ratio=2.0),
        max_result_rows=100,
        timeout_seconds=30.0,
        runtime_repeats=2,
        preserve_metric_ratio=1.5,
        max_query_attempts=8,
    )

    reduced_dir = tmp_path / "reduced"
    fast_query = (reduced_dir / "fast_query.cypher").read_text(encoding="utf-8")
    slow_query = (reduced_dir / "slow_query.cypher").read_text(encoding="utf-8")
    persisted = json.loads((reduced_dir / "reduction_summary.json").read_text(encoding="utf-8"))

    assert summary["mode"] == "validated_reducer"
    assert summary["final"]["equivalence_passed"] is True
    assert summary["final"]["symptom_ratio"] >= 1.5
    assert "ORDER BY" not in fast_query
    assert "ORDER BY" not in slow_query
    assert "slow_marker" in slow_query
    assert persisted["query_reduction"]["fast_query_path"].endswith("fast_query.cypher")


def test_reduce_candidate_reduces_setup_graph_when_reload_available(tmp_path: Path) -> None:
    adapter = FakeReducerAdapter()
    reloads: list[tuple[str, ...]] = []

    def reload_graph(statements: tuple[str, ...]) -> None:
        reloads.append(statements)

    summary = reduce_candidate(
        adapter=adapter,
        case_id="case002",
        case_dir=tmp_path,
        fast_query="MATCH (n) RETURN n",
        slow_query="MATCH (n) WHERE n.slow_marker = true RETURN n",
        parameters={},
        confirmation_config=ConfirmationConfig(min_latency_ratio=2.0, min_latency_delta_ms=5.0, min_db_hits_ratio=2.0, min_rows_ratio=2.0),
        max_result_rows=100,
        timeout_seconds=30.0,
        runtime_repeats=1,
        setup_statements=("CREATE (:Keep)", "CREATE (:Drop)"),
        reload_graph=reload_graph,
        reduce_graph=True,
    )

    graph_summary = summary["graph_reduction"]
    assert graph_summary["status"] == "reduced"
    assert graph_summary["reduced_statement_count"] == 1
    assert (tmp_path / "reduced" / "reduced_graph.cypher").exists()
    assert reloads


def test_reducer_enabled_respects_manifest_switches() -> None:
    assert _reducer_enabled({"enabled": "selective", "strategy": "validated"})
    assert _reducer_enabled({"enabled": True})
    assert not _reducer_enabled({"enabled": False, "strategy": "validated"})
    assert not _reducer_enabled({"enabled": "disabled"})
    assert not _reducer_enabled({"strategy": "no_op"})
