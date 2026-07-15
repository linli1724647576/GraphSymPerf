from __future__ import annotations

import json
from dataclasses import replace

import pytest

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.plan_models import CampaignPhase, RulePlanEntry, RuleStream
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import RuntimePolicy
from graph_sym_perf.runtime.records import ArtifactWriteError, CompactRuntimeStore, PairRuntimeRecord
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.workload.rule_pairs import RulePairFactory


def _entry() -> RulePlanEntry:
    return RulePlanEntry(
        entry_id="p1:neo4j:discovery:constant_true_disjunction:128:0",
        phase=CampaignPhase.P1,
        target=TargetSystem.NEO4J,
        rule_id="constant_true_disjunction",
        family="bool",
        tier="core",
        graph_scale=128,
        shard_index=0,
        shard_seed=7,
        accepted_pair_quota=1,
        max_generated_seed_budget=10,
        max_variants_per_seed=1,
        proof_version="1",
        query_timeout_seconds=30,
        max_entry_runtime_seconds=600,
        measurement_protocol_id="paired-runtime-v1",
        stream=RuleStream.DISCOVERY,
    )


def _pair():
    outcome = RulePairFactory(RuleRegistry.default()).build(
        "MATCH (n) WHERE n.value = 1 RETURN n",
        _entry(),
        TargetSystem.NEO4J,
    )
    assert outcome.pair is not None
    return outcome.pair


class _RuntimeFake:
    def execute(self, query: str, parameters, **_kwargs) -> float:
        return 1.2344 if "OR true" not in query else 5.6784


def _record() -> PairRuntimeRecord:
    pair = _pair()
    evidence = RuntimeHarness().measure_pair(
        _RuntimeFake(),
        pair.query_a,
        pair.query_b,
        parameters=pair.parameters,
        policy=RuntimePolicy(warmups=0, pairs=3, seed=7),
    )
    return PairRuntimeRecord.from_evidence(
        pair=pair,
        entry=_entry(),
        evidence=evidence,
        noise_floor_id="noise-1",
        noise_p99_ms=0.2,
        dbms_fingerprint="d" * 64,
        dbms_health_epoch="e" * 64,
        source_query_ordinal=1,
        graph_fingerprint="f" * 64,
    )


def test_pair_runtime_record_stores_integer_microseconds_without_query_or_plan(tmp_path) -> None:
    store = CompactRuntimeStore(tmp_path / "pair_runtime.jsonl")

    store.append(_record())

    payload = json.loads((tmp_path / "pair_runtime.jsonl").read_text(encoding="utf-8"))
    assert payload["samples_us"][0][1:] == [1234, 5678]
    assert payload["median_a_ms"] == 1.234
    assert payload["median_b_ms"] == 5.678
    assert payload["median_delta_ms"] == 4.444
    assert payload["runtime_direction"] == "b"
    assert "query_a" not in payload and "query_b" not in payload
    assert "profile" not in payload and "plan" not in payload
    assert store.count == 1


def test_store_write_failure_does_not_increment_accepted_count(monkeypatch, tmp_path) -> None:
    store = CompactRuntimeStore(tmp_path / "pair_runtime.jsonl")
    monkeypatch.setattr(
        store,
        "_write_line",
        lambda _line: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(ArtifactWriteError, match="disk full"):
        store.append(_record())

    assert store.count == 0


def test_store_rejects_duplicate_pair_hash_without_appending(tmp_path) -> None:
    store = CompactRuntimeStore(tmp_path / "pair_runtime.jsonl")
    record = _record()
    store.append(record)

    with pytest.raises(ArtifactWriteError, match="duplicate"):
        store.append(replace(record, lifecycle_status="stage1_complete"))

    assert store.count == 1
    assert len((tmp_path / "pair_runtime.jsonl").read_text().splitlines()) == 1


def test_store_rolls_back_and_poisons_a_full_line_when_fsync_reports_failure(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "pair_runtime.jsonl"
    store = CompactRuntimeStore(path)
    real_write = store._write_line

    def write_then_fail(line: str) -> None:
        real_write(line)
        raise OSError("fsync outcome unknown")

    monkeypatch.setattr(store, "_write_line", write_then_fail)

    with pytest.raises(ArtifactWriteError, match="fsync outcome unknown"):
        store.append(_record())

    assert store.count == 0
    assert not path.exists() or path.read_text(encoding="utf-8") == ""
    assert path.with_name("pair_runtime.poison.json").exists()
    with pytest.raises(ArtifactWriteError, match="poison"):
        CompactRuntimeStore(path)


def test_runtime_write_poison_sanitizes_fsync_and_rollback_diagnostics(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "pair_runtime.jsonl"
    store = CompactRuntimeStore(path)
    monkeypatch.setattr(
        store,
        "_write_line",
        lambda _line: (_ for _ in ()).throw(
            OSError(
                '{"storage_password": "fsync-password"} '
                "Authorization: Custom Scheme fsync-auth-tail; "
                "/Users/private/runtime"
            )
        ),
    )
    monkeypatch.setattr(
        store,
        "_rollback",
        lambda _size: OSError("NEO4J_AUTH=neo4j/rollback-secret"),
    )

    with pytest.raises(ArtifactWriteError):
        store.append(_record())

    poison = path.with_name("pair_runtime.poison.json").read_text(encoding="utf-8")
    for forbidden in (
        "fsync-password",
        "fsync-auth-tail",
        "rollback-secret",
        "/Users/private",
    ):
        assert forbidden not in poison
