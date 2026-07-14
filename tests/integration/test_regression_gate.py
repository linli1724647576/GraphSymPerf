from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from graph_sym_perf.experiment.plan_models import RuleStream
from graph_sym_perf.validation.models import EquivalenceEvidence, PipelineStage
from graph_sym_perf.validation.regression import (
    RegressionConfigurationError,
    RegressionEnvironmentIdentity,
    RegressionFailureKind,
    RegressionRunner,
    build_independent_session_runner,
    load_regression_case,
)
from graph_sym_perf.validation.session_runner import SpawnedValidationSessionRunner


FIXTURE = Path("tests/fixtures/regressions/neo4j_bug001/manifest.yaml")
GRAPH_SHA256 = "718fd53bfffc8a16e6df1cc278272669bcf8d0edb89270df9f7e2e876062b6d9"
IMAGE_DIGEST = "sha256:" + "a" * 64
PASSWORD = "regression-secret-must-not-leak"


def _set_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST": IMAGE_DIGEST,
        "GRAPHSYMPERF_NEO4J_BUG001_URI": "bolt://127.0.0.1:17687",
        "GRAPHSYMPERF_NEO4J_BUG001_USER": "neo4j",
        "GRAPHSYMPERF_NEO4J_BUG001_PASSWORD": PASSWORD,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _case(monkeypatch: pytest.MonkeyPatch):
    _set_environment(monkeypatch)
    return load_regression_case(FIXTURE)


def _identity(version: str = "2026.04.0 community") -> RegressionEnvironmentIdentity:
    return RegressionEnvironmentIdentity(
        observed_version=version,
        dbms_fingerprint="b" * 64,
        driver_version="6.0.0",
    )


def _validation_outcome(
    stage: PipelineStage,
    *,
    equivalent: bool = True,
    reason_codes: tuple[str, ...] = (),
    sessions: tuple[object, ...] = (),
):
    return SimpleNamespace(
        stage=stage,
        equivalence=EquivalenceEvidence(equivalent, equivalent, reason_codes),
        reason_codes=reason_codes,
        validated_ratio=4.0 if stage is PipelineStage.VALIDATION_PASSED else None,
        pooled_delta_ms=120.0 if stage is PipelineStage.VALIDATION_PASSED else None,
        sessions=sessions,
        validation_protocol="independent-two-session-v1",
    )


class SyntheticValidator:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.candidates = []
        self.session_runners = []

    def validate(self, candidate, session_runner):
        self.candidates.append(candidate)
        self.session_runners.append(session_runner)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _runner(outcome, report_path: Path) -> tuple[RegressionRunner, SyntheticValidator]:
    validator = SyntheticValidator(outcome)
    runner = RegressionRunner(
        validator,
        report_path=report_path,
        identity_probe=lambda _case: _identity(),
        session_runner_factory=lambda _case: object(),
    )
    return runner, validator


def test_bug001_fixture_is_exact_and_queries_only_swap_comma_pattern_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(monkeypatch)
    fast = case.fast_query
    slow = case.slow_query

    assert case.graph_file_sha256 == GRAPH_SHA256
    assert case.actual_graph_nodes == 1280
    assert case.actual_graph_relationships == 2290
    assert len(case.index_statements) == 7
    assert not re.search(r"\b(?:PROFILE|EXPLAIN)\b", fast + "\n" + slow, re.IGNORECASE)
    assert fast.replace(
        "MATCH (n0:L3)-[r4:T3]->(n6)<-[r5:T3]-(n7:L4), (n5)",
        "MATCH (n5), (n0:L3)-[r4:T3]->(n6)<-[r5:T3]-(n7:L4)",
    ) == slow


def test_regression_index_lists_only_the_isolated_bug001_manifest() -> None:
    payload = yaml.safe_load(
        Path("tests/fixtures/regressions/manifest.yaml").read_text(encoding="utf-8")
    )

    assert payload["cases"] == ["neo4j_bug001/manifest.yaml"]


def test_bug001_manifest_requires_the_affected_version_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch)
    monkeypatch.delenv("GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST")

    with pytest.raises(RegressionConfigurationError, match="IMAGE_DIGEST"):
        load_regression_case(FIXTURE)


@pytest.mark.parametrize(
    "digest",
    ("latest", "sha256:abc", "sha256:" + "A" * 64, "sha512:" + "a" * 64),
)
def test_bug001_manifest_rejects_noncanonical_image_digest(
    monkeypatch: pytest.MonkeyPatch,
    digest: str,
) -> None:
    _set_environment(monkeypatch)
    monkeypatch.setenv("GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST", digest)

    with pytest.raises(RegressionConfigurationError, match="IMAGE_DIGEST"):
        load_regression_case(FIXTURE)


@pytest.mark.parametrize(
    "name",
    (
        "GRAPHSYMPERF_NEO4J_BUG001_URI",
        "GRAPHSYMPERF_NEO4J_BUG001_USER",
        "GRAPHSYMPERF_NEO4J_BUG001_PASSWORD",
    ),
)
def test_bug001_manifest_requires_dedicated_connection_environment(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    _set_environment(monkeypatch)
    monkeypatch.delenv(name)

    with pytest.raises(RegressionConfigurationError, match=name):
        load_regression_case(FIXTURE)


def test_synthetic_affected_version_reproduction_passes_without_discovery_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    runner, validator = _runner(
        _validation_outcome(PipelineStage.VALIDATION_PASSED),
        tmp_path / "regression_report.json",
    )

    report = runner.run(case, current_version_status="reproduced")

    assert report.passed
    assert report.affected_version_reproduced
    assert report.current_version_status == "reproduced"
    assert report.fresh_issue_count == 0
    assert report.discovery_accepted_pairs == 0
    assert report.validated_runtime_yield == 0
    assert report.discovery_budget == 0
    assert report.control_budget == 0
    assert report.submit_ready_created is False
    assert validator.candidates[0].pair.query_a == case.fast_query
    assert validator.candidates[0].pair.query_b == case.slow_query
    assert validator.candidates[0].discovery_slow_query == "b"
    assert validator.candidates[0].dbms_version == case.affected_version
    assert validator.candidates[0].resolved_config["image_digest"] == IMAGE_DIGEST
    assert all(
        not re.search(r"\b(?:PROFILE|EXPLAIN)\b", query, re.IGNORECASE)
        for query in (
            validator.candidates[0].pair.query_a,
            validator.candidates[0].pair.query_b,
        )
    )


def test_synthetic_affected_version_nonreproduction_blocks_gate_as_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    runner, _ = _runner(
        _validation_outcome(
            PipelineStage.REJECTED,
            reason_codes=("ratio_below_threshold",),
        ),
        tmp_path / "regression_report.json",
    )

    report = runner.run(case, current_version_status="not_reproduced")

    assert not report.passed
    assert not report.affected_version_reproduced
    assert report.failure_kind is RegressionFailureKind.RUNTIME
    assert report.current_version_status == "not_reproduced"
    assert report.fresh_issue_count == report.discovery_accepted_pairs == 0
    assert report.validated_runtime_yield == 0


def test_version_mismatch_blocks_before_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    validator = SyntheticValidator(_validation_outcome(PipelineStage.VALIDATION_PASSED))
    runner = RegressionRunner(
        validator,
        report_path=tmp_path / "regression_report.json",
        identity_probe=lambda _case: _identity("2026.05.0 community"),
        session_runner_factory=lambda _case: object(),
    )

    report = runner.run(case)

    assert not report.passed
    assert report.failure_kind is RegressionFailureKind.VERSION
    assert report.observed_version == "2026.05.0 community"
    assert validator.candidates == []


def test_static_graph_count_mismatch_is_a_graph_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = replace(_case(monkeypatch), expected_graph_nodes=1279)
    runner, validator = _runner(
        _validation_outcome(PipelineStage.VALIDATION_PASSED),
        tmp_path / "regression_report.json",
    )

    report = runner.run(case)

    assert not report.passed
    assert report.failure_kind is RegressionFailureKind.GRAPH
    assert "graph_count_mismatch" in report.reason_codes
    assert validator.candidates == []


def test_equivalence_failure_is_not_misreported_as_runtime_or_fresh_issue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    runner, _ = _runner(
        _validation_outcome(
            PipelineStage.REJECTED,
            equivalent=False,
            reason_codes=("typed_result_mismatch",),
        ),
        tmp_path / "regression_report.json",
    )

    report = runner.run(case)

    assert report.failure_kind is RegressionFailureKind.EQUIVALENCE
    assert not report.passed
    assert report.fresh_issue_count == report.discovery_accepted_pairs == 0


def test_validation_exception_is_an_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    runner, _ = _runner(RuntimeError("synthetic outage"), tmp_path / "regression_report.json")

    report = runner.run(case)

    assert not report.passed
    assert report.failure_kind is RegressionFailureKind.INFRASTRUCTURE
    assert report.reason_codes == ("validation_infrastructure_error:RuntimeError",)


def test_independent_session_version_drift_is_classified_as_version_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    session = SimpleNamespace(
        lifecycle=SimpleNamespace(graph_loaded=False, graph_verified=False),
        evidence=SimpleNamespace(
            reason_codes=(
                "affected version mismatch: expected 2026.04.0 community, "
                "observed 2026.05.0 community",
            )
        ),
    )
    runner, _ = _runner(
        _validation_outcome(
            PipelineStage.INVALID,
            equivalent=False,
            reason_codes=("session_lifecycle_incomplete",),
            sessions=(session,),
        ),
        tmp_path / "regression_report.json",
    )

    report = runner.run(case)

    assert report.failure_kind is RegressionFailureKind.VERSION


def test_independent_session_graph_verification_failure_is_classified_as_graph_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    session = SimpleNamespace(
        lifecycle=SimpleNamespace(graph_loaded=True, graph_verified=False),
        evidence=SimpleNamespace(reason_codes=("graph_fingerprint_mismatch",)),
    )
    runner, _ = _runner(
        _validation_outcome(
            PipelineStage.INVALID,
            equivalent=False,
            reason_codes=("session_lifecycle_incomplete",),
            sessions=(session,),
        ),
        tmp_path / "regression_report.json",
    )

    report = runner.run(case)

    assert report.failure_kind is RegressionFailureKind.GRAPH


def test_report_is_atomic_deterministic_identity_bound_and_password_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _case(monkeypatch)
    first_path = tmp_path / "first" / "regression_report.json"
    second_path = tmp_path / "second" / "regression_report.json"
    first, _ = _runner(_validation_outcome(PipelineStage.VALIDATION_PASSED), first_path)
    second, _ = _runner(_validation_outcome(PipelineStage.VALIDATION_PASSED), second_path)

    first_report = first.run(case)
    second_report = second.run(case)

    assert first_path.read_bytes() == second_path.read_bytes()
    assert first_report == second_report
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    serialized = first_path.read_text(encoding="utf-8")
    assert payload["case_id"] == case.case_id
    assert payload["affected_version"] == case.affected_version
    assert payload["observed_version"] == case.affected_version
    assert payload["image_ref"] == case.image_ref
    assert payload["image_digest"] == IMAGE_DIGEST
    assert payload["graph_file_sha256"] == GRAPH_SHA256
    assert payload["environment_identity_hash"]
    assert payload["content_hash"] == first_report.content_hash
    assert PASSWORD not in serialized
    assert not tuple(first_path.parent.glob(".*.tmp"))


def test_default_production_path_builds_task8_spawned_session_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(monkeypatch)

    session_runner = build_independent_session_runner(case)

    assert isinstance(session_runner, SpawnedValidationSessionRunner)
    assert RuleStream.REGRESSION.value == "regression"
