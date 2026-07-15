from types import SimpleNamespace
import json

import pytest

from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.artifacts.store import ContentAddressedStore
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.candidate_pipeline import (
    ProductionCandidatePipeline,
    _ReducerNotApplicable,
    _RuntimeScreen,
    _candidate_record,
    _reduction_summary,
)
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.reducer import ReductionStatus
from graph_sym_perf.core import ExecutionResult
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import SafetyCertificate as TransformCertificate
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import CandidateBundle, PipelineStage, SafetyCertificate
from tests.unit.validation.test_promotion import _reduction, _report


def _candidate() -> CandidateBundle:
    rule = RuleRegistry.default().get("constant_true_disjunction")
    source_certificate = TransformCertificate(
        rule_id=rule.rule_id,
        ast_path="clauses[0].where",
        variables_in_scope=("n",),
        expression_type="boolean",
        nullability="three_valued",
        clause_context="where",
        preconditions=rule.purity_requirements,
        proof_id=rule.proof_id,
        capability_hash="c" * 64,
    )
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Node {id: 1})",))
    pair = EquivalentQueryPair(
        "MATCH (n) WHERE n.id = 1 RETURN n.id",
        "MATCH (n) WHERE (n.id = 1) AND ((n.id = 1) OR true) RETURN n.id",
        {},
        snapshot.content_hash,
        "a" * 64,
        rule.rule_id,
        source_certificate,
    )
    return CandidateBundle.create(
        pair,
        graph_snapshot=snapshot,
        dbms_version="dbms/1",
        resolved_config={"target": "neo4j"},
        discovery_slow_query="b",
        discovery_ratio=5.0,
        certificate=source_certificate,
    )


def test_failed_validation_is_terminal_and_never_enters_reducer(monkeypatch, tmp_path) -> None:
    reference = ArtifactReference("0" * 64, "objects/00/" + "0" * 64, 0, "application/json")
    persisted = []

    class _Validator:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, candidate, _runner):
            return SimpleNamespace(
                stage=PipelineStage.REJECTED,
                reason_codes=("ratio_below_threshold",),
                validation_protocol="independent-two-session-v1",
            )

    monkeypatch.setattr("graph_sym_perf.experiment.candidate_pipeline.IssueValidator", _Validator)
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline.ArtifactChainWriter.persist_validation",
        lambda _writer, report, **_kwargs: persisted.append(report) or SimpleNamespace(reference=reference),
    )
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline._validation_environment",
        lambda *_args, **_kwargs: {"execution_mode": "test"},
    )
    pipeline = ProductionCandidatePipeline(
        load_experiment_spec("config/experiment.yaml"),
        environment={"GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": "sha256:" + "f" * 64},
        run_dir=tmp_path,
    )
    store = ContentAddressedStore(tmp_path / "artifacts", budget_bytes=1024 * 1024)
    outcome = pipeline.process(
        _candidate(),
        task=ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60),
        artifact_store=store,
        candidate_ref=reference,
        driver_version="driver/1",
        graph_snapshot_ref={},
        attempt_dir=tmp_path,
    )

    assert outcome["validation"]["stage"] == "rejected"
    assert persisted and persisted[0].reason_codes == ("ratio_below_threshold",)
    assert outcome["validation"]["validation_ref"] == {
        "content_hash": reference.content_hash,
        "relative_path": reference.relative_path,
        "size_bytes": reference.size_bytes,
        "media_type": reference.media_type,
        "schema_version": reference.schema_version,
    }
    assert outcome["reduction"] == {
        "candidate_hash": outcome["candidate_hash"],
        "reason": "validation_not_passed",
        "status": "not_applicable",
    }


def test_reducer_screen_rejects_a_runtime_direction_reversal() -> None:
    candidate = _candidate()

    class _ReversedAdapter:
        def execute(self, query, _parameters, **_kwargs):
            return ExecutionResult(
                system=TargetSystem.NEO4J,
                query_kind="runtime",
                rows=(),
                elapsed_ms=5.0 if query == candidate.pair.query_a else 1.0,
                summary={},
            )

    evidence = _RuntimeScreen(_ReversedAdapter(), candidate).evaluate(candidate.pair)

    assert evidence.slow_query == "b"
    assert "direction_mismatch" in evidence.reason_codes


def test_reduction_summary_retains_both_validation_hashes_and_references() -> None:
    reduction = _reduction()
    original_ref = ArtifactReference(
        "1" * 64,
        "objects/11/" + "1" * 64,
        100,
        "application/json",
    )
    reduced_ref = ArtifactReference(
        "2" * 64,
        "objects/22/" + "2" * 64,
        100,
        "application/json",
    )

    summary = _reduction_summary(
        reduction,
        original_candidate_hash="a" * 64,
        original_validation_ref=original_ref,
        reduced_validation_ref=reduced_ref,
    )

    assert summary["original_validation_hash"] == reduction.original_validation_hash
    assert summary["reduced_validation_hash"] == reduction.reduced_validation_hash
    assert summary["original_validation_ref"]["content_hash"] == "1" * 64
    assert summary["reduced_validation_ref"]["content_hash"] == "2" * 64


def test_invalidated_reduction_with_final_validation_persists_canonical_reference(
    tmp_path,
) -> None:
    original_validation = _report()
    manifest = original_validation.manifest
    original_candidate = CandidateBundle.create(
        manifest.pair,
        graph_snapshot=manifest.graph_snapshot,
        dbms_version=manifest.dbms_version,
        resolved_config=manifest.resolved_config,
        discovery_slow_query=manifest.discovery_slow_query,
        discovery_ratio=manifest.discovery_ratio,
        validation_policy=original_validation.validation_policy,
        certificate=manifest.certificate,
    )
    reduction = _reduction(status=ReductionStatus.REDUCTION_INVALIDATED)
    pipeline = ProductionCandidatePipeline(
        load_experiment_spec("config/experiment.yaml"),
        environment={"GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": "sha256:" + "f" * 64},
        run_dir=tmp_path,
    )
    store = ContentAddressedStore(tmp_path / "artifacts", budget_bytes=1024 * 1024)
    graph_ref = store.put("graph", b"CREATE (n:Person {id: 1})")
    original_ref = store.put_json("original-validation-placeholder", {"kind": "original"})
    outcome = pipeline._materialize_issue(
        original_candidate,
        original_validation,
        reduction,
        task=ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60),
        store=store,
        original_validation_ref=original_ref,
        graph_snapshot_ref=graph_ref.__dict__,
        environment={
            "dbms_version": reduction.final_validation.manifest.dbms_version,
            "driver_version": "driver/1",
            "execution_mode": "container_image",
            "execution_artifact_digest": "sha256:" + "f" * 64,
            "cpu_architecture": "arm64",
            "config_hash": reduction.final_validation.manifest.content_hashes[
                "resolved_config"
            ],
        },
    )

    assert outcome["promotion"] == "not_applicable"
    assert outcome["original_validation_hash"] == reduction.original_validation_hash
    assert outcome["reduced_validation_hash"] == reduction.reduced_validation_hash
    assert outcome["original_validation_ref"]["content_hash"] == original_ref.content_hash
    assert outcome["reduced_validation_ref"]["content_hash"]
    summary = _reduction_summary(
        reduction,
        original_candidate_hash=original_candidate.candidate_hash,
        original_validation_ref=outcome["original_validation_ref"],
        reduced_validation_ref=outcome["reduced_validation_ref"],
    )
    assert summary["reduced_validation_hash"] == reduction.reduced_validation_hash


@pytest.mark.parametrize(
    "status",
    (
        ReductionStatus.REDUCTION_INVALIDATED,
        ReductionStatus.REDUCTION_PARTIAL,
    ),
)
def test_nonvalid_final_validation_enters_candidate_outcome_and_reduction_summary(
    monkeypatch,
    tmp_path,
    status,
) -> None:
    original_validation = _report()
    manifest = original_validation.manifest
    original_candidate = CandidateBundle.create(
        manifest.pair,
        graph_snapshot=manifest.graph_snapshot,
        dbms_version=manifest.dbms_version,
        resolved_config=manifest.resolved_config,
        discovery_slow_query=manifest.discovery_slow_query,
        discovery_ratio=manifest.discovery_ratio,
        validation_policy=original_validation.validation_policy,
        certificate=manifest.certificate,
    )
    reduction = _reduction(status=status)

    class _Validator:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, _candidate_value, _runner):
            return original_validation

    monkeypatch.setattr("graph_sym_perf.experiment.candidate_pipeline.IssueValidator", _Validator)
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline._validation_environment",
        lambda *_args, **_kwargs: {
            "dbms_version": manifest.dbms_version,
            "driver_version": "driver/1",
            "execution_mode": "container_image",
            "execution_artifact_digest": "sha256:" + "f" * 64,
            "cpu_architecture": "arm64",
            "config_hash": manifest.content_hashes["resolved_config"],
        },
    )
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline._rule_witness",
        lambda _candidate_value: SimpleNamespace(),
    )
    monkeypatch.setattr(
        ProductionCandidatePipeline,
        "_reduce",
        lambda *_args, **_kwargs: reduction,
    )
    pipeline = ProductionCandidatePipeline(
        load_experiment_spec("config/experiment.yaml"),
        environment={"GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": "sha256:" + "f" * 64},
        run_dir=tmp_path,
    )
    store = ContentAddressedStore(tmp_path / "artifacts", budget_bytes=1024 * 1024)
    graph_ref = store.put("graph", b"CREATE (n:Person {id: 1})")
    candidate_ref = store.put_json(
        "original-candidate",
        _candidate_record(
            original_candidate,
            original_validation.sessions[0].evidence,
            graph_snapshot_ref=graph_ref.__dict__,
        ),
    )
    outcome = pipeline.process(
        original_candidate,
        task=ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60),
        artifact_store=store,
        candidate_ref=candidate_ref,
        driver_version="driver/1",
        graph_snapshot_ref=graph_ref.__dict__,
        attempt_dir=tmp_path,
    )

    reduction_outcome = outcome["reduction"]
    assert reduction_outcome["status"] == status.value
    assert reduction_outcome["promotion"] == "not_applicable"
    assert reduction_outcome["original_validation_hash"] == reduction.original_validation_hash
    assert reduction_outcome["reduced_validation_hash"] == reduction.reduced_validation_hash
    assert reduction_outcome["original_validation_ref"]["content_hash"]
    assert reduction_outcome["reduced_validation_ref"]["content_hash"]
    summary = json.loads(store.read(reduction_outcome["reduction_ref"]))
    assert summary["original_validation_hash"] == reduction.original_validation_hash
    assert summary["reduced_validation_hash"] == reduction.reduced_validation_hash
    assert summary["original_validation_ref"] == reduction_outcome["original_validation_ref"]
    assert summary["reduced_validation_ref"] == reduction_outcome["reduced_validation_ref"]


def test_committed_owner_resume_reuses_terminal_validation_refs_in_outcome(
    monkeypatch,
    tmp_path,
) -> None:
    fresh_ref = ArtifactReference(
        "a" * 64, "objects/aa/" + "a" * 64, 100, "application/json"
    )
    original_ref = ArtifactReference(
        "1" * 64, "objects/11/" + "1" * 64, 100, "application/json"
    )
    reduced_ref = ArtifactReference(
        "2" * 64, "objects/22/" + "2" * 64, 100, "application/json"
    )
    reduction = _reduction()

    class _Validator:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, _candidate_value, _runner):
            return _report()

    monkeypatch.setattr("graph_sym_perf.experiment.candidate_pipeline.IssueValidator", _Validator)
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline.ArtifactChainWriter.persist_validation",
        lambda *_args, **_kwargs: SimpleNamespace(reference=fresh_ref),
    )
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline._validation_environment",
        lambda *_args, **_kwargs: {"execution_mode": "test"},
    )
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline._rule_witness",
        lambda _candidate_value: SimpleNamespace(),
    )
    monkeypatch.setattr(
        ProductionCandidatePipeline,
        "_reduce",
        lambda *_args, **_kwargs: reduction,
    )
    monkeypatch.setattr(
        ProductionCandidatePipeline,
        "_materialize_issue",
        lambda *_args, **_kwargs: {
            "promotion": "submit_ready",
            "duplicate_status": "owner_already_committed",
            "resumed": True,
            "issue_id": "issue-1",
            "issue_index": "issues/index/issue-1.json",
            "original_validation_hash": reduction.original_validation_hash,
            "reduced_validation_hash": reduction.reduced_validation_hash,
            "original_validation_ref": original_ref.__dict__,
            "reduced_validation_ref": reduced_ref.__dict__,
        },
    )
    pipeline = ProductionCandidatePipeline(
        load_experiment_spec("config/experiment.yaml"),
        environment={"GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": "sha256:" + "f" * 64},
        run_dir=tmp_path,
    )
    store = ContentAddressedStore(tmp_path / "artifacts", budget_bytes=1024 * 1024)
    outcome = pipeline.process(
        _candidate(),
        task=ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60),
        artifact_store=store,
        candidate_ref=fresh_ref,
        driver_version="driver/1",
        graph_snapshot_ref={},
        attempt_dir=tmp_path,
    )

    assert outcome["validation"]["validation_ref"] == original_ref.__dict__
    assert outcome["reduction"]["original_validation_ref"] == original_ref.__dict__
    assert outcome["reduction"]["reduced_validation_ref"] == reduced_ref.__dict__
    assert outcome["reduction"]["duplicate_status"] == "owner_already_committed"


def test_validated_candidate_without_safe_dependency_graph_does_not_fail_shard(
    monkeypatch,
    tmp_path,
) -> None:
    class _Validator:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, candidate, _runner):
            return SimpleNamespace(
                stage=PipelineStage.VALIDATION_PASSED,
                reason_codes=(),
                validation_protocol="independent-two-session-v1",
            )

    reference = ArtifactReference("0" * 64, "objects/00/" + "0" * 64, 0, "application/json")
    monkeypatch.setattr("graph_sym_perf.experiment.candidate_pipeline.IssueValidator", _Validator)
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline.ArtifactChainWriter.persist_validation",
        lambda *_args, **_kwargs: SimpleNamespace(reference=reference),
    )
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline._validation_environment",
        lambda *_args, **_kwargs: {"execution_mode": "test"},
    )
    monkeypatch.setattr(
        "graph_sym_perf.experiment.candidate_pipeline._rule_witness",
        lambda _candidate: (_ for _ in ()).throw(_ReducerNotApplicable("dependency_graph_unavailable")),
    )
    pipeline = ProductionCandidatePipeline(
        load_experiment_spec("config/experiment.yaml"),
        environment={"GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": "sha256:" + "f" * 64},
        run_dir=tmp_path,
    )
    store = ContentAddressedStore(tmp_path / "artifacts", budget_bytes=1024 * 1024)

    outcome = pipeline.process(
        _candidate(),
        task=ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60),
        artifact_store=store,
        candidate_ref=reference,
        driver_version="driver/1",
        graph_snapshot_ref={},
        attempt_dir=tmp_path,
    )

    assert outcome["validation"]["stage"] == "validation_passed"
    assert outcome["reduction"]["status"] == "not_applicable"
    assert outcome["reduction"]["reason"] == "dependency_graph_unavailable"
    assert outcome["reduction"]["promotion"] == "blocked_reducer_not_applicable"
