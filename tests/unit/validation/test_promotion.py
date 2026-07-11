import os
import hashlib
import copy
from dataclasses import asdict, replace

import pytest

from graph_sym_perf.artifacts.chain import ArtifactChainWriter
from graph_sym_perf.artifacts.policy import ArtifactLevel
from graph_sym_perf.artifacts.schema import ArtifactRecord
from graph_sym_perf.artifacts.store import ContentAddressedStore, load_artifact_record
from graph_sym_perf.reducer.models import (
    MinimalityProof,
    MinimalityUniverse,
    ReductionReport,
    ReductionStatus,
    reduction_pair_hash,
)
from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.runtime.models import EquivalentQueryPair, PairedSample, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    CandidateBundle,
    DuplicateClearance,
    EquivalenceEvidence,
    IssueValidationReport,
    PipelineStage,
    SafetyCertificate,
    SessionLifecycle,
    SubmitReadyIssue,
    ValidationSession,
    ValidationPolicy,
    FrozenMapping,
    safety_certificate_hash,
    _DUPLICATE_CLEARANCE_TOKEN,
    _VALIDATION_REPORT_TOKEN,
)
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.promotion import _certificate_complete, promote
from graph_sym_perf.validation.evidence import combined_runtime_evidence


def _original_pair() -> EquivalentQueryPair:
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    return EquivalentQueryPair(
        "MATCH (n) WHERE n.id >= 0 RETURN n.id ORDER BY n.id",
        "MATCH (n) WHERE true AND n.id >= 0 RETURN n.id ORDER BY n.id",
        {},
        snapshot.content_hash,
        "c" * 64,
    )


def _reduced_pair() -> EquivalentQueryPair:
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    return EquivalentQueryPair(
        "MATCH (n) RETURN n.id ORDER BY n.id",
        "MATCH (n) WHERE true RETURN n.id ORDER BY n.id",
        {},
        snapshot.content_hash,
        "c" * 64,
    )


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _evidence(
    *,
    pair: EquivalentQueryPair | None = None,
    verdict: RuntimeVerdict = RuntimeVerdict.PASS,
    slow_query: str = "b",
) -> RuntimeEvidence:
    pair = pair or _original_pair()
    samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 40.0) for index in range(20))
    return RuntimeEvidence(
        verdict,
        _query_hash(pair.query_a),
        _query_hash(pair.query_b),
        samples,
        4.0,
        30.0,
        4.0,
        4.0,
        1.0,
        0.01,
        (),
        slow_query=slow_query,
        graph_snapshot_hash=pair.graph_snapshot_hash,
        dbms_fingerprint=pair.dbms_fingerprint,
        measurement_source="ordinary_execute",
        measurement_protocol="ordinary-execute-20-pair-v1",
    )


def _lifecycle(index: int) -> SessionLifecycle:
    return SessionLifecycle(True, True, True, True, True, True, True, True, f"connection-{index}")


def _control() -> RuntimeEvidence:
    samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 10.0) for index in range(20))
    return RuntimeEvidence(RuntimeVerdict.PASS, "a", "a", samples, 1.0, 0.0, 1.0, 1.0, 0.5, 0.0, ())


def _report(
    *,
    pair: EquivalentQueryPair | None = None,
    stage: PipelineStage = PipelineStage.VALIDATION_PASSED,
    metadata=None,
) -> IssueValidationReport:
    pair = pair or _original_pair()
    sessions = tuple(
        ValidationSession(
            f"session-{index}",
            os.getpid() + index + 100,
            pair.graph_snapshot_hash,
            pair.dbms_fingerprint,
            _evidence(pair=pair),
            (_control(), _control()),
            lifecycle=_lifecycle(index),
        )
        for index in (1, 2)
    )
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    candidate = CandidateBundle.create(
        pair,
        graph_snapshot=snapshot,
        dbms_version="dbms-version",
        resolved_config={"runtime": {"validation_pairs": 20}},
        discovery_slow_query="b",
        discovery_ratio=8.0,
        certificate=_certificate(),
        validation_policy=ValidationPolicy(),
    )
    return IssueValidationReport._from_validator(
        stage=stage,
        equivalence=EquivalenceEvidence(True, True, ()),
        sessions=sessions,
        discovery_ratio=8.0,
        validated_ratio=4.0,
        magnitude_stability="stable",
        reason_codes=(),
        candidate=candidate,
        metadata={} if metadata is None else metadata,
        token=_VALIDATION_REPORT_TOKEN,
    )


def _reduction(*, status: ReductionStatus = ReductionStatus.REDUCED_VALID, verdict: RuntimeVerdict = RuntimeVerdict.PASS) -> ReductionReport:
    original_pair = _original_pair()
    reduced_pair = _reduced_pair()
    final_validation = _report(pair=reduced_pair)
    universe = MinimalityUniverse.recompute(reduced_pair)
    final_evidence = combined_runtime_evidence(final_validation)
    assert final_evidence is not None
    if verdict is not RuntimeVerdict.PASS:
        final_evidence = replace(final_evidence, verdict=verdict)
    return ReductionReport(
        status,
        original_pair,
        reduced_pair,
        (),
        (),
        final_evidence,
        minimality_proof=MinimalityProof.from_results(
            reduced_pair,
            tuple(
                (deletion.canonical_id, "symptom_not_preserved", f"{index:x}" * 64)
                for index, deletion in enumerate(universe.deletions, start=1)
            ),
        ),
        final_validation=final_validation,
        reduced_candidate_hash=final_validation.candidate_hash,
        validation_policy_hash=final_validation.policy_hash,
        certificate_hash=safety_certificate_hash(_certificate()),
        stop_reason="exhausted_and_validated",
    )


def _tamper_report(report: IssueValidationReport, **changes) -> IssueValidationReport:
    forged = copy.copy(report)
    for name, value in changes.items():
        object.__setattr__(forged, name, value)
    return forged


def _tamper_manifest_hash(report: IssueValidationReport, name: str, value: str) -> IssueValidationReport:
    hashes = dict(report.manifest.content_hashes)
    hashes[name] = value
    manifest = replace(report.manifest, content_hashes=FrozenMapping.from_mapping(hashes))
    return _tamper_report(report, manifest=manifest)


def _certificate(**changes) -> SafetyCertificate:
    values = {
        "rule_id": "pattern_order_symmetry",
        "tier": "core",
        "proof_version": "graph_pattern_product_commutativity_v1",
        "site_class": "match_pattern_component",
    }
    values.update(changes)
    return SafetyCertificate(**values)


def test_control_and_experimental_rules_cannot_masquerade_as_submit_eligible_core() -> None:
    assert not _certificate_complete(
        SafetyCertificate(
            "edge_notation_symmetry",
            "core",
            "edge_parser_normalization_v1",
            "match_pattern_component",
        )
    )
    assert not _certificate_complete(
        SafetyCertificate(
            "pattern_decomposition_symmetry",
            "core",
            "relationship_uniqueness_pending",
            "match_pattern_component",
        )
    )


def _expected_issue_id(
    report: IssueValidationReport | None = None,
    reduction: ReductionReport | None = None,
) -> str:
    report = report or _report()
    reduction = reduction or _reduction()
    payload = {
        "dbms_fingerprint": report.manifest.pair.dbms_fingerprint,
        "dbms_version": report.manifest.dbms_version,
        "original_candidate_hash": report.candidate_hash,
        "reduced_pair_hash": reduction_pair_hash(reduction.reduced_pair),
        "direction": reduction.final_evidence.slow_query,
    }
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


def _duplicate_clearance(
    *,
    possible: bool = False,
    resolved: bool = False,
    issue_id: str | None = None,
) -> DuplicateClearance:
    issue_id = issue_id or _expected_issue_id()
    members = (issue_id, "possible-other") if possible else (issue_id,)
    return DuplicateClearance._from_registry(
        issue_id=issue_id,
        possible_duplicate=possible,
        cluster_members=members,
        cluster_version_hash="f" * 64,
        evidence_url="https://example.test/dedup" if resolved else None,
        token=_DUPLICATE_CLEARANCE_TOKEN,
    )


def test_profile_or_plan_like_metadata_cannot_promote_without_validation() -> None:
    report = _report(
        stage=PipelineStage.SCREENED,
        metadata={"profile_supported": True, "pmc_novelty": 1000, "plan_changed": True},
    )

    assert promote(report, reduction_report=_reduction(), certificate=_certificate(), duplicate_clearance=_duplicate_clearance()) is None


def test_only_validation_passed_reduced_core_certificate_becomes_submit_ready() -> None:
    report = _report()
    reduction = _reduction()
    issue = promote(report, reduction_report=reduction, certificate=_certificate(), duplicate_clearance=_duplicate_clearance())

    assert issue is not None
    assert issue.stage is PipelineStage.SUBMIT_READY
    assert issue.developer_status is DeveloperStatus.WAITING_REPLY
    assert issue.reduction.status is ReductionStatus.REDUCED_VALID
    assert issue.issue_id == _expected_issue_id(report, reduction)


def test_typed_submit_ready_issue_persists_a_resolvable_artifact_hash_chain(tmp_path) -> None:
    report = _report()
    reduction = _reduction()
    issue = promote(
        report,
        reduction_report=reduction,
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    )
    assert issue is not None and reduction.final_validation is not None
    store = ContentAddressedStore(tmp_path / "artifacts", budget_bytes=1024 * 1024)

    def candidate_reference(validation_report: IssueValidationReport):
        manifest = validation_report.manifest
        graph_payload = "\n".join(manifest.graph_snapshot.statements).encode("utf-8")
        graph_ref = store.put("graph", graph_payload)
        candidate = ArtifactRecord(
            level=ArtifactLevel.CANDIDATE,
            payload={
                "candidate_hash": validation_report.candidate_hash,
                "content_hashes": dict(manifest.content_hashes),
                "query_a": manifest.pair.query_a,
                "query_b": manifest.pair.query_b,
                "parameters": dict(manifest.pair.parameters),
                "discovery_paired_timing": tuple(
                    asdict(sample) for sample in validation_report.sessions[0].evidence.paired_samples[:5]
                ),
                "graph_snapshot_ref": asdict(graph_ref),
                "graph_snapshot_hash": manifest.graph_snapshot.content_hash,
                "dbms_fingerprint": manifest.pair.dbms_fingerprint,
                "rule_id": manifest.certificate.rule_id,
                "safety_certificate": asdict(manifest.certificate),
                "discovery_median_ratio": manifest.discovery_ratio,
                "discovery_slow_query": manifest.discovery_slow_query,
            },
        )
        return store.put_json("candidate", candidate.to_dict())

    environment = {
        "dbms_version": "dbms-version",
        "driver_version": "driver-version",
        "execution_mode": "container_image",
        "execution_artifact_digest": "sha256:" + "f" * 64,
        "cpu_architecture": "arm64",
        "config_hash": report.manifest.content_hashes["resolved_config"],
    }
    writer = ArtifactChainWriter(store)
    original_validation = writer.persist_validation(
        report,
        candidate_ref=candidate_reference(report),
        environment=environment,
    )
    with pytest.raises(ValueError, match="candidate artifact"):
        writer.persist_validation(
            report,
            candidate_ref=original_validation.reference,
            environment=environment,
        )
    final_validation = writer.persist_validation(
        reduction.final_validation,
        candidate_ref=candidate_reference(reduction.final_validation),
        environment=environment,
    )
    reproducer = store.put("reproducer", b"CREATE (n:Person {id: 1})")

    stored_issue = writer.persist_issue(
        issue,
        original_validation_ref=original_validation.reference,
        final_validation_ref=final_validation.reference,
        reproducer_ref=reproducer,
    )

    assert load_artifact_record(store, stored_issue.reference).payload["issue_hash"] == stored_issue.record.payload["issue_hash"]


def test_unchanged_pair_cannot_promote_as_reduced_valid() -> None:
    pair = _original_pair()
    final_validation = _report(pair=pair)
    universe = MinimalityUniverse.recompute(pair)
    reduction = ReductionReport(
        ReductionStatus.REDUCED_VALID,
        pair,
        pair,
        (),
        (),
        _evidence(pair=pair),
        minimality_proof=MinimalityProof.from_results(
            pair,
            tuple(
                (deletion.canonical_id, "symptom_not_preserved", f"{index:x}" * 64)
                for index, deletion in enumerate(universe.deletions, start=1)
            ),
        ),
        final_validation=final_validation,
        reduced_candidate_hash=final_validation.candidate_hash,
        validation_policy_hash=final_validation.policy_hash,
        certificate_hash=safety_certificate_hash(_certificate()),
    )
    report = _report(pair=pair)

    assert promote(
        report,
        reduction_report=reduction,
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(issue_id=_expected_issue_id(report, reduction)),
    ) is None


def test_promotion_rejects_clearance_for_a_different_deterministic_issue_id() -> None:
    assert promote(
        _report(),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(issue_id="wrong-issue"),
    ) is None


def test_manually_resolved_possible_duplicate_can_promote_with_evidence() -> None:
    issue = promote(
        _report(),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(possible=True, resolved=True),
    )

    assert issue is not None


@pytest.mark.parametrize(
    ("reduction", "certificate", "possible_duplicate"),
    (
        (_reduction(status=ReductionStatus.REDUCTION_PARTIAL), _certificate(), False),
        (_reduction(verdict=RuntimeVerdict.FAIL), _certificate(), False),
        (_reduction(), _certificate(tier="experimental"), False),
        (_reduction(), _certificate(proof_version=""), False),
        (_reduction(), _certificate(), True),
    ),
)
def test_promotion_rejects_every_reduction_certificate_and_dedup_bypass(
    reduction: ReductionReport,
    certificate: SafetyCertificate,
    possible_duplicate: bool,
) -> None:
    assert promote(
        _report(),
        reduction_report=reduction,
        certificate=certificate,
        duplicate_clearance=_duplicate_clearance(possible=possible_duplicate),
    ) is None


def test_submit_ready_issue_cannot_be_constructed_outside_promotion() -> None:
    with pytest.raises(TypeError, match="promote"):
        SubmitReadyIssue(
            PipelineStage.SUBMIT_READY,
            DeveloperStatus.WAITING_REPLY,
            _report(),
            _reduction(),
            _certificate(),
        )


def test_promotion_recomputes_combined_samples_instead_of_trusting_report_summary() -> None:
    report = _report()
    weak_samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 20.0) for index in range(20))
    weak_sessions = tuple(
        replace(session, evidence=replace(session.evidence, paired_samples=weak_samples))
        for session in report.sessions
    )

    assert promote(
        _tamper_report(report, sessions=weak_sessions),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None


def test_promotion_rechecks_negative_control_noise() -> None:
    report = _report()
    noisy_samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 60.0) for index in range(20))
    noisy_control = replace(_control(), paired_samples=noisy_samples, median_delta_ms=50.0)
    noisy_sessions = tuple(
        replace(session, negative_controls=(noisy_control, session.negative_controls[1]))
        for session in report.sessions
    )

    assert promote(
        _tamper_report(report, sessions=noisy_sessions),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None


def test_promotion_rechecks_session_fingerprints_and_protocol_shape() -> None:
    report = _report()
    forged_session = replace(report.sessions[0], graph_fingerprint="other-graph")

    assert promote(
        _tamper_report(report, sessions=(forged_session, report.sessions[1])),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None


def test_reduced_pair_must_preserve_the_validated_slow_query_direction() -> None:
    reduction = _reduction()
    reversed_reduction = replace(reduction, final_evidence=replace(reduction.final_evidence, slow_query="a"))

    assert promote(
        _report(),
        reduction_report=reversed_reduction,
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None


def test_reduction_requires_complete_samples_independent_revalidation_and_minimality() -> None:
    reduction = _reduction()

    assert promote(
        _report(),
        reduction_report=replace(reduction, final_evidence=replace(reduction.final_evidence, paired_samples=())),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None
    assert promote(
        _report(),
        reduction_report=replace(reduction, final_validation=None),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None
    assert promote(
        _report(),
        reduction_report=replace(reduction, minimality_proof=None),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None


@pytest.mark.parametrize(
    "tampered",
    (
        lambda reduction: replace(reduction.final_evidence, query_a_hash="forged"),
        lambda reduction: _tamper_report(
            reduction.final_validation,
            manifest=replace(
                reduction.final_validation.manifest,
                candidate_hash=_report().candidate_hash,
            ),
        ),
        lambda reduction: _tamper_report(
            reduction.final_validation,
            validation_policy=replace(reduction.final_validation.validation_policy, min_ratio=99.0),
        ),
        lambda reduction: _tamper_manifest_hash(reduction.final_validation, "query_b", "forged"),
    ),
)
def test_reduced_evidence_chain_cannot_be_swapped_with_original_or_forged(tampered) -> None:
    reduction = _reduction()
    value = tampered(reduction)
    changed = (
        replace(reduction, final_evidence=value)
        if isinstance(value, RuntimeEvidence)
        else replace(reduction, final_validation=value)
    )

    assert promote(
        _report(),
        reduction_report=changed,
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None


def test_minimality_proof_hash_chain_is_verified_not_boolean_trusted() -> None:
    reduction = _reduction()
    forged = replace(reduction.minimality_proof, chain_hash="0" * 64)

    assert promote(
        _report(),
        reduction_report=replace(reduction, minimality_proof=forged),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is None


def test_promotion_uses_combined_samples_not_single_session_ci_or_oriented_delta_sign() -> None:
    report = _report()
    aggregate_only = replace(
        report.sessions[0],
        evidence=replace(
            report.sessions[0].evidence,
            verdict=RuntimeVerdict.FAIL,
            ci_ratio_low=1.5,
            median_delta_ms=-999.0,
            reason_codes=("ci_lower_below_threshold", "delta_within_noise"),
        ),
    )
    report = _tamper_report(report, sessions=(aggregate_only, report.sessions[1]))

    assert promote(
        report,
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    ) is not None
