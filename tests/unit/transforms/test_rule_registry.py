from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


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
