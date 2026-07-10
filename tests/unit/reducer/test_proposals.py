from graph_sym_perf.reducer.proposals import (
    graph_setup_proposals,
    hierarchical_paired_proposals,
    paired_reduction_proposals,
)
from graph_sym_perf.runtime.models import EquivalentQueryPair


def _pair(query_a: str, query_b: str | None = None) -> EquivalentQueryPair:
    return EquivalentQueryPair(query_a, query_b or query_a, {}, "graph", "dbms")


def test_public_paired_universe_contains_clause_pattern_and_predicate_deletions() -> None:
    pairs = (
        (_pair("MATCH (n:Person) MATCH (m:Unused) RETURN n"), "clause"),
        (_pair("MATCH (n:Person), (m:Unused) RETURN n"), "pattern_atom"),
        (_pair("MATCH (n) WHERE n.id = 1 AND n.name = 'x' RETURN n"), "predicate_subtree"),
    )

    for pair, expected_layer in pairs:
        proposals = paired_reduction_proposals(pair)
        assert any(proposal.layer == expected_layer for proposal in proposals)
        assert all(proposal.candidate.query_a != pair.query_a for proposal in proposals)
        assert all(proposal.candidate.query_b != pair.query_b for proposal in proposals)


def test_projection_and_list_items_are_reduced_only_as_paired_edits() -> None:
    projection_pair = _pair("MATCH (n), (m) WITH n, m RETURN n")
    list_pair = _pair("MATCH (n) WHERE n.id IN [1, 2, 3] AND true RETURN n")

    projection = paired_reduction_proposals(projection_pair)
    list_items = paired_reduction_proposals(list_pair)

    assert any(item.layer == "projection_item" and "WITH n RETURN" in item.candidate.query_a for item in projection)
    assert any(item.layer == "list_item" and "[1, 2]" in item.candidate.query_a for item in list_items)
    assert all(item.candidate.query_a == item.candidate.query_b for item in (*projection, *list_items))


def test_literal_property_items_have_a_paired_deletion_layer() -> None:
    proposals = paired_reduction_proposals(_pair("MATCH (n {active: true, age: 1}) RETURN n"))

    assert any(
        item.layer == "literal_item" and "{active: true}" in item.candidate.query_a
        for item in proposals
    )


def test_return_schema_and_count_observables_are_never_automatic_proposals() -> None:
    projection = paired_reduction_proposals(_pair("MATCH (n) RETURN n.id, n.name"))
    count = paired_reduction_proposals(_pair("MATCH (n), (m) RETURN count(*) AS rows"))

    assert not any(item.layer == "projection_item" for item in projection)
    assert count == ()


def test_paired_proposal_ids_are_unique_and_side_independent() -> None:
    pair = _pair(
        "MATCH (n), (m) WHERE n.id = 1 AND m.id = 2 RETURN n",
        "MATCH (m), (n) WHERE m.id = 2 AND n.id = 1 RETURN n",
    )

    proposals = paired_reduction_proposals(pair)

    assert proposals
    assert len({proposal.canonical_id for proposal in proposals}) == len(proposals)
    assert all(not proposal.canonical_id.startswith(("a:", "b:")) for proposal in proposals)
    for proposal in (item for item in proposals if item.layer == "predicate_subtree"):
        assert ("n.id = 1" in proposal.candidate.query_a) == ("n.id = 1" in proposal.candidate.query_b)
        assert ("m.id = 2" in proposal.candidate.query_a) == ("m.id = 2" in proposal.candidate.query_b)


def test_hierarchical_enumerator_tries_chunks_before_one_step_refinement() -> None:
    pair = _pair("MATCH (n), (m), (x) RETURN n")

    proposals = hierarchical_paired_proposals(pair)

    assert proposals[0].layer == "pattern_atom_chunk"
    assert proposals[0].candidate.query_a == "MATCH (n) RETURN n"
    assert any(item.layer == "pattern_atom" for item in proposals[1:])


def test_graph_setup_reduction_is_explicitly_fail_closed_without_semantic_proof() -> None:
    result = graph_setup_proposals(("CREATE (n {id: 1})", "CREATE (m {id: 2})"))

    assert result.proposals == ()
    assert result.reason == "graph_setup_semantic_proof_unavailable"
