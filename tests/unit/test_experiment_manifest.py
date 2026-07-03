from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_sym_perf.core import ConfigError, QueryCase, RunPaths, TargetSystem
from graph_sym_perf.experiment.manifest import load_experiment_manifest
from graph_sym_perf.experiment.resume import CheckpointState
from graph_sym_perf.experiment.runner import (
    _apply_run_overrides,
    _build_case_tasks,
    _effective_worker_count,
    _gdsmith_connection_fields,
    _per_system_gdsmith_payload,
    _stable_system_seed,
)
from graph_sym_perf.feedback.state import BOOL_REDUNDANCY_RULES, SYMMETRY_RULES
from graph_sym_perf.experiment.shard import ShardSpec
from graph_sym_perf.gdsmith.parser import GdsmithParsedLog, GdsmithStatementRecord
from graph_sym_perf.gdsmith.runner import GdsmithToolStatus
from graph_sym_perf.reporter import ArtifactWriter


RULES_BY_FAMILY = {
    "bool_redundancy": BOOL_REDUNDANCY_RULES,
    "symmetry": SYMMETRY_RULES,
}

_STRICT_SYMMETRY = frozenset({
    "node_order_symmetry",
    "edge_notation_symmetry",
    "predicate_order_symmetry",
    "pattern_order_symmetry",
    "pattern_decomposition_symmetry",
    "pattern_fusion_symmetry",
    "path_endpoint_symmetry",
    "label_order_symmetry",
    "property_map_order_symmetry",
    "alpha_renaming_symmetry",
})


EXPECTED_REDUCER = {
    "enabled": "selective",
    "strategy": "validated",
    "reduce_graph": True,
    "preserve_metric_ratio": 1.5,
    "max_query_attempts": 32,
}


def test_main_effectiveness_manifest_projects_to_run_config() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")

    run_config = manifest.to_run_config(shard_index=2, num_shards=16)

    assert manifest.experiment_id == "main_effectiveness_2026_01"
    assert [system.target.value for system in manifest.systems] == [
        "neo4j",
        "memgraph",
        "falkordb",
        "agensgraph",
        "arcadedb",
        "apache_age",
        "ladybugdb",
    ]
    assert manifest.source_kind == "multi"
    assert manifest.source_modes == ("gdsmith",)
    assert manifest.total_case_budget(16) == 50000
    assert run_config.query_generation.max_cases == 50000
    assert run_config.query_generation.gdsmith.random_seed == manifest.seed + 2
    assert run_config.paths.artifact_dir.endswith("runs/main_effectiveness_2026_01/{run_id}")


def test_main_effectiveness_manifest_projects_confirmation_thresholds() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")

    run_config = manifest.to_run_config(shard_index=0, num_shards=16)

    assert run_config.confirmation.min_latency_ratio == 5.0
    assert run_config.confirmation.min_latency_delta_ms == 50.0
    assert run_config.confirmation.min_db_hits_ratio == 5.0
    assert run_config.confirmation.min_rows_ratio == 5.0


def test_manifest_projects_coverage_metric_thresholds(tmp_path: Path) -> None:
    manifest_path = tmp_path / "metric_thresholds.yaml"
    manifest_path.write_text(
        """
experiment_id: metric_thresholds
systems:
  targets:
    - neo4j
source:
  kind: gdsmith
  max_cases: 1
transforms:
  enabled_families:
    - symmetry
  enabled_rules:
    - pattern_order_symmetry
coverage:
  metric_thresholds:
    rows: 7.0
execution:
  cases_per_shard: 1
""",
        encoding="utf-8",
    )
    manifest = load_experiment_manifest(manifest_path)

    run_config = manifest.to_run_config(shard_index=0, num_shards=1)

    assert run_config.coverage.metric_thresholds["rows"] == 7.0


def test_replay_known_manifest_uses_cases_jsonl_source() -> None:
    manifest = load_experiment_manifest("config/experiments/replay_known.yaml")

    assert manifest.source_kind == "cases_jsonl"
    assert manifest.execution.shards == 1
    assert Path(str(manifest.source["cases_path"])).name == "replay_known_cases.jsonl"


def test_experiment_manifests_cover_enabled_rule_spaces() -> None:
    # Exploratory rules are intentionally excluded from main/calibration configs
    # (document/总体研究主线.md §4: strict-only for core claims).
    EXPLORATORY_RULES = {
        "exploratory_pattern_order_symmetry",
        "exploratory_predicate_order_symmetry",
        "exploratory_optional_match_order_symmetry",
        "exploratory_with_filter_shift_symmetry",
        "exploratory_projection_reorder_symmetry",
    }
    STRICT_ONLY_CONFIGS = {
        "calibration.yaml",
        "main_effectiveness.yaml",
    }
    EXPLORATORY_ONLY_CONFIGS = {
        "exploratory_symmetry.yaml",
    }
    # Smoke-test configs intentionally use a reduced rule set for speed.
    SMOKE_CONFIGS = {
        "local_ladybugdb_smoke.yaml",
        "local_multidb_gdsmith_smoke.yaml",
        "local_multidb_smoke.yaml",
        "local_neo4j_gdsmith_smoke.yaml",
    }
    for path in sorted(Path("config/experiments").glob("*.yaml")):
        if path.name in SMOKE_CONFIGS:
            continue
        manifest = load_experiment_manifest(path)
        transform_config = manifest.to_run_config(shard_index=0, num_shards=manifest.execution.shards).transforms
        enabled = set(transform_config.enabled_rules)
        missing = [
            rule
            for family in transform_config.enabled_families
            for rule in RULES_BY_FAMILY.get(family, ())
            if rule not in enabled
        ]
        # Allow strict-only configs to intentionally exclude exploratory rules.
        if path.name in STRICT_ONLY_CONFIGS:
            missing = [r for r in missing if r not in EXPLORATORY_RULES]
        # Allow exploratory-only configs to intentionally exclude strict rules.
        if path.name in EXPLORATORY_ONLY_CONFIGS:
            missing = [r for r in missing if r not in _STRICT_SYMMETRY]

        assert not missing, f"{path} misses enabled transform rules: {missing}"


def test_experiment_manifests_use_validated_reducer_config() -> None:
    for path in sorted(Path("config/experiments").glob("*.yaml")):
        manifest = load_experiment_manifest(path)

        assert dict(manifest.reducer) == EXPECTED_REDUCER, f"{path} has stale reducer config"


def test_experiment_run_overrides_project_to_run_config() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    run_config = manifest.to_run_config(shard_index=0, num_shards=16)

    updated = _apply_run_overrides(
        run_config,
        systems="neo4j,memgraph",
        max_cases=25,
        feedback_enabled=False,
    )

    assert [system.target.value for system in updated.systems] == ["neo4j", "memgraph"]
    assert updated.query_generation.max_cases == 25
    assert updated.query_generation.gdsmith.num_queries == 25
    assert updated.feedback.enabled is False


def test_experiment_run_overrides_reject_unknown_system() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    run_config = manifest.to_run_config(shard_index=0, num_shards=16)

    with pytest.raises(ConfigError):
        _apply_run_overrides(run_config, systems="unknown", max_cases=None, feedback_enabled=None)


def test_gdsmith_connection_fields_support_agensgraph_uri() -> None:
    fields = _gdsmith_connection_fields(
        TargetSystem.AGENSGRAPH,
        {"GRAPHSYMPERF_AGENSGRAPH_URI": "postgresql://postgres:secret@127.0.0.1:5456/postgres"},
    )

    assert fields == ("127.0.0.1", 5456, "postgres", "secret")


def test_gdsmith_connection_fields_support_apache_age_uri() -> None:
    fields = _gdsmith_connection_fields(
        TargetSystem.APACHE_AGE,
        {"GRAPHSYMPERF_APACHE_AGE_URI": "postgresql://ageuser:secret@age.example:15432/postgres"},
    )

    assert fields == ("age.example", 15432, "ageuser", "secret")


def test_gdsmith_connection_fields_support_arcadedb_http_url() -> None:
    fields = _gdsmith_connection_fields(
        TargetSystem.ARCADEDB,
        {
            "GRAPHSYMPERF_ARCADEDB_HTTP_URL": "http://arcade.example:2488",
            "GRAPHSYMPERF_ARCADEDB_USER": "root",
            "GRAPHSYMPERF_ARCADEDB_PASSWORD": "secret",
        },
    )

    assert fields == ("arcade.example", 2488, "root", "secret")


def test_stable_system_seed_is_reproducible_and_distinct() -> None:
    seed = 20260522

    assert _stable_system_seed(seed, TargetSystem.NEO4J) == _stable_system_seed(seed, TargetSystem.NEO4J)
    assert _stable_system_seed(seed, TargetSystem.NEO4J) != _stable_system_seed(seed, TargetSystem.MEMGRAPH)


def test_per_system_gdsmith_respects_total_query_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    run_config = manifest.to_run_config(shard_index=0, num_shards=1)
    run_config = replace(
        run_config,
        paths=RunPaths(artifact_dir=f"{tmp_path.as_posix()}/{{run_id}}"),
        query_generation=replace(
            run_config.query_generation,
            max_cases=1,
            gdsmith=replace(run_config.query_generation.gdsmith, num_queries=1),
        ),
    )
    writer = ArtifactWriter(run_config, "budget-smoke")
    calls: list[int] = []

    class Adapter:
        def __init__(self, target: TargetSystem) -> None:
            self.target = target

    def fake_run_gdsmith(**kwargs):
        calls.append(int(kwargs["num_queries"]))
        parsed = GdsmithParsedLog(
            graph_statements=(),
            query_records=(
                GdsmithStatementRecord(
                    index=1,
                    statement=f"MATCH (n) RETURN {len(calls)} AS query_id",
                    kind="query",
                    accepted=True,
                ),
            ),
        )
        return SimpleNamespace(command=("gdsmith",), returncode=0, parsed=parsed)

    monkeypatch.setattr("graph_sym_perf.experiment.runner.run_gdsmith", fake_run_gdsmith)
    monkeypatch.setattr(
        "graph_sym_perf.experiment.runner.check_gdsmith_tooling",
        lambda _config, _env: GdsmithToolStatus(
            state="ready",
            java_ok=True,
            jar_ok=True,
            destructive_ok=True,
            reason="mocked",
            java_path=None,
            jar_env="",
            jar_path=None,
            allow_destructive_env="",
        ),
    )

    payload = _per_system_gdsmith_payload(
        manifest,
        run_config,
        writer,
        {
            "GRAPHSYMPERF_NEO4J_URI": "bolt://127.0.0.1:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
            "GRAPHSYMPERF_MEMGRAPH_HOST": "127.0.0.1",
            "GRAPHSYMPERF_MEMGRAPH_PORT": "7687",
            "GRAPHSYMPERF_FALKORDB_HOST": "127.0.0.1",
            "GRAPHSYMPERF_FALKORDB_PORT": "6379",
        },
        [Adapter(TargetSystem.NEO4J), Adapter(TargetSystem.MEMGRAPH), Adapter(TargetSystem.FALKORDB)],
    )

    assert calls == [1]
    assert len(payload.query_cases) == 1


def test_worker_count_uses_manifest_workers_for_non_destructive_cases() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    manifest = replace(manifest, execution=replace(manifest.execution, workers_per_shard=4))
    case = QueryCase(
        case_id="seed",
        template_name="unit",
        query="MATCH (n) RETURN n",
        parameters={},
        base_predicate="true",
        safe_expression="true",
    )

    tasks = _build_case_tasks(
        manifest,
        ShardSpec(index=0, total=1),
        ((0, case),),
        manifest.systems[:2],
        CheckpointState(),
    )

    assert len(tasks) == 2
    assert _effective_worker_count(manifest, tasks) == 2


def test_worker_count_forces_serial_when_setup_graph_is_destructive() -> None:
    manifest = load_experiment_manifest("config/experiments/replay_known.yaml")
    manifest = replace(manifest, execution=replace(manifest.execution, workers_per_shard=4))
    case = QueryCase(
        case_id="known",
        template_name="unit",
        query="MATCH (n) RETURN n",
        parameters={},
        base_predicate="true",
        safe_expression="true",
        target_system="neo4j",
        setup_path="config/experiments/replay_known_setups/neo4j_13783_reproducer_graph.cypher.txt",
    )

    tasks = _build_case_tasks(
        manifest,
        ShardSpec(index=0, total=1),
        ((0, case),),
        manifest.systems,
        CheckpointState(),
    )

    assert len(tasks) == 1
    assert _effective_worker_count(manifest, tasks) == 1
