import pytest

from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.transforms.safety_points import SafetyCertificate, enumerate_safety_points


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


def test_certificate_rejects_unknown_rule_ids_in_canonical_and_legacy_forms() -> None:
    with pytest.raises(ValueError, match="unknown canonical rule"):
        SafetyCertificate(
            "unknown_rule",
            "bool",
            "core",
            True,
            "unknown-proof",
            "1",
            "where_boolean",
            "clauses[0].where",
            "cap",
            "0" * 64,
        )
    with pytest.raises(ValueError, match="unknown canonical rule"):
        SafetyCertificate(
            "unknown_rule",
            "clauses[0].where",
            (),
            "boolean",
            "three_valued",
            "where",
            ("pure",),
            "unknown-proof",
            "cap",
        )


def test_canonical_certificate_cannot_derive_missing_facts_from_only_a_rule_id() -> None:
    with pytest.raises(ValueError, match="canonical certificate fields"):
        SafetyCertificate(
            canonical_rule_id="constant_true_disjunction",
            site_class="where_boolean",
            ast_path="clauses[0].where",
            capability_hash="cap",
        )


@pytest.mark.parametrize(
    ("proof_id", "preconditions"),
    (
        ("forged-proof", ("pure", "total", "low_cost")),
        ("kleene_or_true_v1", ("pure",)),
    ),
)
def test_legacy_certificate_rejects_conflicting_existing_facts(
    proof_id: str,
    preconditions: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="legacy certificate"):
        SafetyCertificate(
            "constant_true_disjunction",
            "clauses[0].where",
            (),
            "boolean",
            "three_valued",
            "where",
            preconditions,
            proof_id,
            "cap",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tier", "control"),
        ("site_class", "match_pattern_component"),
    ),
)
def test_legacy_certificate_rejects_supplied_conflicting_new_facts(
    field: str,
    value: object,
) -> None:
    rule = RuleRegistry.default().get("constant_true_disjunction")
    values = {
        "rule_id": rule.rule_id,
        "ast_path": "clauses[0].where",
        "variables_in_scope": (),
        "expression_type": "boolean",
        "nullability": "three_valued",
        "clause_context": "where",
        "preconditions": rule.purity_requirements,
        "proof_id": rule.proof_id,
        "capability_hash": "cap",
        field: value,
    }
    with pytest.raises(ValueError, match="legacy certificate"):
        SafetyCertificate(**values)


def test_legacy_alias_certificate_normalizes_identity_and_proof_hash() -> None:
    rule = RuleRegistry.default().get("null_complete_excluded_middle")
    canonical = enumerate_safety_points(
        "MATCH (n) WHERE n.x = 1 RETURN n",
        rule,
        "cap",
    )[0]
    alias = SafetyCertificate(
        "and_excluded_middle_or_null",
        "clauses[0].where",
        ("n",),
        "boolean",
        "three_valued",
        "where",
        rule.purity_requirements,
        rule.proof_id,
        "cap",
    )

    assert alias.rule_id == alias.canonical_rule_id == rule.canonical_rule_id
    assert alias.semantic_proof_hash == canonical.semantic_proof_hash
