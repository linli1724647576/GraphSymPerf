from graph_sym_perf.reducer.ddmin import (
    DDMinCandidate,
    DDMinEvaluation,
    HierarchicalDDMin,
    ddmin,
    reduction_cache_key,
)
from graph_sym_perf.runtime.models import EquivalentQueryPair


def test_ddmin_finds_one_minimal_subset_under_symptom_predicate() -> None:
    result = ddmin(("a", "keep", "b", "c"), lambda candidate: "keep" in candidate)
    assert result == ("keep",)


def test_reload_exception_advances_monotonic_attempt_index() -> None:
    candidates = (
        DDMinCandidate("reload", "bad", "clause"),
        DDMinCandidate("usable", "good", "clause"),
    )

    def evaluate(value: str) -> DDMinEvaluation:
        if value == "bad":
            raise RuntimeError("reload failed")
        return DDMinEvaluation(True, "symptom_preserved", "a" * 64)

    result = HierarchicalDDMin(max_attempts=10, deadline_seconds=30).run(
        "original",
        proposals=lambda _value: candidates,
        evaluate=evaluate,
        cache_key=lambda value: value,
    )

    assert tuple(attempt.index for attempt in result.attempts) == (1, 2)
    assert len({attempt.attempt_id for attempt in result.attempts}) == 2
    assert result.attempts[0].outcome == "evaluation_error:RuntimeError"
    assert result.best == "good"


def test_deadline_returns_non_exhaustive_result_without_evaluating_late_candidate() -> None:
    ticks = iter((0.0, 2.0))
    evaluated: list[str] = []
    result = HierarchicalDDMin(max_attempts=10, deadline_seconds=1, clock=lambda: next(ticks)).run(
        "original",
        proposals=lambda _value: (DDMinCandidate("late", "candidate", "clause"),),
        evaluate=lambda value: evaluated.append(value) or DDMinEvaluation(True, "symptom_preserved", "b" * 64),
        cache_key=lambda value: value,
    )

    assert not result.exhausted
    assert result.stop_reason == "deadline"
    assert result.attempts == ()
    assert evaluated == []


def test_cache_key_binds_graph_both_queries_and_policy() -> None:
    pair = EquivalentQueryPair("RETURN 1", "RETURN 2", {}, "graph", "dbms")
    baseline = reduction_cache_key(pair, policy_hash="policy")

    assert baseline != reduction_cache_key(EquivalentQueryPair("RETURN 0", pair.query_b, {}, "graph", "dbms"), policy_hash="policy")
    assert baseline != reduction_cache_key(EquivalentQueryPair(pair.query_a, "RETURN 3", {}, "graph", "dbms"), policy_hash="policy")
    assert baseline != reduction_cache_key(EquivalentQueryPair(pair.query_a, pair.query_b, {}, "other", "dbms"), policy_hash="policy")
    assert baseline != reduction_cache_key(pair, policy_hash="other")
