from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


def test_registry_exposes_only_submit_eligible_core_rules() -> None:
    rules = RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)
    assert rules
    assert all(rule.submit_eligible and rule.proof_id for rule in rules)
