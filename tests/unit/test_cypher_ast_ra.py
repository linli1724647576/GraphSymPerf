from __future__ import annotations

import pytest

from graph_sym_perf.cypher.ast import EdgePattern, PatternList, VarLengthPathPattern
from graph_sym_perf.cypher.parser import CypherParseError, parse_and_predicate, parse_match_clause, parse_query
from graph_sym_perf.cypher.proof import (
    ProofFailure,
    prove_edge_notation,
    prove_path_endpoint,
    prove_predicate_order,
)


def _first_match(query: str):
    return parse_match_clause(parse_query(query).ordinary_match_clauses()[0])


def test_edge_notation_ast_lowers_to_same_ra() -> None:
    original = _first_match("MATCH (a:L0)-[r:T0]->(b:L1) RETURN a, b").pattern
    transformed = _first_match("MATCH (b:L1)<-[r:T0]-(a:L0) RETURN a, b").pattern

    assert isinstance(original, EdgePattern)
    assert isinstance(transformed, EdgePattern)
    proof = prove_edge_notation(original, transformed)

    assert proof.theorem == "DirectedEdgeNotationEquivalence"
    assert proof.original_ra == proof.transformed_ra


def test_chained_pattern_lowers_to_pattern_list_join() -> None:
    pattern = _first_match("MATCH (a)-[r:T0]->(b)-[s:T1]->(c) RETURN a, b, c").pattern

    assert isinstance(pattern, PatternList)
    assert pattern.separator == "chain"
    assert len(pattern.atoms) == 2


def test_predicate_order_uses_ra_multiset_signature() -> None:
    original = parse_and_predicate("n.id = 1 AND n.name IS NOT NULL")
    transformed = parse_and_predicate("n.name IS NOT NULL AND n.id = 1")

    proof = prove_predicate_order(original, transformed)

    assert proof.theorem == "PureTotalThreeValuedAndCommutativity"
    assert proof.original_ra == proof.transformed_ra


def test_ordering_predicate_is_outside_total_predicate_fragment() -> None:
    with pytest.raises(CypherParseError) as exc:
        parse_and_predicate("n.id > 1 AND n.name IS NOT NULL")

    assert exc.value.code == "UNPROVEN_PREDICATE_TERM"


def test_finite_undirected_path_endpoint_bijection_ra() -> None:
    original = _first_match("MATCH (a:L0)-[:T0*1..3]-(b:L1) RETURN a, b").pattern
    transformed = _first_match("MATCH (b:L1)-[:T0*1..3]-(a:L0) RETURN a, b").pattern

    assert isinstance(original, VarLengthPathPattern)
    assert isinstance(transformed, VarLengthPathPattern)
    proof = prove_path_endpoint(original, transformed)

    assert proof.theorem == "FiniteUndirectedPathEndpointBijection"
    assert proof.original_ra == proof.transformed_ra


def test_relationship_list_variable_blocks_path_proof() -> None:
    original = _first_match("MATCH (a:L0)-[r:T0*1..3]-(b:L1) RETURN a, b").pattern
    transformed = _first_match("MATCH (b:L1)-[r:T0*1..3]-(a:L0) RETURN a, b").pattern

    assert isinstance(original, VarLengthPathPattern)
    assert isinstance(transformed, VarLengthPathPattern)
    with pytest.raises(ProofFailure) as exc:
        prove_path_endpoint(original, transformed)

    assert exc.value.code == "RELATIONSHIP_LIST_VARIABLE"
