import pytest

from graph_sym_perf.reducer.models import (
    PartialManualReview,
    ReductionReport,
    ReductionStatus,
    RuleWitness,
)
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.models import SafetyCertificate


def _pair() -> EquivalentQueryPair:
    return EquivalentQueryPair(
        "MATCH (n) WHERE n.id = 1 AND true RETURN n.id",
        "MATCH (n) WHERE true AND n.id = 1 RETURN n.id",
        {},
        "graph",
        "dbms",
    )


def _certificate() -> SafetyCertificate:
    return SafetyCertificate("predicate_order_symmetry", "core", "v1", "where_boolean")


def test_rule_witness_binds_relation_fragments_scope_and_output_schema() -> None:
    witness = RuleWitness.create(
        rule_id="predicate_order_symmetry",
        proof_version="v1",
        relation="conjunct_order",
        required_fragments_a=("n.id = 1 AND true",),
        required_fragments_b=("true AND n.id = 1",),
        required_variables=("n",),
        output_schema=("n.id",),
    )

    assert witness.verify(_pair(), _certificate())
    assert len(witness.content_hash) == 64


def test_rule_witness_fails_closed_when_relation_or_output_is_deleted() -> None:
    witness = RuleWitness.create(
        rule_id="predicate_order_symmetry",
        proof_version="v1",
        relation="conjunct_order",
        required_fragments_a=("AND true",),
        required_fragments_b=("true AND",),
        required_variables=("n",),
        output_schema=("n.id",),
    )
    missing_relation = EquivalentQueryPair(
        "MATCH (n) WHERE n.id = 1 RETURN n.id",
        "MATCH (n) WHERE n.id = 1 RETURN n.id",
        {},
        "graph",
        "dbms",
    )
    changed_output = EquivalentQueryPair(
        _pair().query_a.replace("RETURN n.id", "RETURN count(*) AS rows"),
        _pair().query_b.replace("RETURN n.id", "RETURN count(*) AS rows"),
        {},
        "graph",
        "dbms",
    )

    assert not witness.verify(missing_relation, _certificate())
    assert not witness.verify(changed_output, _certificate())
    assert not witness.verify(_pair(), SafetyCertificate("other", "core", "v1", "where_boolean"))


def test_partial_manual_review_is_hash_bound_audit_only() -> None:
    pair = _pair()
    evidence = RuntimeEvidence(
        RuntimeVerdict.INVALID,
        "a",
        "b",
        (),
        None,
        None,
        None,
        None,
        0.0,
        None,
        ("attempt_limit",),
    )
    partial = ReductionReport(ReductionStatus.REDUCTION_PARTIAL, pair, pair, (), (), evidence)

    review = PartialManualReview.create(
        partial,
        reviewer="reviewer@example.test",
        reviewed_at="2026-07-10T23:30:00+08:00",
        reason="Attempt budget reached after preserving a smaller pair.",
    )

    assert review.verify(partial)
    assert len(review.report_hash) == 64
    with pytest.raises(ValueError, match="partial"):
        PartialManualReview.create(
            ReductionReport(ReductionStatus.REDUCED_VALID, pair, pair, (), (), evidence),
            reviewer="reviewer@example.test",
            reviewed_at="2026-07-10T23:30:00+08:00",
            reason="not allowed",
        )
