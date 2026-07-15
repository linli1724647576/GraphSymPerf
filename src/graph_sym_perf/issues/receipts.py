"""Canonical schema boundary for durable issue materialization receipts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from graph_sym_perf.artifacts.schema import canonical_artifact_reference_payload


_RECEIPT_FIELDS = (
    "artifact_store",
    "export_path",
    "export_sha256",
    "issue_id",
    "issue_ref",
)
_RECEIPT_FIELD_SET = frozenset(_RECEIPT_FIELDS)
_COMMIT_FIELD_SET = _RECEIPT_FIELD_SET | {"registry_record_hash"}


def canonical_issue_receipt_payload(
    value: object,
    *,
    issue_id: str | None = None,
) -> dict[str, object]:
    """Validate and normalize the exact durable receipt schema."""

    if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELD_SET:
        raise ValueError("issue materialization receipt is incomplete")
    payload = dict(value)
    for name in ("artifact_store", "export_path", "export_sha256", "issue_id"):
        field = payload.get(name)
        if not isinstance(field, str) or not field:
            raise ValueError(f"issue materialization receipt {name} must be text")
    if issue_id is not None and payload["issue_id"] != issue_id:
        raise ValueError("issue materialization receipt has the wrong identity")
    if re.fullmatch(r"[0-9a-f]{64}", str(payload["export_sha256"])) is None:
        raise ValueError("issue materialization receipt export hash is invalid")
    payload["issue_ref"] = canonical_artifact_reference_payload(payload["issue_ref"])
    return {name: payload[name] for name in _RECEIPT_FIELDS}


def canonical_issue_receipt_dumps(
    value: object,
    *,
    issue_id: str | None = None,
) -> str:
    """Serialize one validated receipt for identity comparisons."""

    return json.dumps(
        canonical_issue_receipt_payload(value, issue_id=issue_id),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_issue_commit_payload(
    value: object,
    *,
    issue_id: str | None = None,
) -> dict[str, object]:
    """Validate a receipt plus its committed registry-record hash."""

    if not isinstance(value, Mapping) or set(value) != _COMMIT_FIELD_SET:
        raise ValueError("owner commit is incomplete")
    receipt = canonical_issue_receipt_payload(
        {name: value[name] for name in _RECEIPT_FIELDS},
        issue_id=issue_id,
    )
    record_hash = value.get("registry_record_hash")
    if not isinstance(record_hash, str) or re.fullmatch(r"[0-9a-f]{64}", record_hash) is None:
        raise ValueError("owner commit registry hash is invalid")
    return {**receipt, "registry_record_hash": record_hash}


def canonical_issue_commit_receipt_payload(
    value: object,
    *,
    issue_id: str | None = None,
) -> dict[str, object]:
    """Extract a canonical receipt only from a fully validated commit."""

    commit = canonical_issue_commit_payload(value, issue_id=issue_id)
    return canonical_issue_receipt_payload(
        {name: commit[name] for name in _RECEIPT_FIELDS},
        issue_id=issue_id,
    )


def issue_receipts_equal(
    left: object,
    right: object,
    *,
    issue_id: str | None = None,
) -> bool:
    """Compare receipt identity only after strict canonical validation."""

    return canonical_issue_receipt_dumps(
        left,
        issue_id=issue_id,
    ) == canonical_issue_receipt_dumps(right, issue_id=issue_id)


__all__ = [
    "canonical_issue_commit_payload",
    "canonical_issue_commit_receipt_payload",
    "canonical_issue_receipt_dumps",
    "canonical_issue_receipt_payload",
    "issue_receipts_equal",
]
