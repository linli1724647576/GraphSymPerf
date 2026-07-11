import pytest

from graph_sym_perf.sampling.adaptive import AdaptiveRuleSampler
from graph_sym_perf.sampling.yield_store import RuleQuarantineStore, ValidatedYieldStore, YieldSnapshot


def test_sampler_uses_exploration_floor_then_validated_yield_only() -> None:
    sampler = AdaptiveRuleSampler(("r1", "r2"), exploration_floor=2, epoch_size=100)
    assert sampler.select("neo4j", 0, 0, YieldSnapshot({"r1": (0, 0), "r2": (0, 0)})).rule_id == "r1"
    with pytest.raises(ValueError):
        sampler.select("neo4j", 0, 100, {"r1": {"accepted": 1, "profile": 99}})  # type: ignore[arg-type]


def test_sampler_explores_every_rule_floor_and_freezes_epoch_identity() -> None:
    sampler = AdaptiveRuleSampler(("r1", "r2"), exploration_floor=2, epoch_size=100)
    selection = sampler.select("neo4j", 3, 17, YieldSnapshot({"r1": (2, 0), "r2": (1, 1)}))
    assert selection.rule_id == "r2"
    assert selection.phase == "exploration_floor"

    before = sampler.select("neo4j", 3, 98, YieldSnapshot({"r1": (50, 5), "r2": (49, 10)}))
    after = sampler.select("neo4j", 3, 100, YieldSnapshot({"r1": (50, 5), "r2": (50, 10)}))
    assert before.epoch == 0
    assert after.epoch == 1
    assert before.snapshot_hash != after.snapshot_hash


def test_yield_snapshot_rejects_non_runtime_outcome_fields() -> None:
    for forbidden in ("plan", "profile", "pmc", "developer_status"):
        with pytest.raises(ValueError, match="forbidden"):
            YieldSnapshot({forbidden: (1, 1)})


def test_quarantine_is_versioned_per_dbms_and_persists_counterexample(tmp_path) -> None:
    store = RuleQuarantineStore(tmp_path / "quarantine.jsonl")
    store.quarantine(
        dbms="neo4j",
        rule_id="predicate_order_symmetry",
        rule_version="2",
        counterexample={"query_a_hash": "a" * 64, "query_b_hash": "b" * 64},
    )

    assert store.is_quarantined("neo4j", "predicate_order_symmetry", "2")
    assert not store.is_quarantined("memgraph", "predicate_order_symmetry", "2")
    assert store.records()[0]["counterexample"]["query_a_hash"] == "a" * 64


def test_validated_yield_store_accepts_only_completed_validation_evidence(tmp_path) -> None:
    store = ValidatedYieldStore(tmp_path / "validated_yield.jsonl")
    store.record_accepted_pair(rule_id="r1", accepted_pair_hash="1" * 64)
    store.record_accepted_pair(rule_id="r1", accepted_pair_hash="2" * 64)
    store.record_accepted_pair(rule_id="r2", accepted_pair_hash="3" * 64)
    store.record_completed_validation(rule_id="r1", passed=True, validation_report_hash="a" * 64)
    store.record_completed_validation(rule_id="r1", passed=False, validation_report_hash="b" * 64)
    store.record_completed_validation(rule_id="r2", passed=True, validation_report_hash="c" * 64)

    snapshot = store.snapshot(("r1", "r2"))
    assert snapshot.counts == {"r1": (2, 1), "r2": (1, 1)}
    with pytest.raises(ValueError, match="report hash"):
        store.record_completed_validation(rule_id="r1", passed=True, validation_report_hash="not-complete")
