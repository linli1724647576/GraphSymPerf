from dataclasses import replace

from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import CandidateBundle, SafetyCertificate


def _bundle() -> CandidateBundle:
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    pair = EquivalentQueryPair(
        "MATCH (n) RETURN n.id ORDER BY n.id",
        "MATCH (m) RETURN m.id ORDER BY m.id",
        {"limit": 10},
        snapshot.content_hash,
        "dbms-fingerprint",
    )
    return CandidateBundle.create(
        pair,
        graph_snapshot=snapshot,
        dbms_version="Neo4j/2026.07",
        resolved_config={"runtime": {"pairs": 20}, "system": "neo4j"},
        discovery_slow_query="b",
        discovery_ratio=8.0,
        certificate=SafetyCertificate("pattern_order_symmetry", "core", "v1", "match"),
    )


def test_candidate_bundle_binds_all_validation_inputs_by_content_hash() -> None:
    bundle = _bundle()

    assert bundle.verify_integrity() == ()
    assert set(bundle.content_hashes) == {
        "query_a",
        "query_b",
        "parameters",
        "graph_snapshot",
        "dbms_fingerprint",
        "dbms_version",
        "resolved_config",
        "discovery_slow_query",
        "discovery_ratio",
        "validation_policy",
        "certificate",
    }


def test_candidate_bundle_rejects_content_replaced_after_discovery() -> None:
    bundle = _bundle()
    changed_pair = replace(bundle.pair, parameters={"limit": 11})

    assert "parameters_hash_mismatch" in replace(bundle, pair=changed_pair).verify_integrity()
    assert "dbms_version_hash_mismatch" in replace(bundle, dbms_version="Neo4j/other").verify_integrity()
    assert "resolved_config_hash_mismatch" in replace(bundle, resolved_config={"runtime": {"pairs": 5}}).verify_integrity()


def test_candidate_bundle_rejects_snapshot_or_direction_identity_mismatch() -> None:
    bundle = _bundle()
    other_snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 2})",))

    assert "graph_snapshot_hash_mismatch" in replace(bundle, graph_snapshot=other_snapshot).verify_integrity()
    changed_pair = replace(bundle.pair, graph_snapshot_hash="wrong")
    assert "pair_graph_snapshot_mismatch" in replace(bundle, pair=changed_pair).verify_integrity()
    assert "discovery_slow_query_hash_mismatch" in replace(bundle, discovery_slow_query="a").verify_integrity()
    assert "discovery_ratio_hash_mismatch" in replace(bundle, discovery_ratio=999.0).verify_integrity()
    assert "invalid_discovery_slow_query" in replace(bundle, discovery_slow_query="query_b").verify_integrity()


def test_candidate_bundle_binds_the_full_validation_policy() -> None:
    bundle = _bundle()
    changed_policy = replace(bundle.validation_policy, min_ratio=99.0)

    assert "validation_policy_hash_mismatch" in replace(bundle, validation_policy=changed_policy).verify_integrity()


def test_candidate_bundle_content_and_hash_bindings_are_deeply_immutable() -> None:
    bundle = _bundle()

    try:
        bundle.content_hashes["query_a"] = "forged"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("content hashes must not be mutable")
    try:
        bundle.resolved_config["runtime"]["pairs"] = 5  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("nested resolved config must not be mutable")
    try:
        bundle.pair.parameters["limit"] = 99  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("parameters must not be mutable")


def test_candidate_bundle_fail_closes_when_versioned_dbms_identity_is_unavailable() -> None:
    bundle = _bundle()
    unavailable = CandidateBundle.create(
        replace(bundle.pair, dbms_fingerprint=""),
        graph_snapshot=bundle.graph_snapshot,
        dbms_version="",
        resolved_config={},
        discovery_slow_query="b",
        discovery_ratio=8.0,
        certificate=bundle.certificate,
    )

    assert "dbms_fingerprint_unavailable" in unavailable.verify_integrity()
    assert "dbms_version_unavailable" in unavailable.verify_integrity()
    assert "resolved_config_unavailable" in unavailable.verify_integrity()
