from graph_sym_perf.core import TargetSystem
from graph_sym_perf.dialects.base import DecisionKind
from graph_sym_perf.experiment.worker import _dialect_for
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.generation.corpus import PortableQuery
from graph_sym_perf.transforms.engine import RuleNotApplicable, apply_certified_rule
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


_QUERIES = {
    "constant_true_disjunction": "MATCH (n:A) WHERE n.x = 1 AND n.y = 2 RETURN n",
    "constant_false_conjunction": "MATCH (n:A) WHERE n.x = 1 AND n.y = 2 RETURN n",
    "null_partition_tautology": "MATCH (n:A) WHERE n.x = 1 AND n.y = 2 RETURN n",
    "null_contradiction": "MATCH (n:A) WHERE n.x = 1 AND n.y = 2 RETURN n",
    "null_complete_excluded_middle": "MATCH (n:A) WHERE n.x = 1 AND n.y = 2 RETURN n",
    "pattern_order_symmetry": "MATCH (a:A), (b:B) RETURN a, b",
    "predicate_order_symmetry": "MATCH (a:A) WHERE a.x = 1 AND a.y = 2 RETURN a",
    "node_order_symmetry": "MATCH (a:A)-[r:R]-(b:B) RETURN a, b, r",
    "label_order_symmetry": "MATCH (a:A:B) RETURN a",
}


def test_every_applicable_core_rule_compiles_for_each_target_dialect() -> None:
    registry = RuleRegistry.default()
    for target in TargetSystem:
        dialect = _dialect_for(target)
        for rule in registry.rules_for(target.value, SafetyTier.CORE):
            query = _QUERIES[rule.rule_id]
            transformed = None
            for certificate in enumerate_safety_points(query, rule, "cap"):
                try:
                    transformed = apply_certified_rule(query, rule, certificate)
                    break
                except RuleNotApplicable:
                    continue
            assert transformed is not None, (target, rule.rule_id)
            assert dialect.check(PortableQuery(query)).kind is DecisionKind.ACCEPT
            assert dialect.check(PortableQuery(transformed)).kind is DecisionKind.ACCEPT


def test_system_capability_profiles_match_the_runtime_dialect_objects() -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    for target in spec.targets:
        configured = "multiple_node_labels" in spec.systems[target].capabilities
        assert _dialect_for(target).capabilities.multiple_node_labels is configured
