from types import SimpleNamespace

from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.artifacts.store import ContentAddressedStore
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.candidate_pipeline import (
    ProductionCandidatePipeline,
    _ReducerNotApplicable,
    _RuntimeScreen,
)
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.core import ExecutionResult
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import SafetyCertificate as TransformCertificate
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import CandidateBundle, PipelineStage, SafetyCertificate


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
        certificate=SafetyCertificate(rule.rule_id, "core", rule.proof_id, "where_boolean"),
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
