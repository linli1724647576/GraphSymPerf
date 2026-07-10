"""Export boundary: only a typed SubmitReadyIssue can cross it."""

from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.validation.models import PipelineStage, SubmitReadyIssue


def export_issue(issue: SubmitReadyIssue, destination: str | Path) -> Path:
    if not isinstance(issue, SubmitReadyIssue):
        raise TypeError("issue exporter accepts SubmitReadyIssue only")
    if issue.stage is not PipelineStage.SUBMIT_READY:
        raise ValueError("only SUBMIT_READY issues can be exported")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stage": issue.stage.value, "rule_id": issue.certificate.rule_id, "validated_ratio": issue.validation.validated_ratio}, indent=2) + "\n", encoding="utf-8")
    return path
