from __future__ import annotations

import hashlib
from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.reducer.models import ReductionReport, ReductionStatus, reduction_pair_hash
from graph_sym_perf.runtime.models import RuntimeVerdict
from graph_sym_perf.validation.evidence import issue_validation_report_is_complete
from graph_sym_perf.validation.models import (
    DuplicateClearance,
    IssueValidationReport,
    PipelineStage,
    SafetyCertificate,
    SubmitReadyIssue,
    _PROMOTION_TOKEN,
    safety_certificate_hash,
)
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


def promote(
    report: IssueValidationReport,
    *,
    reduction_report: ReductionReport,
    certificate: SafetyCertificate | None,
    duplicate_clearance: DuplicateClearance,
) -> SubmitReadyIssue | None:
    if not issue_validation_report_is_complete(report) or not _certificate_complete(certificate):
        return None
    if not _reduction_gate_complete(reduction_report, report, certificate) or not _direction_preserved(report, reduction_report):
        return None
    issue_id = deterministic_issue_id(report, reduction_report)
    if not duplicate_clearance.verify() or duplicate_clearance.issue_id != issue_id:
        return None
    return SubmitReadyIssue._from_promotion(
        report,
        reduction_report,
        certificate,
        duplicate_clearance,
        issue_id=issue_id,
        token=_PROMOTION_TOKEN,
    )


def submit_ready_issue_is_valid(issue: SubmitReadyIssue) -> bool:
    return (
        isinstance(issue, SubmitReadyIssue)
        and issue.stage is PipelineStage.SUBMIT_READY
        and issue.issue_id == deterministic_issue_id(issue.validation, issue.reduction)
        and issue.duplicate_clearance.issue_id == issue.issue_id
        and issue_validation_report_is_complete(issue.validation)
        and _reduction_gate_complete(issue.reduction, issue.validation, issue.certificate)
        and _direction_preserved(issue.validation, issue.reduction)
        and _certificate_complete(issue.certificate)
        and issue.duplicate_clearance.verify()
    )


def _reduction_gate_complete(
    reduction: ReductionReport,
    original_validation: IssueValidationReport,
    certificate: SafetyCertificate,
) -> bool:
    evidence = reduction.final_evidence
    final_validation = reduction.final_validation
    original_a_hash = _query_hash(reduction.original_pair.query_a)
    original_b_hash = _query_hash(reduction.original_pair.query_b)
    reduced_a_hash = _query_hash(reduction.reduced_pair.query_a)
    reduced_b_hash = _query_hash(reduction.reduced_pair.query_b)
    certificate_hash = safety_certificate_hash(certificate)
    pair_changed = (original_a_hash, original_b_hash) != (reduced_a_hash, reduced_b_hash)
    return (
        reduction.status is ReductionStatus.REDUCED_VALID
        and reduction.stop_reason == "exhausted_and_validated"
        and reduction.minimality_proof is not None
        and reduction.minimality_proof.verify(reduction.reduced_pair)
        and isinstance(final_validation, IssueValidationReport)
        and issue_validation_report_is_complete(final_validation)
        and original_validation.query_a_hash == original_a_hash
        and original_validation.query_b_hash == original_b_hash
        and evidence.query_a_hash == reduced_a_hash
        and evidence.query_b_hash == reduced_b_hash
        and final_validation.query_a_hash == reduced_a_hash
        and final_validation.query_b_hash == reduced_b_hash
        and final_validation.candidate_hash == reduction.reduced_candidate_hash
        and bool(reduction.reduced_candidate_hash)
        and pair_changed
        and final_validation.candidate_hash != original_validation.candidate_hash
        and final_validation.policy_hash == reduction.validation_policy_hash
        and original_validation.policy_hash == reduction.validation_policy_hash
        and final_validation.certificate_hash == reduction.certificate_hash
        and original_validation.certificate_hash == reduction.certificate_hash
        and reduction.certificate_hash == certificate_hash
        and final_validation.rule_id == certificate.rule_id == original_validation.rule_id
        and final_validation.proof_version == certificate.proof_id == original_validation.proof_version
        and evidence.verdict is RuntimeVerdict.PASS
        and len(evidence.paired_samples) == 40
        and sum(sample.order == "AB" for sample in evidence.paired_samples) == 20
        and sum(sample.order == "BA" for sample in evidence.paired_samples) == 20
        and all(
            sample.a_ms is not None
            and sample.b_ms is not None
            and not sample.a_timeout
            and not sample.b_timeout
            for sample in evidence.paired_samples
        )
        and evidence.median_ratio is not None
        and evidence.ci_ratio_low is not None
        and evidence.median_delta_ms is not None
        and evidence.ci_delta_low_ms is not None
        and evidence.median_ratio == final_validation.validated_ratio
        and evidence.median_delta_ms == final_validation.pooled_delta_ms
        and evidence.ci_ratio_low == final_validation.pooled_ratio_ci_low
        and evidence.ci_delta_low_ms == final_validation.pooled_delta_ci_low_ms
        and evidence.slow_query in {"a", "b"}
        and evidence.graph_snapshot_hash == reduction.reduced_pair.graph_snapshot_hash
        and evidence.dbms_fingerprint == reduction.reduced_pair.dbms_fingerprint
        and evidence.measurement_source == "ordinary_execute"
        and evidence.measurement_protocol == "ordinary-execute-40-pair-combined-v1"
        and all(
            session.graph_fingerprint == reduction.reduced_pair.graph_snapshot_hash
            and session.dbms_fingerprint == reduction.reduced_pair.dbms_fingerprint
            for session in final_validation.sessions
        )
        and final_validation.sessions[0].evidence.slow_query == evidence.slow_query
    )


def _certificate_complete(certificate: SafetyCertificate | None) -> bool:
    if certificate is None or certificate.tier.lower() != "core":
        return False
    try:
        rule = RuleRegistry.default().get(certificate.rule_id)
    except KeyError:
        return False
    return bool(
        rule.tier is SafetyTier.CORE
        and rule.submit_eligible
        and certificate.submit_eligible
        and certificate.proof_id == rule.proof_id
        and certificate.proof_version == rule.proof_version
        and certificate.site_class in rule.allowed_site_classes
    )


def _direction_preserved(report: IssueValidationReport, reduction: ReductionReport) -> bool:
    return bool(
        report.sessions
        and reduction.final_evidence.slow_query == report.sessions[0].evidence.slow_query
    )


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def deterministic_issue_id(
    report: IssueValidationReport,
    reduction: ReductionReport,
) -> str:
    payload = {
        "dbms_fingerprint": report.manifest.pair.dbms_fingerprint,
        "dbms_version": report.manifest.dbms_version,
        "original_candidate_hash": report.candidate_hash,
        "reduced_pair_hash": reduction_pair_hash(reduction.reduced_pair),
        "direction": reduction.final_evidence.slow_query,
    }
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()
