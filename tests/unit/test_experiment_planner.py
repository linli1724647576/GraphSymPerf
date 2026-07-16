from __future__ import annotations

import hashlib
import json
import pickle
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import graph_sym_perf.experiment.planner as planner_module

from graph_sym_perf.core import ConfigError, TargetSystem
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    FrozenMapping,
    RulePlanEntry,
    RuleStream,
    SourceRevision,
)
from graph_sym_perf.experiment.planner import (
    build_rule_plan,
    build_run_plan,
    write_resolved_manifest,
)
from graph_sym_perf.experiment.spec import load_experiment_spec
from tests.gdsmith_artifact_fixture import (
    assume_schema_v3_sources_clean,
    verified_gdsmith_environment,
)


@pytest.fixture(autouse=True)
def _default_rule_plan_tooling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assume_schema_v3_sources_clean(monkeypatch)
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(
        tmp_path / "default-gdsmith",
        spec.generation.expected_commit,
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def test_planner_resolves_repository_commit_from_config_parent_by_default() -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    expected = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    plan = build_run_plan(spec)

    assert plan.source_revision.repository_commit == expected


def test_p1_rule_plan_has_independent_rule_scale_budgets() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    plan = build_rule_plan(spec, CampaignPhase.P1)

    discovery = [entry for entry in plan.entries if entry.stream is RuleStream.DISCOVERY]
    control = [entry for entry in plan.entries if entry.stream is RuleStream.CONTROL]
    assert len(discovery) == 9 * 3
    assert len(control) == 2 * 3
    assert all(entry.accepted_pair_quota == 200 for entry in discovery)
    assert all(entry.accepted_pair_quota == 50 for entry in control)
    assert all(
        entry.max_generated_seed_budget == entry.accepted_pair_quota * 10
        for entry in plan.entries
    )
    assert all(entry.max_variants_per_seed == 2 for entry in plan.entries)
    assert all(entry.query_timeout_seconds == 60 for entry in plan.entries)
    assert all(entry.measurement_protocol_id == "validated-yield-v1" for entry in plan.entries)
    assert len({entry.shard_seed for entry in plan.entries}) == len(plan.entries)
    assert plan.discovery_accepted_pair_budget == 5400
    assert plan.control_accepted_pair_budget == 300

    by_rule = {entry.rule_id: entry for entry in discovery}
    assert by_rule["constant_true_disjunction"].family == "bool"
    assert by_rule["constant_true_disjunction"].tier == "core"
    assert by_rule["constant_true_disjunction"].proof_version == "1"
    assert by_rule["label_order_symmetry"].family == "sym"


def test_rule_plan_seals_verified_gdsmith_artifact_digests_and_fails_closed(
    tmp_path: Path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(tmp_path, spec.generation.expected_commit)

    plan = build_rule_plan(spec, CampaignPhase.P1, environment=environment)

    assert plan.source_revision.gdsmith_jar_sha256 == environment[
        "GRAPHSYMPERF_GDSMITH_JAR_SHA256"
    ]
    assert plan.source_revision.gdsmith_build_manifest_sha256
    assert plan.source_revision.provenance_complete
    with pytest.raises(ConfigError, match="verified GDsmith artifact"):
        build_rule_plan(spec, CampaignPhase.P1, environment={})


@pytest.mark.parametrize(
    ("dirty_source", "expected_message"),
    (
        ("repository", "GraphSymPerf repository must be clean"),
        ("gdsmith", "GDsmith checkout must be clean"),
    ),
)
def test_schema3_rule_plan_rejects_any_already_dirty_source_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_source: str,
    expected_message: str,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(tmp_path, spec.generation.expected_commit)
    repository_root = spec.source_path.parents[1]

    def dirty(path: Path) -> bool:
        if dirty_source == "repository":
            return path == repository_root
        return path == spec.generation.checkout

    monkeypatch.setattr(planner_module, "_git_dirty", dirty)

    with pytest.raises(ConfigError, match=expected_message):
        build_rule_plan(spec, CampaignPhase.P1, environment=environment)


def test_p2_requires_bounded_explicit_rule_scale_cells() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    with pytest.raises(ConfigError, match="selected_cells"):
        build_rule_plan(spec, CampaignPhase.P2)
    with pytest.raises(ConfigError, match="at most 6"):
        build_rule_plan(
            spec,
            CampaignPhase.P2,
            selected_cells=tuple(
                (rule_id, scale)
                for rule_id in spec.rules.discovery_rule_ids[:3]
                for scale in spec.matrix.graph_scales
            )[:7],
        )

    plan = build_rule_plan(
        spec,
        CampaignPhase.P2,
        selected_cells=(
            ("constant_true_disjunction", 128),
            ("pattern_order_symmetry", 512),
        ),
    )
    p1 = build_rule_plan(spec, CampaignPhase.P1)

    assert len(plan.entries) == 2 * 3
    assert all(entry.stream is RuleStream.DISCOVERY for entry in plan.entries)
    assert all(entry.accepted_pair_quota == 1000 for entry in plan.entries)
    assert {(entry.rule_id, entry.graph_scale) for entry in plan.entries} == {
        ("constant_true_disjunction", 128),
        ("pattern_order_symmetry", 512),
    }
    assert {entry.shard_index for entry in plan.entries} == {0, 1, 2}
    assert plan.discovery_accepted_pair_budget == 6000
    assert plan.control_accepted_pair_budget == 0
    assert not ({entry.shard_seed for entry in plan.entries} & {entry.shard_seed for entry in p1.entries})


def test_p2_absolute_cell_limit_survives_a_forged_larger_spec() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    forged = replace(
        spec,
        campaign=replace(
            spec.campaign,
            p2=replace(spec.campaign.p2, max_rule_scale_cells=99),
        ),
    )
    selected_cells = tuple(
        (rule_id, scale)
        for rule_id in spec.rules.discovery_rule_ids[:3]
        for scale in spec.matrix.graph_scales
    )[:7]

    with pytest.raises(ConfigError, match="at most 6"):
        build_rule_plan(forged, CampaignPhase.P2, selected_cells=selected_cells)


@pytest.mark.parametrize("invalid_scale", (128.0, True, 129, "128"))
def test_p2_rejects_non_integer_or_unconfigured_graph_scales(invalid_scale: object) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    with pytest.raises(ConfigError, match="graph scale"):
        build_rule_plan(
            spec,
            CampaignPhase.P2,
            selected_cells=(("constant_true_disjunction", invalid_scale),),  # type: ignore[arg-type]
        )


def test_p2_rejects_alias_duplicate_cells_after_canonicalization() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    with pytest.raises(ConfigError, match="duplicate canonical cells"):
        build_rule_plan(
            spec,
            CampaignPhase.P2,
            selected_cells=(
                ("null_complete_excluded_middle", 128),
                ("and_excluded_middle_or_null", 128),
            ),
        )


@pytest.mark.parametrize("rule_id", ("alpha_renaming_symmetry", "double_negation_predicate"))
def test_p2_rejects_control_or_inactive_non_core_rules(rule_id: str) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    with pytest.raises(ConfigError, match="not an approved discovery rule"):
        build_rule_plan(
            spec,
            CampaignPhase.P2,
            selected_cells=((rule_id, 128),),
        )


def test_p0_fails_closed_when_configured_regression_manifest_is_missing(tmp_path: Path) -> None:
    raw = yaml.safe_load(
        Path("config/neo4j_validated_runtime_yield.yaml").read_text(encoding="utf-8")
    )
    raw["regression"]["manifest"] = str(tmp_path / "missing-regression.yaml")
    path = tmp_path / "campaign.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    for name in ("systems.yaml", "rules.yaml"):
        (tmp_path / name).write_bytes((Path("config") / name).read_bytes())
    spec = load_experiment_spec(path)

    with pytest.raises(ConfigError, match="regression manifest not found"):
        build_rule_plan(spec, CampaignPhase.P0)


def test_p0_has_one_zero_quota_regression_entry_outside_campaign_budgets() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    plan = build_rule_plan(spec, CampaignPhase.P0)

    regression = [entry for entry in plan.entries if entry.stream is RuleStream.REGRESSION]
    assert len(regression) == 1
    assert regression[0].regression_case_id == "neo4j_bug001_pattern_order"
    assert regression[0].accepted_pair_quota == 0
    assert regression[0].max_generated_seed_budget == 0
    assert regression[0].rule_id == "pattern_order_symmetry"
    assert plan.discovery_accepted_pair_budget == 450
    assert plan.control_accepted_pair_budget == 100


def test_p0_regression_entry_uses_its_dedicated_runtime_budget() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    plan = build_rule_plan(spec, CampaignPhase.P0)

    regression = next(
        entry for entry in plan.entries if entry.stream is RuleStream.REGRESSION
    )
    assert regression.max_entry_runtime_seconds == 2_700
    assert all(
        entry.max_entry_runtime_seconds == 300
        for entry in plan.entries
        if entry.stream is not RuleStream.REGRESSION
    )


def test_p1_and_p2_do_not_repeat_the_regression_entry() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    p1 = build_rule_plan(spec, CampaignPhase.P1)
    p2 = build_rule_plan(
        spec,
        CampaignPhase.P2,
        selected_cells=(("pattern_order_symmetry", 128),),
    )

    assert all(entry.stream is not RuleStream.REGRESSION for entry in (*p1.entries, *p2.entries))
    assert all(entry.regression_case_id is None for entry in (*p1.entries, *p2.entries))


@pytest.mark.parametrize(
    "changes",
    (
        {"phase": CampaignPhase.P1},
        {"target": TargetSystem.APACHE_AGE},
        {"accepted_pair_quota": 1},
        {"max_generated_seed_budget": 1},
        {"max_variants_per_seed": 1},
        {"regression_case_id": None},
    ),
)
def test_regression_rule_plan_entry_invariants_fail_closed(changes: dict[str, object]) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    entry = next(
        value
        for value in build_rule_plan(spec, CampaignPhase.P0).entries
        if value.stream is RuleStream.REGRESSION
    )

    with pytest.raises(ValueError, match="REGRESSION"):
        replace(entry, **changes)


def test_regression_rule_plan_entry_rejects_noncanonical_case_identity() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    entry = next(
        value
        for value in build_rule_plan(spec, CampaignPhase.P0).entries
        if value.stream is RuleStream.REGRESSION
    )

    with pytest.raises(ValueError, match="REGRESSION"):
        replace(entry, regression_case_id="forged-regression-case")


def test_non_regression_entry_cannot_carry_a_regression_case_id() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    entry = build_rule_plan(spec, CampaignPhase.P1).entries[0]

    with pytest.raises(ValueError, match="regression_case_id"):
        replace(entry, regression_case_id="neo4j_bug001_pattern_order")


def test_p0_resolved_manifest_roundtrips_the_frozen_regression_identity(tmp_path: Path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    plan = build_rule_plan(spec, CampaignPhase.P0)
    path = tmp_path / "p0-rule-plan.json"

    write_resolved_manifest(plan, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    regression = next(
        entry for entry in payload["entries"] if entry["stream"] == RuleStream.REGRESSION.value
    )
    assert regression["phase"] == CampaignPhase.P0.value
    assert regression["target"] == TargetSystem.NEO4J.value
    assert regression["regression_case_id"] == "neo4j_bug001_pattern_order"
    assert regression["accepted_pair_quota"] == 0
    assert regression["max_generated_seed_budget"] == 0
    assert regression["max_variants_per_seed"] == 0


def test_rule_plan_resolved_manifest_is_atomic_immutable_and_auditable(tmp_path: Path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    plan = build_rule_plan(spec, CampaignPhase.P1)
    path = tmp_path / "p1-rule-plan.json"

    write_resolved_manifest(plan, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["phase"] == "p1"
    assert len(payload["entries"]) == 33
    assert payload["discovery_accepted_pair_budget"] == 5400
    assert payload["control_accepted_pair_budget"] == 300
    assert payload["source_revision"]["repository_commit"]
    assert payload["source_revision"]["gdsmith_commit"] == spec.generation.expected_commit
    assert payload["source_revision"]["rules_schema_sha256"] == hashlib.sha256(
        Path("config/rules.yaml").read_bytes()
    ).hexdigest()
    assert {
        "rule_id",
        "graph_scale",
        "accepted_pair_quota",
        "shard_seed",
        "proof_version",
        "query_timeout_seconds",
    } <= set(payload["entries"][0])
    with pytest.raises(FileExistsError):
        write_resolved_manifest(plan, path)


def test_planner_records_exact_gdsmith_commit_and_clean_or_dirty_state(tmp_path: Path) -> None:
    checkout = tmp_path / "gdsmith"
    checkout.mkdir()
    subprocess.run(("git", "init", "-q", str(checkout)), check=True)
    (checkout / "README.md").write_text("clean\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(checkout), "add", "README.md"), check=True)
    subprocess.run(
        (
            "git", "-C", str(checkout), "-c", "user.name=GraphSymPerf Tests",
            "-c", "user.email=tests@example.invalid", "commit", "-qm", "fixture",
        ),
        check=True,
    )
    expected_commit = subprocess.run(
        ("git", "-C", str(checkout), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    spec = load_experiment_spec("config/experiment.yaml")
    spec = replace(
        spec,
        generation=replace(spec.generation, checkout=checkout, expected_commit=expected_commit),
    )

    environment = verified_gdsmith_environment(tmp_path, expected_commit)
    clean = build_run_plan(spec, repository_root=checkout, environment=environment)
    (checkout / "README.md").write_text("dirty\n", encoding="utf-8")
    dirty = build_run_plan(spec, repository_root=checkout, environment=environment)

    assert clean.source_revision.repository_dirty is False
    assert clean.source_revision.gdsmith_commit == expected_commit
    assert clean.source_revision.gdsmith_dirty is False
    assert clean.resolved_spec is not None
    assert clean.resolved_spec.systems[TargetSystem.APACHE_AGE].generation_backend == "agensgraph_proxy"
    age_task = next(task for task in clean.tasks if task.target is TargetSystem.APACHE_AGE)
    assert age_task.generation_backend == "agensgraph_proxy"
    assert dirty.source_revision.repository_dirty is True
    assert dirty.source_revision.gdsmith_commit == expected_commit
    assert dirty.source_revision.gdsmith_dirty is True
    assert clean.source_revision.provenance_complete
    assert dirty.source_revision.provenance_complete
    assert not SourceRevision(None, expected_commit, True, spec.rules_schema_sha256).provenance_complete


def test_planner_is_deterministic_and_assigns_one_unique_seed_per_shard(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    first = build_run_plan(spec, repository_root=tmp_path)
    second = build_run_plan(spec, repository_root=tmp_path)

    assert len(first.tasks) == 112
    assert first.total_accepted_pair_budget == 112_000
    assert first == second
    assert len({task.seed for task in first.tasks}) == 112
    assert all(0 <= task.seed <= (1 << 63) - 1 for task in first.tasks)
    assert {(task.target, task.shard_index) for task in first.tasks} == {
        (target, shard) for target in spec.targets for shard in range(16)
    }


def test_planner_rejects_when_core_exploration_floor_cannot_fit_shard_budget(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    spec = replace(
        spec,
        workload=replace(spec.workload, accepted_query_pairs_per_shard=100),
    )

    with pytest.raises(ConfigError, match="CORE exploration floor"):
        build_run_plan(spec, repository_root=tmp_path)


def test_planner_intersects_declared_capabilities_with_target_profiles(tmp_path: Path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    systems = dict(spec.systems)
    for target in (TargetSystem.AGENSGRAPH, TargetSystem.ARCADEDB):
        systems[target] = replace(
            systems[target],
            capabilities=(*systems[target].capabilities, "multi_label_intersection"),
        )
    systems[TargetSystem.NEO4J] = replace(
        systems[TargetSystem.NEO4J],
        capabilities=("native_parameters",),
    )

    plan = build_run_plan(
        replace(spec, systems=FrozenMapping(systems)),
        repository_root=tmp_path,
    )

    assert "label_order_symmetry" not in plan.rule_yield_snapshot["agensgraph"]
    assert "label_order_symmetry" not in plan.rule_yield_snapshot["arcadedb"]
    assert "label_order_symmetry" not in plan.rule_yield_snapshot["neo4j"]


def test_resolved_manifest_is_a_secret_free_auditable_plan(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    marker = "do-not-serialize-this-secret"
    plan = build_run_plan(
        spec,
        repository_root=Path.cwd(),
        environment={"GRAPHSYMPERF_NEO4J_URI": marker},
    )
    path = tmp_path / "resolved_manifest.json"

    write_resolved_manifest(plan, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["total_accepted_pair_budget"] == 112_000
    assert len(payload["tasks"]) == 112
    assert payload["environment"]["GRAPHSYMPERF_NEO4J_URI"] == "present"
    assert payload["environment"]["GRAPHSYMPERF_NEO4J_PASSWORD"] == "missing"
    assert payload["environment"]["GRAPHSYMPERF_GDSMITH_JAR"] == "missing"
    assert payload["environment"]["GRAPHSYMPERF_GDSMITH_JAR_SHA256"] == "missing"
    assert payload["environment"]["GRAPHSYMPERF_NEO4J_CONTAINER"] == "missing"
    assert payload["environment"]["GRAPHSYMPERF_NEO4J_IMAGE_DIGEST"] == "missing"
    assert payload["environment"]["GRAPHSYMPERF_LADYBUGDB_PACKAGE_DIGEST"] == "missing"
    assert payload["resolved_spec"]["generation"]["backend"] == "gdsmith"
    assert payload["resolved_spec"]["systems"]["neo4j"]["dialect"] == "cypher"
    assert payload["resolved_spec"]["systems"]["apache_age"]["generation_backend"] == "agensgraph_proxy"
    assert payload["rule_yield_snapshot"]["neo4j"]["pattern_order_symmetry"] == {
        "accepted": 0,
        "validation_passed": 0,
    }
    assert not ({"plan", "profile", "pmc", "developer_status"} & set(payload["rule_yield_snapshot"]["neo4j"]))
    assert payload["resolved_spec"]["rules_catalog"]["schema_version"] == 1
    assert payload["validator_protocol_version"] == "runtime-first-v3"
    assert payload["reducer_protocol_version"] == "paired-ast-ddmin-v1"
    assert payload["source_revision"]["repository_commit"]
    assert payload["source_revision"]["gdsmith_commit"]
    assert isinstance(payload["source_revision"]["gdsmith_dirty"], bool)
    assert payload["source_revision"]["rules_schema_sha256"] == hashlib.sha256(
        Path("config/rules.yaml").read_bytes()
    ).hexdigest()
    serialized = path.read_text(encoding="utf-8")
    assert marker not in serialized
    assert "password" not in serialized.lower().replace("GRAPHSYMPERF_NEO4J_PASSWORD".lower(), "")


def test_resolved_manifest_cannot_be_overwritten(tmp_path: Path) -> None:
    plan = build_run_plan(load_experiment_spec("config/experiment.yaml"), repository_root=Path.cwd())
    path = tmp_path / "resolved_manifest.json"
    write_resolved_manifest(plan, path)

    with pytest.raises(FileExistsError):
        write_resolved_manifest(plan, path)


def test_resolved_manifest_serialization_failure_does_not_claim_destination(tmp_path: Path) -> None:
    plan = build_run_plan(load_experiment_spec("config/experiment.yaml"), repository_root=Path.cwd())
    invalid = replace(plan, spec_name=object())  # type: ignore[arg-type]
    path = tmp_path / "resolved_manifest.json"

    with pytest.raises(TypeError):
        write_resolved_manifest(invalid, path)

    assert not path.exists()


def test_resolved_manifest_rejects_non_standard_nan_json(tmp_path: Path) -> None:
    plan = build_run_plan(load_experiment_spec("config/experiment.yaml"), repository_root=Path.cwd())
    invalid = replace(plan, total_accepted_pair_budget=float("nan"))  # type: ignore[arg-type]
    path = tmp_path / "resolved_manifest.json"

    with pytest.raises(ValueError):
        write_resolved_manifest(invalid, path)

    assert not path.exists()


def test_planner_rejects_total_accepted_pair_budget_overflow(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/experiment.yaml").read_text(encoding="utf-8"))
    raw["workload"]["accepted_query_pairs_per_shard"] = 1001
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    for name in ("systems.yaml", "rules.yaml"):
        (tmp_path / name).write_bytes((Path("config") / name).read_bytes())
    spec = load_experiment_spec(path)

    with pytest.raises(ConfigError, match="budget"):
        build_run_plan(spec, repository_root=Path.cwd())


def test_resolved_experiment_spec_is_pickle_safe_for_spawned_workers() -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    restored = pickle.loads(pickle.dumps(spec))

    assert restored == spec
    assert restored.runtime["screen"]["pairs"] == 5
