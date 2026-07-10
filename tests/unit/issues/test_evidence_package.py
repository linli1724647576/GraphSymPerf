from graph_sym_perf.issues.evidence import IssueEvidence, write_evidence_package


def test_evidence_package_writes_hash_inventory(tmp_path) -> None:
    package = write_evidence_package(tmp_path, IssueEvidence("neo4j-13783", "https://github.com/neo4j/neo4j/issues/13783", {"query.cypher": "MATCH (n) RETURN n"}))
    assert (package / "evidence_inventory.json").exists()
