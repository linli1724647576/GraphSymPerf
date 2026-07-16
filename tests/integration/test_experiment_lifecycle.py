import json
import hashlib
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import graph_sym_perf.experiment.lifecycle as lifecycle_module
import graph_sym_perf.experiment.planner as planner_module
from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle
from graph_sym_perf.experiment.evidence_validation import canonical_hash
from graph_sym_perf.experiment.campaign import (
    CampaignLifecycle,
    MeasurementSessionBusy,
    MeasurementSessionLock,
    MeasurementSessionOwnershipError,
    PhaseAncestryError,
)
from graph_sym_perf.experiment import campaign as campaign_module
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.plan_models import CampaignPhase, FrozenMapping
from graph_sym_perf.experiment.selection import select_p2_cells, write_pilot_selection
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.state import ManifestMismatchError
from graph_sym_perf.experiment.supervisor import RunSummary
from tests.gdsmith_artifact_fixture import (
    assume_schema_v3_sources_clean,
    verified_gdsmith_environment,
)


@pytest.fixture(autouse=True)
def _clean_schema_v3_sources(monkeypatch) -> None:
    assume_schema_v3_sources_clean(monkeypatch)


class _CompleteLifecycleWorker:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        hashes = [
            hashlib.sha256(f"{task.target.value}:{task.shard_index}:{index}".encode()).hexdigest()
            for index in range(task.accepted_query_pairs)
        ]
        (self.context.shard_dir / "accepted_pairs.jsonl").write_text(
            "".join(json.dumps({"pair_hash": value}) + "\n" for value in hashes),
            encoding="utf-8",
        )
        stages = {}
        for stage in ("screening", "validation", "reduction"):
            path = self.context.attempt_dir / f"{stage}.json"
            path.write_text(json.dumps({"stage": stage}), encoding="utf-8")
            stages[stage] = {
                "path": str(path.relative_to(self.context.shard_dir)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return {"accepted_pairs": len(hashes), "accepted_pair_hashes": hashes, "stage_evidence": stages}


class _CampaignFixtureWorker:
    def __init__(self, *, underfill_first: bool = False, assert_lock=None) -> None:
        self.underfill_first = underfill_first
        self.assert_lock = assert_lock
        self.calls = 0

    def bind_run_dir(self, _run_dir) -> None:
        return None

    def bind_attempt_context(self, _context) -> None:
        return None

    def __call__(self, entry):
        self.calls += 1
        if self.assert_lock is not None:
            with pytest.raises(MeasurementSessionBusy, match="MEASUREMENT_SESSION_BUSY"):
                self.assert_lock.acquire()
        accepted = entry.accepted_pair_quota
        terminal = "completed"
        if self.underfill_first and self.calls == 1:
            accepted -= 1
            terminal = "underfilled"
        is_winner = entry.stream.value == "discovery" and (
            (entry.rule_id == "constant_true_disjunction" and entry.graph_scale == 128)
            or (entry.rule_id == "pattern_order_symmetry" and entry.graph_scale == 512)
        )
        return {
            "accepted_pairs": accepted,
            "artifact_complete": True,
            "certificate_complete": True,
            "control_validation_pass": False,
            "entry_id": entry.entry_id,
            "regression_artifact_complete": True,
            "regression_passed": True,
            "terminal_state": terminal,
            "validation_passed_count": int(is_winner),
            "validation_pair_yield": 0.1 if is_winner else 0.0,
            "validation_yield_ci_low": 0.05 if is_winner else 0.0,
            "best_validated_delta_ms": 80.0 if is_winner else 0.0,
            "best_validated_ratio": 2.0 if is_winner else 1.0,
            "best_ratio_ci_low": 1.5 if is_winner else 1.0,
            "best_delta_over_noise": 6.0 if is_winner else 0.0,
        }


def _verified_regression_reference(_spec, run_dir, _environment):
    return {
        "artifact_complete": True,
        "campaign_identity_hash": "d" * 64,
        "case_id": "neo4j_bug001_pattern_order",
        "environment_identity_hash": "e" * 64,
        "passed": True,
        "report_content_hash": "b" * 64,
        "report_path": str(
            (run_dir / "regressions" / "neo4j_bug001_pattern_order" / "regression_report.json")
            .relative_to(run_dir)
        ),
        "report_sha256": "a" * 64,
        "validation_evidence_sha256": "c" * 64,
    }


def _campaign_lifecycle(root):
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    return CampaignLifecycle(
        root,
        environment=verified_gdsmith_environment(
            root / "gdsmith-fixture",
            spec.generation.expected_commit,
        ),
        regression_verifier=_verified_regression_reference,
        measurement_lock_root=root / "measurement-locks",
        process_isolation=False,
    )


def _cell_summary_root(result) -> str:
    return json.loads(
        (result.run_dir / "phase_outcome.json").read_text(encoding="utf-8")
    )["cell_summary_root"]


def test_campaign_uses_configured_infrastructure_retry_limit(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    calls: dict[str, int] = {}
    delegate = _CampaignFixtureWorker()

    class Worker:
        def __call__(self, entry):
            calls[entry.entry_id] = calls.get(entry.entry_id, 0) + 1
            if calls[entry.entry_id] == 1:
                return {
                    "accepted_pairs": 0,
                    "artifact_complete": True,
                    "certificate_complete": False,
                    "entry_id": entry.entry_id,
                    "terminal_state": "infrastructure_failed",
                }
            return delegate(entry)

    result = lifecycle.run(
        spec,
        Worker(),
        phase=CampaignPhase.P0,
        run_id="p0-configured-retry",
    )

    assert result.passed
    assert set(calls.values()) == {2}
    events = [
        json.loads(line)
        for line in (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["attempt"] == 1 for event in events)


def test_campaign_phase_ancestry_selection_and_exact_p2_expansion(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    p0 = lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="p0")
    assert p0.passed
    p1 = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P1,
        run_id="p1",
        parent_run="p0",
    )
    assert p1.passed

    selection = select_p2_cells(
        lifecycle.cell_summaries("p1"),
        campaign_id=p1.campaign_id,
        input_p1_manifest_hash=p1.manifest_hash,
        input_p1_cell_summary_root=_cell_summary_root(p1),
    )
    assert len(selection.cells) == 4  # two winners plus one deterministic fallback per family
    selection_path = tmp_path / "pilot_selection.json"
    write_pilot_selection(selection, selection_path)
    p2 = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P2,
        run_id="p2",
        parent_run="p1",
        selection=selection_path,
    )
    assert p2.passed

    p1_plan = json.loads((p1.run_dir / "rule_plan.json").read_text(encoding="utf-8"))
    p2_plan = json.loads((p2.run_dir / "rule_plan.json").read_text(encoding="utf-8"))
    assert len(p2_plan["entries"]) == len(selection.cells) * 3
    assert {entry["shard_index"] for entry in p2_plan["entries"]} == {0, 1, 2}
    assert all(entry["accepted_pair_quota"] == 1000 for entry in p2_plan["entries"])
    assert not (
        {entry["shard_seed"] for entry in p1_plan["entries"]}
        & {entry["shard_seed"] for entry in p2_plan["entries"]}
    )


def test_campaign_binds_p1_directly_to_p0_regression_artifact_root(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)

    class _BoundWorker(_CampaignFixtureWorker):
        def __init__(self):
            super().__init__()
            self.regression_roots = []

        def bind_regression_artifact_root(self, root):
            self.regression_roots.append(__import__("pathlib").Path(root))

    p0_worker = _BoundWorker()
    p0 = lifecycle.run(spec, p0_worker, phase=CampaignPhase.P0, run_id="p0")
    p1_worker = _BoundWorker()
    p1 = lifecycle.run(
        spec,
        p1_worker,
        phase=CampaignPhase.P1,
        run_id="p1",
        parent_run="p0",
    )
    selection_path = tmp_path / "selection.json"
    lifecycle.create_selection(p1_run="p1", output=selection_path)
    p2_worker = _BoundWorker()
    p2 = lifecycle.run(
        spec,
        p2_worker,
        phase=CampaignPhase.P2,
        run_id="p2",
        parent_run="p1",
        selection=selection_path,
    )

    assert p0.passed and p1.passed and p2.passed
    assert p0_worker.regression_roots == [p0.run_dir]
    assert p1_worker.regression_roots == [p0.run_dir]
    assert p2_worker.regression_roots == [p0.run_dir]
    p1_manifest = json.loads((p1.run_dir / "resolved_manifest.json").read_text(encoding="utf-8"))
    assert p1_manifest["parent"]["regression_artifact"]["report_sha256"] == "a" * 64


def test_p0_fails_closed_when_regression_artifact_cannot_be_verified(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    def rejected_verifier(*_args, **_kwargs):
        raise ValueError("regression_gate_artifact_identity_mismatch")

    lifecycle = CampaignLifecycle(
        tmp_path,
        environment=verified_gdsmith_environment(
            tmp_path / "gdsmith-fixture",
            spec.generation.expected_commit,
        ),
        regression_verifier=rejected_verifier,
        measurement_lock_root=tmp_path / "measurement-locks",
    )

    result = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P0,
        run_id="p0",
    )

    assert result.passed is False
    outcome = json.loads((result.run_dir / "phase_outcome.json").read_text(encoding="utf-8"))
    assert "regression_artifact_invalid" in outcome["reason_codes"]


def test_campaign_requires_successful_matching_parent_and_preserves_manifests(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    with pytest.raises(PhaseAncestryError, match="P0 parent"):
        lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P1, run_id="p1")

    failed = lifecycle.run(
        spec,
        _CampaignFixtureWorker(underfill_first=True),
        phase=CampaignPhase.P0,
        run_id="failed-p0",
    )
    assert not failed.passed
    with pytest.raises(PhaseAncestryError, match="successful"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P1,
            run_id="blocked-p1",
            parent_run="failed-p0",
        )

    good = lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="good-p0")
    assert good.passed
    with pytest.raises(PhaseAncestryError, match="campaign"):
        lifecycle.run(
            replace(spec, name="identity-drift"),
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P1,
            run_id="drift-p1",
            parent_run="good-p0",
        )
    with pytest.raises(FileExistsError, match="run id"):
        lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="good-p0")

    interrupted = tmp_path / "interrupted"
    interrupted.mkdir()
    (interrupted / "rule_plan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="run id"):
        lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="interrupted")


def test_campaign_rejects_an_incomplete_rule_plan_source_revision_before_p0(
    monkeypatch,
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    real_builder = campaign_module.build_rule_plan

    def incomplete_builder(*args, **kwargs):
        plan = real_builder(*args, **kwargs)
        return replace(
            plan,
            source_revision=replace(
                plan.source_revision,
                gdsmith_build_manifest_sha256=None,
            ),
        )

    monkeypatch.setattr(campaign_module, "build_rule_plan", incomplete_builder)

    with pytest.raises(PhaseAncestryError, match="source revision is incomplete"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P0,
            run_id="incomplete-p0",
        )
    assert not (tmp_path / "incomplete-p0").exists()


def test_campaign_rejects_a_forged_dirty_rule_plan_before_p0(
    monkeypatch,
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    real_builder = campaign_module.build_rule_plan

    def dirty_builder(*args, **kwargs):
        plan = real_builder(*args, **kwargs)
        return replace(
            plan,
            source_revision=replace(plan.source_revision, repository_dirty=True),
        )

    monkeypatch.setattr(campaign_module, "build_rule_plan", dirty_builder)
    monkeypatch.setattr(
        campaign_module,
        "_campaign_source_provenance_guard",
        lambda *_args, **_kwargs: lambda: None,
    )

    with pytest.raises(PhaseAncestryError, match="source revision must be clean"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P0,
            run_id="dirty-p0",
        )
    assert not (tmp_path / "dirty-p0").exists()


def test_campaign_rejects_incomplete_parent_source_revision_even_when_resealed(
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    p0 = lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="p0")
    rule_plan_path = p0.run_dir / "rule_plan.json"
    rule_plan = json.loads(rule_plan_path.read_text(encoding="utf-8"))
    rule_plan["source_revision"]["gdsmith_jar_sha256"] = None
    rule_plan_path.write_text(json.dumps(rule_plan), encoding="utf-8")
    manifest_path = p0.run_dir / "resolved_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("content_hash")
    manifest["rule_plan_hash"] = canonical_hash(rule_plan)
    manifest_path.write_text(
        json.dumps({**manifest, "content_hash": canonical_hash(manifest)}),
        encoding="utf-8",
    )

    with pytest.raises(PhaseAncestryError, match="source revision is incomplete"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P1,
            run_id="blocked-p1",
            parent_run="p0",
        )


def test_campaign_rejects_tampered_parent_and_selection_context(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    p0 = lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="p0")
    outcome_path = p0.run_dir / "phase_outcome.json"
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    outcome["passed"] = False
    outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
    with pytest.raises(PhaseAncestryError, match="content hash"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P1,
            run_id="p1",
            parent_run="p0",
        )

    intact = lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="intact-p0")
    rule_plan_path = intact.run_dir / "rule_plan.json"
    rule_plan = json.loads(rule_plan_path.read_text(encoding="utf-8"))
    rule_plan["entries"][0]["accepted_pair_quota"] = 999
    rule_plan_path.write_text(json.dumps(rule_plan), encoding="utf-8")
    with pytest.raises(PhaseAncestryError, match="rule plan content hash"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P1,
            run_id="tampered-plan-p1",
            parent_run="intact-p0",
        )


def test_phase_outcome_binds_rule_plan_ordered_cell_summary_root(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="p0")
    p1 = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P1,
        run_id="p1",
        parent_run="p0",
    )
    outcome = json.loads((p1.run_dir / "phase_outcome.json").read_text(encoding="utf-8"))
    assert isinstance(outcome["cell_summary_root"], str)

    summary_path = next((p1.run_dir / "entries").glob("*/cell_summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["best_validated_delta_ms"] = 9999.0
    body = {key: value for key, value in summary.items() if key != "content_hash"}
    summary["content_hash"] = canonical_hash(body)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(PhaseAncestryError, match="cell summary root"):
        lifecycle.cell_summaries("p1")


def test_campaign_runs_manifest_bound_source_guard_before_and_after_each_entry(
    monkeypatch,
    tmp_path,
) -> None:
    observed = []

    def guard_factory(_spec, plan, *, environment):
        assert environment["GRAPHSYMPERF_GDSMITH_JAR_SHA256"] == (
            plan.source_revision.gdsmith_jar_sha256
        )

        def guard():
            observed.append(plan.source_revision)

        return guard

    monkeypatch.setattr(
        campaign_module,
        "_campaign_source_provenance_guard",
        guard_factory,
    )
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    result = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P0,
        run_id="p0",
    )
    rule_plan = json.loads((result.run_dir / "rule_plan.json").read_text(encoding="utf-8"))

    assert len(observed) == 2 * len(rule_plan["entries"])
    assert all(asdict(source) == rule_plan["source_revision"] for source in observed)


def test_campaign_source_guard_rehashes_gdsmith_artifacts_and_rejects_drift(
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(
        tmp_path / "gdsmith-fixture",
        spec.generation.expected_commit,
    )
    plan = campaign_module.build_rule_plan(
        spec,
        CampaignPhase.P0,
        environment=environment,
    )
    guard = campaign_module._campaign_source_provenance_guard(
        spec,
        plan,
        environment=environment,
    )
    with open(environment["GRAPHSYMPERF_GDSMITH_JAR"], "ab") as stream:
        stream.write(b"drift")

    with pytest.raises(ManifestMismatchError, match="source provenance changed"):
        guard()


def test_campaign_source_guard_rejects_gdsmith_drift_between_plan_and_guard(
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(
        tmp_path / "gdsmith-fixture",
        spec.generation.expected_commit,
    )
    plan = campaign_module.build_rule_plan(
        spec,
        CampaignPhase.P0,
        environment=environment,
    )
    jar_path = Path(environment["GRAPHSYMPERF_GDSMITH_JAR"])
    jar_path.write_bytes(jar_path.read_bytes() + b"plan-to-guard-drift")

    with pytest.raises(ManifestMismatchError, match="source provenance changed"):
        guard = campaign_module._campaign_source_provenance_guard(
            spec,
            plan,
            environment=environment,
        )
        guard()


def test_source_guard_constructor_rejects_drift_after_tooling_verification(
    monkeypatch,
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(
        tmp_path / "gdsmith-fixture",
        spec.generation.expected_commit,
    )
    plan = campaign_module.build_rule_plan(
        spec,
        CampaignPhase.P0,
        environment=environment,
    )
    jar_path = Path(environment["GRAPHSYMPERF_GDSMITH_JAR"])
    real_verify = lifecycle_module._SourceProvenanceGuard._verify_artifact_provenance
    mutated = False

    def verify_then_mutate(guard) -> None:
        nonlocal mutated
        real_verify(guard)
        if not mutated:
            mutated = True
            jar_path.write_bytes(jar_path.read_bytes() + b"post-verify-drift")

    monkeypatch.setattr(
        lifecycle_module._SourceProvenanceGuard,
        "_verify_artifact_provenance",
        verify_then_mutate,
    )

    with pytest.raises(ManifestMismatchError, match="source provenance changed"):
        campaign_module._campaign_source_provenance_guard(
            spec,
            plan,
            environment=environment,
        )


def test_source_guard_rechecks_tooling_on_every_attempt(
    monkeypatch,
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(
        tmp_path / "gdsmith-fixture",
        spec.generation.expected_commit,
    )
    plan = campaign_module.build_rule_plan(
        spec,
        CampaignPhase.P0,
        environment=environment,
    )
    guard = campaign_module._campaign_source_provenance_guard(
        spec,
        plan,
        environment=environment,
    )
    real_check = lifecycle_module.check_gdsmith_tooling
    tooling_checks = 0

    def check_tooling(*args, **kwargs):
        nonlocal tooling_checks
        tooling_checks += 1
        return real_check(*args, **kwargs)

    monkeypatch.setattr(lifecycle_module, "check_gdsmith_tooling", check_tooling)

    guard()
    guard()

    assert tooling_checks == 2


@pytest.mark.parametrize("changed_source", ("repository", "gdsmith"))
def test_campaign_source_guard_rejects_cleanliness_drift_after_plan(
    monkeypatch,
    tmp_path,
    changed_source,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = verified_gdsmith_environment(
        tmp_path / "gdsmith-fixture",
        spec.generation.expected_commit,
    )
    repository_root = spec.source_path.parents[1]
    dirty_state = {"repository": False, "gdsmith": False}

    def dirty(path):
        if path == repository_root:
            return dirty_state["repository"]
        if path == spec.generation.checkout:
            return dirty_state["gdsmith"]
        return False

    monkeypatch.setattr(lifecycle_module, "_git_dirty", dirty)
    monkeypatch.setattr(planner_module, "_git_dirty", dirty)
    plan = campaign_module.build_rule_plan(
        spec,
        CampaignPhase.P0,
        environment=environment,
    )
    dirty_state[changed_source] = True
    guard = campaign_module._campaign_source_provenance_guard(
        spec,
        plan,
        environment=environment,
    )

    with pytest.raises(ManifestMismatchError, match="source provenance changed"):
        guard()


def test_p2_rejects_self_consistent_selection_not_built_from_parent_p1_cells(
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="p0")
    p1 = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P1,
        run_id="p1",
        parent_run="p0",
    )
    actual = lifecycle.cell_summaries("p1")
    forged = tuple(
        replace(
            cell,
            validation_passed_count=int(
                cell.stream == "discovery"
                and cell.rule_id == "constant_true_disjunction"
                and cell.graph_scale == 256
            ),
            validation_pair_yield=(
                0.5
                if cell.stream == "discovery"
                and cell.rule_id == "constant_true_disjunction"
                and cell.graph_scale == 256
                else 0.0
            ),
            validation_yield_ci_low=(
                0.2
                if cell.stream == "discovery"
                and cell.rule_id == "constant_true_disjunction"
                and cell.graph_scale == 256
                else 0.0
            ),
            best_validated_delta_ms=(
                500.0
                if cell.stream == "discovery"
                and cell.rule_id == "constant_true_disjunction"
                and cell.graph_scale == 256
                else 0.0
            ),
        )
        for cell in actual
    )
    forged_selection = select_p2_cells(
        forged,
        campaign_id=p1.campaign_id,
        input_p1_manifest_hash=p1.manifest_hash,
        input_p1_cell_summary_root=_cell_summary_root(p1),
    )
    selection_path = tmp_path / "forged-selection.json"
    write_pilot_selection(forged_selection, selection_path)

    with pytest.raises(PhaseAncestryError, match="parent P1"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P2,
            run_id="forged-p2",
            parent_run="p1",
            selection=selection_path,
        )


def test_p2_rejects_self_consistent_selection_with_non_preregistered_max_cells(
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    p0 = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P0,
        run_id="p0",
    )
    p1 = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P1,
        run_id="p1",
        parent_run=p0.run_id,
    )
    selection = select_p2_cells(
        lifecycle.cell_summaries(p1.run_id),
        max_cells=2,
        campaign_id=p1.campaign_id,
        input_p1_manifest_hash=p1.manifest_hash,
        input_p1_cell_summary_root=_cell_summary_root(p1),
    )
    selection_path = tmp_path / "max-two-selection.json"
    write_pilot_selection(selection, selection_path)

    with pytest.raises(PhaseAncestryError, match="preregistered"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P2,
            run_id="p2",
            parent_run=p1.run_id,
            selection=selection_path,
        )

    assert not (tmp_path / "p2").exists()


def test_measurement_session_is_cross_process_exclusive_and_owner_only(tmp_path) -> None:
    lock = MeasurementSessionLock(tmp_path)
    lease = lock.acquire()
    try:
        with pytest.raises(MeasurementSessionBusy, match="MEASUREMENT_SESSION_BUSY"):
            MeasurementSessionLock(tmp_path).acquire()
        script = (
            "from graph_sym_perf.experiment.campaign import MeasurementSessionLock, MeasurementSessionBusy; "
            f"root={str(tmp_path)!r}; "
            "\ntry: MeasurementSessionLock(root).acquire()"
            "\nexcept MeasurementSessionBusy: raise SystemExit(23)"
            "\nraise SystemExit(0)"
        )
        completed = subprocess.run(
            (sys.executable, "-c", script),
            check=False,
            env={**os.environ, "PYTHONPATH": str((__import__("pathlib").Path.cwd() / "src"))},
        )
        assert completed.returncode == 23

        owner_path = tmp_path / ".neo4j-measurement-session" / "owner.json"
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        owner["token"] = "forged-owner"
        owner_path.write_text(json.dumps(owner), encoding="utf-8")
        with pytest.raises(MeasurementSessionOwnershipError, match="ownership"):
            lease.release()
    finally:
        # Test-only cleanup after deliberately corrupting the owner receipt.
        import shutil

        shutil.rmtree(tmp_path / ".neo4j-measurement-session", ignore_errors=True)


def test_campaign_holds_measurement_lock_across_worker_validation(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    result = lifecycle.run(
        spec,
        _CampaignFixtureWorker(assert_lock=lifecycle.measurement_lock(spec)),
        phase=CampaignPhase.P0,
        run_id="p0",
    )
    assert result.passed


def test_campaign_rejects_a_forged_p2_seed_overlap(monkeypatch, tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="p0")
    p1 = lifecycle.run(
        spec,
        _CampaignFixtureWorker(),
        phase=CampaignPhase.P1,
        run_id="p1",
        parent_run="p0",
    )
    selection_path = tmp_path / "pilot_selection.json"
    write_pilot_selection(
        select_p2_cells(
            lifecycle.cell_summaries("p1"),
            campaign_id=p1.campaign_id,
            input_p1_manifest_hash=p1.manifest_hash,
            input_p1_cell_summary_root=_cell_summary_root(p1),
        ),
        selection_path,
    )
    p1_plan = json.loads((p1.run_dir / "rule_plan.json").read_text(encoding="utf-8"))
    overlapping_seed = p1_plan["entries"][0]["shard_seed"]
    from graph_sym_perf.experiment import campaign as campaign_module

    real_builder = campaign_module.build_rule_plan

    def forged_builder(*args, **kwargs):
        plan = real_builder(*args, **kwargs)
        if plan.phase is CampaignPhase.P2:
            plan = replace(
                plan,
                entries=(replace(plan.entries[0], shard_seed=overlapping_seed), *plan.entries[1:]),
            )
        return plan

    monkeypatch.setattr(campaign_module, "build_rule_plan", forged_builder)
    with pytest.raises(PhaseAncestryError, match="overlap P1"):
        lifecycle.run(
            spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P2,
            run_id="forged-p2",
            parent_run="p1",
            selection=selection_path,
        )


def test_measurement_lock_recovers_only_a_content_bound_dead_owner(tmp_path) -> None:
    first = MeasurementSessionLock(tmp_path).acquire()
    owner_path = tmp_path / ".neo4j-measurement-session" / "owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["pid"] = 2_000_000_000
    body = {key: value for key, value in owner.items() if key != "content_hash"}
    owner["content_hash"] = canonical_hash(body)
    owner_path.write_text(
        json.dumps(owner, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    recovered = MeasurementSessionLock(tmp_path).acquire()
    recovered.release()
    with pytest.raises(MeasurementSessionOwnershipError):
        first.release()


def test_measurement_lock_never_steals_a_live_pid_with_mismatched_identity(tmp_path) -> None:
    lease = MeasurementSessionLock(tmp_path).acquire()
    owner_path = tmp_path / ".neo4j-measurement-session" / "owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["process_start_identity"] = "forged-live-identity"
    body = {key: value for key, value in owner.items() if key != "content_hash"}
    owner["content_hash"] = canonical_hash(body)
    owner_path.write_text(
        json.dumps(owner, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(MeasurementSessionBusy, match="MEASUREMENT_SESSION_BUSY"):
            MeasurementSessionLock(tmp_path).acquire()
    finally:
        import shutil

        shutil.rmtree(tmp_path / ".neo4j-measurement-session", ignore_errors=True)
    with pytest.raises(MeasurementSessionOwnershipError):
        lease.release()


def test_measurement_lock_never_reclaims_a_foreign_host_owner(tmp_path) -> None:
    lease = MeasurementSessionLock(tmp_path).acquire()
    owner_path = tmp_path / ".neo4j-measurement-session" / "owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["host"] = "remote-builder.example"
    owner["pid"] = 2_000_000_000
    body = {key: value for key, value in owner.items() if key != "content_hash"}
    owner["content_hash"] = canonical_hash(body)
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    try:
        with pytest.raises(MeasurementSessionBusy, match="MEASUREMENT_SESSION_BUSY"):
            MeasurementSessionLock(tmp_path).acquire()
    finally:
        import shutil

        shutil.rmtree(tmp_path / ".neo4j-measurement-session", ignore_errors=True)
    with pytest.raises(MeasurementSessionOwnershipError):
        lease.release()


def test_measurement_owner_publish_failure_rolls_back_without_poisoning_lock(
    monkeypatch,
    tmp_path,
) -> None:
    original = campaign_module._create_owner_file
    monkeypatch.setattr(
        campaign_module,
        "_create_owner_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected-owner-write")),
    )
    with pytest.raises(OSError, match="injected-owner-write"):
        MeasurementSessionLock(tmp_path).acquire()
    assert not (tmp_path / ".neo4j-measurement-session").exists()

    monkeypatch.setattr(campaign_module, "_create_owner_file", original)
    lease = MeasurementSessionLock(tmp_path).acquire()
    lease.release()


def test_same_neo4j_target_contends_across_different_campaign_roots(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    environment = {
        "GRAPHSYMPERF_NEO4J_URI": "bolt://127.0.0.1:7687/neo4j",
        "GRAPHSYMPERF_NEO4J_DATABASE": "neo4j",
        "GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-main",
    }
    identity = campaign_module.measurement_target_identity(spec, environment)
    changed_metadata = {
        **environment,
        "GRAPHSYMPERF_NEO4J_URI": "neo4j://127.0.0.1:7687/neo4j",
        "GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": "sha256:" + "b" * 64,
        "GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-recreated",
    }
    same_server_identity = campaign_module.measurement_target_identity(
        spec,
        changed_metadata,
    )
    assert same_server_identity == identity
    lock_root = tmp_path / "shared-locks"
    first = MeasurementSessionLock(lock_root, target_identity=identity).acquire()
    try:
        with pytest.raises(MeasurementSessionBusy, match="MEASUREMENT_SESSION_BUSY"):
            MeasurementSessionLock(
                lock_root,
                target_identity=same_server_identity,
            ).acquire()
    finally:
        first.release()


def test_measurement_target_identity_uses_preregistered_id_without_dns(
    monkeypatch,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")

    def reject_dns(*_args, **_kwargs):
        raise AssertionError("measurement target identity must not perform DNS lookup")

    monkeypatch.setattr(campaign_module.socket, "getaddrinfo", reject_dns)

    def identity(uri, database="neo4j"):
        return campaign_module.measurement_target_identity(
            spec,
            {
                "GRAPHSYMPERF_NEO4J_URI": uri,
                "GRAPHSYMPERF_NEO4J_DATABASE": database,
            },
        )

    first = identity("bolt://alias-one.internal:7687/neo4j")
    assert first == identity("bolt://alias-two.internal:7687/neo4j")
    assert first == identity("bolt://[fe80::1%25en0]:7687/neo4j")
    assert first == identity("bolt://other-alias.internal:7688/system", "system")


def test_different_preregistered_measurement_targets_can_run_concurrently(
    tmp_path,
) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    systems = dict(spec.systems)
    systems[TargetSystem.NEO4J] = replace(
        systems[TargetSystem.NEO4J],
        measurement_target_id="neo4j-independent-target-v2",
    )
    other_spec = replace(spec, systems=FrozenMapping(systems))
    first_identity = campaign_module.measurement_target_identity(spec, {})
    second_identity = campaign_module.measurement_target_identity(other_spec, {})
    assert first_identity != second_identity

    first = MeasurementSessionLock(
        tmp_path,
        target_identity=first_identity,
    ).acquire()
    try:
        second = MeasurementSessionLock(
            tmp_path,
            target_identity=second_identity,
        ).acquire()
        second.release()
    finally:
        first.release()


def test_campaign_binds_measurement_target_id_across_phases(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = _campaign_lifecycle(tmp_path)
    p0 = lifecycle.run(spec, _CampaignFixtureWorker(), phase=CampaignPhase.P0, run_id="p0")
    assert p0.passed

    systems = dict(spec.systems)
    systems[TargetSystem.NEO4J] = replace(
        systems[TargetSystem.NEO4J],
        measurement_target_id="neo4j-drifted-target-v2",
    )
    drifted_spec = replace(spec, systems=FrozenMapping(systems))
    with pytest.raises(PhaseAncestryError, match="campaign"):
        lifecycle.run(
            drifted_spec,
            _CampaignFixtureWorker(),
            phase=CampaignPhase.P1,
            run_id="p1-target-drift",
            parent_run="p0",
        )


def test_busy_measurement_target_never_publishes_a_campaign_run(tmp_path) -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    lifecycle = CampaignLifecycle(
        tmp_path / "campaign",
        environment=verified_gdsmith_environment(
            tmp_path / "gdsmith-fixture",
            spec.generation.expected_commit,
        ),
        regression_verifier=_verified_regression_reference,
        measurement_lock_root=tmp_path / "locks",
    )
    lease = lifecycle.measurement_lock(spec).acquire()
    try:
        with pytest.raises(MeasurementSessionBusy, match="MEASUREMENT_SESSION_BUSY"):
            lifecycle.run(
                spec,
                _CampaignFixtureWorker(),
                phase=CampaignPhase.P0,
                run_id="must-not-exist",
            )
    finally:
        lease.release()
    assert not (tmp_path / "campaign" / "must-not-exist").exists()


def test_lifecycle_writes_immutable_manifest_and_summary(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    result = ExperimentLifecycle(tmp_path, process_isolation=False).run(spec, _CompleteLifecycleWorker())
    assert (result.run_dir / "resolved_manifest.json").exists()
    summary = json.loads((result.run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["completed_count"] == 112


def test_formal_lifecycle_enables_process_isolation(monkeypatch, tmp_path) -> None:
    captured = {}

    class _Supervisor:
        def __init__(self, _run_dir, **kwargs) -> None:
            captured.update(kwargs)

        def run(self, _plan, _worker) -> RunSummary:
            return RunSummary((), (), ())

    monkeypatch.setattr("graph_sym_perf.experiment.lifecycle.ShardSupervisor", _Supervisor)
    spec = load_experiment_spec("config/experiment.yaml")

    ExperimentLifecycle(tmp_path).run(spec, lambda _task: {"accepted_pairs": 0})

    assert captured["process_isolation"] is True
    assert captured["heartbeat_seconds"] == 10
    assert captured["stale_after_seconds"] == 90
    assert captured["max_parallel_generation_systems"] == 3
    assert captured["max_parallel_measurement_systems"] == 1
    assert captured["max_parallel_shards_per_system"] == 1


def test_formal_lifecycle_attempt_guard_rejects_source_revision_drift(monkeypatch, tmp_path) -> None:
    captured = {}

    class _Supervisor:
        def __init__(self, _run_dir, **kwargs) -> None:
            captured.update(kwargs)

        def run(self, plan, _worker) -> RunSummary:
            captured["plan"] = plan
            return RunSummary((), (), ())

    monkeypatch.setattr("graph_sym_perf.experiment.lifecycle.ShardSupervisor", _Supervisor)
    spec = load_experiment_spec("config/experiment.yaml")
    ExperimentLifecycle(tmp_path).run(spec, lambda _task: {"accepted_pairs": 0})

    guard = captured["attempt_guard"]
    monkeypatch.setattr("graph_sym_perf.experiment.lifecycle._git_output", lambda *_args: "changed")

    with pytest.raises(ManifestMismatchError, match="source provenance changed"):
        guard()


def test_formal_lifecycle_attempt_guard_rehashes_changed_gdsmith_artifact(monkeypatch, tmp_path) -> None:
    captured = {}

    class _Supervisor:
        def __init__(self, _run_dir, **kwargs) -> None:
            captured.update(kwargs)

        def run(self, _plan, _worker) -> RunSummary:
            return RunSummary((), (), ())

    monkeypatch.setattr("graph_sym_perf.experiment.lifecycle.ShardSupervisor", _Supervisor)
    spec = load_experiment_spec("config/experiment.yaml")
    ExperimentLifecycle(tmp_path).run(spec, lambda _task: {"accepted_pairs": 0})

    guard = captured["attempt_guard"]
    monkeypatch.setattr(guard, "_artifact_signature", lambda: ("changed",))
    monkeypatch.setattr(
        "graph_sym_perf.experiment.lifecycle.check_gdsmith_tooling",
        lambda *_args, **_kwargs: SimpleNamespace(
            jar_sha256="sha256:changed",
            build_manifest_sha256="sha256:changed",
        ),
    )

    with pytest.raises(ManifestMismatchError, match="source provenance changed"):
        guard()


def test_lifecycle_binds_the_immutable_run_directory_to_a_shard_worker(tmp_path) -> None:
    class _Worker:
        bound_run_dir = None

        def bind_run_dir(self, run_dir) -> None:
            self.bound_run_dir = run_dir

        def __call__(self, task):
            return {"accepted_pairs": task.accepted_query_pairs}

    worker = _Worker()
    result = ExperimentLifecycle(tmp_path, process_isolation=False).run(
        load_experiment_spec("config/experiment.yaml"),
        worker,
    )

    assert worker.bound_run_dir == result.run_dir


def test_lifecycle_resume_reads_existing_terminal_checkpoints_without_rerunning_workers(tmp_path) -> None:
    lifecycle = ExperimentLifecycle(tmp_path, process_isolation=False)
    spec = load_experiment_spec("config/experiment.yaml")
    original = lifecycle.run(spec, _CompleteLifecycleWorker(), run_id="resume-me")

    def must_not_run(_task):
        raise AssertionError("completed shards must not rerun during resume")

    resumed = lifecycle.resume(spec, must_not_run, run_id=original.run_id)
    status = lifecycle.status(original.run_id)

    assert len(resumed.summary.completed) == 112
    assert resumed.summary.failed == ()
    assert status.total == 112
    assert status.completed == 112
    assert status.active == 0
    assert status.invalid == 0


def test_lifecycle_resume_rejects_tampered_resolved_manifest(tmp_path) -> None:
    lifecycle = ExperimentLifecycle(tmp_path, process_isolation=False)
    spec = load_experiment_spec("config/experiment.yaml")
    original = lifecycle.run(spec, lambda task: {"accepted_pairs": task.accepted_query_pairs}, run_id="tampered")
    manifest_path = original.run_dir / "resolved_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["spec_name"] = "changed-after-run"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable manifest"):
        lifecycle.resume(spec, lambda _task: {"accepted_pairs": 0}, run_id=original.run_id)


@pytest.mark.parametrize("run_id", ("../escape", "/tmp/escape", "nested/escape", ".."))
def test_lifecycle_rejects_run_ids_that_escape_the_output_root(tmp_path, run_id) -> None:
    lifecycle = ExperimentLifecycle(tmp_path)
    spec = load_experiment_spec("config/experiment.yaml")

    with pytest.raises(ValueError, match="run id"):
        lifecycle.run(spec, object(), run_id=run_id)

    with pytest.raises(ValueError, match="run id"):
        lifecycle.resume(spec, object(), run_id=run_id)

    with pytest.raises(ValueError, match="run id"):
        lifecycle.status(run_id)
