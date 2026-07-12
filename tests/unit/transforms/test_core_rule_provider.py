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


def test_core_provider_falls_back_to_an_applicable_certified_rule_for_the_same_query(tmp_path) -> None:
    provider = CoreRulePairProvider(
        ("MATCH (a:A), (b:B) RETURN a, b",),
        dbms="neo4j",
        shard_index=0,
        capability_hash="cap",
        exploration_floor=1,
        epoch_size=100,
        quarantine_path=tmp_path / "quarantine.jsonl",
    )

    proposal = provider(SeedContext(1, 1, "neo4j", 0))

    assert proposal is not None
    assert proposal.rule_id == "pattern_order_symmetry"
    assert proposal.safety_certificate.rule_id == "pattern_order_symmetry"
    assert provider.not_applicable_counts["constant_true_disjunction"] == 1


def test_core_provider_can_emit_two_distinct_certified_variants_for_one_seed(tmp_path) -> None:
    source = "MATCH (a:A), (b:B) WHERE a.x = 1 AND b.y = 2 RETURN a, b"
    provider = CoreRulePairProvider(
        (source,),
        dbms="ladybugdb",
        shard_index=0,
        capability_hash="cap",
        exploration_floor=1,
        epoch_size=100,
        quarantine_path=tmp_path / "quarantine.jsonl",
    )

    proposals = provider.proposals(
        SeedContext(1, 1, "ladybugdb", 0),
        max_variants=2,
    )

    assert len(proposals) == 2
    assert {proposal.query_a.text for proposal in proposals} == {source}
    assert len({proposal.rule_id for proposal in proposals}) == 2
    assert len({proposal.pair_hash for proposal in proposals}) == 2
