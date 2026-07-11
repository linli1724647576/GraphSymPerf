from __future__ import annotations

import hashlib
import json
import pickle
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from graph_sym_perf.core import ConfigError, TargetSystem
from graph_sym_perf.experiment.plan_models import SourceRevision
from graph_sym_perf.experiment.planner import build_run_plan, write_resolved_manifest
from graph_sym_perf.experiment.spec import load_experiment_spec


def _gdsmith_artifact_environment(root: Path, source_commit: str) -> dict[str, str]:
    jar = root / "GDsmith.jar"
    jar.write_bytes(b"planner-fixture")
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    jar.with_suffix(jar.suffix + ".manifest.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "source_commit": source_commit,
                "compatibility_guards": [
                    "reject_multi_label_node_patterns",
                    "reject_double_quoted_tokens",
                    "exclude_unbuildable_tugraph_provider",
                    "shade_runnable_jar",
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "GRAPHSYMPERF_GDSMITH_JAR": str(jar),
        "GRAPHSYMPERF_GDSMITH_JAR_SHA256": "sha256:" + digest,
    }


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

    environment = _gdsmith_artifact_environment(tmp_path, expected_commit)
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
