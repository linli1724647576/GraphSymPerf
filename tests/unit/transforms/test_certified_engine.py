from graph_sym_perf.transforms import engine
from graph_sym_perf.transforms.engine import RuleNotApplicable, apply_certified_rule
from graph_sym_perf.transforms import safety_points
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


def _rule(rule_id: str):
    return RuleRegistry.default().get(rule_id)


def test_boolean_core_rules_apply_at_a_certificate_bound_where_site() -> None:
    query = "MATCH (n:A) WHERE n.x = 1 AND n.y = 2 RETURN n"
    for rule_id in (
        "constant_true_disjunction",
        "constant_false_conjunction",
        "null_partition_tautology",
        "null_contradiction",
        "null_complete_excluded_middle",
    ):
        rule = _rule(rule_id)
        certificate = enumerate_safety_points(query, rule, "cap")[1]
        transformed = apply_certified_rule(query, rule, certificate)
        assert transformed != query
        assert "RETURN n" in transformed


def test_symmetry_core_rules_apply_through_ast_proofs() -> None:
    cases = {
        "pattern_order_symmetry": "MATCH (a:A), (b:B) RETURN a, b",
        "predicate_order_symmetry": "MATCH (a:A) WHERE a.x = 1 AND a.y = 2 RETURN a",
        "label_order_symmetry": "MATCH (a:A:B) RETURN a",
    }
    for rule_id, query in cases.items():
        rule = _rule(rule_id)
        certificate = enumerate_safety_points(query, rule, "cap")[0]
        transformed = apply_certified_rule(query, rule, certificate)
        assert transformed != query


def test_certificate_for_another_rule_or_query_is_rejected() -> None:
    query = "MATCH (n:A) WHERE n.x = 1 RETURN n"
    rule = _rule("null_complete_excluded_middle")
    wrong = enumerate_safety_points(query, _rule("constant_true_disjunction"), "cap")[0]

    try:
        apply_certified_rule(query, rule, wrong)
    except ValueError as exc:
        assert "certificate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("mismatched certificate must fail closed")


def test_control_rules_remain_executable_but_outside_the_core_provider() -> None:
    registry = RuleRegistry.default()
    cases = {
        "duplicate_predicate_control": "MATCH (n:A) WHERE n.x = 1 RETURN n",
        "edge_notation_symmetry": "MATCH (a:A)-[r:R]->(b:B) RETURN a, b, r",
        "alpha_renaming_symmetry": "MATCH (visible:A) MATCH (hidden:B) RETURN visible",
    }
    for rule_id, query in cases.items():
        rule = registry.get(rule_id)
        transformed = None
        for certificate in enumerate_safety_points(query, rule, "cap"):
            try:
                transformed = apply_certified_rule(query, rule, certificate)
                break
            except RuleNotApplicable:
                continue
        assert transformed is not None and transformed != query


def test_certified_engine_returns_actual_proof_and_certificate_binds_its_canonical_hash() -> None:
    query = "MATCH (a:A)-[r:R]->(b:B) RETURN a, b, r"
    rule = _rule("edge_notation_symmetry")
    certificate = enumerate_safety_points(query, rule, "cap")[0]

    result = engine.apply_certified_rule_with_proof(query, rule, certificate)

    assert result.query != query
    assert result.semantic_proof["theorem"] == "DirectedEdgeNotationEquivalence"
    assert result.semantic_proof["canonical_rule_id"] == rule.rule_id
    assert result.semantic_proof["site_class"] == certificate.site_class
    assert result.certificate.semantic_proof_hash == safety_points.canonical_semantic_proof_hash(
        result.semantic_proof
    )
    assert result.certificate.semantic_proof_template_hash == certificate.semantic_proof_hash
    assert result.certificate.semantic_proof_hash != result.certificate.semantic_proof_template_hash


def test_boolean_certified_engine_emits_a_real_kleene_identity_proof() -> None:
    query = "MATCH (n:A) WHERE n.x = 1 RETURN n"
    rule = _rule("null_partition_tautology")
    certificate = enumerate_safety_points(query, rule, "cap")[0]

    result = engine.apply_certified_rule_with_proof(query, rule, certificate)

    assert result.semantic_proof["theorem"] == "KleeneNullPartitionTautology"
    assert result.semantic_proof["original_ra"]
    assert result.semantic_proof["transformed_ra"]
    assert result.certificate.semantic_proof_hash == safety_points.canonical_semantic_proof_hash(
        result.semantic_proof
    )
