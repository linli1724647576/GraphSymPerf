from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class IssueEvidence:
    issue_id: str
    issue_url: str
    files: Mapping[str, str]


def write_evidence_package(root: str | Path, evidence: IssueEvidence) -> Path:
    package = Path(root) / evidence.issue_id
    package.mkdir(parents=True, exist_ok=True)
    inventory = []
    for name, content in evidence.files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        inventory.append({"path": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    (package / "metadata.json").write_text(json.dumps({"issue_id": evidence.issue_id, "issue_url": evidence.issue_url}, indent=2) + "\n", encoding="utf-8")
    (package / "evidence_inventory.json").write_text(json.dumps({"files": inventory}, indent=2) + "\n", encoding="utf-8")
    return package
