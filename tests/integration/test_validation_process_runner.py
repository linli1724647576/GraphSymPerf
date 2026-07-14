import os
import time
from pathlib import Path

import pytest

from graph_sym_perf.adapters.base import DBMSFingerprint
from graph_sym_perf.core import ExecutionResult, QueryTimeoutError, TargetSystem
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import CandidateBundle, SafetyCertificate, ValidationPolicy
from graph_sym_perf.validation.session_runner import (
    SpawnedValidationSessionRunner,
    validation_schedule_seed,
)
from graph_sym_perf.validation.validator import IssueValidator, SubprocessSessionRunner


class LifecycleAdapter:
    def __init__(self, candidate: CandidateBundle) -> None:
        self.candidate = candidate
        self.connection_identity = f"driver-{os.getpid()}"
        self._graph_fingerprint = ""

    def connectivity_check(self):
        if self.candidate.resolved_config.get("mode") == "hang":
            time.sleep(1.0)
        self._event("connect")
        return True, "connected"

    def execute(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
        self._event(query_kind)
        mode = self.candidate.resolved_config.get("mode")
        if query_kind == "validation_graph_nodes":
            properties = {"id": 2 if mode == "graph_mismatch" else 1}
            rows = ({"node_id": 1, "labels": ["Person"], "properties": properties},)
            return ExecutionResult(TargetSystem.NEO4J, query_kind, rows, 1.0, {"truncated": False})
        if query_kind == "validation_graph_relationships":
            return ExecutionResult(TargetSystem.NEO4J, query_kind, (), 1.0, {"truncated": False})
        if mode == "aa_result_unstable" and query_kind == "validation_control_equivalence_a_2":
            return ExecutionResult(TargetSystem.NEO4J, query_kind, ({"value": 2},), 1.0, {"truncated": False})
        is_b = query == self.candidate.pair.query_b
        if mode == "execution_error" and is_b and query_kind == "validation_equivalence_b":
            raise RuntimeError("forced execution error")
        if mode == "equivalence_timeout" and is_b and query_kind == "validation_equivalence_b":
            raise QueryTimeoutError("forced equivalence timeout")
        if mode == "target_timeout" and is_b and query_kind == "runtime":
            raise QueryTimeoutError("forced runtime timeout")
        elapsed = 40.0 if query == self.candidate.pair.query_b and query_kind == "runtime" else 10.0
        rows = () if query_kind == "validation_reset" else ({"value": 2 if mode == "typed_mismatch" and is_b else 1},)
        truncated = bool(mode == "truncated" and is_b and query_kind == "validation_equivalence_b")
        return ExecutionResult(TargetSystem.NEO4J, query_kind, rows, elapsed, {"truncated": truncated})

    def load_gdsmith_graph(self, statements):
        self._event("load")
        self._graph_fingerprint = GraphSnapshot.from_statements(statements).content_hash

    def reset_graph(self, *, timeout_seconds=None) -> bool:
        if self.candidate.resolved_config.get("mode") != "database_reset":
            return False
        self._event("database_reset")
        return True

    def dbms_fingerprint(self):
        return DBMSFingerprint(self.candidate.pair.dbms_fingerprint)

    def close(self):
        self._event("close")

    def health_event(self):
        return "dbms_restart" if self.candidate.resolved_config.get("mode") == "health_event" else None

    def _event(self, name: str) -> None:
        path = Path(str(self.candidate.resolved_config["event_log"]))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}:{name}\n")


def _adapter_factory(candidate: CandidateBundle):
    return LifecycleAdapter(candidate)


def _crashing_adapter_factory(_candidate: CandidateBundle):
    os._exit(17)


def _raising_adapter_factory(_candidate: CandidateBundle):
    raise RuntimeError("adapter factory exploded")


def _certificate() -> SafetyCertificate:
    rule = RuleRegistry.default().get("pattern_order_symmetry")
    return SafetyCertificate(
        rule.rule_id,
        "clauses[0].pattern.components[0]",
        (),
        "graph_pattern",
        "pattern_defined",
        "match",
        rule.purity_requirements,
        rule.proof_id,
        "capability-hash",
    )


def _bundle(
    event_log: Path,
    *,
    mode: str | None = None,
    session_timeout_seconds: float = 300.0,
) -> CandidateBundle:
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    pair = EquivalentQueryPair(
        "MATCH (n) RETURN n.id AS value ORDER BY value",
        "MATCH (n) WHERE true RETURN n.id AS value ORDER BY value",
        {},
        snapshot.content_hash,
        "versioned-dbms-fingerprint",
    )
    return CandidateBundle.create(
        pair,
        graph_snapshot=snapshot,
        dbms_version="Neo4j/2026.07",
        resolved_config={"event_log": str(event_log), "mode": mode},
        discovery_slow_query="b",
        discovery_ratio=4.0,
        certificate=_certificate(),
        validation_policy=ValidationPolicy(
            session_timeout_seconds=session_timeout_seconds,
        ),
    )


def test_subprocess_runner_performs_real_fresh_process_session_lifecycle(tmp_path: Path) -> None:
    candidate = _bundle(tmp_path / "events.log")
    runner = SubprocessSessionRunner(_adapter_factory)

    first_equivalence, first = runner.run_session(
        candidate,
        1,
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )
    second_equivalence, second = runner.run_session(
        candidate,
        2,
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )

    assert first_equivalence.equivalent and first_equivalence.complete_result
    assert second_equivalence.equivalent and second_equivalence.complete_result
    assert first.process_id != os.getpid()
    assert second.process_id != os.getpid()
    assert first.process_id != second.process_id
    assert first.lifecycle is not None and first.lifecycle.complete
    assert second.lifecycle is not None and second.lifecycle.complete
    assert first.lifecycle.connection_id != second.lifecycle.connection_id
    assert len(first.evidence.paired_samples) == 20
    assert {sample.order for sample in first.evidence.paired_samples} == {"AB", "BA"}
    assert len(first.negative_controls) == 2
    assert first.evidence.slow_query == "b"
    events = (tmp_path / "events.log").read_text(encoding="utf-8")
    for event in ("connect", "validation_reset", "load", "validation_graph_nodes", "validation_graph_relationships", "close"):
        assert f":{event}\n" in events
    events_by_pid = {
        pid: tuple(
            line.split(":", 1)[1]
            for line in events.splitlines()
            if line.startswith(f"{pid}:")
        )
        for pid in (str(first.process_id), str(second.process_id))
    }
    assert str(os.getpid()) not in {line.split(":", 1)[0] for line in events.splitlines()}
    for process_events in events_by_pid.values():
        assert process_events.count("validation_reset") == 1
        assert process_events.count("load") == 1
        assert process_events.count("runtime") == 130


def test_spawned_runner_derives_distinct_schedule_seeds_from_candidate_hash(tmp_path: Path) -> None:
    candidate = _bundle(tmp_path / "seed-events.log")
    runner = SpawnedValidationSessionRunner(_adapter_factory)

    _, first = runner.run_session(
        candidate,
        1,
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )
    _, second = runner.run_session(
        candidate,
        2,
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )

    assert first.schedule_seed == validation_schedule_seed(candidate.candidate_hash, 1)
    assert second.schedule_seed == validation_schedule_seed(candidate.candidate_hash, 2)
    assert first.schedule_seed != second.schedule_seed
    assert first.schedule_seed not in {1, 2}
    assert second.schedule_seed not in {1, 2}


def test_spawned_runner_hard_timeout_fails_closed(tmp_path: Path) -> None:
    candidate = _bundle(
        tmp_path / "timeout-events.log",
        mode="hang",
        session_timeout_seconds=0.05,
    )

    with pytest.raises(RuntimeError, match="VALIDATION_SESSION_TIMEOUT"):
        SpawnedValidationSessionRunner(_adapter_factory).run_session(
            candidate,
            1,
            expected_slow_query="b",
            noise_floor_ms=0.0,
            timer_resolution_ms=0.001,
        )


@pytest.mark.parametrize(
    "adapter_factory",
    (_crashing_adapter_factory, _raising_adapter_factory),
)
def test_spawned_runner_empty_queue_or_child_failure_fails_closed(
    tmp_path: Path,
    adapter_factory,
) -> None:
    candidate = _bundle(tmp_path / "failed-child-events.log")

    with pytest.raises(RuntimeError, match="VALIDATION_SESSION_FAILED"):
        SpawnedValidationSessionRunner(adapter_factory).run_session(
            candidate,
            1,
            expected_slow_query="b",
            noise_floor_ms=0.0,
            timer_resolution_ms=0.001,
        )


def test_spawned_runner_closes_adapter_when_session_work_fails(tmp_path: Path) -> None:
    event_log = tmp_path / "failed-work-events.log"
    candidate = _bundle(event_log, mode="execution_error")

    SpawnedValidationSessionRunner(_adapter_factory).run_session(
        candidate,
        1,
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )

    assert f"{event_log.read_text(encoding='utf-8').split(':', 1)[0]}:close\n" in event_log.read_text(encoding="utf-8")


def test_subprocess_runner_prefers_database_level_reset_when_supported(tmp_path: Path) -> None:
    event_log = tmp_path / "events.log"
    candidate = _bundle(event_log, mode="database_reset")

    equivalence, session = SubprocessSessionRunner(_adapter_factory).run_session(
        candidate,
        1,
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )

    events = event_log.read_text(encoding="utf-8")
    assert equivalence.equivalent and session.lifecycle is not None and session.lifecycle.complete
    assert ":database_reset\n" in events
    assert ":validation_reset\n" not in events


def test_subprocess_runner_fail_closes_when_loaded_graph_state_does_not_match(tmp_path: Path) -> None:
    candidate = _bundle(tmp_path / "events.log", mode="graph_mismatch")
    runner = SubprocessSessionRunner(_adapter_factory)

    report = IssueValidator().validate(candidate, runner)

    assert report.stage.value == "invalid"
    assert "session_lifecycle_incomplete" in report.reason_codes


@pytest.mark.parametrize(
    ("mode", "reason"),
    (
        ("truncated", "result_truncated"),
        ("typed_mismatch", "typed_result_mismatch"),
        ("execution_error", "forced execution error"),
        ("equivalence_timeout", "forced equivalence timeout"),
    ),
)
def test_full_equivalence_errors_never_pass_validation(tmp_path: Path, mode: str, reason: str) -> None:
    candidate = _bundle(tmp_path / f"{mode}.log", mode=mode)

    report = IssueValidator().validate(candidate, SubprocessSessionRunner(_adapter_factory))

    assert report.stage.value == "rejected"
    assert reason in report.reason_codes


def test_target_timeout_is_inconclusive_not_a_runtime_pass(tmp_path: Path) -> None:
    candidate = _bundle(tmp_path / "timeout.log", mode="target_timeout")

    report = IssueValidator().validate(candidate, SubprocessSessionRunner(_adapter_factory))

    assert report.stage.value == "inconclusive"
    assert report.submission_path is None
    assert report.metadata["timeout_validation_confirmed"] is True
    assert report.sessions[0].process_id != report.sessions[1].process_id
    assert all(
        session.graph_fingerprint == candidate.pair.graph_snapshot_hash
        and session.dbms_fingerprint == candidate.pair.dbms_fingerprint
        and session.health_event is None
        for session in report.sessions
    )


def test_health_event_rejects_otherwise_strong_runtime_evidence(tmp_path: Path) -> None:
    candidate = _bundle(tmp_path / "health.log", mode="health_event")

    report = IssueValidator().validate(candidate, SubprocessSessionRunner(_adapter_factory))

    assert report.stage.value == "rejected"
    assert "session_fingerprint_or_health_failure" in report.reason_codes


def test_graph_mismatch_invalidates_the_session(tmp_path: Path) -> None:
    candidate = _bundle(tmp_path / "graph.log", mode="graph_mismatch")

    report = IssueValidator().validate(candidate, SubprocessSessionRunner(_adapter_factory))

    assert report.stage.value == "invalid"


def test_aa_typed_result_instability_never_passes_validation(tmp_path: Path) -> None:
    candidate = _bundle(tmp_path / "aa-result.log", mode="aa_result_unstable")

    report = IssueValidator().validate(candidate, SubprocessSessionRunner(_adapter_factory))

    assert report.stage.value == "rejected"
    assert "negative_control_result_unstable" in report.reason_codes
