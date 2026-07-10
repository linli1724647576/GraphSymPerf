from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


def test_safety_points_enumerate_all_where_boolean_sites_not_only_first_one() -> None:
    rule = RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)[0]
    certificates = enumerate_safety_points("MATCH (n) WHERE n.a = 1 AND n.b = 2 RETURN n", rule, "cap")
    assert len(certificates) == 2
    assert all(certificate.capability_hash == "cap" for certificate in certificates)
