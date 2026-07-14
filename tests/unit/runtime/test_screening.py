from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from graph_sym_perf.runtime.models import PairedSample, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.runtime.records import PairRuntimeRecord
from graph_sym_perf.runtime.records import ArtifactIntegrityError, ArtifactWriteError
from graph_sym_perf.runtime.screening import (
    DurableTimeoutQueue,
    TimeoutQueueRecord,
    evaluate_stage2,
    stage1_k,
    stage1_top_k,
)


def _pair(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _record(
    label: str,
    *,
    delta: float,
    ratio: float,
    noise_score: float,
    timeout_state: str = "none",
) -> PairRuntimeRecord:
    pair_hash = _pair(label)
    return PairRuntimeRecord(
        pair_hash=pair_hash,
        rule_id="pattern_order_symmetry",
        family="sym",
        stream="discovery",
        phase="p1",
        graph_scale=256,
        shard_seed=7,
        dbms_fingerprint="d" * 64,
        dbms_health_epoch="e" * 64,
        query_a_hash="a" * 64,
        query_b_hash="b" * 64,
        site_class="ordinary_match_comma_pattern_list",
        proof_version="1",
        safety_certificate_hash="c" * 64,
        equivalence_summary="complete_typed_equivalent",
        noise_floor_id="f" * 64,
        samples_us=(("AB", 1_000, int((1.0 + delta) * 1_000)),),
        median_a_ms=1.0,
        median_b_ms=1.0 + delta,
        median_delta_ms=delta,
        median_ratio=ratio,
        runtime_direction="b",
        delta_over_noise=noise_score,
        direction_consistency=1.0,
        timeout_state=timeout_state,
        screen_stage="stage1",
        protocol_version="validated-yield-v1",
        lifecycle_status="stage1_recorded",
        source_query_ordinal=1,
        graph_fingerprint="g" * 64,
    )


def _rank_fixture_records(accepted: int) -> tuple[PairRuntimeRecord, ...]:
    records = [
        _record(
            f"ordinary-{index:04d}",
            delta=1.1 + index / 1000,
            ratio=1.1 + index / 10_000,
            noise_score=1.1 + index / 1000,
        )
        for index in range(accepted - 3)
    ]
    records.extend(
        (
            _record("largest_abs", delta=100.0, ratio=1.2, noise_score=2.0),
            _record(
                "largest_ratio_above_half_noise",
                delta=1.0,
                ratio=50.0,
                noise_score=0.5,
            ),
            _record("largest_noise_normalized", delta=5.0, ratio=1.3, noise_score=100.0),
        )
    )
    return tuple(records)


@pytest.mark.parametrize("accepted, expected", ((50, 3), (200, 10), (1000, 30)))
def test_k1_formula(accepted: int, expected: int) -> None:
    assert stage1_k(accepted) == expected


def test_stage1_top_k_uses_abs_ratio_and_noise_queues() -> None:
    selected = stage1_top_k(_rank_fixture_records(200), noise_p99_ms=2.0)

    assert len(selected) == 10
    assert _pair("largest_abs") in selected
    assert _pair("largest_ratio_above_half_noise") in selected
    assert _pair("largest_noise_normalized") in selected
    assert len(set(selected)) == len(selected)


def test_stage1_top_k_is_deterministic_and_breaks_score_ties_by_pair_hash() -> None:
    records = tuple(
        _record(label, delta=20.0, ratio=4.0, noise_score=10.0)
        for label in ("zeta", "alpha", "gamma", "beta")
    )

    forward = stage1_top_k(records, noise_p99_ms=2.0)
    reversed_input = stage1_top_k(tuple(reversed(records)), noise_p99_ms=2.0)

    assert forward == reversed_input
    assert forward == tuple(sorted((_pair(label) for label in ("zeta", "alpha", "gamma", "beta")))[:3])


def test_stage1_timeout_records_are_not_ranked_as_ordinary_runtime() -> None:
    timeout = _record(
        "timeout",
        delta=1_000.0,
        ratio=1_000.0,
        noise_score=1_000.0,
        timeout_state="b_timeout",
    )
    ordinary = tuple(
        _record(f"ordinary-{index}", delta=10.0 - index, ratio=2.0, noise_score=5.0)
        for index in range(4)
    )

    selected = stage1_top_k((timeout, *ordinary), noise_p99_ms=2.0)

    assert timeout.pair_hash not in selected


@pytest.mark.parametrize("noise", (0.0, -1.0, float("nan"), float("inf")))
def test_stage1_top_k_rejects_an_invalid_noise_floor(noise: float) -> None:
    with pytest.raises(ValueError, match="noise_p99_ms"):
        stage1_top_k((_record("record", delta=1.0, ratio=2.0, noise_score=1.0),), noise_p99_ms=noise)


def test_stage1_top_k_fails_closed_on_nonfinite_record_scores() -> None:
    record = replace(
        _record("record", delta=1.0, ratio=2.0, noise_score=1.0),
        median_ratio=float("nan"),
    )

    with pytest.raises(ValueError, match="finite"):
        stage1_top_k((record,), noise_p99_ms=2.0)


def _evidence(
    *,
    ratio: float,
    ratio_ci_low: float,
    delta: float,
    delta_ci_low: float,
    direction: float,
    reason_codes: tuple[str, ...] = (),
) -> RuntimeEvidence:
    return RuntimeEvidence(
        verdict=RuntimeVerdict.PASS,
        query_a_hash="a" * 64,
        query_b_hash="b" * 64,
        paired_samples=tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 10.0 + delta) for index in range(12)),
        median_ratio=ratio,
        median_delta_ms=delta,
        ci_ratio_low=ratio_ci_low,
        ci_ratio_high=ratio + 0.2,
        direction_consistency=direction,
        relative_mad=0.01,
        reason_codes=reason_codes,
        slow_query="b",
        median_a_ms=10.0,
        median_b_ms=10.0 + delta,
        slower_side="b",
        ci_delta_low_ms=delta_ci_low,
        ci_delta_high_ms=delta + 1.0,
    )


def test_stage2_relative_wide_gate() -> None:
    decision = evaluate_stage2(
        _evidence(ratio=1.6, ratio_ci_low=1.2, delta=8.0, delta_ci_low=1.0, direction=0.83),
        noise_p99_ms=5.0,
    )

    assert decision.relative_pass and not decision.absolute_pass
    assert decision.promote


def test_stage2_absolute_wide_gate() -> None:
    decision = evaluate_stage2(
        _evidence(ratio=1.2, ratio_ci_low=1.05, delta=25.0, delta_ci_low=4.0, direction=0.92),
        noise_p99_ms=8.0,
    )

    assert decision.absolute_pass and not decision.relative_pass
    assert decision.promote


def test_stage2_rejects_large_ratio_inside_noise() -> None:
    decision = evaluate_stage2(
        _evidence(ratio=10.0, ratio_ci_low=2.0, delta=0.1, delta_ci_low=-0.1, direction=0.9),
        noise_p99_ms=1.0,
    )

    assert not decision.promote


@pytest.mark.parametrize(
    ("changes", "relative", "absolute"),
    (
        ({"ratio": 1.5, "ratio_ci_low": 1.100_001, "delta": 5.001, "direction": 0.75}, True, False),
        ({"ratio": 1.5, "ratio_ci_low": 1.1, "delta": 5.001, "direction": 0.75}, False, False),
        ({"ratio": 1.100_001, "delta": 10.0, "delta_ci_low": 0.001, "direction": 0.75}, False, True),
        ({"ratio": 1.1, "delta": 10.0, "delta_ci_low": 0.001, "direction": 0.75}, False, False),
    ),
)
def test_stage2_gate_operators_are_exact(
    changes: dict[str, float],
    relative: bool,
    absolute: bool,
) -> None:
    values = {
        "ratio": 1.2,
        "ratio_ci_low": 1.05,
        "delta": 9.0,
        "delta_ci_low": -0.1,
        "direction": 0.75,
        **changes,
    }

    decision = evaluate_stage2(_evidence(**values), noise_p99_ms=5.0)

    assert decision.relative_pass is relative
    assert decision.absolute_pass is absolute


def test_stage2_rejects_a_direction_change_from_stage1() -> None:
    decision = evaluate_stage2(
        _evidence(
            ratio=5.0,
            ratio_ci_low=4.0,
            delta=50.0,
            delta_ci_low=40.0,
            direction=1.0,
            reason_codes=("direction_mismatch",),
        ),
        noise_p99_ms=1.0,
    )

    assert not decision.promote
    assert "stage1_direction_changed" in decision.reason_codes


@pytest.mark.parametrize("noise", (0.0, float("nan"), float("inf")))
def test_stage2_rejects_an_invalid_noise_floor(noise: float) -> None:
    with pytest.raises(ValueError, match="noise_p99_ms"):
        evaluate_stage2(
            _evidence(ratio=2.0, ratio_ci_low=1.5, delta=10.0, delta_ci_low=5.0, direction=1.0),
            noise_p99_ms=noise,
        )


def _timeout_record() -> TimeoutQueueRecord:
    return TimeoutQueueRecord(
        entry_id="p1:neo4j:discovery:rule:256:0",
        pair_hash="1" * 64,
        timeout_side="b",
        stage="stage2",
        evidence_digest="2" * 64,
        graph_snapshot_hash="3" * 64,
        dbms_fingerprint="4" * 64,
        dbms_health_epoch="5" * 64,
        noise_floor_id="6" * 64,
    )


def test_timeout_queue_is_durable_and_exactly_deduplicated(tmp_path) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)

    assert store.append(_timeout_record())
    assert not store.append(_timeout_record())
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    loaded = DurableTimeoutQueue(path)
    assert loaded.count == 1
    assert loaded.records[0]["dedupe_key"] == _timeout_record().dedupe_key


def test_timeout_queue_rejects_a_truncated_tail(tmp_path) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    DurableTimeoutQueue(path).append(_timeout_record())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"truncated"')

    with pytest.raises(ArtifactIntegrityError, match="truncated"):
        DurableTimeoutQueue(path)


def test_timeout_queue_rolls_back_a_failed_append(tmp_path, monkeypatch) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)
    real_fsync = __import__("os").fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk sync failed")
        real_fsync(descriptor)

    monkeypatch.setattr("graph_sym_perf.runtime.screening.os.fsync", fail_first_fsync)

    with pytest.raises(ArtifactWriteError, match="disk sync failed"):
        store.append(_timeout_record())

    assert not path.exists() or path.read_bytes() == b""
    assert store.count == 0
