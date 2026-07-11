from __future__ import annotations

from graph_sym_perf.generation.corpus import PortableQuery
from graph_sym_perf.workload.acceptance import AcceptedPairBuilder
from graph_sym_perf.workload.models import (
    AcceptanceStatus,
    AcceptedPairRequest,
    PairProposal,
    PairValidation,
)
from graph_sym_perf.transforms.safety_points import SafetyCertificate


def _proposal(context, suffix: str = "") -> PairProposal:
    return PairProposal(
        query_a=PortableQuery(f"MATCH (n:L{context.ordinal}{suffix}) RETURN n"),
        query_b=PortableQuery(f"MATCH (n:L{context.ordinal}{suffix}) WHERE true RETURN n"),
        rule_id="fake_core_rule",
        safety_certificate=SafetyCertificate(
            "fake_core_rule", "clauses[0].where", (), "boolean", "three_valued", "where",
            ("pure",), "fake-proof", "cap",
        ),
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
