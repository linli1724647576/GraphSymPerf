from __future__ import annotations

from graph_sym_perf.core import QueryCase, TransformConfig
from graph_sym_perf.transforms import generate_symmetry_family


def _assert_proof_metadata(family) -> None:
    assert len(family.variants) == 1
    proof = family.variants[0].metadata["semantic_proof"]
    assert proof["theorem"]
    assert proof["fragment"]
    assert proof["premises"]
    assert proof["invariant"]
    assert proof["original_ra"] == proof["transformed_ra"]


def _case(query: str) -> QueryCase:
    return QueryCase(
        case_id="case_0001",
        template_name="gdsmith",
        query=query,
        parameters={},
        base_predicate="",
        safe_expression="",
        source="gdsmith",
    )


def test_node_order_symmetry_for_undirected_edge() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0)-[r:T0]-(b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("node_order_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (b:L1)-[r:T0]-(a:L0) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_edge_notation_symmetry_preserves_directed_edge_semantics() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0)-[r:T0]->(b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("edge_notation_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (b:L1)<-[r:T0]-(a:L0) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_edge_notation_symmetry_reverses_chained_patterns() -> None:
    family = generate_symmetry_family(
        _case("MATCH (n0:L0:L5)-[r0:T1]->(n1:L4)-[r1:T2]->(n2) RETURN n0, n1, n2"),
        TransformConfig(enabled_rules=("edge_notation_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (n2)<-[r1:T2]-(n1:L4)<-[r0:T1]-(n0:L0:L5) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_node_order_symmetry_reverses_undirected_chained_patterns() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0)-[r:T0]-(b:L1)-[s:T1]-(c:L2) RETURN a, b, c"),
        TransformConfig(enabled_rules=("node_order_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (c:L2)-[s:T1]-(b:L1)-[r:T0]-(a:L0) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_predicate_order_symmetry_reorders_top_level_and_terms() -> None:
    family = generate_symmetry_family(
        _case("MATCH (n:L0) WHERE n.id = 1 AND n.name IS NOT NULL RETURN n.id"),
        TransformConfig(enabled_rules=("predicate_order_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "WHERE n.name IS NOT NULL AND n.id = 1 RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_predicate_order_symmetry_skips_ordering_comparisons() -> None:
    family = generate_symmetry_family(
        _case("MATCH (n:L0) WHERE n.id > 1 AND n.name IS NOT NULL RETURN n.id"),
        TransformConfig(enabled_rules=("predicate_order_symmetry",)),
    )

    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "UNPROVEN_PREDICATE_TERM"


def test_predicate_order_symmetry_skips_exception_sensitive_terms() -> None:
    family = generate_symmetry_family(
        _case("MATCH (n:L0) WHERE n.x / n.y > 1 AND n.name IS NOT NULL RETURN n.id"),
        TransformConfig(enabled_rules=("predicate_order_symmetry",)),
    )

    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "UNSAFE_PREDICATE_TERM"


def test_pattern_order_symmetry_swaps_adjacent_match_clauses() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0) MATCH (b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("pattern_order_symmetry",)),
    )

    assert len(family.variants) == 1
    assert family.variants[0].query.startswith("MATCH (b:L1) MATCH (a:L0)")
    _assert_proof_metadata(family)


def test_pattern_order_symmetry_skips_shared_variables() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0) MATCH (a)-[r:T0]->(b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("pattern_order_symmetry",)),
    )

    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "SHARED_MATCH_VARIABLES"


def test_pattern_order_symmetry_skips_filtered_match_clauses() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0) WHERE a.id > 1 MATCH (b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("pattern_order_symmetry",)),
    )

    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "MATCH_WHERE_BOUNDARY"


def test_path_endpoint_symmetry_skips_returned_path_variable() -> None:
    family = generate_symmetry_family(
        _case("MATCH p = (a:L0)-[*1..3]-(b:L1) RETURN p"),
        TransformConfig(enabled_rules=("path_endpoint_symmetry",)),
    )

    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "PATH_VARIABLE"


def test_path_endpoint_symmetry_skips_relationship_list_variable() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0)-[r:T0*1..3]-(b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("path_endpoint_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (b:L1)-[r:T0*1..3]-(a:L0) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_path_endpoint_symmetry_for_anonymous_finite_undirected_path() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0)-[:T0*1..3]-(b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("path_endpoint_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (b:L1)-[:T0*1..3]-(a:L0) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_label_order_symmetry_reorders_unique_labels() -> None:
    family = generate_symmetry_family(
        _case("MATCH (n:L2:L0:L1) RETURN n"),
        TransformConfig(enabled_rules=("label_order_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (n:L0:L1:L2) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_property_map_order_symmetry_reorders_safe_keys() -> None:
    family = generate_symmetry_family(
        _case("MATCH (n:L0 {b: 2, a: 1}) RETURN n"),
        TransformConfig(enabled_rules=("property_map_order_symmetry",)),
    )

    assert len(family.variants) == 1
    assert "MATCH (n:L0 {a: 1, b: 2}) RETURN" in family.variants[0].query
    _assert_proof_metadata(family)


def test_exploratory_predicate_order_marks_tier() -> None:
    family = generate_symmetry_family(
        _case("MATCH (n:L0) WHERE n.id > 1 AND n.name IS NOT NULL RETURN n.id"),
        TransformConfig(enabled_rules=("exploratory_predicate_order_symmetry",)),
    )

    assert len(family.variants) == 1
    assert family.variants[0].safety_tier == "exploratory"
    assert "dynamic equivalence" in family.variants[0].proof_obligation.lower()


def test_alpha_renaming_skips_return_star() -> None:
    family = generate_symmetry_family(
        _case("MATCH (hidden:L0) RETURN *"),
        TransformConfig(enabled_rules=("alpha_renaming_symmetry",)),
    )

    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "NO_UNOBSERVABLE_VARIABLE"


def test_path_endpoint_skips_return_star_with_path_variable() -> None:
    family = generate_symmetry_family(
        _case("MATCH p = (a:L0)-[*1..3]-(b:L1) RETURN *"),
        TransformConfig(enabled_rules=("path_endpoint_symmetry",)),
    )

    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "PATH_VARIABLE"
