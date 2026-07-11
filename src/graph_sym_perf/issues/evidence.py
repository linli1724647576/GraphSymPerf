from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml


REQUIRED_EVIDENCE_FILES = frozenset(
    {
        "metadata.yaml",
        "setup.cypher",
        "original.cypher",
        "transformed.cypher",
        "reduced_original.cypher",
        "reduced_transformed.cypher",
        "validation.json",
        "README.md",
    }
)
_REQUIRED_METADATA = frozenset(
    {
        "issue_url", "dbms", "dbms_version", "detector_version", "rule_id", "rule_version",
        "validator_version", "reducer_version", "discovery_runtime", "validated_runtime",
        "developer_status", "status_evidence_url", "last_checked_at", "revalidated_by_v3",
        "unknown_reason",
    }
)
_DEVELOPER_STATUSES = frozenset(
    {"waiting_reply", "confirmed", "fixed", "duplicate", "intended_behavior", "uninterested", "unknown"}
)


@dataclass(frozen=True)
class IssueEvidence:
    issue_id: str
    metadata: Mapping[str, object]
    files: Mapping[str, str]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z0-9-]+", self.issue_id) is None:
            raise ValueError("issue evidence id is invalid")
        missing = _REQUIRED_METADATA - set(self.metadata)
        if missing:
            raise ValueError(f"issue metadata missing fields: {', '.join(sorted(missing))}")
        if not isinstance(self.metadata["revalidated_by_v3"], bool):
            raise TypeError("revalidated_by_v3 must be boolean")
        if self.metadata["developer_status"] not in _DEVELOPER_STATUSES:
            raise ValueError("developer_status is not a supported evidence status")
        expected = REQUIRED_EVIDENCE_FILES - {"metadata.yaml"}
        if set(self.files) != expected:
            raise ValueError("issue evidence files must match the public package contract")
        if any(not isinstance(content, str) or not content.strip() for content in self.files.values()):
            raise ValueError("issue evidence files must be non-empty text")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


def write_evidence_package(root: str | Path, evidence: IssueEvidence) -> Path:
    package = Path(root) / evidence.issue_id
    package.mkdir(parents=True, exist_ok=False)
    payloads = {
        "metadata.yaml": yaml.safe_dump(dict(evidence.metadata), allow_unicode=True, sort_keys=True),
        **dict(evidence.files),
    }
    inventory = []
    for name in sorted(payloads):
        content = payloads[name]
        path = package / name
        path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        inventory.append({"path": name, "sha256": _sha256(path.read_bytes()), "size_bytes": path.stat().st_size})
    (package / "evidence_inventory.json").write_text(
        json.dumps({"schema_version": 1, "files": inventory}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return package


def audit_evidence_package(package: str | Path) -> tuple[str, ...]:
    root = Path(package)
    reasons: list[str] = []
    actual_files = {path.name for path in root.iterdir() if path.is_file()}
    missing = REQUIRED_EVIDENCE_FILES - actual_files
    reasons.extend(f"missing:{name}" for name in sorted(missing))
    unexpected = actual_files - REQUIRED_EVIDENCE_FILES - {"evidence_inventory.json"}
    reasons.extend(f"unexpected:{name}" for name in sorted(unexpected))
    try:
        inventory = json.loads((root / "evidence_inventory.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return tuple(reasons + ["inventory_invalid"])
    records = inventory.get("files") if isinstance(inventory, dict) else None
    if not isinstance(records, list) or inventory.get("schema_version") != 1:
        return tuple(reasons + ["inventory_invalid"])
    indexed = {record.get("path"): record for record in records if isinstance(record, dict)}
    if len(indexed) != len(records) or set(indexed) != REQUIRED_EVIDENCE_FILES:
        reasons.append("inventory_file_set_invalid")
    for name in REQUIRED_EVIDENCE_FILES:
        path = root / name
        if not path.is_file():
            continue
        record = indexed.get(name)
        if (
            not isinstance(record, dict)
            or record.get("sha256") != _sha256(path.read_bytes())
            or record.get("size_bytes") != path.stat().st_size
        ):
            reasons.append(f"hash_mismatch:{name}")
    try:
        metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    except (FileNotFoundError, yaml.YAMLError):
        metadata = None
    if not isinstance(metadata, dict) or _REQUIRED_METADATA - set(metadata):
        reasons.append("metadata_invalid")
    elif (
        not isinstance(metadata.get("revalidated_by_v3"), bool)
        or metadata.get("developer_status") not in _DEVELOPER_STATUSES
        or not str(metadata.get("issue_url", "")).startswith("https://github.com/")
        or not str(metadata.get("status_evidence_url", "")).startswith("https://")
    ):
        reasons.append("metadata_invalid")
    try:
        validation = json.loads((root / "validation.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        validation = None
    if not isinstance(validation, dict):
        reasons.append("validation_invalid")
    elif isinstance(metadata, dict) and validation.get("revalidated_by_v3") is not metadata.get("revalidated_by_v3"):
        reasons.append("validation_status_mismatch")
    return tuple(sorted(set(reasons)))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = ["IssueEvidence", "REQUIRED_EVIDENCE_FILES", "audit_evidence_package", "write_evidence_package"]
