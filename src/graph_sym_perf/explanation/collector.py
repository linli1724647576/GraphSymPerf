from __future__ import annotations

import hashlib
from typing import Protocol

from graph_sym_perf.adapters.base import DBMSFingerprint
from graph_sym_perf.core import ExecutionResult
from graph_sym_perf.explanation.models import ExplanationEvidence
from graph_sym_perf.plan import diff_normalized_plans, profile_record_from_execution
from graph_sym_perf.validation.models import PipelineStage, SubmitReadyIssue
from graph_sym_perf.validation.promotion import submit_ready_issue_is_valid


class ExplanationAdapter(Protocol):
    def profile(
        self,
        query: str,
        parameters: object,
        query_kind: str,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult: ...

    def dbms_fingerprint(self) -> DBMSFingerprint: ...


def collect_explanation(issue: SubmitReadyIssue, adapter: ExplanationAdapter) -> ExplanationEvidence:
    if not isinstance(issue, SubmitReadyIssue) or not submit_ready_issue_is_valid(issue):
        raise TypeError("explanation collection requires a valid SubmitReadyIssue")
    if issue.stage is not PipelineStage.SUBMIT_READY:
        raise ValueError("explanation is available only after submit readiness")
    pair = issue.reduction.reduced_pair
    before = adapter.dbms_fingerprint()
    if not before.available or before.value != pair.dbms_fingerprint:
        raise ValueError("explanation DBMS fingerprint does not match the validated reduced pair")
    timeout = issue.validation.validation_policy.query_timeout_seconds
    reasons: list[str] = []
    records = []
    for label, query in (("a", pair.query_a), ("b", pair.query_b)):
        try:
            result = adapter.profile(
                query,
                pair.parameters,
                f"explanation_{label}",
                max_rows=1,
                timeout_seconds=timeout,
            )
            record = profile_record_from_execution(result)
            if not record.available:
                reasons.append(f"profile_{label}_unavailable")
            records.append(record)
        except Exception as exc:
            reasons.append(f"profile_{label}_error:{type(exc).__name__}")
            records.append(None)
    after = adapter.dbms_fingerprint()
    if not after.available or after.value != before.value:
        reasons.append("dbms_fingerprint_changed_during_explanation")
    first, second = records
    plan_a = first.plan if first is not None else None
    plan_b = second.plan if second is not None else None
    if after.value != before.value:
        plan_a = plan_b = None
    difference = diff_normalized_plans(plan_a, plan_b) if plan_a is not None or plan_b is not None else None
    return ExplanationEvidence(
        issue_id=issue.issue_id,
        issue_stage=issue.stage,
        query_a_hash=hashlib.sha256(pair.query_a.encode("utf-8")).hexdigest(),
        query_b_hash=hashlib.sha256(pair.query_b.encode("utf-8")).hexdigest(),
        dbms_fingerprint_before=before.value or "",
        dbms_fingerprint_after=after.value or "",
        plan_a=plan_a,
        plan_b=plan_b,
        profile_metrics_a=first.metrics if first is not None else {},
        profile_metrics_b=second.metrics if second is not None else {},
        structural_changes=difference.changes if difference is not None else (),
        reason_codes=tuple(reasons),
    )


__all__ = ["ExplanationAdapter", "collect_explanation"]
