from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


BOOLEAN_RULES = tuple(
    rule
    for rule in RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)
    if rule.site_classes == ("where_boolean",)
)


__all__ = ["BOOLEAN_RULES"]
