from __future__ import annotations

from graph_sym_perf.core import DatasetConfig, QueryCase, QueryGenerationConfig, TransformConfig
from graph_sym_perf.datasets import generate_mini_ldbc
from graph_sym_perf.querygen import generate_query_cases
from graph_sym_perf.transforms import generate_bool_redundancy_family, inject_redundant_predicate
from graph_sym_perf.transforms.redundant_predicate import TransformSkipError


def test_mini_ldbc_generation_is_deterministic() -> None:
    config = DatasetConfig(seed=1234)

    left = generate_mini_ldbc(config)
    right = generate_mini_ldbc(config)

    assert left == right
    assert left.nodes_by_label("Person")
    assert any(rel.relationship_type == "KNOWS" for rel in left.relationships)


def test_query_generation_and_redundant_transform() -> None:
    dataset = DatasetConfig(dataset_id="test_dataset")
    cases = generate_query_cases(dataset, QueryGenerationConfig(max_cases=2, result_limit=7))

    assert len(cases) == 2
    family = generate_bool_redundancy_family(cases[0])
    pair = inject_redundant_predicate(cases[0])

    assert family.variants
    assert family.variants[0].metadata["transform_family"] == "bool_redundancy"
    assert pair.case_id == cases[0].case_id
    assert "true OR" in pair.transformed_query
    assert pair.parameters["dataset_id"] == "test_dataset"
    assert pair.parameters["limit"] == 7
    assert pair.injection_site is not None
    proof = pair.transform_metadata["semantic_proof"]
    assert proof["theorem"] == "RedundantBooleanPredicateIdentity"
    assert proof["original_ra"] == proof["transformed_ra"]


def test_ldbc_transform_rejects_base_predicate_mismatch() -> None:
    case = QueryCase(
        case_id="case_mismatch",
        template_name="person_age_filter",
        query="MATCH (p:Person) WHERE p.age >= $min_age RETURN p",
        parameters={"min_age": 20},
        base_predicate="p.age > $min_age",
        safe_expression="p.gbp_id IS NOT NULL",
    )

    try:
        inject_redundant_predicate(case)
    except TransformSkipError as exc:
        assert exc.code == "UNSAFE_PREDICATE_PROOF_FRAGMENT"
    else:
        raise AssertionError("expected mismatched base predicate to be rejected")


def test_ldbc_bool_rules_use_enabled_canonical_names() -> None:
    case = QueryCase(
        case_id="case_bool",
        template_name="person_age_filter",
        query="MATCH (p:Person) WHERE p.age >= $min_age RETURN p",
        parameters={"min_age": 20},
        base_predicate="p.age >= $min_age",
        safe_expression="p.gbp_id IS NOT NULL",
    )

    family = generate_bool_redundancy_family(
        case,
        TransformConfig(
            enabled_families=("bool_redundancy",),
            enabled_rules=("and_true_or", "or_false_and"),
            max_variants_per_case=2,
        ),
    )
    rules = {variant.transform_rule for variant in family.variants}

    assert rules == {"and_true_or", "or_false_and"}
    assert "where_and_true_or_expr" not in rules
