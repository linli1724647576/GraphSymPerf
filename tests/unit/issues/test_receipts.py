import copy
import json

import pytest

from graph_sym_perf.issues.receipts import (
    canonical_issue_commit_payload,
    canonical_issue_receipt_dumps,
    canonical_issue_receipt_payload,
)


def _receipt() -> dict[str, object]:
    return {
        "artifact_store": "shards/neo4j-000/artifacts",
        "export_path": "issues/submit-ready/issue-1.json",
        "export_sha256": "a" * 64,
        "issue_id": "issue-1",
        "issue_ref": {
            "content_hash": "b" * 64,
            "relative_path": "objects/bb/" + "b" * 64,
            "size_bytes": 100,
            "media_type": "application/json",
            "schema_version": 1,
        },
    }


def test_receipt_serialization_is_stable_and_exact() -> None:
    receipt = _receipt()
    reversed_receipt = dict(reversed(tuple(receipt.items())))

    assert canonical_issue_receipt_dumps(receipt) == canonical_issue_receipt_dumps(
        reversed_receipt
    )
    assert json.loads(canonical_issue_receipt_dumps(receipt)) == receipt


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artifact_store", True),
        ("export_path", None),
        ("export_sha256", 1),
        ("issue_id", False),
    ),
)
def test_receipt_rejects_non_string_top_level_fields(field, value) -> None:
    receipt = _receipt()
    receipt[field] = value

    with pytest.raises(ValueError):
        canonical_issue_receipt_payload(receipt)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("size_bytes", True),
        ("size_bytes", "100"),
        ("schema_version", True),
        ("schema_version", "1"),
    ),
)
def test_receipt_reference_rejects_ambiguous_integer_fields(field, value) -> None:
    receipt = copy.deepcopy(_receipt())
    receipt["issue_ref"][field] = value

    with pytest.raises(ValueError):
        canonical_issue_receipt_payload(receipt)


def test_commit_requires_exact_receipt_plus_registry_hash() -> None:
    receipt = _receipt()
    commit = {**receipt, "registry_record_hash": "c" * 64}

    assert canonical_issue_commit_payload(commit)["registry_record_hash"] == "c" * 64
    with pytest.raises(ValueError):
        canonical_issue_commit_payload({**commit, "extra": "forged"})
    with pytest.raises(ValueError):
        canonical_issue_commit_payload({**commit, "registry_record_hash": True})
