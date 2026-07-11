from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from graph_sym_perf.cli.runtime_first import main as cli_main
from graph_sym_perf.core import ConfigError, TargetSystem
from graph_sym_perf.experiment import migrate_legacy_config_to_v2
from graph_sym_perf.experiment.spec import load_experiment_spec


def _legacy_config_payload() -> dict[str, Any]:
    return {
        "experiment_id": "legacy_runtime_campaign",
        "seed": 20260710,
        "systems": {
            "targets": [
                "neo4j", "memgraph", "falkordb", "agensgraph",
                "arcadedb", "apache_age", "ladybugdb",
            ]
        },
        "source": {
            "enabled": ["gdsmith"],
            "gdsmith": {"max_seed_queries_per_shard": 10_000},
        },
        "execution": {
            "shards": 16,
            "accepted_query_pairs_per_shard": 1_000,
        },
    }


def _write_config_bundle(
    tmp_path: Path,
    *,
    experiment: dict[str, Any] | None = None,
    systems: dict[str, Any] | None = None,
    rules: dict[str, Any] | None = None,
) -> Path:
    documents = {
        "experiment.yaml": experiment,
        "systems.yaml": systems,
        "rules.yaml": rules,
    }
    for name, replacement in documents.items():
        source = Path("config") / name
        destination = tmp_path / name
        if replacement is None:
            shutil.copyfile(source, destination)
        else:
            destination.write_text(yaml.safe_dump(replacement, sort_keys=False), encoding="utf-8")
    return tmp_path / "experiment.yaml"


def test_runtime_first_experiment_spec_is_the_exact_seven_by_sixteen_matrix() -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    assert spec.schema_version == 2
    assert spec.name == "main_runtime_first"
    assert spec.targets == (
        TargetSystem.NEO4J,
        TargetSystem.MEMGRAPH,
        TargetSystem.FALKORDB,
        TargetSystem.AGENSGRAPH,
        TargetSystem.ARCADEDB,
        TargetSystem.APACHE_AGE,
        TargetSystem.LADYBUGDB,
    )
    assert spec.matrix.shards_per_system == 16
    assert spec.workload.accepted_query_pairs_per_shard == 1000
    assert spec.workload.max_generated_seeds_per_shard == 10000
    assert spec.generation.backend == "gdsmith"
    assert spec.generation.expected_commit == "b2166f1b70cfa9b5816fe707327dd6aed2a22a45"
    assert spec.total_accepted_pair_budget == 112_000


def test_schema_v2_rejects_unknown_nested_policy_field(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/experiment.yaml").read_text(encoding="utf-8"))
    raw["runtime"]["screen"]["profile_guidance"] = True
    path = _write_config_bundle(tmp_path, experiment=raw)

    with pytest.raises(ConfigError, match="runtime.screen"):
        load_experiment_spec(path)


@pytest.mark.parametrize(
    "policy_path",
    (
        ("scheduling",),
        ("runtime",),
        ("runtime", "noise"),
        ("validation",),
        ("reducer",),
        ("rules",),
        ("artifacts",),
    ),
)
def test_schema_v2_rejects_unknown_field_at_every_policy_level(
    tmp_path: Path,
    policy_path: tuple[str, ...],
) -> None:
    raw = yaml.safe_load(Path("config/experiment.yaml").read_text(encoding="utf-8"))
    policy = raw
    for part in policy_path:
        policy = policy[part]
    policy["unexpected"] = True
    path = _write_config_bundle(tmp_path, experiment=raw)

    with pytest.raises(ConfigError, match="unknown field"):
        load_experiment_spec(path)


def test_schema_v2_rejects_unknown_system_definition_field(tmp_path: Path) -> None:
    systems = yaml.safe_load(Path("config/systems.yaml").read_text(encoding="utf-8"))
    systems["systems"]["neo4j"]["unexpected"] = True
    path = _write_config_bundle(tmp_path, systems=systems)

    with pytest.raises(ConfigError, match="systems.neo4j"):
        load_experiment_spec(path)


def test_rules_yaml_is_loaded_and_strictly_validated(tmp_path: Path) -> None:
    rules = yaml.safe_load(Path("config/rules.yaml").read_text(encoding="utf-8"))
    rules["tiers"]["core"]["unexpected"] = True
    path = _write_config_bundle(tmp_path, rules=rules)

    with pytest.raises(ConfigError, match="rules.tiers.core"):
        load_experiment_spec(path)


def test_rules_yaml_requires_schema_version_one(tmp_path: Path) -> None:
    rules = yaml.safe_load(Path("config/rules.yaml").read_text(encoding="utf-8"))
    rules["schema_version"] = 2
    path = _write_config_bundle(tmp_path, rules=rules)

    with pytest.raises(ConfigError, match="schema_version: 1"):
        load_experiment_spec(path)


@pytest.mark.parametrize("missing_tier", ("core", "experimental", "control"))
def test_rules_yaml_requires_complete_core_experimental_control_tiers(
    tmp_path: Path,
    missing_tier: str,
) -> None:
    rules = yaml.safe_load(Path("config/rules.yaml").read_text(encoding="utf-8"))
    del rules["tiers"][missing_tier]
    path = _write_config_bundle(tmp_path, rules=rules)

    with pytest.raises(ConfigError, match="rules.tiers"):
        load_experiment_spec(path)


@pytest.mark.parametrize(
    ("tier", "field", "invalid"),
    (
        ("core", "minimum_accepted_pairs_per_rule", 0),
        ("core", "scheduling", "validated_yield_only"),
        ("experimental", "auto_submit", "false"),
        ("control", "auto_submit", 0),
    ),
)
def test_rules_yaml_validates_tier_field_types_and_enums(
    tmp_path: Path,
    tier: str,
    field: str,
    invalid: object,
) -> None:
    rules = yaml.safe_load(Path("config/rules.yaml").read_text(encoding="utf-8"))
    rules["tiers"][tier][field] = invalid
    path = _write_config_bundle(tmp_path, rules=rules)

    with pytest.raises(ConfigError, match=f"rules.tiers.{tier}"):
        load_experiment_spec(path)


@pytest.mark.parametrize(
    ("policy_path", "invalid"),
    (
        (("scheduling", "shard_retry_limit"), -1),
        (("runtime", "cache_mode"), "profile_guided"),
        (("runtime", "screen", "warmups"), "3"),
        (("runtime", "noise", "absolute_delta_quantile"), 1.5),
        (("validation", "sessions"), 0),
        (("validation", "reconnect_between_sessions"), "false"),
        (("reducer", "enabled"), "true"),
        (("rules", "forbidden_feedback"), "plan"),
        (("artifacts", "shard_budget_mb"), 0),
    ),
)
def test_schema_v2_rejects_invalid_policy_value_types_ranges_and_enums(
    tmp_path: Path,
    policy_path: tuple[str, ...],
    invalid: object,
) -> None:
    raw = yaml.safe_load(Path("config/experiment.yaml").read_text(encoding="utf-8"))
    policy = raw
    for part in policy_path[:-1]:
        policy = policy[part]
    policy[policy_path[-1]] = invalid
    path = _write_config_bundle(tmp_path, experiment=raw)

    with pytest.raises(ConfigError):
        load_experiment_spec(path)


@pytest.mark.parametrize(
    ("policy_path", "invalid"),
    (
        (("runtime", "screen", "ratio"), float("nan")),
        (("runtime", "screen", "ratio"), float("inf")),
        (("runtime", "screen", "ratio"), float("-inf")),
        (("validation", "min_ratio"), float("nan")),
        (("validation", "min_ci_lower_ratio"), float("inf")),
    ),
)
def test_schema_v2_rejects_non_finite_runtime_thresholds(
    tmp_path: Path,
    policy_path: tuple[str, ...],
    invalid: float,
) -> None:
    raw = yaml.safe_load(Path("config/experiment.yaml").read_text(encoding="utf-8"))
    policy = raw
    for part in policy_path[:-1]:
        policy = policy[part]
    policy[policy_path[-1]] = invalid
    path = _write_config_bundle(tmp_path, experiment=raw)

    with pytest.raises(ConfigError, match="finite"):
        load_experiment_spec(path)


def test_schema_v2_requires_runtime_screen_ratio_above_one(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/experiment.yaml").read_text(encoding="utf-8"))
    raw["runtime"]["screen"]["ratio"] = 1.0
    path = _write_config_bundle(tmp_path, experiment=raw)

    with pytest.raises(ConfigError, match="runtime.screen.ratio"):
        load_experiment_spec(path)


def test_schema_v2_requires_validation_ratio_at_least_ci_lower_ratio(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/experiment.yaml").read_text(encoding="utf-8"))
    raw["validation"]["min_ratio"] = 1.5
    raw["validation"]["min_ci_lower_ratio"] = 2.0
    path = _write_config_bundle(tmp_path, experiment=raw)

    with pytest.raises(ConfigError, match="at least"):
        load_experiment_spec(path)


def test_experiment_spec_policy_values_are_deeply_immutable() -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    with pytest.raises(TypeError):
        spec.runtime["screen"]["pairs"] = 99  # type: ignore[index]
    with pytest.raises(AttributeError):
        spec.runtime._items = ()  # type: ignore[attr-defined]


def test_experiment_spec_has_one_explicit_migration_to_legacy_run_config() -> None:
    assert importlib.util.find_spec("graph_sym_perf.config.loader") is not None, "explicit migration module is missing"
    migrate = importlib.import_module("graph_sym_perf.config.loader").migrate_experiment_spec_to_run_config
    spec = load_experiment_spec("config/experiment.yaml")

    config = migrate(spec, TargetSystem.NEO4J)

    assert config.source_path == str(spec.source_path)
    assert config.systems[0].target is TargetSystem.NEO4J
    assert config.systems[0].required_env == spec.systems[TargetSystem.NEO4J].required_env
    assert config.dataset.seed == spec.seed
    assert config.query_generation.max_cases == spec.workload.max_generated_seeds_per_shard
    assert config.query_generation.gdsmith.random_seed == spec.seed
    assert config.equivalence.max_result_rows == spec.validation["max_result_rows"]
    assert config.equivalence.timeout_seconds == spec.systems[TargetSystem.NEO4J].default_timeout_seconds
    assert config.confirmation.timeout_seconds == spec.systems[TargetSystem.NEO4J].default_timeout_seconds
    assert config.transforms.max_variants_per_case == spec.workload.max_variants_per_seed


@pytest.mark.parametrize(
    "statement",
    (
        "import graph_sym_perf.config; import graph_sym_perf.experiment",
        "import graph_sym_perf.experiment; import graph_sym_perf.config",
        "import graph_sym_perf.config; import graph_sym_perf.cli.runtime_first",
    ),
)
def test_config_import_order_is_safe_in_a_fresh_interpreter(statement: str) -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, 'src'); {statement}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_legacy_config_is_converted_only_by_offline_migration_command(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        yaml.safe_dump(_legacy_config_payload(), sort_keys=False),
        encoding="utf-8",
    )
    output = tmp_path / "migrated" / "experiment.yaml"

    result = cli_main(
        [
            "experiment", "migrate-config", "--input", str(legacy),
            "--output", str(output), "--template", "config/experiment.yaml",
        ]
    )

    assert result == 0
    migrated = load_experiment_spec(output)
    assert migrated.name == "legacy_runtime_campaign"
    assert migrated.total_accepted_pair_budget == 112_000
    assert (output.parent / "systems.yaml").exists()
    assert (output.parent / "rules.yaml").exists()


def test_legacy_config_migration_rolls_back_partial_bundle_on_publish_failure(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(yaml.safe_dump(_legacy_config_payload(), sort_keys=False), encoding="utf-8")
    output_dir = tmp_path / "migrated"
    output_dir.mkdir()
    (output_dir / "rules.yaml").symlink_to("missing-rules-target")
    output = output_dir / "experiment.yaml"

    with pytest.raises(FileExistsError):
        migrate_legacy_config_to_v2(legacy, output, template_path="config/experiment.yaml")

    assert not output.exists()
    assert not (output_dir / "systems.yaml").exists()


@pytest.mark.parametrize(
    "patch",
    (
        "matrix:\n  shards_per_system: 0\n",
        "workload:\n  accepted_query_pairs_per_shard: 0\n",
        "targets:\n  - neo4j\n  - neo4j\n",
        "generation:\n  backend: alternate\n",
        "generation:\n  backend: gdsmith\n  source: alternate_templates\n",
        "coverage:\n  enabled: true\n",
        "feedback:\n  enabled: true\n",
        "include_" + "plan_" + "only: true\n",
        "unknown_field: true\n",
    ),
)
def test_schema_v2_rejects_invalid_or_unknown_formal_configuration(tmp_path: Path, patch: str) -> None:
    source = Path("config/experiment.yaml").read_text(encoding="utf-8")
    path = tmp_path / "invalid.yaml"
    path.write_text(source + "\n" + patch, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_experiment_spec(path)
