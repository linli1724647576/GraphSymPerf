from __future__ import annotations

from graph_sym_perf.core import QueryCase, TransformConfig
from graph_sym_perf.transforms import available_families, generate_transform_family, select_family_name


def test_transform_registry_lists_expected_families() -> None:
    assert set(available_families()) == {"bool_redundancy", "symmetry"}


def test_transform_registry_prefers_symmetry_for_gdsmith_cases() -> None:
    case = QueryCase(
        case_id="gdsmith_case_0001",
        template_name="gdsmith",
        query="MATCH (a:L0)-[r:T0]-(b:L1) RETURN a, b",
        parameters={},
        base_predicate="",
        safe_expression="",
        source="gdsmith",
    )

    config = TransformConfig(enabled_families=("bool_redundancy", "symmetry"), enabled_rules=("node_order_symmetry",))

    assert select_family_name(case, config) == "symmetry"
    family = generate_transform_family(case, config)
    assert family.variants


def test_transform_registry_prefers_bool_redundancy_for_neutral_fixture_cases() -> None:
    case = QueryCase(
        case_id="case_0001",
        template_name="person_age_filter",
        query="MATCH (p:Person) WHERE p.age >= $min_age RETURN p",
        parameters={"min_age": 20},
        base_predicate="p.age >= $min_age",
        safe_expression="p.gbp_id IS NOT NULL",
        source="neutral_fixture",
    )

    config = TransformConfig(
        enabled_families=("symmetry", "bool_redundancy"),
        enabled_rules=("constant_true_disjunction",),
    )

    assert select_family_name(case, config) == "bool_redundancy"
    family = generate_transform_family(case, config)
    assert family.variants[0].metadata["transform_family"] == "bool_redundancy"
