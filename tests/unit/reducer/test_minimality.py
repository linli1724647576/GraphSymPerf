from dataclasses import replace

from graph_sym_perf.reducer.models import MinimalityProof, MinimalityUniverse
from graph_sym_perf.runtime.models import EquivalentQueryPair


def _pair() -> EquivalentQueryPair:
    return EquivalentQueryPair(
        "MATCH (n), (m) WHERE n.x > 0 AND m.x = 1 RETURN n ORDER BY n.x",
        "MATCH (n), (m) WHERE n.x > 0 AND m.x = 1 RETURN n ORDER BY n.x",
        {},
        "graph",
        "dbms",
    )


def _full_results() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (deletion.canonical_id, "symptom_not_preserved", f"{index:x}" * 64)
        for index, deletion in enumerate(MinimalityUniverse.recompute(_pair()).deletions, start=1)
    )


def test_minimality_proof_recomputes_and_requires_the_complete_deletion_universe() -> None:
    proof = MinimalityProof.from_results(_pair(), _full_results()[:1])

    assert not proof.verify(_pair())


def test_minimality_proof_rejects_an_extra_nonexistent_deletion() -> None:
    proof = MinimalityProof.from_results(
        _pair(),
        (*_full_results(), ("a:drop_clause_99", "symptom_not_preserved", "7" * 64)),
    )

    assert not proof.verify(_pair())


def test_minimality_proof_rejects_duplicate_or_tampered_universe_entries() -> None:
    duplicate = MinimalityProof.from_results(_pair(), (*_full_results(), _full_results()[0]))
    complete = MinimalityProof.from_results(_pair(), _full_results())
    missing_universe_entry = replace(
        complete,
        enumerated_deletions=complete.enumerated_deletions[:-1],
    )

    assert not duplicate.verify(_pair())
    assert not missing_universe_entry.verify(_pair())


def test_minimality_proof_accepts_exactly_one_failed_attempt_per_recomputed_proposal() -> None:
    proof = MinimalityProof.from_results(_pair(), _full_results())

    assert proof.verify(_pair())
