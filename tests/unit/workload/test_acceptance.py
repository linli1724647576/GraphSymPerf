from __future__ import annotations

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.plan_models import CampaignPhase, RulePlanEntry, RuleStream
from graph_sym_perf.generation.corpus import PortableQuery
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.workload.acceptance import AcceptedPairBuilder
from graph_sym_perf.workload.models import (
    AcceptanceStatus,
    AcceptedPairRequest,
    PairProposal,
    PairValidation,
)
from graph_sym_perf.workload.rule_pairs import RulePairFactory


_ACCEPTANCE_FIXTURE_RULE_ID = "duplicate_predicate_control"


def _entry(
    *,
    rule_id: str,
    family: str,
    tier: str = "core",
    stream: RuleStream = RuleStream.DISCOVERY,
) -> RulePlanEntry:
    return RulePlanEntry(
        entry_id=f"p1:neo4j:discovery:{rule_id}:128:0",
        phase=CampaignPhase.P1,
        target=TargetSystem.NEO4J,
        rule_id=rule_id,
        family=family,
        tier=tier,
        graph_scale=128,
        shard_index=0,
        shard_seed=7,
        accepted_pair_quota=1,
        max_generated_seed_budget=10,
        max_variants_per_seed=1,
        proof_version="1",
        query_timeout_seconds=30,
        max_entry_runtime_seconds=3600,
        measurement_protocol_id="paired-runtime-v1",
        stream=stream,
    )


def test_rule_pair_factory_uses_the_entry_rule_not_a_worker_constant() -> None:
    factory = RulePairFactory(RuleRegistry.default())
    query = "MATCH (a:L0), (b:L1) WHERE a.id = 1 RETURN a.id, b.id"

    bool_result = factory.build(
        query,
        _entry(rule_id="null_partition_tautology", family="bool"),
        TargetSystem.NEO4J,
    )
    sym_result = factory.build(
        query,
        _entry(rule_id="pattern_order_symmetry", family="sym"),
        TargetSystem.NEO4J,
    )

    assert bool_result.pair is not None
    assert sym_result.pair is not None
    assert bool_result.pair.certificate.canonical_rule_id == "null_partition_tautology"
    assert sym_result.pair.certificate.canonical_rule_id == "pattern_order_symmetry"
    assert bool_result.pair.query_b != sym_result.pair.query_b
    assert bool_result.pair.transform_metadata["semantic_proof_sha256"]
    assert sym_result.pair.transform_metadata["semantic_proof_sha256"]


def test_rule_pair_factory_quarantines_entry_proof_version_drift() -> None:
    entry = _entry(rule_id="null_partition_tautology", family="bool")
    drifted = RulePlanEntry(**{**entry.__dict__, "proof_version": "stale"})

    result = RulePairFactory(RuleRegistry.default()).build(
        "MATCH (n) WHERE n.id = 1 RETURN n",
        drifted,
        TargetSystem.NEO4J,
    )

    assert result.pair is None
    assert result.quarantine is True
    assert result.reject_code == "RULE_PLAN_INVARIANT_MISMATCH"


def test_rule_pair_factory_executes_a_control_rule_without_entering_core_selection() -> None:
    result = RulePairFactory(RuleRegistry.default()).build(
        "MATCH (a)-[r:R]->(b) RETURN a, b, r",
        _entry(
            rule_id="edge_notation_symmetry",
            family="sym",
            tier="control",
            stream=RuleStream.CONTROL,
        ),
        TargetSystem.NEO4J,
    )

    assert result.pair is not None
    assert result.pair.certificate.canonical_rule_id == "edge_notation_symmetry"
    assert result.pair.certificate.submit_eligible is False
    assert result.pair.transform_metadata["semantic_proof_sha256"]


def _legacy_certificate(rule_id: str) -> SafetyCertificate:
    """Build a valid legacy certificate for acceptance-builder isolation tests."""

    rule = RuleRegistry.default().get(rule_id)
    return SafetyCertificate(
        rule.rule_id,
        "clauses[0].where",
        (),
        "boolean",
        "three_valued",
        "where",
        rule.purity_requirements,
        rule.proof_id,
        "cap",
    )


def _proposal(context, suffix: str = "") -> PairProposal:
    return PairProposal(
        query_a=PortableQuery(f"MATCH (n:L{context.ordinal}{suffix}) RETURN n"),
        query_b=PortableQuery(f"MATCH (n:L{context.ordinal}{suffix}) WHERE true RETURN n"),
        rule_id=_ACCEPTANCE_FIXTURE_RULE_ID,
        safety_certificate=_legacy_certificate(_ACCEPTANCE_FIXTURE_RULE_ID),
        seed=context.seed,
    )


def test_accepted_builder_tops_up_until_exact_pair_budget_after_rejections() -> None:
    def provider(context):
        return _proposal(context) if context.ordinal % 2 else None

    def validator(proposal):
        if proposal.query_a.text.endswith("L3) RETURN n"):
            return PairValidation(False, "TYPED_EQUIVALENCE_FAILED")
        return PairValidation(True, "COMPLETE_TYPED_EQUIVALENT", {"rows": 1})

    corpus = AcceptedPairBuilder(provider, validator).build(
        AcceptedPairRequest(
            accepted_pairs=3,
            max_generated_seeds=10,
            experiment_seed=9,
            target="neo4j",
            shard_index=2,
        )
    )

    assert corpus.status is AcceptanceStatus.COMPLETE
    assert corpus.accepted_count == 3
    assert corpus.generated_seeds == 7
    assert corpus.reject_counts == {"RULE_NOT_APPLICABLE": 3, "TYPED_EQUIVALENCE_FAILED": 1}


def test_builder_rejects_cross_shard_ast_and_pair_duplicates() -> None:
    first = _proposal(type("Context", (), {"ordinal": 1, "seed": 11})())
    request = AcceptedPairRequest(
        accepted_pairs=1,
        max_generated_seeds=2,
        experiment_seed=1,
        target="neo4j",
        shard_index=0,
        cross_shard_ast_hashes=frozenset({first.query_a.ast_hash}),
        cross_shard_pair_hashes=frozenset(),
    )
    corpus = AcceptedPairBuilder(lambda context: first, lambda proposal: PairValidation(True, "OK")).build(request)

    assert corpus.status is AcceptanceStatus.INCOMPLETE
    assert corpus.accepted_count == 0
    assert corpus.reject_counts == {"CROSS_SHARD_DUPLICATE_AST": 2}


def test_builder_returns_incomplete_at_seed_or_time_budget() -> None:
    ticks = iter((0.0, 0.0, 6.1, 6.1))
    builder = AcceptedPairBuilder(
        lambda context: _proposal(context),
        lambda proposal: PairValidation(True, "OK"),
        clock=lambda: next(ticks),
    )
    corpus = builder.build(
        AcceptedPairRequest(
            accepted_pairs=2,
            max_generated_seeds=10,
            max_seconds=6.0,
            experiment_seed=1,
            target="memgraph",
            shard_index=1,
        )
    )

    assert corpus.status is AcceptanceStatus.INCOMPLETE
    assert corpus.stop_reason == "TIME_BUDGET_EXHAUSTED"
    assert corpus.accepted_count == 1


def test_builder_requires_safety_certificate_before_validation() -> None:
    proposal = PairProposal(
        PortableQuery("MATCH (n) RETURN n"),
        PortableQuery("MATCH (n) WHERE true RETURN n"),
        "fake",
        None,
        1,
    )
    called = False

    def validator(_proposal):
        nonlocal called
        called = True
        return PairValidation(True, "OK")

    corpus = AcceptedPairBuilder(lambda context: proposal, validator).build(
        AcceptedPairRequest(1, 1, experiment_seed=1, target="neo4j", shard_index=0)
    )

    assert corpus.reject_counts == {"SAFETY_CERTIFICATE_MISSING": 1}
    assert called is False


def test_builder_does_not_revalidate_a_rejected_ast() -> None:
    proposal = _proposal(type("Context", (), {"ordinal": 1, "seed": 11})())
    calls = 0

    def validator(_proposal):
        nonlocal calls
        calls += 1
        return PairValidation(False, "TYPED_EQUIVALENCE_FAILED")

    corpus = AcceptedPairBuilder(lambda _context: proposal, validator).build(
        AcceptedPairRequest(1, 3, experiment_seed=1, target="neo4j", shard_index=0)
    )

    assert calls == 1
    assert corpus.reject_counts == {"DUPLICATE_AST": 2, "TYPED_EQUIVALENCE_FAILED": 1}


def test_builder_accepts_two_certified_variants_from_one_generated_seed() -> None:
    source = PortableQuery("MATCH (n:L1) WHERE n.x = 1 AND n.y = 2 RETURN n")

    class TwoVariantProvider:
        def proposals(self, context, *, max_variants):
            assert max_variants == 2
            return (
                PairProposal(
                    source,
                    PortableQuery("MATCH (n:L1) WHERE (n.x = 1 AND n.y = 2) OR true RETURN n"),
                    "constant_true_disjunction",
                    _legacy_certificate("constant_true_disjunction"),
                    context.seed,
                ),
                PairProposal(
                    source,
                    PortableQuery("MATCH (n:L1) WHERE n.y = 2 AND n.x = 1 RETURN n"),
                    "predicate_order_symmetry",
                    _legacy_certificate("predicate_order_symmetry"),
                    context.seed,
                ),
            )

    corpus = AcceptedPairBuilder(
        TwoVariantProvider(),
        lambda proposal: PairValidation(True, "COMPLETE_TYPED_EQUIVALENT"),
    ).build(
        AcceptedPairRequest(
            accepted_pairs=2,
            max_generated_seeds=2,
            max_variants_per_seed=2,
            experiment_seed=1,
            target="ladybugdb",
            shard_index=0,
        )
    )

    assert corpus.status is AcceptanceStatus.COMPLETE
    assert corpus.accepted_count == 2
    assert corpus.generated_seeds == 1
    assert len({pair.proposal.pair_hash for pair in corpus.pairs}) == 2
