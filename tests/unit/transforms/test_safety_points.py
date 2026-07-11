from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


def test_safety_points_enumerate_all_where_boolean_sites_not_only_first_one() -> None:
    rule = next(
        rule
        for rule in RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)
        if rule.rule_id == "predicate_order_symmetry"
    )
    certificates = enumerate_safety_points("MATCH (n) WHERE n.a = 1 AND n.b = 2 RETURN n", rule, "cap")
    assert {certificate.ast_path for certificate in certificates} == {
        "clauses[0].where",
        "clauses[0].where.and[0]",
        "clauses[0].where.and[1]",
    }
    assert all(certificate.capability_hash == "cap" for certificate in certificates)


def test_safety_points_cover_every_match_component_and_accumulate_bound_scope() -> None:
    rule = next(
        rule
        for rule in RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)
        if rule.rule_id == "pattern_order_symmetry"
    )
    certificates = enumerate_safety_points(
        "MATCH (a:A), (b:B) MATCH (c:C) RETURN a, b, c",
        rule,
        "cap",
    )

    assert {certificate.ast_path for certificate in certificates} == {
        "clauses[0].pattern.components[0]",
        "clauses[0].pattern.components[1]",
        "clauses[1].pattern.components[0]",
    }
    assert certificates[-1].variables_in_scope == ("a", "b", "c")


def test_safety_points_cover_all_consecutive_mandatory_match_clauses() -> None:
    rule = next(
        rule
        for rule in RuleRegistry.default().rules_for("neo4j", SafetyTier.EXPERIMENTAL)
        if rule.rule_id == "independent_mandatory_match_reorder"
    )

    certificates = enumerate_safety_points("MATCH (a:A) MATCH (b:B) RETURN a, b", rule, "cap")

    assert [certificate.ast_path for certificate in certificates] == [
        "clauses[0].mandatory_match",
        "clauses[1].mandatory_match",
    ]


def test_core_boolean_safety_points_reject_volatile_or_expensive_expression_sites() -> None:
    rule = next(
        rule
        for rule in RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)
        if rule.rule_id == "constant_true_disjunction"
    )

    assert not enumerate_safety_points("MATCH (n) WHERE rand() > 0.5 RETURN n", rule, "cap")
    assert not enumerate_safety_points("MATCH (n) WHERE n.x + 1 > 2 RETURN n", rule, "cap")
