from dataclasses import FrozenInstanceError, replace

import pytest

from graph_sym_perf.core import QueryCase, TargetSystem, TransformConfig
from graph_sym_perf.transforms.bool_redundancy import generate_bool_redundancy_family
from graph_sym_perf.transforms.registry import RuleRegistry, generate_rule_transform
from graph_sym_perf.transforms.rule_models import RuleFamily, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points
from graph_sym_perf.transforms.symmetry import generate_symmetry_family


def test_default_registry_exposes_the_approved_neo4j_rule_set() -> None:
    registry = RuleRegistry.default()
    core = registry.active_rules(
        dialect="cypher",
        tier=SafetyTier.CORE,
        capabilities=frozenset({"multi_label_intersection"}),
    )
    assert {rule.canonical_rule_id for rule in core} == {
        "constant_true_disjunction",
        "constant_false_conjunction",
        "null_partition_tautology",
        "null_contradiction",
        "null_complete_excluded_middle",
        "pattern_order_symmetry",
        "predicate_order_symmetry",
        "node_order_symmetry",
        "label_order_symmetry",
    }
    assert {rule.family for rule in core} == {RuleFamily.BOOL, RuleFamily.SYM}
    assert all(rule.submit_eligible for rule in core)


def test_legacy_alias_resolves_without_creating_a_second_budget_identity() -> None:
    registry = RuleRegistry.default()
    assert registry.resolve_id("and_excluded_middle_or_null") == "null_complete_excluded_middle"
    assert registry.get("and_excluded_middle_or_null") is registry.get("null_complete_excluded_middle")


def test_label_order_is_fail_closed_without_declared_capability() -> None:
    active = RuleRegistry.default().active_rules(
        dialect="cypher", tier=SafetyTier.CORE, capabilities=frozenset()
    )
    assert "label_order_symmetry" not in {rule.canonical_rule_id for rule in active}


@pytest.mark.parametrize("dbms", ("agensgraph", "arcadedb"))
def test_configured_capabilities_cannot_expand_the_target_profile(dbms: str) -> None:
    active = RuleRegistry.default().rules_for(
        dbms,
        SafetyTier.CORE,
        frozenset({"multi_label_intersection"}),
    )
    assert "label_order_symmetry" not in {rule.canonical_rule_id for rule in active}


def test_neo4j_label_order_requires_a_declared_profile_capability() -> None:
    registry = RuleRegistry.default()
    without_declaration = registry.rules_for("neo4j", SafetyTier.CORE, frozenset())
    with_declaration = registry.rules_for(
        "neo4j",
        SafetyTier.CORE,
        frozenset({"multi_label_intersection"}),
    )
    assert "label_order_symmetry" not in {rule.canonical_rule_id for rule in without_declaration}
    assert "label_order_symmetry" in {rule.canonical_rule_id for rule in with_declaration}


def test_rule_specs_expose_canonical_metadata_and_bind_their_family_factory() -> None:
    registry = RuleRegistry.default()
    for rule in registry.rules:
        assert rule.canonical_rule_id == rule.rule_id
        assert rule.proof_version == rule.version
        assert rule.supported_dialects == rule.dialects
        assert rule.allowed_site_classes == rule.site_classes
        expected_factory = (
            generate_bool_redundancy_family
            if rule.family is RuleFamily.BOOL
            else generate_symmetry_family
        )
        assert rule.transform_factory is expected_factory


@pytest.mark.parametrize(
    ("rule_id", "query"),
    (
        ("duplicate_predicate_control", "MATCH (n) WHERE n.id = 1 RETURN n"),
        ("edge_notation_symmetry", "MATCH (a)-[r:R]->(b) RETURN a, b, r"),
    ),
)
def test_control_rule_execution_calls_its_bound_rule_factory(rule_id, query) -> None:
    rule = RuleRegistry.default().get(rule_id)
    calls = []
    bound_factory = rule.transform_factory

    def tracking_factory(query_case, config, target):
        calls.append((query_case, config, target))
        return bound_factory(query_case, config, target)

    tracked = replace(rule, transform_factory=tracking_factory)
    family_name = "bool_redundancy" if rule.family is RuleFamily.BOOL else "symmetry"
    family = generate_rule_transform(
        tracked,
        QueryCase("control", "gdsmith", query, {}, "", "", source="gdsmith"),
        TransformConfig(
            enabled_families=(family_name,),
            enabled_rules=(rule.implementation_rule_id,),
            max_variants_per_case=1,
        ),
        TargetSystem.NEO4J,
    )

    assert len(calls) == 1
    assert family.variants
    assert family.variants[0].transform_rule == rule_id


def test_boolean_rules_keep_canonical_and_implementation_ids_separate() -> None:
    expected = {
        "constant_true_disjunction": "and_true_or",
        "constant_false_conjunction": "or_false_and",
        "null_partition_tautology": "and_expr_null_partition",
        "null_contradiction": "or_expr_null_contradiction",
        "null_complete_excluded_middle": "and_excluded_middle_or_null",
    }
    registry = RuleRegistry.default()
    assert {
        rule.canonical_rule_id: rule.implementation_rule_id
        for rule in registry.active_rules(
            dialect="cypher",
            tier=SafetyTier.CORE,
            capabilities=frozenset({"multi_label_intersection"}),
        )
        if rule.family is RuleFamily.BOOL
    } == expected


def test_registry_rejects_aliases_that_do_not_resolve_to_a_canonical_rule() -> None:
    rule = RuleRegistry.default().get("constant_true_disjunction")
    with pytest.raises(ValueError, match="alias"):
        RuleRegistry((rule,), aliases={"legacy": "missing"})


def test_safety_certificate_carries_frozen_canonical_rule_facts() -> None:
    rule = RuleRegistry.default().get("pattern_order_symmetry")
    certificate = enumerate_safety_points(
        "MATCH (a:A), (b:B) RETURN a, b", rule, "capability-v1"
    )[0]
    other_capability = enumerate_safety_points(
        "MATCH (a:A), (b:B) RETURN a, b", rule, "capability-v2"
    )[0]

    assert certificate.canonical_rule_id == rule.canonical_rule_id
    assert certificate.rule_id == rule.canonical_rule_id
    assert certificate.family == rule.family.value
    assert certificate.tier == rule.tier.value
    assert certificate.submit_eligible is rule.submit_eligible
    assert certificate.proof_id == rule.proof_id
    assert certificate.proof_version == rule.proof_version
    assert certificate.site_class == "match_pattern_component"
    assert certificate.semantic_proof_hash
    assert certificate.semantic_proof_hash == other_capability.semantic_proof_hash
    assert certificate.capability_hash != other_capability.capability_hash
    with pytest.raises(FrozenInstanceError):
        certificate.tier = SafetyTier.CONTROL.value  # type: ignore[misc]


def test_registry_exposes_only_submit_eligible_core_rules() -> None:
    rules = RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)
    assert rules
    assert all(rule.submit_eligible and rule.proof_id for rule in rules)


def test_registry_matches_the_audited_core_control_and_experimental_catalog() -> None:
    registry = RuleRegistry.default()
    neo4j_core = {rule.rule_id for rule in registry.rules_for("neo4j", SafetyTier.CORE)}
    agens_core = {rule.rule_id for rule in registry.rules_for("agensgraph", SafetyTier.CORE)}

    assert neo4j_core == {
        "constant_true_disjunction",
        "constant_false_conjunction",
        "null_partition_tautology",
        "null_contradiction",
        "null_complete_excluded_middle",
        "pattern_order_symmetry",
        "predicate_order_symmetry",
        "node_order_symmetry",
        "label_order_symmetry",
    }
    assert agens_core == neo4j_core - {"label_order_symmetry"}
    assert {rule.rule_id for rule in registry.rules_for("arcadedb", SafetyTier.CORE)} == neo4j_core - {
        "label_order_symmetry"
    }
    assert {rule.rule_id for rule in registry.rules_for("neo4j", SafetyTier.CONTROL)} == {
        "duplicate_predicate_control",
        "edge_notation_symmetry",
        "alpha_renaming_symmetry",
    }
    assert {"double_negation_predicate", "de_morgan_predicate", "pattern_decomposition_symmetry"} <= {
        rule.rule_id for rule in registry.rules_for("neo4j", SafetyTier.EXPERIMENTAL)
    }


def test_every_rule_has_versioned_proof_scope_and_explicit_role() -> None:
    for rule in RuleRegistry.default().rules:
        assert rule.version and rule.proof_id and rule.proof_theorem
        assert rule.purity_requirements and rule.site_classes
        assert rule.role
        assert rule.submit_eligible is (rule.tier is SafetyTier.CORE)
