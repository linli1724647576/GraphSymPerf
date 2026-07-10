from graph_sym_perf.transforms.rule_models import RuleRegistry


BOOLEAN_RULES = tuple(rule for rule in RuleRegistry.default().rules if "excluded_middle" in rule.rule_id)
