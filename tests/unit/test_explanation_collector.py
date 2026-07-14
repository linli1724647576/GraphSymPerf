from dataclasses import fields

import pytest

from graph_sym_perf.adapters.base import DBMSFingerprint
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.explanation.collector import collect_explanation
from graph_sym_perf.explanation.models import ExplanationEvidence
from graph_sym_perf.validation.models import PipelineStage, SubmitReadyIssue
from graph_sym_perf.validation.promotion import promote
from tests.unit.validation.test_promotion import (
    _certificate,
    _duplicate_clearance,
    _family_decision,
    _reduction,
    _report,
)


def _issue() -> SubmitReadyIssue:
    report = _report()
    reduction = _reduction()
    decision = _family_decision(report, reduction)
    issue = promote(
        report,
        reduction_report=reduction,
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(family_decision=decision),
        family_decision=decision,
    )
    assert issue is not None
    return issue


class _Adapter:
    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        self.calls = []

    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint(self.fingerprint)

    def profile(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        self.calls.append((query, parameters, query_kind, kwargs))
        return ExecutionResult(
            TargetSystem.NEO4J,
            query_kind,
            (),
            1.0,
            {
                "profile": {
                    "operator_type": "NodeByLabelScan",
                    "arguments": {"Rows": 3, "DbHits": 4},
                    "children": [],
                }
            },
        )


def test_explanation_requires_valid_submit_ready_issue_and_cannot_change_stage() -> None:
    with pytest.raises(TypeError):
        collect_explanation({}, object())  # type: ignore[arg-type]
    issue = _issue()
    adapter = _Adapter(issue.reduction.reduced_pair.dbms_fingerprint)

    evidence = collect_explanation(issue, adapter)

    assert evidence.issue_stage is PipelineStage.SUBMIT_READY
    assert issue.stage is PipelineStage.SUBMIT_READY
    assert evidence.available
    assert len(adapter.calls) == 2
    assert {call[2] for call in adapter.calls} == {"explanation_a", "explanation_b"}


def test_explanation_refuses_an_unrelated_dbms_before_profile_execution() -> None:
    issue = _issue()
    adapter = _Adapter("other-dbms")

    with pytest.raises(ValueError, match="fingerprint"):
        collect_explanation(issue, adapter)

    assert adapter.calls == []


def test_explanation_evidence_has_no_candidate_scoring_or_promotion_surface() -> None:
    names = {field.name for field in fields(ExplanationEvidence)}
    assert not ({"is_candidate", "reward", "novelty", "promotion", "promote"} & names)


def test_explanation_invalidates_plans_if_dbms_changes_during_collection() -> None:
    issue = _issue()

    class RestartingAdapter(_Adapter):
        def dbms_fingerprint(self) -> DBMSFingerprint:
            return DBMSFingerprint(self.fingerprint if len(self.calls) < 2 else "restarted-dbms")

    adapter = RestartingAdapter(issue.reduction.reduced_pair.dbms_fingerprint)
    evidence = collect_explanation(issue, adapter)

    assert not evidence.available
    assert evidence.plan_a is None and evidence.plan_b is None
    assert "dbms_fingerprint_changed_during_explanation" in evidence.reason_codes
