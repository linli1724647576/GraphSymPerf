from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import graph_sym_perf.experiment.triage as triage_module
from graph_sym_perf.core import (
    ClusteringConfig,
    ConfirmationConfig,
    CoverageConfig,
    DatasetConfig,
    EquivalenceConfig,
    FeedbackConfig,
    OracleConfig,
    ExecutionResult,
    QueryExecutionError,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
    TransformConfig,
)
from graph_sym_perf.experiment.triage import _query_artifact_pair


def test_client_timeout_interrupts_slow_validation_call() -> None:
    with pytest.raises(TimeoutError, match="slow_call exceeded client timeout"):
        triage_module._call_with_client_timeout(lambda: time.sleep(1), 0.01, "slow_call")


def test_query_artifact_pair_uses_explicit_fast_and_slow_paths(tmp_path: Path) -> None:
    case_dir = tmp_path / "shard_000" / "neo4j" / "case001"
    case_dir.mkdir(parents=True)

    fast, slow = _query_artifact_pair(case_dir, {"fast_query": "fast.cypher", "slow_query": "slow.cypher"})

    assert fast == "fast.cypher"
    assert slow == "slow.cypher"


def test_query_artifact_pair_resolves_pair_paths_from_pmc_evidence(tmp_path: Path) -> None:
    case_dir = tmp_path / "shard_000" / "neo4j" / "case001"
    case_dir.mkdir(parents=True)
    variant_id = "case001_01_and_not_expr_null_contradiction"
    (case_dir / "pmc_evidence.json").write_text(
        json.dumps({"fast_variant_id": "seed", "slow_variant_id": variant_id}),
        encoding="utf-8",
    )

    fast, slow = _query_artifact_pair(
        case_dir,
        {
            f"pair_{variant_id}_fast_query": f"neo4j/case001/pair_{variant_id}_fast_query.cypher",
            f"pair_{variant_id}_slow_query": f"neo4j/case001/pair_{variant_id}_slow_query.cypher",
        },
    )

    assert fast == f"neo4j/case001/pair_{variant_id}_fast_query.cypher"
    assert slow == f"neo4j/case001/pair_{variant_id}_slow_query.cypher"


def test_validate_triage_records_item_error_and_continues(tmp_path: Path, monkeypatch) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "aggregate_experiment_manifest.json").write_text("{}", encoding="utf-8")
    case_dir = run_root / "shard_000" / "neo4j" / "case001"
    case_dir.mkdir(parents=True)
    triage_path = run_root / "triage_one.json"
    triage_path.write_text(
        json.dumps(
            {
                "run_dir": str(run_root),
                "top": 1,
                "items": [
                    {
                        "representative_case_id": "case001",
                        "system": "neo4j",
                        "case_dir": str(case_dir),
                        "artifact_paths": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    run_config = RunConfig(
        source_path="config/test.yaml",
        paths=RunPaths(artifact_dir=str(run_root / "{run_id}")),
        systems=(
            SystemConfig(
                target=TargetSystem.NEO4J,
                required_env=(),
                optional_env={},
                settings={},
            ),
        ),
        dataset=DatasetConfig(),
        query_generation=QueryGenerationConfig(),
        equivalence=EquivalenceConfig(),
        confirmation=ConfirmationConfig(),
        oracle=OracleConfig(),
        coverage=CoverageConfig(),
        feedback=FeedbackConfig(),
        clustering=ClusteringConfig(),
        transforms=TransformConfig(),
    )

    class FakeManifest:
        reducer = {}
        metadata = {}
        execution = object()

        def to_run_config(self):
            return run_config

    closed = {"value": False}

    class FakeAdapter:
        def close(self):
            closed["value"] = True

    monkeypatch.setattr(triage_module, "load_experiment_manifest", lambda path: FakeManifest())
    monkeypatch.setattr(triage_module, "environment_with_dotenv", lambda env: {})
    monkeypatch.setattr(triage_module, "create_adapter", lambda *args, **kwargs: FakeAdapter())

    def fail_validation(*args, **kwargs):
        raise QueryExecutionError("invalid generated predicate")

    monkeypatch.setattr(triage_module, "_validate_one", fail_validation)

    summary = triage_module.validate_triage(triage_path)

    assert closed["value"]
    assert summary["validated"] == 1
    assert summary["passed"] == 0
    assert summary["validation_errors"] == 1
    assert summary["results"][0]["validation_status"] == "error"
    assert summary["results"][0]["error_type"] == "QueryExecutionError"
    assert summary["issue_tracking"]["reportable_count"] == 0


def test_validate_triage_uses_shard_manifest_for_case_config(tmp_path: Path, monkeypatch) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    aggregate_manifest = run_root / "aggregate_experiment_manifest.json"
    aggregate_manifest.write_text("{}", encoding="utf-8")
    case_dir = run_root / "shard_014" / "neo4j" / "case001"
    case_dir.mkdir(parents=True)
    shard_manifest = run_root / "shard_014" / "experiment_manifest.json"
    shard_manifest.write_text("{}", encoding="utf-8")
    triage_path = run_root / "triage_one.json"
    triage_path.write_text(
        json.dumps(
            {
                "run_dir": str(run_root),
                "top": 1,
                "items": [
                    {
                        "representative_case_id": "case001",
                        "system": "neo4j",
                        "case_dir": str(case_dir),
                        "artifact_paths": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def run_config_with_timeout(timeout: float) -> RunConfig:
        return RunConfig(
            source_path="config/test.yaml",
            paths=RunPaths(artifact_dir=str(run_root / "{run_id}")),
            systems=(
                SystemConfig(
                    target=TargetSystem.NEO4J,
                    required_env=(),
                    optional_env={},
                    settings={},
                ),
            ),
            dataset=DatasetConfig(),
            query_generation=QueryGenerationConfig(),
            equivalence=EquivalenceConfig(timeout_seconds=timeout),
            confirmation=ConfirmationConfig(timeout_seconds=timeout),
            oracle=OracleConfig(),
            coverage=CoverageConfig(),
            feedback=FeedbackConfig(),
            clustering=ClusteringConfig(),
            transforms=TransformConfig(),
        )

    class FakeManifest:
        reducer = {}
        metadata = {}
        execution = object()

        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def to_run_config(self):
            return run_config_with_timeout(self.timeout)

    def fake_load_manifest(path):
        if Path(path) == shard_manifest:
            return FakeManifest(10)
        return FakeManifest(60)

    class FakeAdapter:
        def close(self):
            pass

    seen: dict[str, float] = {}

    def fake_validate_one(adapter, case_dir, *, runtime_repeats, reduce, top_item, run_config, manifest):
        seen["equivalence_timeout"] = run_config.equivalence.timeout_seconds
        seen["confirmation_timeout"] = run_config.confirmation.timeout_seconds
        seen["manifest_timeout"] = manifest.timeout
        return {
            "case_id": top_item["representative_case_id"],
            "system": top_item["system"],
            "equivalence_passed": True,
            "support_level": "none",
            "latency_ratio": 1.0,
            "latency_delta_ms": 0.0,
            "db_hits_ratio": None,
            "rows_ratio": None,
            "reducer": {"mode": "not_requested"},
            "issue_draft_path": None,
            "issue_url": None,
            "developer_status": "new",
        }

    monkeypatch.setattr(triage_module, "load_experiment_manifest", fake_load_manifest)
    monkeypatch.setattr(triage_module, "environment_with_dotenv", lambda env: {})
    monkeypatch.setattr(triage_module, "create_adapter", lambda *args, **kwargs: FakeAdapter())
    monkeypatch.setattr(triage_module, "_validate_one", fake_validate_one)

    summary = triage_module.validate_triage(triage_path)

    assert summary["validated"] == 1
    assert seen == {
        "equivalence_timeout": 10,
        "confirmation_timeout": 10,
        "manifest_timeout": 10,
    }


def test_validate_one_passes_manifest_query_timeout_to_equivalence_replay(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    case_dir = run_root / "shard_000" / "neo4j" / "case001"
    case_dir.mkdir(parents=True)
    (case_dir / "parameters.json").write_text("{}", encoding="utf-8")
    (case_dir / "fast.cypher").write_text("RETURN 1 AS ok\n", encoding="utf-8")
    (case_dir / "slow.cypher").write_text("RETURN 1 AS ok\n", encoding="utf-8")

    execute_calls: list[dict[str, object]] = []

    class FakeAdapter:
        target = TargetSystem.NEO4J

        def execute(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
            execute_calls.append(
                {
                    "query": query,
                    "query_kind": query_kind,
                    "max_rows": max_rows,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return ExecutionResult(TargetSystem.NEO4J, query_kind, ({"ok": 1},), 1.0, {})

        def profile(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
            return ExecutionResult(TargetSystem.NEO4J, query_kind, ({"ok": 1},), 1.0, {})

    run_config = RunConfig(
        source_path="config/test.yaml",
        paths=RunPaths(artifact_dir=str(run_root / "{run_id}")),
        systems=(
            SystemConfig(
                target=TargetSystem.NEO4J,
                required_env=(),
                optional_env={},
                settings={},
            ),
        ),
        dataset=DatasetConfig(),
        query_generation=QueryGenerationConfig(),
        equivalence=EquivalenceConfig(timeout_seconds=13),
        confirmation=ConfirmationConfig(timeout_seconds=17),
        oracle=OracleConfig(),
        coverage=CoverageConfig(),
        feedback=FeedbackConfig(),
        clustering=ClusteringConfig(),
        transforms=TransformConfig(),
    )

    result = triage_module._validate_one(
        FakeAdapter(),
        case_dir,
        runtime_repeats=1,
        reduce=False,
        top_item={
            "representative_case_id": "case001",
            "system": "neo4j",
            "case_dir": str(case_dir),
            "artifact_paths": {"fast_query": "neo4j/case001/fast.cypher", "slow_query": "neo4j/case001/slow.cypher"},
        },
        run_config=run_config,
        manifest=object(),
    )

    assert result["equivalence_passed"]
    assert [call["timeout_seconds"] for call in execute_calls] == [13, 13]
