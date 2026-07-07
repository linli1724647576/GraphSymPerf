from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_sym_perf.core import ConfigError, QueryCase, RunPaths, TargetSystem
from graph_sym_perf.experiment.manifest import load_experiment_manifest
from graph_sym_perf.experiment.resume import CheckpointState
from graph_sym_perf.experiment.runner import (
    _GDMSITH_PROXY,
    _apply_run_overrides,
    _build_case_tasks,
    _effective_worker_count,
    _gdsmith_backend,
    _gdsmith_backend_target,
    _gdsmith_connection_fields,
    _per_system_gdsmith_payload,
    _select_shard_cases,
    _reuse_per_system_gdsmith_payload,
    _resolve_proxy_connection_fields,
    _stable_system_seed,
    _validate_gdsmith_env,
    SourcePayload,
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


@pytest.mark.parametrize(
    ("path", "expected_experiment_id", "expected_output_root", "expected_queries", "backend", "expected_node_cap", "description_token"),
    [
        (
            "config/experiments/neo4j_10k.yaml",
            "neo4j_10k_2026_07_07",
            "runs/neo4j_10k_2026_07_07",
            10000,
            "neo4j",
            "512",
            "10k-query",
        ),
        (
            "config/experiments/memgraph_10k.yaml",
            "memgraph_10k_2026_07_07",
            "runs/memgraph_10k_2026_07_07",
            10000,
            "memgraph",
            "512",
            "10k-query",
        ),
        (
            "config/experiments/neo4j_5k.yaml",
            "neo4j_5k_2026_07_07",
            "runs/neo4j_5k_2026_07_07",
            5000,
            "neo4j",
            "128",
            "128 nodes",
        ),
    ],
)
def test_runtime_preset_manifests_match_declared_scale(
    path: str,
    expected_experiment_id: str,
    expected_output_root: str,
    expected_queries: int,
    backend: str,
    expected_node_cap: str,
    description_token: str,
) -> None:
    manifest = load_experiment_manifest(path)
    run_config = manifest.to_run_config(shard_index=0, num_shards=1)

    assert manifest.experiment_id == expected_experiment_id
    assert manifest.output_root == Path(expected_output_root)
    assert description_token in manifest.description
    assert run_config.query_generation.gdsmith.num_queries == expected_queries
    args = run_config.query_generation.gdsmith.backend_extra_args[backend]
    assert "--max-node-num" in args
    assert args[args.index("--max-node-num") + 1] == expected_node_cap


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


def test_manifest_loader_accepts_runtime_snapshot_systems(tmp_path: Path) -> None:
    manifest_path = tmp_path / "snapshot_manifest.json"
    manifest_path.write_text(
        """
experiment_id: snapshot_manifest
output_root: runs/snapshot_manifest
systems:
  - target: neo4j
    required_env:
      - GRAPHSYMPERF_NEO4J_URI
    optional_env:
      database: GRAPHSYMPERF_NEO4J_DATABASE
    settings: {}
    capabilities:
      supports_execute: true
      supports_explain: true
      supports_profile: true
      has_db_hits: true
      has_actual_rows: true
      has_estimated_rows: true
      has_page_cache_metrics: true
      has_memory_metrics: true
      plan_tree_reliable: true
      profile_text_reliable: true
      latency_reliable: true
      supports_destructive_load: true
      dialect: cypher
      notes:
        - loaded-from-snapshot
source:
  kind: gdsmith
  max_cases: 1
transforms:
  enabled_families:
    - symmetry
  enabled_rules:
    - pattern_order_symmetry
execution:
  cases_per_shard: 1
""",
        encoding="utf-8",
    )

    manifest = load_experiment_manifest(manifest_path)
    run_config = manifest.to_run_config(shard_index=0, num_shards=1)

    assert [system.target.value for system in manifest.systems] == ["neo4j"]
    assert manifest.systems[0].capabilities.supports_profile is True
    assert manifest.systems[0].capabilities.notes == ("loaded-from-snapshot",)
    assert run_config.systems[0].capabilities.has_db_hits is True


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
        "main_effectiveness_v2.yaml",
        "main_effectiveness_v2_gap_targets.yaml",
        "symmetry_effectiveness.yaml",
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
        "local_single_dbms_smoke.yaml",
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


def test_per_system_gdsmith_backend_mapping_uses_agensgraph_for_postgres_graph_targets() -> None:
    assert _gdsmith_backend(TargetSystem.AGENSGRAPH) == "agensgraph"
    assert _gdsmith_backend_target("agensgraph") is TargetSystem.AGENSGRAPH
    assert TargetSystem.AGENSGRAPH not in _GDMSITH_PROXY
    assert _GDMSITH_PROXY[TargetSystem.APACHE_AGE] == "agensgraph"
    assert _GDMSITH_PROXY[TargetSystem.LADYBUGDB] == "neo4j"


def test_agensgraph_proxy_rejects_bolt_uri() -> None:
    with pytest.raises(ConfigError, match="PostgreSQL URI"):
        _resolve_proxy_connection_fields(
            TargetSystem.APACHE_AGE,
            TargetSystem.AGENSGRAPH,
            "agensgraph",
            {"GRAPHSYMPERF_GDMSITH_PROXY_APACHE_AGE_URI": "bolt://127.0.0.1:7697"},
        )


def test_gdsmith_connection_fields_prefer_arcadedb_postgres_endpoint() -> None:
    fields = _gdsmith_connection_fields(
        TargetSystem.ARCADEDB,
        {
            "GRAPHSYMPERF_ARCADEDB_HTTP_URL": "http://arcade.example:2488",
            "GRAPHSYMPERF_ARCADEDB_POSTGRES_HOST": "arcade-pg.example",
            "GRAPHSYMPERF_ARCADEDB_POSTGRES_PORT": "15432",
            "GRAPHSYMPERF_ARCADEDB_BOLT_HOST": "arcade-bolt.example",
            "GRAPHSYMPERF_ARCADEDB_BOLT_PORT": "2425",
            "GRAPHSYMPERF_ARCADEDB_USER": "root",
            "GRAPHSYMPERF_ARCADEDB_PASSWORD": "secret",
        },
    )

    assert fields == ("arcade-pg.example", 15432, "root", "secret")


def test_gdsmith_connection_fields_fall_back_to_arcadedb_bolt_endpoint() -> None:
    fields = _gdsmith_connection_fields(
        TargetSystem.ARCADEDB,
        {
            "GRAPHSYMPERF_ARCADEDB_HTTP_URL": "http://arcade.example:2488",
            "GRAPHSYMPERF_ARCADEDB_BOLT_HOST": "arcade-bolt.example",
            "GRAPHSYMPERF_ARCADEDB_BOLT_PORT": "2425",
            "GRAPHSYMPERF_ARCADEDB_USER": "root",
            "GRAPHSYMPERF_ARCADEDB_PASSWORD": "secret",
        },
    )

    assert fields == ("arcade-bolt.example", 2425, "root", "secret")


def test_validate_gdsmith_env_accepts_arcadedb_postgres_endpoint() -> None:
    _validate_gdsmith_env(
        TargetSystem.ARCADEDB,
        {
            "GRAPHSYMPERF_ARCADEDB_POSTGRES_HOST": "arcade-pg.example",
            "GRAPHSYMPERF_ARCADEDB_POSTGRES_PORT": "15432",
        },
    )


def test_gdsmith_connection_fields_support_arcadedb_http_url_fallback() -> None:
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


def test_per_system_gdsmith_loads_native_target_graph_into_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    writer = ArtifactWriter(run_config, "native-load-smoke")
    events: list[str] = []

    class Adapter:
        target = TargetSystem.FALKORDB

        def load_gdsmith_graph(self, statements):
            assert tuple(statements) == ("CREATE (n0 :L0{id : 1})",)
            events.append("load")

    def fake_run_gdsmith(**_kwargs):
        parsed = GdsmithParsedLog(
            graph_statements=("CREATE (n0 :L0{id : 1})",),
            query_records=(
                GdsmithStatementRecord(
                    index=1,
                    statement="MATCH (n) RETURN n",
                    kind="query",
                    accepted=True,
                ),
            ),
        )
        return SimpleNamespace(command=("gdsmith",), returncode=0, parsed=parsed)

    def fake_reset(adapter) -> None:
        assert adapter.target is TargetSystem.FALKORDB
        events.append("reset")

    monkeypatch.setattr("graph_sym_perf.experiment.runner.run_gdsmith", fake_run_gdsmith)
    monkeypatch.setattr("graph_sym_perf.experiment.runner._reset_adapter_for_known_issue_setup", fake_reset)
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
            "GRAPHSYMPERF_FALKORDB_HOST": "127.0.0.1",
            "GRAPHSYMPERF_FALKORDB_PORT": "6379",
        },
        [Adapter()],
    )

    assert events == ["reset", "load"]
    assert len(payload.query_cases) == 1


def test_per_system_gdsmith_loads_graph_into_every_target_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    run_config = manifest.to_run_config(shard_index=0, num_shards=1)
    run_config = replace(
        run_config,
        paths=RunPaths(artifact_dir=f"{tmp_path.as_posix()}/{{run_id}}"),
        query_generation=replace(
            run_config.query_generation,
            max_cases=7,
            gdsmith=replace(run_config.query_generation.gdsmith, num_queries=7),
        ),
    )
    writer = ArtifactWriter(run_config, "all-target-load-smoke")
    events: list[str] = []

    class Adapter:
        def __init__(self, target: TargetSystem) -> None:
            self.target = target

        def load_gdsmith_graph(self, statements):
            assert tuple(statements) == ("CREATE (n0 :L0{id : 1})",)
            events.append(f"load:{self.target.value}")

    targets = (
        TargetSystem.NEO4J,
        TargetSystem.MEMGRAPH,
        TargetSystem.FALKORDB,
        TargetSystem.AGENSGRAPH,
        TargetSystem.ARCADEDB,
        TargetSystem.APACHE_AGE,
        TargetSystem.LADYBUGDB,
    )

    def fake_run_gdsmith(**_kwargs):
        parsed = GdsmithParsedLog(
            graph_statements=("CREATE (n0 :L0{id : 1})",),
            query_records=(
                GdsmithStatementRecord(
                    index=1,
                    statement="MATCH (n) RETURN n",
                    kind="query",
                    accepted=True,
                ),
            ),
        )
        return SimpleNamespace(command=("gdsmith",), returncode=0, parsed=parsed)

    def fake_reset(adapter) -> None:
        events.append(f"reset:{adapter.target.value}")

    monkeypatch.setattr("graph_sym_perf.experiment.runner.run_gdsmith", fake_run_gdsmith)
    monkeypatch.setattr("graph_sym_perf.experiment.runner._reset_adapter_for_known_issue_setup", fake_reset)
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
            "GRAPHSYMPERF_AGENSGRAPH_URI": "postgresql://postgres:secret@127.0.0.1:5432/postgres",
            "GRAPHSYMPERF_ARCADEDB_HTTP_URL": "http://127.0.0.1:2480",
            "GRAPHSYMPERF_GDMSITH_PROXY_APACHE_AGE_URI": (
                "postgresql://postgres:secret@127.0.0.1:15432/postgres"
            ),
            "GRAPHSYMPERF_GDMSITH_PROXY_LADYBUGDB_URI": "bolt://127.0.0.1:17687",
        },
        [Adapter(target) for target in targets],
    )

    assert events == [event for target in targets for event in (f"reset:{target.value}", f"load:{target.value}")]
    assert [case.target_system for case in payload.query_cases] == [target.value for target in targets]


def test_per_system_gdsmith_resets_proxy_target_before_cross_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    writer = ArtifactWriter(run_config, "proxy-reset-smoke")
    events: list[str] = []

    class Adapter:
        target = TargetSystem.LADYBUGDB

        def load_gdsmith_graph(self, statements):
            assert tuple(statements) == ("CREATE (n0 :L0{id : 1})",)
            events.append("load")

    def fake_run_gdsmith(**_kwargs):
        parsed = GdsmithParsedLog(
            graph_statements=("CREATE (n0 :L0{id : 1})",),
            query_records=(
                GdsmithStatementRecord(
                    index=1,
                    statement="MATCH (n) RETURN n",
                    kind="query",
                    accepted=True,
                ),
            ),
        )
        return SimpleNamespace(command=("gdsmith",), returncode=0, parsed=parsed)

    def fake_reset(adapter) -> None:
        assert adapter.target is TargetSystem.LADYBUGDB
        events.append("reset")

    monkeypatch.setattr("graph_sym_perf.experiment.runner.run_gdsmith", fake_run_gdsmith)
    monkeypatch.setattr("graph_sym_perf.experiment.runner._reset_adapter_for_known_issue_setup", fake_reset)
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
        },
        [Adapter()],
    )

    assert events == ["reset", "load"]
    assert len(payload.query_cases) == 1


def test_resume_reuses_per_system_gdsmith_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    run_config = manifest.to_run_config(shard_index=0, num_shards=1)
    run_config = replace(run_config, paths=RunPaths(artifact_dir=f"{tmp_path.as_posix()}/{{run_id}}"))
    writer = ArtifactWriter(run_config, "resume-source-smoke")
    events: list[str] = []

    class Adapter:
        target = TargetSystem.LADYBUGDB

        def load_gdsmith_graph(self, statements):
            events.append(f"load:{tuple(statements)}")

    (writer.run_dir / "ladybugdb_gdsmith_queries.jsonl").write_text(
        (
            '{"accepted":true,"index":7,"kind":"query","skip_reason":null,'
            '"statement":"MATCH (n) RETURN n"}\n'
        ),
        encoding="utf-8",
    )
    (writer.run_dir / "ladybugdb_gdsmith_graph.cypher").write_text(
        "CREATE (n0 :L0{id : 1});\n",
        encoding="utf-8",
    )

    def fail_run_gdsmith(**_kwargs):
        raise AssertionError("resume should not regenerate GDsmith source")

    def fake_reset(adapter) -> None:
        assert adapter.target is TargetSystem.LADYBUGDB
        events.append("reset")

    monkeypatch.setattr("graph_sym_perf.experiment.runner.run_gdsmith", fail_run_gdsmith)
    monkeypatch.setattr("graph_sym_perf.experiment.runner._reset_adapter_for_known_issue_setup", fake_reset)

    payload = _reuse_per_system_gdsmith_payload(writer, [Adapter()])

    assert payload is not None
    assert [case.target_system for case in payload.query_cases] == ["ladybugdb"]
    assert payload.query_cases[0].query == "MATCH (n) RETURN n"
    assert events == ["reset", "load:('CREATE (n0 :L0{id : 1})',)"]


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


def test_runtime_generated_source_payload_is_not_resliced_by_shard() -> None:
    cases = tuple(
        QueryCase(
            case_id=f"gdsmith-{index}",
            template_name="unit",
            query=f"RETURN {index} AS q",
            parameters={},
            base_predicate="true",
            safe_expression="true",
        )
        for index in range(10)
    )

    shard_cases = _select_shard_cases(
        SourcePayload(query_cases=cases, partitioning="shard_local"),
        ShardSpec(index=3, total=10),
    )

    assert [local_index for local_index, _case in shard_cases] == list(range(10))
    assert [case.query for _local_index, case in shard_cases] == [f"RETURN {index} AS q" for index in range(10)]


def test_cases_jsonl_source_payload_still_slices_by_shard() -> None:
    cases = tuple(
        QueryCase(
            case_id=f"known-{index}",
            template_name="unit",
            query=f"RETURN {index} AS q",
            parameters={},
            base_predicate="true",
            safe_expression="true",
        )
        for index in range(10)
    )

    shard_cases = _select_shard_cases(
        SourcePayload(query_cases=cases, partitioning="shared"),
        ShardSpec(index=3, total=4),
    )

    assert [local_index for local_index, _case in shard_cases] == [0, 1]
    assert [case.query for _local_index, case in shard_cases] == ["RETURN 3 AS q", "RETURN 7 AS q"]
