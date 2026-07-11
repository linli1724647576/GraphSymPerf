"""Export boundary: only a typed SubmitReadyIssue can cross it."""

from __future__ import annotations

import json
import os
import tempfile
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
    payload = (
        json.dumps(
            {
                "issue_id": issue.issue_id,
                "stage": issue.stage.value,
                "dbms_version": issue.validation.manifest.dbms_version,
                "rule_id": issue.certificate.rule_id,
                "validated_ratio": issue.reduction.final_validation.validated_ratio,
                "reduction_status": issue.reduction.status.value,
                "reduced_original": issue.reduction.reduced_pair.query_a,
                "reduced_transformed": issue.reduction.reduced_pair.query_b,
            },
            indent=2,
        )
        + "\n"
    )
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return path
