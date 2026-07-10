"""Export boundary: only a typed SubmitReadyIssue can cross it."""

from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.validation.models import SubmitReadyIssue
from graph_sym_perf.validation.promotion import submit_ready_issue_is_valid


def export_issue(issue: SubmitReadyIssue, destination: str | Path) -> Path:
    if not isinstance(issue, SubmitReadyIssue):
        raise TypeError("issue exporter accepts SubmitReadyIssue only")
    if not submit_ready_issue_is_valid(issue):
        raise ValueError("SubmitReadyIssue no longer satisfies promotion gates")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "issue_id": issue.issue_id,
                "stage": issue.stage.value,
                "rule_id": issue.certificate.rule_id,
                "validated_ratio": issue.validation.validated_ratio,
                "reduction_status": issue.reduction.status.value,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
