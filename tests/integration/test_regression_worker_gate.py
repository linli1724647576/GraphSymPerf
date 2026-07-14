from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import graph_sym_perf.experiment.worker as worker_module
import graph_sym_perf.validation.regression as regression_module

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.plan_models import CampaignPhase, RuleStream
from graph_sym_perf.experiment.planner import build_rule_plan
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import RuntimeFirstShardWorker
from graph_sym_perf.validation.regression import RegressionEnvironmentIdentity, RegressionRunner
from graph_sym_perf.validation.validator import IssueValidator
from tests.integration.test_issue_validation import FakeIndependentRunner


def _entry(phase: CampaignPhase):
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    selected = (("pattern_order_symmetry", 128),) if phase is CampaignPhase.P2 else ()
    plan = build_rule_plan(spec, phase, selected_cells=selected)
    if phase is CampaignPhase.P0:
        return spec, next(entry for entry in plan.entries if entry.stream is RuleStream.REGRESSION)
    return spec, next(entry for entry in plan.entries if entry.stream is RuleStream.DISCOVERY)


def _must_not_generate(*_args, **_kwargs):
    raise AssertionError("regression gate must run before GDsmith generation")


def _environment() -> dict[str, str]:
    return {
        "GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "GRAPHSYMPERF_NEO4J_BUG001_CONTAINER": "neo4j-bug001",
        "GRAPHSYMPERF_NEO4J_BUG001_URI": "bolt://127.0.0.1:17687/bug001",
        "GRAPHSYMPERF_NEO4J_BUG001_USER": "bug-user",
        "GRAPHSYMPERF_NEO4J_BUG001_PASSWORD": "bug-secret",
        "GRAPHSYMPERF_NEO4J_URI": "bolt://127.0.0.1:7687/main",
        "GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-main",
    }


def _install_offline_production_gate(monkeypatch: pytest.MonkeyPatch, *, validator=None):
    loaded = []
    real_load = regression_module.load_regression_case

    def load_case(path, environment):
        case = real_load(
            path,
            environment,
            provenance_probe=lambda mapped, target: (
                mapped["GRAPHSYMPERF_NEO4J_IMAGE_DIGEST"]
                if target is TargetSystem.NEO4J
                else None
            ),
        )
        loaded.append(case)
        return case

    def build_runner(path):
        case = loaded[-1]
        identity = RegressionEnvironmentIdentity(
            case.affected_version,
            "b" * 64,
            "6.0.0",
            case.provenance.verified_image_digest,
            case.provenance.container_identity,
            case.provenance.endpoint_identity,
            case.index_names,
            ("ONLINE",) * 7,
        )
        return RegressionRunner._for_test(
            validator or IssueValidator(),
            report_path=path,
            identity_probe=lambda _case: identity,
            session_runner_factory=lambda _case: FakeIndependentRunner(),
        )

    monkeypatch.setattr(worker_module, "load_regression_case", load_case)
    monkeypatch.setattr(worker_module, "build_production_regression_runner", build_runner)
    return loaded


def test_p0_regression_config_failure_dispatches_without_generation_and_lands_redacted_gate(
    tmp_path: Path,
) -> None:
    spec, entry = _entry(CampaignPhase.P0)
    worker = RuntimeFirstShardWorker(
        spec,
        environment={"GRAPHSYMPERF_NEO4J_BUG001_PASSWORD": "must-not-leak"},
        generation_runner=_must_not_generate,
    )
    worker.bind_run_dir(tmp_path)

    summary = worker(entry)

    report_path = (
        tmp_path
        / "regressions"
        / "neo4j_bug001_pattern_order"
        / "regression_report.json"
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert summary["terminal_state"] == "infrastructure_failed"
    assert payload["passed"] is False
    assert payload["failure_kind"] == "configuration"
    assert "must-not-leak" not in report_path.read_text(encoding="utf-8")
    assert "must-not-leak" not in repr(summary)
    artifact = regression_module.load_regression_gate_artifact(report_path)
    assert artifact.failure_kind is regression_module.RegressionFailureKind.CONFIGURATION
    assert artifact.verify_integrity()


@pytest.mark.parametrize("phase", (CampaignPhase.P1, CampaignPhase.P2))
def test_later_phase_blocks_before_generation_when_p0_gate_artifact_is_missing(
    tmp_path: Path,
    phase: CampaignPhase,
) -> None:
    spec, entry = _entry(phase)
    worker = RuntimeFirstShardWorker(
        spec,
        environment={},
        generation_runner=_must_not_generate,
    )
    worker.bind_run_dir(tmp_path)

    summary = worker(entry)

    assert summary["terminal_state"] == "infrastructure_failed"
    assert summary["generated_query_records"] == 0
    assert summary["acceptance_generated_seeds"] == 0
    assert "regression" in str(summary["infrastructure_reason"])


def test_p0_dispatches_the_production_runner_but_trusts_only_the_landed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, entry = _entry(CampaignPhase.P0)
    called: list[Path] = []

    class LyingRunner:
        def run(self, _case, **_kwargs):
            called.append(tmp_path)
            return type("ForgedReturn", (), {"passed": True})()

    monkeypatch.setattr(
        worker_module,
        "load_regression_case",
        lambda *_args, **_kwargs: object(),
        raising=False,
    )
    monkeypatch.setattr(
        worker_module,
        "build_production_regression_runner",
        lambda _path: LyingRunner(),
        raising=False,
    )
    worker = RuntimeFirstShardWorker(spec, environment={}, generation_runner=_must_not_generate)
    worker.bind_run_dir(tmp_path)

    summary = worker(entry)

    assert called == [tmp_path]
    assert summary["terminal_state"] == "infrastructure_failed"
    assert "artifact" in str(summary["infrastructure_reason"])


@pytest.mark.parametrize(
    "payload",
    (
        {"passed": True},
        {"passed": False, "failure_kind": "runtime"},
    ),
)
def test_later_phase_rejects_incomplete_or_failed_landed_gate_before_generation(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    spec, entry = _entry(CampaignPhase.P1)
    report_path = (
        tmp_path
        / "regressions"
        / "neo4j_bug001_pattern_order"
        / "regression_report.json"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    worker = RuntimeFirstShardWorker(spec, environment={}, generation_runner=_must_not_generate)
    worker.bind_run_dir(tmp_path)

    summary = worker(entry)

    assert summary["terminal_state"] == "infrastructure_failed"
    assert "regression" in str(summary["infrastructure_reason"])


def test_separate_phase_run_dirs_share_an_explicit_regression_artifact_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_offline_production_gate(monkeypatch)
    artifact_root = tmp_path / "campaign-artifacts"
    p0_spec, p0_entry = _entry(CampaignPhase.P0)
    p0 = RuntimeFirstShardWorker(
        p0_spec, environment=_environment(), generation_runner=_must_not_generate
    )
    p0.bind_run_dir(tmp_path / "p0-run")
    p0.bind_regression_artifact_root(artifact_root)

    assert p0(p0_entry)["terminal_state"] == "completed"
    report_path = (
        artifact_root
        / "regressions"
        / "neo4j_bug001_pattern_order"
        / "regression_report.json"
    )
    assert report_path.is_file()

    class GatePassed(RuntimeError):
        pass

    def gate_passed(*_args, **_kwargs):
        raise GatePassed

    p1_spec, p1_entry = _entry(CampaignPhase.P1)
    p1 = RuntimeFirstShardWorker(p1_spec, environment=_environment(), generation_runner=gate_passed)
    p1.bind_run_dir(tmp_path / "p1-run")
    p1.bind_regression_artifact_root(artifact_root)

    with pytest.raises(GatePassed):
        p1(p1_entry)


def test_current_provenance_drift_blocks_a_valid_landed_gate_before_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_offline_production_gate(monkeypatch)
    artifact_root = tmp_path / "campaign-artifacts"
    spec, p0_entry = _entry(CampaignPhase.P0)
    p0 = RuntimeFirstShardWorker(spec, environment=_environment(), generation_runner=_must_not_generate)
    p0.bind_run_dir(tmp_path / "p0")
    p0.bind_regression_artifact_root(artifact_root)
    assert p0(p0_entry)["terminal_state"] == "completed"

    drifted = _environment()
    drifted["GRAPHSYMPERF_NEO4J_BUG001_CONTAINER"] = "neo4j-bug001-restarted"
    p1_spec, p1_entry = _entry(CampaignPhase.P1)
    p1 = RuntimeFirstShardWorker(p1_spec, environment=drifted, generation_runner=_must_not_generate)
    p1.bind_run_dir(tmp_path / "p1")
    p1.bind_regression_artifact_root(artifact_root)

    summary = p1(p1_entry)

    assert summary["terminal_state"] == "infrastructure_failed"
    assert "identity" in str(summary["infrastructure_reason"])


def test_failed_p0_runner_report_is_preserved_and_terminally_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = SimpleNamespace(
        validate=lambda *_args: SimpleNamespace(
            stage="validation_passed", sessions=(), reason_codes=()
        )
    )
    _install_offline_production_gate(monkeypatch, validator=forged)
    spec, entry = _entry(CampaignPhase.P0)
    worker = RuntimeFirstShardWorker(spec, environment=_environment(), generation_runner=_must_not_generate)
    worker.bind_run_dir(tmp_path)

    summary = worker(entry)
    payload = json.loads(
        (
            tmp_path
            / "regressions"
            / "neo4j_bug001_pattern_order"
            / "regression_report.json"
        ).read_text(encoding="utf-8")
    )

    assert summary["terminal_state"] == "infrastructure_failed"
    assert payload["passed"] is False


def test_manifest_drift_after_planning_still_lands_a_typed_configuration_failure(
    tmp_path: Path,
) -> None:
    fixture_dir = tmp_path / "fixture"
    shutil.copytree(
        Path("tests/fixtures/regressions/neo4j_bug001"),
        fixture_dir,
    )
    manifest_path = fixture_dir / "manifest.yaml"
    spec = replace(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        regression={"manifest": manifest_path},
    )
    entry = next(
        value
        for value in build_rule_plan(spec, CampaignPhase.P0).entries
        if value.stream is RuleStream.REGRESSION
    )
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    payload["post_plan_drift"] = True
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    worker = RuntimeFirstShardWorker(spec, environment=_environment(), generation_runner=_must_not_generate)
    worker.bind_run_dir(tmp_path / "run")

    summary = worker(entry)

    report_path = (
        tmp_path
        / "run"
        / "regressions"
        / "neo4j_bug001_pattern_order"
        / "regression_report.json"
    )
    artifact = regression_module.load_regression_gate_artifact(report_path)
    assert summary["terminal_state"] == "infrastructure_failed"
    assert artifact.failure_kind is regression_module.RegressionFailureKind.CONFIGURATION
