from graph_sym_perf.transforms.provider import CoreRulePairProvider
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.workload.models import SeedContext


def test_core_provider_reaches_each_rule_exploration_floor_without_external_feedback(tmp_path) -> None:
    queries = tuple(
        f"MATCH (a:A:B)-[r:R]-(b:B:A), (c:C) WHERE a.x = {index} AND b.y = 2 RETURN a, b, c, r"
        for index in range(180)
    )
    provider = CoreRulePairProvider(
        queries,
        dbms="neo4j",
        shard_index=0,
        capability_hash="cap",
        exploration_floor=20,
        epoch_size=100,
        quarantine_path=tmp_path / "quarantine.jsonl",
    )

    for ordinal in range(1, 181):
        proposal = provider(SeedContext(ordinal, ordinal, "neo4j", 0))
        assert proposal is not None
        provider.record_accepted(proposal.rule_id)

    expected = {rule.rule_id for rule in RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)}
    assert provider.accepted_counts == {rule_id: 20 for rule_id in expected}


def test_core_provider_quarantines_dynamic_equivalence_mismatch(tmp_path) -> None:
    provider = CoreRulePairProvider(
        ("MATCH (n:A) WHERE n.x = 1 RETURN n",),
        dbms="neo4j",
        shard_index=0,
        capability_hash="cap",
        exploration_floor=20,
        epoch_size=100,
        quarantine_path=tmp_path / "quarantine.jsonl",
    )
    proposal = provider(SeedContext(1, 1, "neo4j", 0))
    assert proposal is not None

    provider.quarantine_mismatch(proposal, reason="TYPED_EQUIVALENCE_FAILED")

    assert proposal.rule_id not in provider.active_rule_ids
    assert provider.quarantine_store.is_quarantined("neo4j", proposal.rule_id, "1")
