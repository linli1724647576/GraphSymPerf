from graph_sym_perf.core import QueryCase, TargetSystem, TransformConfig
from graph_sym_perf.transforms.symmetry import generate_symmetry_family


def _case(query: str) -> QueryCase:
    return QueryCase("case_1", "gdsmith", query, {}, "", "", source="gdsmith")


def _variant(query: str, rule: str, *, target: TargetSystem = TargetSystem.NEO4J):
    family = generate_symmetry_family(
        _case(query),
        TransformConfig(enabled_families=("symmetry",), enabled_rules=(rule,)),
        target,
    )
    assert len(family.variants) == 1
    assert family.variants[0].static_safe
    assert family.variants[0].safety_tier == "core"
    assert family.variants[0].metadata["safety_certificate"].rule_id == rule
    return family.variants[0]


def test_node_order_symmetry_reverses_a_certified_undirected_component() -> None:
    variant = _variant("MATCH (a:A)-[r:R]-(b:B) RETURN a, b, r", "node_order_symmetry")
    assert "(b:B)-[r:R]-(a:A)" in variant.query


def test_predicate_order_symmetry_reorders_only_a_proved_conjunction() -> None:
    variant = _variant(
        "MATCH (a:A) WHERE a.x = 1 AND a.y = 2 RETURN a",
        "predicate_order_symmetry",
    )
    assert "a.y = 2 AND a.x = 1" in variant.query


def test_pattern_order_symmetry_reorders_one_comma_pattern_list() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0), (b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("pattern_order_symmetry",)),
    )
    assert len(family.variants) == 1
    assert family.variants[0].query.startswith("MATCH (b:L1), (a:L0)")
    assert family.variants[0].metadata["semantic_proof"]["theorem"] == "MatchPatternConjunctionCommutativity"


def test_core_pattern_order_never_swaps_match_clauses() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0) MATCH (b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("pattern_order_symmetry",)),
    )
    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "NO_REORDERABLE_PATTERN_LIST"


def test_label_order_is_capability_gated() -> None:
    variant = _variant("MATCH (a:A:B) RETURN a", "label_order_symmetry")
    assert "(a:B:A)" in variant.query

    agens = generate_symmetry_family(
        _case("MATCH (a:A:B) RETURN a"),
        TransformConfig(enabled_families=("symmetry",), enabled_rules=("label_order_symmetry",)),
        TargetSystem.AGENSGRAPH,
    )
    assert not agens.variants
