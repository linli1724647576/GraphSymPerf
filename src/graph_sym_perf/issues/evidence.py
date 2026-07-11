from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from graph_sym_perf.gdsmith.replay import gdsmith_read_query_is_replayable


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
PUBLIC_EVIDENCE_PACKAGE_IDS = frozenset(
    {
        "apache-age-2428",
        "arcadedb-4405",
        "arcadedb-5116",
        "arcadedb-5117",
        "falkordb-2081",
        "ladybugdb-668",
        "ladybugdb-669",
        "memgraph-4208",
        "neo4j-13783",
        "neo4j-13813",
        "neo4j-13814",
    }
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


def build_evidence_inventory(root: str | Path) -> dict[str, object]:
    """Build the global, independently checkable historical-evidence inventory."""

    evidence_root = Path(root)
    packages: list[dict[str, object]] = []
    for package in sorted(path for path in evidence_root.iterdir() if path.is_dir()):
        metadata_path = package / "metadata.yaml"
        package_inventory_path = package / "evidence_inventory.json"
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        reasons = audit_evidence_package(package)
        metadata_valid = isinstance(metadata, dict) and "metadata_invalid" not in reasons
        issue_url = str(metadata.get("issue_url", "")) if isinstance(metadata, dict) else ""
        github_url_valid = re.fullmatch(r"https://github\.com/[^/]+/[^/]+/issues/\d+", issue_url) is not None
        hashes_valid = not any(
            reason == "inventory_invalid"
            or reason == "inventory_file_set_invalid"
            or reason.startswith("hash_mismatch:")
            for reason in reasons
        )
        reproducer_available = _historical_reproducer_available(package)
        verified = not reasons and metadata_valid and github_url_valid and hashes_valid and reproducer_available
        packages.append(
            {
                "id": package.name,
                "issue_url": issue_url,
                "inventory_sha256": _sha256(package_inventory_path.read_bytes()),
                "metadata_sha256": _sha256(metadata_path.read_bytes()),
                "hashes_valid": hashes_valid,
                "metadata_schema_valid": metadata_valid,
                "github_url_valid": github_url_valid,
                "reproducer_status": "AVAILABLE_HISTORICAL" if reproducer_available else "UNAVAILABLE",
                "regression_eligible": False,
                "regression_exclusion_reason": "historical_evidence_not_v3_executable",
                "audit_status": "VERIFIED" if verified else "FAILED",
                "audit_reasons": list(reasons),
            }
        )
    return {"schema_version": 3, "packages": packages}


def audit_evidence_inventory(root: str | Path) -> tuple[str, ...]:
    """Verify the tracked global inventory against all evidence package bytes."""

    evidence_root = Path(root)
    try:
        actual = json.loads((evidence_root.parent / "evidence_inventory.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ("evidence_inventory_invalid",)
    expected = build_evidence_inventory(evidence_root)
    reasons: list[str] = []
    package_ids = {record.get("id") for record in expected["packages"]}
    if package_ids != PUBLIC_EVIDENCE_PACKAGE_IDS:
        reasons.append("evidence_package_set_invalid")
    if actual.get("schema_version") != 3 or not isinstance(actual.get("packages"), list):
        reasons.append("evidence_inventory_invalid")
    if actual != expected:
        reasons.append("evidence_inventory_mismatch")
    if any(record.get("audit_status") != "VERIFIED" for record in expected["packages"]):
        reasons.append("evidence_package_audit_failed")
    return tuple(sorted(set(reasons)))


def _historical_reproducer_available(package: Path) -> bool:
    try:
        setup = (package / "setup.cypher").read_text(encoding="utf-8")
        original = (package / "original.cypher").read_text(encoding="utf-8")
        transformed = (package / "transformed.cypher").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return bool(
        setup.strip()
        and not setup.lstrip().startswith("// unknown")
        and gdsmith_read_query_is_replayable(original)
        and gdsmith_read_query_is_replayable(transformed)
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "IssueEvidence",
    "PUBLIC_EVIDENCE_PACKAGE_IDS",
    "REQUIRED_EVIDENCE_FILES",
    "audit_evidence_inventory",
    "audit_evidence_package",
    "build_evidence_inventory",
    "write_evidence_package",
]
