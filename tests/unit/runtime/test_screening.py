from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
import graph_sym_perf.runtime.screening as screening_module

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


def test_screening_policy_is_immutable_and_drives_k_and_stage2_thresholds() -> None:
    policy_type = screening_module.ScreeningPolicy
    default = screening_module.DEFAULT_SCREENING_POLICY
    policy = replace(
        default,
        stage1_top_fraction=1.0,
        stage1_min_top_k=1,
        stage1_max_top_k=10,
        stage2_relative_ratio=2.0,
    )

    assert stage1_k(4, policy=policy) == 4
    decision = evaluate_stage2(
        _evidence(
            ratio=1.6,
            ratio_ci_low=1.2,
            delta=8.0,
            delta_ci_low=1.0,
            direction=0.83,
        ),
        noise_p99_ms=5.0,
        policy=policy,
    )
    assert not decision.relative_pass
    assert policy_type.__dataclass_params__.frozen is True
    with pytest.raises(Exception):
        policy.stage2_relative_ratio = 1.0


def test_screening_policy_cannot_construct_a_four_queue_variant() -> None:
    with pytest.raises(TypeError):
        screening_module.ScreeningPolicy(stage1_queue_count=4)
    assert screening_module.DEFAULT_SCREENING_POLICY.stage1_queue_count == 3
    assert (
        screening_module.DEFAULT_SCREENING_POLICY.to_payload()["stage1"][
            "queue_count"
        ]
        == 3
    )


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


def test_stage1_top_k_accepts_zero_noise_and_keeps_deterministic_ranking() -> None:
    records = _rank_fixture_records(200)

    selected = stage1_top_k(records, noise_p99_ms=0.0)

    assert len(selected) == 10
    assert selected == stage1_top_k(tuple(reversed(records)), noise_p99_ms=0.0)
    assert _pair("largest_abs") in selected
    assert _pair("largest_ratio_above_half_noise") in selected
    assert _pair("largest_noise_normalized") in selected


@pytest.mark.parametrize("noise", (-1.0, float("nan"), float("inf")))
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


def test_stage2_uses_raw_zero_noise_for_gates() -> None:
    decision = evaluate_stage2(
        _evidence(
            ratio=1.5,
            ratio_ci_low=1.100_001,
            delta=0.001,
            delta_ci_low=-0.1,
            direction=0.75,
        ),
        noise_p99_ms=0.0,
    )

    assert decision.relative_pass
    assert not decision.absolute_pass


@pytest.mark.parametrize("noise", (-1.0, float("nan"), float("inf")))
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


def _second_timeout_record() -> TimeoutQueueRecord:
    return replace(
        _timeout_record(),
        pair_hash="7" * 64,
        evidence_digest="8" * 64,
        stage="stage1",
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


def test_timeout_queue_rejects_a_failed_pending_head_write(tmp_path, monkeypatch) -> None:
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


def test_timeout_queue_reconcile_allows_only_an_exact_expected_subset(tmp_path) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)
    first = _timeout_record()
    second = _second_timeout_record()
    store.append(first)

    store.reconcile((first, second))

    assert store.count == 2
    assert {record["dedupe_key"] for record in store.records} == {
        first.dedupe_key,
        second.dedupe_key,
    }
    reopened = DurableTimeoutQueue(path)
    assert reopened.count == 2


def test_timeout_queue_reconcile_rejects_foreign_or_contradictory_evidence(
    tmp_path,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    expected = _timeout_record()
    foreign = _second_timeout_record()
    store = DurableTimeoutQueue(path)
    store.append(foreign)

    with pytest.raises(ArtifactIntegrityError, match="expected|foreign|contradictory"):
        store.reconcile((expected,))


@pytest.mark.parametrize("write_shape", ("partial", "full"))
def test_timeout_queue_unknown_write_outcome_is_resolved_from_pending_head(
    tmp_path,
    monkeypatch,
    write_shape,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)

    def write_then_fail(line: str) -> None:
        if write_shape == "full":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(line + "\n", encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(line[: len(line) // 2], encoding="utf-8")
        raise OSError(f"{write_shape} write outcome unknown")

    monkeypatch.setattr(store, "_write_line", write_then_fail, raising=False)

    with pytest.raises(ArtifactWriteError, match="outcome unknown"):
        store.append(_timeout_record())

    head = json.loads(store.head_path.read_text(encoding="utf-8"))
    assert head["state"] == "pending"
    assert head["content_hash"] == _sha256_payload(
        {key: value for key, value in head.items() if key != "content_hash"}
    )
    assert store.count == 0
    if write_shape == "partial":
        with pytest.raises(ArtifactIntegrityError, match="truncated|head"):
            DurableTimeoutQueue(path)
    else:
        assert DurableTimeoutQueue(path).count == 1


def test_timeout_queue_head_tamper_remains_fail_closed(tmp_path) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    head_path = path.with_name("timeout_queue.head.json")
    head_path.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="head"):
        DurableTimeoutQueue(path)


def test_timeout_queue_pending_head_failure_writes_no_queue_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)
    monkeypatch.setattr(
        store,
        "_write_head",
        lambda _body: (_ for _ in ()).throw(OSError("head disk full")),
    )

    with pytest.raises(ArtifactWriteError, match="head disk full"):
        store.append(_timeout_record())

    assert not path.exists() or path.read_bytes() == b""


def test_timeout_queue_writes_pending_head_before_line_and_keeps_committed_head(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)
    real_write = store._write_line

    def observe_pending(line: str) -> None:
        head = json.loads(store.head_path.read_text(encoding="utf-8"))
        assert head["state"] == "pending"
        assert head["record_count"] == 0
        assert head["new_record_key"] == _timeout_record().dedupe_key
        real_write(line)

    monkeypatch.setattr(store, "_write_line", observe_pending)

    assert store.append(_timeout_record())
    committed = json.loads(store.head_path.read_text(encoding="utf-8"))
    assert committed["state"] == "committed"
    assert committed["record_count"] == 1
    assert committed["head_record_key"] == _timeout_record().dedupe_key
    assert DurableTimeoutQueue(path).count == 1


def test_timeout_queue_recovers_pending_before_line_to_committed_empty(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)

    class SimulatedCrash(BaseException):
        pass

    monkeypatch.setattr(
        store,
        "_write_line",
        lambda _line: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        store.append(_timeout_record())

    recovered = DurableTimeoutQueue(path)
    head = json.loads(recovered.head_path.read_text(encoding="utf-8"))
    assert recovered.count == 0
    assert head["state"] == "committed"
    assert head["record_count"] == 0


def test_timeout_queue_recovers_pending_with_exact_new_tail(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)

    class SimulatedCommitCrash(BaseException):
        pass

    monkeypatch.setattr(
        store,
        "_commit_head",
        lambda **_kwargs: (_ for _ in ()).throw(SimulatedCommitCrash()),
        raising=False,
    )
    with pytest.raises(SimulatedCommitCrash):
        store.append(_timeout_record())

    recovered = DurableTimeoutQueue(path)
    head = json.loads(recovered.head_path.read_text(encoding="utf-8"))
    assert recovered.count == 1
    assert head["state"] == "committed"
    assert head["record_count"] == 1


def test_timeout_queue_rejects_committed_head_mismatch_and_missing_nonempty_head(
    tmp_path,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)
    store.append(_timeout_record())
    head = json.loads(store.head_path.read_text(encoding="utf-8"))
    head["record_count"] = 2
    body = {key: value for key, value in head.items() if key != "content_hash"}
    head["content_hash"] = _sha256_payload(body)
    store.head_path.write_text(json.dumps(head) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="head|count|digest"):
        DurableTimeoutQueue(path)

    store.head_path.unlink()
    with pytest.raises(ArtifactIntegrityError, match="head|marker"):
        DurableTimeoutQueue(path)


def test_timeout_queue_rejects_valid_extra_line_beyond_committed_head(tmp_path) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)
    store.append(_timeout_record())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_second_timeout_record().to_payload()) + "\n")

    with pytest.raises(ArtifactIntegrityError, match="head|count|digest"):
        DurableTimeoutQueue(path)


def test_timeout_queue_commit_transition_fsync_failure_reopens_from_new_head(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "timeout_queue.jsonl"
    store = DurableTimeoutQueue(path)
    real_fsync_directory = screening_module._fsync_directory

    def fail_after_committed_replace(directory) -> None:
        if store.head_path.exists():
            head = json.loads(store.head_path.read_text(encoding="utf-8"))
            if head.get("state") == "committed":
                raise OSError("committed head directory fsync failed")
        real_fsync_directory(directory)

    monkeypatch.setattr(
        screening_module,
        "_fsync_directory",
        fail_after_committed_replace,
    )
    with pytest.raises(ArtifactWriteError, match="committed head directory fsync failed"):
        store.append(_timeout_record())

    monkeypatch.setattr(screening_module, "_fsync_directory", real_fsync_directory)
    assert DurableTimeoutQueue(path).count == 1


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
