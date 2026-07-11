import json
from pathlib import Path

import yaml

from graph_sym_perf.issues.evidence import (
    IssueEvidence,
    REQUIRED_EVIDENCE_FILES,
    audit_evidence_inventory,
    audit_evidence_package,
    write_evidence_package,
)


def _evidence() -> IssueEvidence:
    return IssueEvidence(
        issue_id="neo4j-13783",
        metadata={
            "issue_url": "https://github.com/neo4j/neo4j/issues/13783",
            "dbms": "neo4j",
            "dbms_version": "unknown",
            "detector_version": "unknown",
            "rule_id": "unknown",
            "rule_version": "unknown",
            "validator_version": "unknown",
            "reducer_version": "unknown",
            "discovery_runtime": "unknown",
            "validated_runtime": "unknown",
            "developer_status": "fixed",
            "status_evidence_url": "https://github.com/neo4j/neo4j/issues/13783",
            "last_checked_at": "2026-07-11",
            "revalidated_by_v3": False,
            "unknown_reason": "historical issue has not passed the v3 validator",
        },
        files={
            "setup.cypher": "// unknown: public setup attachment was not recovered\n",
            "original.cypher": "MATCH (n) RETURN n\n",
            "transformed.cypher": "MATCH (n) WHERE true RETURN n\n",
            "reduced_original.cypher": "// unknown: no v3 reduction\n",
            "reduced_transformed.cypher": "// unknown: no v3 reduction\n",
            "validation.json": json.dumps({"revalidated_by_v3": False, "reason": "historical_only"}),
            "README.md": "# Evidence boundary\n\nHistorical public issue; not v3 validated.\n",
        },
    )


def test_evidence_package_writes_required_contract_and_hash_inventory(tmp_path) -> None:
    package = write_evidence_package(tmp_path, _evidence())
    assert REQUIRED_EVIDENCE_FILES <= {path.name for path in package.iterdir()}
    assert yaml.safe_load((package / "metadata.yaml").read_text())["revalidated_by_v3"] is False
    assert audit_evidence_package(package) == ()


def test_all_historical_issue_directories_are_complete_but_not_claimed_v3_valid() -> None:
    packages = sorted(path for path in Path("issues/evidence").iterdir() if path.is_dir())
    assert len(packages) == 11
    for package in packages:
        assert audit_evidence_package(package) == ()
        metadata = yaml.safe_load((package / "metadata.yaml").read_text(encoding="utf-8"))
        assert metadata["revalidated_by_v3"] is False
        validation = json.loads((package / "validation.json").read_text(encoding="utf-8"))
        assert validation["revalidated_by_v3"] is False
        assert "independent two-session" in validation["reason"]
        assert set(path.name for path in package.iterdir()) == REQUIRED_EVIDENCE_FILES | {"evidence_inventory.json"}

    global_inventory = json.loads(Path("issues/evidence_inventory.json").read_text(encoding="utf-8"))
    assert global_inventory["schema_version"] == 3
    assert {record["id"] for record in global_inventory["packages"]} == {package.name for package in packages}
    assert audit_evidence_inventory("issues/evidence") == ()


def test_evidence_inventory_requires_the_exact_public_package_set(tmp_path) -> None:
    evidence_root = tmp_path / "issues" / "evidence"
    evidence_root.mkdir(parents=True)
    (evidence_root.parent / "evidence_inventory.json").write_text(
        json.dumps({"schema_version": 3, "packages": []}),
        encoding="utf-8",
    )

    assert "evidence_package_set_invalid" in audit_evidence_inventory(evidence_root)
