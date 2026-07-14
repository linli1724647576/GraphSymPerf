"""Shared real typed regression gate fixture for P1/P2 worker tests."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import graph_sym_perf.validation.regression as regression_module
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.validation.regression import (
    RegressionEnvironmentIdentity,
    RegressionRunner,
)
from graph_sym_perf.validation.session_runner import validation_schedule_seed
from graph_sym_perf.validation.validator import IssueValidator
from tests.integration.test_issue_validation import FakeIndependentRunner


@dataclass(frozen=True)
class VerifiedRegressionGateFixture:
    root: Path
    environment: dict[str, str]


class _SeededFakeIndependentRunner(FakeIndependentRunner):
    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(candidate, session_number, **kwargs)
        return equivalence, replace(
            session,
            schedule_seed=validation_schedule_seed(candidate.candidate_hash, session_number),
        )


def regression_environment() -> dict[str, str]:
    return {
        "GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "GRAPHSYMPERF_NEO4J_BUG001_CONTAINER": "neo4j-bug001",
        "GRAPHSYMPERF_NEO4J_BUG001_URI": "bolt://127.0.0.1:17687",
        "GRAPHSYMPERF_NEO4J_BUG001_USER": "bug-user",
        "GRAPHSYMPERF_NEO4J_BUG001_PASSWORD": "bug-secret",
        "GRAPHSYMPERF_NEO4J_URI": "bolt://127.0.0.1:7687/main",
        "GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-main",
    }


def load_offline_regression_case(path, environment):
    return regression_module.load_regression_case(
        path,
        environment,
        provenance_probe=lambda mapped, target: (
            mapped["GRAPHSYMPERF_NEO4J_IMAGE_DIGEST"]
            if target is TargetSystem.NEO4J
            else None
        ),
    )


def current_identity(case) -> RegressionEnvironmentIdentity:
    return RegressionEnvironmentIdentity(
        case.affected_version,
        "b" * 64,
        "6.0.0",
        case.provenance.verified_image_digest,
        case.provenance.container_identity,
        case.provenance.endpoint_identity,
        case.index_names,
        ("ONLINE",) * 7,
    )


def build_verified_regression_gate_template(root: Path) -> VerifiedRegressionGateFixture:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = regression_environment()
    case = load_offline_regression_case(spec.regression["manifest"], environment)
    identity = current_identity(case)
    campaign_identity = regression_module.regression_campaign_identity(
        spec_name=spec.name,
        schema_version=spec.schema_version,
        manifest_sha256=case.manifest_sha256,
    )
    report_path = root / "regressions" / case.case_id / "regression_report.json"
    RegressionRunner._for_test(
        IssueValidator(),
        report_path=report_path,
        identity_probe=lambda _case: identity,
        session_runner_factory=lambda _case: _SeededFakeIndependentRunner(),
    ).run(case, campaign_identity_hash=campaign_identity)
    regression_module.load_verified_regression_report(
        report_path,
        case=case,
        campaign_identity_hash=campaign_identity,
        current_identity=identity,
    )
    return VerifiedRegressionGateFixture(root=root, environment=environment)


def install_verified_regression_gate(
    monkeypatch,
    worker_module,
    fixture: VerifiedRegressionGateFixture,
) -> None:
    """Install strict case resolution, typed live identity, and copied immutable artifacts."""

    monkeypatch.setattr(worker_module, "load_regression_case", load_offline_regression_case)
    monkeypatch.setattr(
        worker_module,
        "probe_current_regression_environment_identity",
        current_identity,
    )
    original_bind = worker_module.RuntimeFirstShardWorker.bind_run_dir

    def bind_with_gate(worker, run_dir):
        destination = Path(run_dir)
        worker.environment.update(fixture.environment)
        shutil.copytree(
            fixture.root / "regressions",
            destination / "regressions",
            dirs_exist_ok=True,
        )
        return original_bind(worker, destination)

    monkeypatch.setattr(
        worker_module.RuntimeFirstShardWorker,
        "bind_run_dir",
        bind_with_gate,
    )


__all__ = [
    "VerifiedRegressionGateFixture",
    "build_verified_regression_gate_template",
    "install_verified_regression_gate",
]
