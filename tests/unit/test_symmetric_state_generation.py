from graph_sym_perf.gdsmith.replay import parse_gdsmith_replay_graph
from graph_sym_perf.generation.symmetric_state import build_involutive_symmetry_state


_BASE_GRAPH = (
    "CREATE (n0:L0 {id: 1, score: 7})",
    "CREATE (n1:L1 {id: 2, score: 9})",
    "MATCH (n0 {id: 1}), (n1 {id: 2}) "
    "MERGE (n0)-[r:R0 {id: 11, weight: 3}]->(n1)",
)


def test_builds_two_isomorphic_orbits_and_an_endpoint_symmetry_pair() -> None:
    state = build_involutive_symmetry_state(_BASE_GRAPH, max_pairs=10, seed=17)

    graph = parse_gdsmith_replay_graph(state.graph_statements)
    assert len(graph.nodes) == 4
    assert len(graph.relationships) == 2
    assert state.metadata["state_symmetry_order"] == 2
    assert state.metadata["semantic_identity_exclusion"] == "id"
    assert state.metadata["involution_verified"] is True

    pair = next(item for item in state.query_pairs if item.rule_id == "undirected_endpoint_orbit_symmetry")
    assert pair.query_a != pair.query_b
    assert "RETURN count(*) AS gsp_count" in pair.query_a
    assert pair.certificate["state_relation"] == "involutive_isomorphism_modulo_replay_id"
    assert pair.certificate["syntactic_symmetry"] == "undirected_endpoint_reversal"
    assert pair.certificate["source_graph_sha256"] == state.metadata["source_graph_sha256"]


def test_generates_only_graph_pattern_symmetries_for_a_two_hop_base_graph() -> None:
    statements = _BASE_GRAPH + (
        "CREATE (n2:L2 {id: 3, score: 11})",
        "MATCH (n1 {id: 2}), (n2 {id: 3}) "
        "MERGE (n1)-[r:R1 {id: 12, weight: 5}]->(n2)",
    )

    state = build_involutive_symmetry_state(statements, max_pairs=100, seed=23)

    rule_ids = {pair.rule_id for pair in state.query_pairs}
    assert rule_ids == {
        "undirected_endpoint_orbit_symmetry",
        "two_hop_path_endpoint_orbit_symmetry",
        "incident_pattern_order_orbit_symmetry",
    }
    assert all(" WHERE " not in pair.query_a.upper() for pair in state.query_pairs)
    assert all(" WHERE " not in pair.query_b.upper() for pair in state.query_pairs)
    assert all(" OR NOT " not in pair.query_b.upper() for pair in state.query_pairs)


def test_materialized_involution_preserves_every_semantic_node_and_edge_attribute() -> None:
    state = build_involutive_symmetry_state(_BASE_GRAPH, max_pairs=10, seed=17)
    graph = parse_gdsmith_replay_graph(state.graph_statements)

    left_nodes, right_nodes = graph.nodes[:2], graph.nodes[2:]
    for left, right in zip(left_nodes, right_nodes, strict=True):
        assert left.labels == right.labels
        assert {key: value for key, value in left.properties.items() if key != "id"} == {
            key: value for key, value in right.properties.items() if key != "id"
        }

    left_edge, right_edge = graph.relationships
    assert left_edge.relationship_type == right_edge.relationship_type
    assert {key: value for key, value in left_edge.properties.items() if key != "id"} == {
        key: value for key, value in right_edge.properties.items() if key != "id"
    }
    assert left_edge.start_id <= 2 and left_edge.end_id <= 2
    assert right_edge.start_id > 2 and right_edge.end_id > 2

    repeated = build_involutive_symmetry_state(_BASE_GRAPH, max_pairs=10, seed=17)
    assert repeated == state


def test_projects_multi_label_gdsmith_nodes_to_an_explicit_portable_primary_label() -> None:
    statements = (
        "CREATE (n0:First:Second {id: 1})",
        "CREATE (n1:Third:Fourth {id: 2})",
        "MATCH (n0 {id: 1}), (n1 {id: 2}) MERGE (n0)-[r:R0 {id: 11}]->(n1)",
    )

    state = build_involutive_symmetry_state(statements, max_pairs=10, seed=29)
    graph = parse_gdsmith_replay_graph(state.graph_statements)

    assert all(len(node.labels) == 1 for node in graph.nodes)
    assert [node.labels for node in graph.nodes] == [
        ("First",),
        ("Third",),
        ("First",),
        ("Third",),
    ]
    assert state.metadata["label_projection"] == "portable_primary_label"
