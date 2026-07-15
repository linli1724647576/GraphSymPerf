import json
import hashlib
import os
import signal
import shutil
import time
from dataclasses import replace

import pytest

import graph_sym_perf.experiment.supervisor as supervisor_module
from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import CampaignPhase, RunPlan, ShardTask, SourceRevision
from graph_sym_perf.experiment.evidence_validation import canonical_hash
from graph_sym_perf.experiment.state import EntryState, ShardJournal, ShardState
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle
from graph_sym_perf.experiment.planner import write_resolved_manifest
from graph_sym_perf.experiment.supervisor import (
    AttemptContext,
    EntryResultIntegrityError,
    ManifestMismatchError,
    RulePlanSupervisor,
    ShardSupervisor,
    classify_rule_plan_result,
    _manifest_hash,
    _rule_plan_entry_directory,
    _task_identity,
)
from graph_sym_perf.experiment.worker import (
    GeneratedShard,
    RuntimeFirstShardWorker,
    _entry_directory_name,
)
from graph_sym_perf.experiment.worker import WorkerFailure
from graph_sym_perf.experiment.state import ErrorKind
from graph_sym_perf.sanitization import (
    EvidencePayload,
    SanitizationIntegrityError,
    unwrap_evidence_payload,
)
from tests.gdsmith_artifact_fixture import (
    assume_schema_v3_sources_clean,
    verified_gdsmith_environment,
)


@pytest.fixture(autouse=True)
def _default_rule_plan_tooling(tmp_path, monkeypatch) -> None:
    assume_schema_v3_sources_clean(monkeypatch)
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    for name, value in verified_gdsmith_environment(
        tmp_path / "gdsmith-fixture",
        spec.generation.expected_commit,
    ).items():
        monkeypatch.setenv(name, value)


def _p1_rule_plan():
    from graph_sym_perf.experiment.planner import build_rule_plan

    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    plan = build_rule_plan(spec, CampaignPhase.P1)
    return replace(plan, entries=(plan.entries[0],))


@pytest.mark.parametrize(
    ("terminal_state", "accepted", "expected"),
    (
        ("completed", 200, "completed"),
        ("completed", 199, "underfilled"),
        ("underfilled", 199, "underfilled"),
        ("quarantined", 17, "quarantined"),
        ("resource_exhausted", 17, "resource_exhausted"),
        ("infrastructure_failed", 0, "infrastructure_failed"),
    ),
)
def test_rule_plan_terminal_classification_is_exact(
    terminal_state: str,
    accepted: int,
    expected: str,
) -> None:
    entry = _p1_rule_plan().entries[0]
    assert classify_rule_plan_result(
        entry,
        {"terminal_state": terminal_state, "accepted_pairs": accepted},
    ).value == expected


def test_rule_plan_terminal_classification_rejects_overfill_unknown_and_identity_drift() -> None:
    entry = _p1_rule_plan().entries[0]
    with pytest.raises(EntryResultIntegrityError, match="overfill"):
        classify_rule_plan_result(
            entry,
            {"terminal_state": "completed", "accepted_pairs": entry.accepted_pair_quota + 1},
        )
    with pytest.raises(EntryResultIntegrityError, match="unknown terminal"):
        classify_rule_plan_result(entry, {"terminal_state": "mystery", "accepted_pairs": 0})
    with pytest.raises(EntryResultIntegrityError, match="identity"):
        classify_rule_plan_result(
            entry,
            {
                "entry_id": "other-entry",
                "terminal_state": "completed",
                "accepted_pairs": entry.accepted_pair_quota,
            },
        )


def test_rule_plan_supervisor_retries_in_new_attempt_and_writes_typed_cell_summary(tmp_path) -> None:
    plan = _p1_rule_plan()
    calls = 0

    class Worker:
        run_dir = None
        context = None

        def bind_run_dir(self, run_dir) -> None:
            self.run_dir = run_dir

        def bind_attempt_context(self, context) -> None:
            self.context = context

        def __call__(self, entry):
            nonlocal calls
            calls += 1
            assert self.context is not None
            if calls == 1:
                return {
                    "entry_id": entry.entry_id,
                    "terminal_state": "infrastructure_failed",
                    "accepted_pairs": 0,
                    "certificate_complete": False,
                    "artifact_complete": True,
                }
            return {
                "entry_id": entry.entry_id,
                "terminal_state": "completed",
                "accepted_pairs": entry.accepted_pair_quota,
                "certificate_complete": True,
                "artifact_complete": True,
            }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=1,
        process_isolation=False,
    ).run(plan, Worker())

    assert summary.completed == plan.entries
    entry_dir = next((tmp_path / "entries").iterdir())
    assert (entry_dir / "attempt-000" / "attempt.json").is_file()
    retry = json.loads((entry_dir / "attempt-001" / "attempt.json").read_text(encoding="utf-8"))
    assert retry["previous_attempt"] == "attempt-000"
    first_result = json.loads(
        (entry_dir / "attempt-000" / "terminal_result.json").read_text(encoding="utf-8")
    )
    assert retry["previous_attempt_result_hash"] == first_result["content_hash"]
    cell = json.loads((entry_dir / "cell_summary.json").read_text(encoding="utf-8"))
    assert cell["terminal_state"] == "completed"
    assert cell["quota"] == plan.entries[0].accepted_pair_quota
    assert len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()) >= 4


def test_rule_plan_supervisor_writes_one_cell_summary_for_every_terminal_class(tmp_path) -> None:
    plan = _p1_rule_plan()
    base = plan.entries[0]
    entries = tuple(
        replace(
            base,
            entry_id=f"p1:neo4j:discovery:rule-{index}:128:0",
            rule_id=f"rule-{index}",
            shard_seed=base.shard_seed + index + 1,
        )
        for index in range(4)
    )
    plan = replace(plan, entries=entries)
    states = {
        entries[0].entry_id: ("underfilled", 1),
        entries[1].entry_id: ("quarantined", 1),
        entries[2].entry_id: ("resource_exhausted", 1),
        entries[3].entry_id: ("infrastructure_failed", 0),
    }

    def worker(entry):
        state, accepted = states[entry.entry_id]
        return {
            "entry_id": entry.entry_id,
            "terminal_state": state,
            "accepted_pairs": accepted,
            "certificate_complete": state != "quarantined",
            "artifact_complete": True,
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=0,
        process_isolation=False,
    ).run(plan, worker)

    assert len(summary.underfilled) == 1
    assert len(summary.quarantined) == 1
    assert len(summary.resource_exhausted) == 1
    assert len(summary.infrastructure_failed) == 1
    assert len(tuple((tmp_path / "entries").glob("*/cell_summary.json"))) == 4


def test_rule_plan_supervisor_and_worker_share_one_entry_directory_identity() -> None:
    entry = _p1_rule_plan().entries[0]

    assert _rule_plan_entry_directory(entry) == _entry_directory_name(entry.entry_id)


def test_rule_plan_cell_summary_conversion_failure_is_typed_infrastructure(
    tmp_path,
) -> None:
    plan = _p1_rule_plan()

    def worker(entry):
        return {
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
            "accepted_pairs": entry.accepted_pair_quota,
            "certificate_complete": True,
            "artifact_complete": True,
            "health_events": "not-an-integer",
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=False,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    payload = json.loads(
        next((tmp_path / "entries").glob("*/cell_summary.json")).read_text(
            encoding="utf-8"
        )
    )
    assert payload["terminal_state"] == "infrastructure_failed"
    assert payload["artifact_complete"] is False
    assert payload["certificate_complete"] is False


def test_rule_plan_supervisor_runs_formal_entry_in_an_isolated_process(tmp_path) -> None:
    plan = _p1_rule_plan()
    parent_pid = os.getpid()

    def worker(entry):
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
            "worker_pid": os.getpid(),
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=True,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.completed == plan.entries
    terminal = json.loads(
        next((tmp_path / "entries").glob("*/attempt-000/terminal_result.json")).read_text(
            encoding="utf-8"
        )
    )
    assert terminal["worker_pid"] != parent_pid


def test_rule_plan_supervisor_enforces_entry_hard_timeout(tmp_path) -> None:
    plan = _p1_rule_plan()
    plan = replace(
        plan,
        entries=(replace(plan.entries[0], max_entry_runtime_seconds=1),),
    )

    def worker(entry):
        time.sleep(2.0)
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
        }

    started = time.monotonic()
    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=True,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert time.monotonic() - started < 1.8
    assert summary.resource_exhausted == plan.entries
    assert summary.infrastructure_failed == ()
    terminal = json.loads(
        next((tmp_path / "entries").glob("*/attempt-000/terminal_result.json")).read_text(
            encoding="utf-8"
        )
    )
    assert terminal["resource_exhaustion_kind"] == ErrorKind.RESOURCE_EXHAUSTED.value
    assert terminal["terminal_state"] == EntryState.RESOURCE_EXHAUSTED.value


def test_rule_plan_attempt_guard_fails_closed_before_retry(tmp_path) -> None:
    plan = _p1_rule_plan()
    guard_calls = 0
    worker_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls >= 2:
            raise ManifestMismatchError("source provenance changed during run")

    def worker(_entry):
        nonlocal worker_calls
        worker_calls += 1
        raise RuntimeError("retryable worker failure")

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=1,
        process_isolation=False,
        attempt_guard=guard,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    assert worker_calls == 1
    terminal = json.loads(
        next((tmp_path / "entries").glob("*/attempt-000/terminal_result.json")).read_text(
            encoding="utf-8"
        )
    )
    assert terminal["infrastructure_error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert "source provenance changed" in terminal["infrastructure_reason"]
    assert terminal["secondary_terminal"]["infrastructure_error_kind"] == (
        ErrorKind.INFRASTRUCTURE_ERROR.value
    )
    assert "retryable worker failure" in terminal["secondary_terminal"][
        "infrastructure_reason"
    ]


def test_rule_plan_post_guard_runs_after_child_crash_and_preserves_typed_failure(
    tmp_path,
) -> None:
    plan = _p1_rule_plan()
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls >= 2:
            raise ManifestMismatchError("source provenance changed after crash")

    def worker(_entry):
        os._exit(17)

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=True,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
        attempt_guard=guard,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    terminal = json.loads(
        next((tmp_path / "entries").glob("*/attempt-000/terminal_result.json")).read_text(
            encoding="utf-8"
        )
    )
    assert terminal["infrastructure_error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert terminal["secondary_terminal"]["infrastructure_error_kind"] == (
        ErrorKind.WORKER_CRASHED.value
    )
    assert "source provenance changed" in terminal["infrastructure_reason"]


def test_rule_plan_post_guard_drift_overrides_resource_exhaustion_with_secondary_evidence(
    tmp_path,
) -> None:
    plan = _p1_rule_plan()
    plan = replace(
        plan,
        entries=(replace(plan.entries[0], max_entry_runtime_seconds=1),),
    )
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls >= 2:
            raise ManifestMismatchError("source provenance changed during timeout")

    def worker(_entry):
        time.sleep(2.0)
        raise AssertionError("hard timeout should terminate the child first")

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=True,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
        attempt_guard=guard,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    assert summary.resource_exhausted == ()
    terminal = json.loads(
        next((tmp_path / "entries").glob("*/attempt-000/terminal_result.json")).read_text(
            encoding="utf-8"
        )
    )
    assert terminal["infrastructure_error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert terminal["secondary_terminal"]["terminal_state"] == (
        EntryState.RESOURCE_EXHAUSTED.value
    )
    assert terminal["secondary_terminal"]["resource_exhaustion_kind"] == (
        ErrorKind.RESOURCE_EXHAUSTED.value
    )


def test_rule_plan_attempt_guard_fails_closed_after_worker(tmp_path) -> None:
    plan = _p1_rule_plan()
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls >= 2:
            raise ManifestMismatchError("source provenance changed during run")

    def worker(entry):
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=False,
        attempt_guard=guard,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    assert not summary.completed
    terminal = json.loads(
        next((tmp_path / "entries").glob("*/attempt-000/terminal_result.json")).read_text(
            encoding="utf-8"
        )
    )
    assert terminal["infrastructure_error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert terminal["secondary_terminal"]["terminal_state"] == EntryState.COMPLETED.value


def test_rule_plan_supervisor_classifies_child_crash_without_killing_campaign(
    tmp_path,
) -> None:
    plan = _p1_rule_plan()

    def worker(_entry):
        os._exit(17)

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=True,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    terminal = json.loads(
        next((tmp_path / "entries").glob("*/attempt-000/terminal_result.json")).read_text(
            encoding="utf-8"
        )
    )
    assert terminal["infrastructure_error_kind"] == ErrorKind.WORKER_CRASHED.value


@pytest.mark.parametrize("process_isolation", (False, True))
def test_rule_plan_terminal_error_is_typed_sanitized_and_bounded(
    tmp_path,
    process_isolation,
) -> None:
    plan = _p1_rule_plan()
    sensitive_message = (
        "Traceback (most recent call last):\n"
        '  File "/Users/sakuraq/private/worker.py", line 7\n'
        "bolt://neo4j:hunter2@db.internal:7687 "
        "GRAPHSYMPERF_NEO4J_PASSWORD=hunter2 "
        "NEO4J_PASSWORD=driver-password "
        "NEO4J_AUTH=neo4j/driver-auth "
        '{"quoted_password": "json-password"}; '
        "{'quoted_secret': 'python-secret'}; "
        "Authorization: Custom Multi Word opaque-authorization-value; "
        + ("x" * 1000)
    )

    def worker(_entry):
        raise RuntimeError(sensitive_message)

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=0,
        process_isolation=process_isolation,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    terminal = json.loads(
        next(
            (tmp_path / "entries").glob(
                "*/attempt-000/terminal_result.json"
            )
        ).read_text(encoding="utf-8")
    )
    reason = terminal["infrastructure_reason"]
    assert terminal["infrastructure_error_kind"] == (
        ErrorKind.WORKER_CRASHED.value
        if process_isolation
        else ErrorKind.INFRASTRUCTURE_ERROR.value
    )
    assert len(reason) <= 512
    assert "\n" not in reason
    assert "Traceback" not in reason
    assert "/Users/sakuraq" not in reason
    assert "hunter2" not in reason
    assert "driver-password" not in reason
    assert "driver-auth" not in reason
    assert "json-password" not in reason
    assert "python-secret" not in reason
    assert "opaque-authorization-value" not in reason
    assert "bolt://" not in reason
    cell_summary = next(
        (tmp_path / "entries").glob("*/cell_summary.json")
    ).read_text(encoding="utf-8")
    assert "hunter2" not in cell_summary
    assert "driver-password" not in cell_summary
    assert "driver-auth" not in cell_summary
    assert "json-password" not in cell_summary
    assert "python-secret" not in cell_summary
    assert "opaque-authorization-value" not in cell_summary


def test_cell_summary_failure_reason_is_sanitized(tmp_path, monkeypatch) -> None:
    plan = _p1_rule_plan()

    def worker(entry):
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
        }

    def fail_cell_summary(*_args, **_kwargs):
        raise ValueError(
            "/Users/sakuraq/private/cell.py "
            "postgresql://admin:hunter2@db.internal/graph "
            "PASSWORD=hunter2 "
            "Authorization: Custom Multi Word cell-auth-tail; "
            "{'cell_token': 'cell-token-secret'}"
        )

    monkeypatch.setattr(
        supervisor_module,
        "cell_summary_from_result",
        fail_cell_summary,
    )
    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        process_isolation=False,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    failure = json.loads(
        next(
            (tmp_path / "entries").glob("*/cell_summary_failure.json")
        ).read_text(encoding="utf-8")
    )
    reason = failure["reason"]
    assert failure["error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert "/Users/sakuraq" not in reason
    assert "hunter2" not in reason
    assert "postgresql://" not in reason
    assert "cell-auth-tail" not in reason
    assert "cell-token-secret" not in reason


@pytest.mark.parametrize("process_isolation", (False, True))
def test_terminal_result_is_recursively_sanitized_without_rewriting_query_results(
    tmp_path,
    process_isolation,
) -> None:
    plan = _p1_rule_plan()

    def worker(entry):
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
            "diagnostics": {
                "NEO4J_AUTH": "neo4j/terminal-auth",
                "nested": [{"adapter_password": "terminal-password"}],
            },
            "evidence": EvidencePayload(
                {
                    "query": "MATCH (n) WHERE auth = 'literal' RETURN n",
                    "rows": [
                        {
                            "password": "literal-row-password",
                            "message": "Authorization: literal row",
                        }
                    ],
                }
            ),
            "query_text": "password=ordinary-query-password RETURN n",
            "result_rows": [{"password": "ordinary-result-password"}],
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=0,
        process_isolation=process_isolation,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.completed == plan.entries
    terminal_path = next(
        (tmp_path / "entries").glob("*/attempt-000/terminal_result.json")
    )
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    evidence = unwrap_evidence_payload(terminal["evidence"])
    assert evidence == {
        "query": "MATCH (n) WHERE auth = 'literal' RETURN n",
        "rows": [
            {
                "password": "literal-row-password",
                "message": "Authorization: literal row",
            }
        ],
    }
    assert terminal["result_rows"] == [{"password": "<redacted>"}]
    receipt = terminal.pop("content_hash")
    assert receipt == canonical_hash(terminal)
    raw = terminal_path.read_text(encoding="utf-8")
    assert "terminal-auth" not in raw
    assert "terminal-password" not in raw
    assert "ordinary-query-password" not in raw
    assert "ordinary-result-password" not in raw
    assert "terminal-auth" not in next(
        (tmp_path / "entries").glob("*/cell_summary.json")
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("process_isolation", (False, True))
def test_terminal_boundary_rejects_stale_seal_instead_of_rewriting_receipt(
    tmp_path,
    process_isolation,
) -> None:
    plan = _p1_rule_plan()

    def worker(entry):
        envelope_body = {
            "payload": {
                "message": (
                    "Authorization: Custom Scheme terminal-sealed-secret; retry=true"
                ),
            },
        }
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
            "nested_receipt": {
                **envelope_body,
                "content_hash": canonical_hash(envelope_body),
            },
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=0,
        process_isolation=process_isolation,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    terminal_path = next(
        (tmp_path / "entries").glob("*/attempt-000/terminal_result.json")
    )
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    assert terminal["infrastructure_error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert "terminal-sealed-secret" not in terminal_path.read_text(encoding="utf-8")
    assert terminal.get("nested_receipt") is None


def test_nonisolated_sanitization_integrity_worker_error_is_nonretryable(
    tmp_path,
) -> None:
    plan = _p1_rule_plan()
    worker_calls = 0

    def worker(_entry):
        nonlocal worker_calls
        worker_calls += 1
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: invalid producer envelope"
        )

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=2,
        process_isolation=False,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    assert worker_calls == 1
    terminal = json.loads(
        next(
            (tmp_path / "entries").glob("*/attempt-000/terminal_result.json")
        ).read_text(encoding="utf-8")
    )
    assert terminal["infrastructure_error_kind"] == (
        ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    )


def test_isolated_evidence_integrity_worker_error_is_nonretryable(
    tmp_path,
) -> None:
    plan = _p1_rule_plan()

    def worker(entry):
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
            "invalid_envelope": {
                "payload": {"message": "ordinary value"},
                "content_hash": "not-a-hash",
            },
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=2,
        process_isolation=True,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    attempts = sorted((tmp_path / "entries").glob("*/attempt-*"))
    assert [path.name for path in attempts] == ["attempt-000"]
    terminal = json.loads(
        (attempts[0] / "terminal_result.json").read_text(encoding="utf-8")
    )
    assert terminal["infrastructure_error_kind"] == (
        ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    )


@pytest.mark.parametrize("process_isolation", (False, True))
def test_nonfinite_envelope_hash_failure_becomes_typed_terminal(
    tmp_path,
    process_isolation,
) -> None:
    plan = _p1_rule_plan()

    def worker(entry):
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
            "invalid_envelope": {
                "payload": {"message": float("nan")},
                "content_hash": "a" * 64,
            },
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=2,
        process_isolation=process_isolation,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    attempts = sorted((tmp_path / "entries").glob("*/attempt-*"))
    assert [path.name for path in attempts] == ["attempt-000"]
    terminal = json.loads(
        (attempts[0] / "terminal_result.json").read_text(encoding="utf-8")
    )
    assert terminal["infrastructure_error_kind"] == (
        ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    )


@pytest.mark.parametrize("process_isolation", (False, True))
def test_nonfinite_initial_evidence_seal_failure_becomes_typed_terminal(
    tmp_path,
    process_isolation,
) -> None:
    plan = _p1_rule_plan()

    def worker(entry):
        return {
            "accepted_pairs": entry.accepted_pair_quota,
            "artifact_complete": True,
            "certificate_complete": True,
            "entry_id": entry.entry_id,
            "terminal_state": "completed",
            "evidence": EvidencePayload({"rows": [float("nan")]}),
        }

    summary = RulePlanSupervisor(
        tmp_path,
        campaign_id="campaign-fixture",
        input_manifest_hash="a" * 64,
        retry_limit=2,
        process_isolation=process_isolation,
        heartbeat_seconds=0.05,
        stale_after_seconds=1.0,
    ).run(plan, worker)

    assert summary.infrastructure_failed == plan.entries
    attempts = sorted((tmp_path / "entries").glob("*/attempt-*"))
    assert [path.name for path in attempts] == ["attempt-000"]
    terminal = json.loads(
        (attempts[0] / "terminal_result.json").read_text(encoding="utf-8")
    )
    assert terminal["infrastructure_error_kind"] == (
        ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    )


def _hash_for(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _durable_result(
    context: AttemptContext,
    task,
    *,
    accepted_count: int | None = None,
    accepted_hashes: list[str] | None = None,
):
    count = accepted_count if accepted_count is not None else int(getattr(task, "accepted_query_pairs", 1000))
    task_label = getattr(getattr(task, "target", None), "value", str(task))
    hashes = accepted_hashes or [
        _hash_for(f"{task_label}:{getattr(task, 'shard_index', 0)}:{index}") for index in range(count)
    ]
    count = len(hashes)
    accepted_path = context.shard_dir / "accepted_pairs.jsonl"
    accepted_path.write_text("".join(json.dumps({"pair_hash": value}) + "\n" for value in hashes), encoding="utf-8")
    stages = {}
    for stage in ("screening", "validation", "reduction"):
        path = context.attempt_dir / f"{stage}.json"
        path.write_text(json.dumps({"stage": stage, "task": task_label}), encoding="utf-8")
        stages[stage] = {
            "path": str(path.relative_to(context.shard_dir)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {"accepted_pairs": count, "accepted_pair_hashes": hashes, "stage_evidence": stages}


class _CompleteWorker:
    def __init__(self, *, fail_once_for=None, accepted_count=None) -> None:
        self.context = None
        self.fail_once_for = fail_once_for
        self.failed = False
        self.accepted_count = accepted_count

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        if task == self.fail_once_for and not self.failed:
            self.failed = True
            raise RuntimeError("retryable")
        assert self.context is not None
        count = self.accepted_count(task) if callable(self.accepted_count) else self.accepted_count
        return _durable_result(self.context, task, accepted_count=count)


def test_retryable_shard_failure_does_not_stop_other_tasks(tmp_path) -> None:
    tasks = (
        ShardTask.__new__(ShardTask),
    )
    # Use small stand-in tasks: supervisor only needs a stable task identity.
    plan = RunPlan("test", 2, ("bad", "good"), 2, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(tmp_path, retry_limit=1).run(plan, _CompleteWorker(fail_once_for="bad"))
    assert summary.completed == ("bad", "good")


def test_attempt_guard_fails_closed_when_provenance_drifts_before_retry(tmp_path) -> None:
    plan = RunPlan("test", 2, ("task",), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    guard_calls = 0
    worker_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls >= 2:
            raise ValueError("source provenance changed during run")

    def worker(_task):
        nonlocal worker_calls
        worker_calls += 1
        raise RuntimeError("retry once")

    summary = ShardSupervisor(tmp_path, retry_limit=1, attempt_guard=guard).run(plan, worker)

    assert summary.failed == ("task",)
    assert worker_calls == 1
    events = [
        json.loads(line)
        for line in (tmp_path / "shards" / "task-000" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert "source provenance changed" in events[-1]["reason"]
    override = json.loads(
        (tmp_path / "shards" / "task-000" / "attempt-000" / "post_guard_override.json")
        .read_text(encoding="utf-8")
    )
    assert override["secondary_terminal"]["error_kind"] == ErrorKind.WORKER_CRASHED.value
    assert "retry once" in override["secondary_terminal"]["reason"]


def test_attempt_guard_fails_closed_when_provenance_drifts_during_worker(tmp_path) -> None:
    plan = RunPlan("test", 2, ("task",), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls >= 2:
            raise ValueError("source provenance changed during run")

    summary = ShardSupervisor(tmp_path, retry_limit=0, attempt_guard=guard).run(plan, _CompleteWorker())

    assert summary.failed == ("task",)
    events = [
        json.loads(line)
        for line in (tmp_path / "shards" / "task-000" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    assert "source provenance changed" in events[-1]["reason"]


def test_shard_post_guard_runs_after_typed_worker_process_error(tmp_path) -> None:
    plan = RunPlan("test", 2, ("task",), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls >= 2:
            raise RuntimeError("source provenance changed after WPE")

    def worker(_task):
        raise supervisor_module.WorkerProcessError(
            ErrorKind.WORKER_HUNG,
            "typed worker process failure",
        )

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        attempt_guard=guard,
    ).run(plan, worker)

    assert summary.failed == ("task",)
    events = [
        json.loads(line)
        for line in (tmp_path / "shards" / "task-000" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value
    override = json.loads(
        (tmp_path / "shards" / "task-000" / "attempt-000" / "post_guard_override.json")
        .read_text(encoding="utf-8")
    )
    assert override["secondary_terminal"]["error_kind"] == ErrorKind.WORKER_HUNG.value


def test_supervisor_uses_task_specific_accepted_pair_target(tmp_path) -> None:
    class Task:
        accepted_query_pairs = 3

    plan = RunPlan("test", 2, (Task(),), 3, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(tmp_path).run(plan, _CompleteWorker())
    assert len(summary.completed) == 1


class _Adapter:
    target = TargetSystem.NEO4J

    def connectivity_check(self):
        return True, "connected"

    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        return ExecutionResult(
            system=self.target,
            query_kind=query_kind,
            rows=({"n": 1},),
            elapsed_ms=1.0,
            summary={"truncated": False},
        )

    def ensure_schema(self) -> None:
        return None

    def load_gdsmith_graph(self, _statements) -> None:
        return None

    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint("a" * 64, server_version="test-dbms/1")

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        return DBMSHealthEpoch("e" * 64, "test")

    def close(self) -> None:
        return None


def test_retry_uses_a_new_attempt_directory_instead_of_reusing_the_shard_directory(tmp_path) -> None:
    calls = 0

    def generation_runner(_task, _raw_dir):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected GDsmith crash")
        return GeneratedShard(
            graph_statements=("CREATE (n:Node {value: 1})",),
            query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith"},
        )

    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    plan = RunPlan("retry", 2, (task,), 1, SourceRevision(None, None, None))
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=generation_runner,
        adapter_factory=lambda _task: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    summary = ShardSupervisor(tmp_path, retry_limit=1).run(plan, worker)

    shard_dir = tmp_path / "shards" / "neo4j-000"
    assert summary.incomplete == (task,)
    assert (shard_dir / "attempt-000").is_dir()
    assert (shard_dir / "attempt-001").is_dir()


def test_accepted_count_above_the_exact_target_is_failed_not_completed(tmp_path) -> None:
    class Task:
        accepted_query_pairs = 1000

    task = Task()
    plan = RunPlan("overflow", 2, (task,), 1000, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(plan, lambda _task: {"accepted_pairs": 1001})

    assert summary.completed == ()
    assert summary.failed == (task,)


class _InflightDeadlineTask:
    target = TargetSystem.NEO4J
    shard_index = 0
    accepted_query_pairs = 1
    max_runtime_seconds = 1


class _WritesInflightThenBlocks:
    def __init__(self, run_dir) -> None:
        self.run_dir = run_dir

    def __call__(self, _task):
        attempt_dir = self.run_dir / "shards" / "neo4j-000" / "attempt-000"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "inflight.json").write_text(
            json.dumps(
                {
                    "query_hash": "b" * 64,
                    "query_kind": "runtime",
                    "started_at": time.time(),
                    "deadline": time.time() + 0.05,
                    "graph_hash": "graph",
                    "dbms_fingerprint": "dbms",
                }
            ),
            encoding="utf-8",
        )
        time.sleep(10)
        return {"accepted_pairs": 1}


def test_inflight_query_deadline_kills_a_permanently_blocked_worker_before_shard_timeout(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("query-timeout", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    started = time.monotonic()

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.6,
    ).run(plan, _WritesInflightThenBlocks(tmp_path))

    elapsed = time.monotonic() - started
    assert elapsed < 0.9
    assert summary.incomplete == (task,)
    assert "QUERY_TIMEOUT" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    timeout_quarantine = tmp_path / "shards" / "neo4j-000" / "query_timeout_quarantine.jsonl"
    record = json.loads(timeout_quarantine.read_text(encoding="utf-8").splitlines()[0])
    assert record["query_hash"] == "b" * 64
    assert record["graph_hash"] == "graph"
    assert record["error_kind"] == "QUERY_TIMEOUT"


class _TimesOutDistinctQueriesThenCompletes:
    def __init__(self, timeout_count: int) -> None:
        self.timeout_count = timeout_count
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        if self.context.attempt < self.timeout_count:
            (self.context.attempt_dir / "inflight.json").write_text(
                json.dumps(
                    {
                        "query_hash": _hash_for(f"slow-query-{self.context.attempt}"),
                        "query_kind": "acceptance_base",
                        "started_at": time.time(),
                        "deadline": time.time() + 0.03,
                        "graph_hash": "graph",
                        "dbms_fingerprint": "dbms",
                    }
                ),
                encoding="utf-8",
            )
            time.sleep(10)
        return _durable_result(self.context, task)


def test_distinct_query_timeouts_do_not_consume_infrastructure_retry_budget(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("query-timeout-recovery", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=1.0,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
    ).run(plan, _TimesOutDistinctQueriesThenCompletes(timeout_count=4))

    assert summary.completed == (task,)
    shard_dir = tmp_path / "shards" / "neo4j-000"
    assert len(tuple(shard_dir.glob("attempt-*"))) == 5
    quarantine = (shard_dir / "query_timeout_quarantine.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(quarantine) == 4
    assert len({json.loads(line)["query_hash"] for line in quarantine}) == 4


def test_query_timeout_recovery_preserves_one_total_shard_deadline(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("query-timeout-budget", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    started = time.monotonic()

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.2,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.1,
    ).run(plan, _TimesOutDistinctQueriesThenCompletes(timeout_count=100))

    assert time.monotonic() - started < 0.5
    assert summary.incomplete == (task,)


class _RepeatsOneTimedOutQuery(_TimesOutDistinctQueriesThenCompletes):
    def __call__(self, task):
        assert self.context is not None
        (self.context.attempt_dir / "inflight.json").write_text(
            json.dumps(
                {
                    "query_hash": _hash_for("same-slow-query"),
                    "query_kind": "acceptance_base",
                    "started_at": time.time(),
                    "deadline": time.time() + 0.03,
                    "graph_hash": "graph",
                    "dbms_fingerprint": "dbms",
                }
            ),
            encoding="utf-8",
        )
        time.sleep(10)
        return _durable_result(self.context, task)


def test_repeated_quarantined_query_timeout_fails_closed_instead_of_looping(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("repeated-query-timeout", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=1.0,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
    ).run(plan, _RepeatsOneTimedOutQuery(timeout_count=100))

    assert summary.failed == (task,)
    shard_dir = tmp_path / "shards" / "neo4j-000"
    assert len(tuple(shard_dir.glob("attempt-*"))) == 2
    quarantine = (shard_dir / "query_timeout_quarantine.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(quarantine) == 1
    final_event = json.loads((shard_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert final_event["error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value


class _StopsEntireWorkerProcess:
    def __call__(self, _task):
        os.kill(os.getpid(), signal.SIGSTOP)
        return {"accepted_pairs": 1}


def test_stale_heartbeat_kills_a_stopped_worker_before_shard_timeout(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("stale-heartbeat", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    started = time.monotonic()

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.8,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.08,
    ).run(plan, _StopsEntireWorkerProcess())

    assert time.monotonic() - started < 0.4
    assert summary.failed == (task,)
    shard_events = tmp_path / "shards" / "neo4j-000" / "events.jsonl"
    assert "WORKER_HUNG" in shard_events.read_text(encoding="utf-8")


def test_resume_rejects_a_different_immutable_manifest(tmp_path) -> None:
    task = _InflightDeadlineTask()
    original = RunPlan("manifest-a", 2, (task,), 1, SourceRevision("a", None, None))  # type: ignore[arg-type]
    changed = RunPlan("manifest-b", 2, (task,), 1, SourceRevision("b", None, None))  # type: ignore[arg-type]
    supervisor = ShardSupervisor(tmp_path, retry_limit=0)
    supervisor.run(original, lambda _task: {"accepted_pairs": 1, "accepted_pair_hashes": ["pair-a"]})

    with pytest.raises(ManifestMismatchError):
        supervisor.resume(changed, lambda _task: {"accepted_pairs": 1, "accepted_pair_hashes": ["pair-a"]})


def test_duplicate_pair_hashes_cannot_satisfy_exactly_once_target(tmp_path) -> None:
    class Task:
        accepted_query_pairs = 2

    task = Task()
    plan = RunPlan("duplicates", 2, (task,), 2, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(
        plan,
        lambda _task: {"accepted_pairs": 2, "accepted_pair_hashes": ["same", "same"]},
    )

    assert summary.failed == (task,)
    assert summary.completed == ()


class _ContextAwareResumeWorker:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, _task):
        assert self.context is not None
        first = _hash_for("pair-a")
        assert self.context.accepted_pair_hashes == frozenset({first})
        return _durable_result(self.context, _task, accepted_hashes=[first, _hash_for("pair-b")])


def test_resume_recovers_checkpoint_and_fsynced_artifact_after_supervisor_sigkill(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 2, 2, 60)
    plan = RunPlan("sigkill", 2, (task,), 2, SourceRevision(None, None, None))
    shard_dir = tmp_path / "shards" / "neo4j-000"
    journal = ShardJournal(shard_dir, manifest_hash=_manifest_hash(plan), task_identity=_task_identity(task))
    journal.transition(ShardState.GENERATING, attempt=0)
    (shard_dir / "attempt-000").mkdir()
    with (shard_dir / "accepted_pairs.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pair_hash": _hash_for("pair-a")}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    summary = ShardSupervisor(tmp_path, retry_limit=1).resume(plan, _ContextAwareResumeWorker())

    assert summary.completed == (task,)
    checkpoint = json.loads((shard_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert len(checkpoint["accepted_pair_hashes"]) == 2
    assert checkpoint["attempt"] == 1


def _append_phase(path, phase: str, event: str) -> None:
    payload = json.dumps({"phase": phase, "event": event, "pid": os.getpid(), "time": time.monotonic()}) + "\n"
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _LeaseProbeWorker:
    def __init__(self, trace_path) -> None:
        self.trace_path = trace_path
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        with self.context.generation_lease():
            _append_phase(self.trace_path, "generation", "start")
            time.sleep(0.08)
            _append_phase(self.trace_path, "generation", "end")
        with self.context.measurement_lease():
            _append_phase(self.trace_path, "measurement", "start")
            time.sleep(0.05)
            _append_phase(self.trace_path, "measurement", "end")
        return _durable_result(self.context, task)


def _peak_concurrency(records, phase: str) -> int:
    ordered = sorted(
        ((record["time"], 1 if record["event"] == "start" else -1) for record in records if record["phase"] == phase),
        key=lambda item: (item[0], item[1]),
    )
    active = 0
    peak = 0
    for _, delta in ordered:
        active += delta
        peak = max(peak, active)
    return peak


def test_scheduling_leases_allow_three_generation_systems_but_only_one_measurement_system(tmp_path) -> None:
    tasks = tuple(
        ShardTask(target, 0, index + 1, 1, 1, 60)
        for index, target in enumerate((TargetSystem.NEO4J, TargetSystem.MEMGRAPH, TargetSystem.FALKORDB))
    )
    plan = RunPlan("leases", 2, tasks, 3, SourceRevision(None, None, None))
    trace_path = tmp_path / "phase-trace.jsonl"

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=2,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.5,
        max_parallel_generation_systems=3,
        max_parallel_measurement_systems=1,
        max_parallel_shards_per_system=1,
    ).run(plan, _LeaseProbeWorker(trace_path))

    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(summary.completed) == 3
    assert 2 <= _peak_concurrency(records, "generation") <= 3
    assert _peak_concurrency(records, "measurement") == 1


class _FailsAfterPersistingScreeningPhase:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, _task):
        assert self.context is not None
        self.context.transition(ShardState.LOADING)
        self.context.transition(ShardState.BUILDING_ACCEPTED_PAIRS)
        self.context.transition(ShardState.SCREENING)
        raise WorkerFailure(ErrorKind.DBMS_CRASHED, "restart during screening")


def test_parent_reloads_child_phase_journal_before_recording_failure(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("phase-failure", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        max_parallel_generation_systems=1,
    ).run(plan, _FailsAfterPersistingScreeningPhase())

    events = [
        json.loads(line)
        for line in (tmp_path / "shards" / "neo4j-000" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert summary.failed == (task,)
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
    assert events[-1]["previous_state"] == "screening"
    assert events[-1]["error_kind"] == "DBMS_CRASHED"


class _ParallelRecoveryWorker:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        return _durable_result(self.context, task)


def test_corrupt_artifact_fails_only_its_shard_and_other_future_continues(tmp_path) -> None:
    bad = ShardTask(TargetSystem.NEO4J, 0, 11, 1, 1, 60)
    good = ShardTask(TargetSystem.MEMGRAPH, 0, 12, 1, 1, 60)
    plan = RunPlan("corrupt-one", 2, (bad, good), 2, SourceRevision(None, None, None))
    bad_dir = tmp_path / "shards" / "neo4j-000"
    bad_dir.mkdir(parents=True)
    (bad_dir / "accepted_pairs.jsonl").write_text("{not-json}\n", encoding="utf-8")

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        max_parallel_generation_systems=2,
    ).resume(plan, _ParallelRecoveryWorker())

    assert summary.failed == (bad,)
    assert summary.completed == (good,)
    checkpoint = json.loads((bad_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["state"] == "failed_final"
    events = (bad_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "INFRASTRUCTURE_ERROR" in events


class _DurableStageWorker:
    def __init__(self, *, result_hash_override: str | None = None) -> None:
        self.context = None
        self.result_hash_override = result_hash_override

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        pair_hash = _hash_for(f"{task.target.value}:{task.shard_index}")
        accepted_path = self.context.shard_dir / "accepted_pairs.jsonl"
        accepted_path.write_text(json.dumps({"pair_hash": pair_hash}) + "\n", encoding="utf-8")
        stages = {}
        for stage in ("screening", "validation", "reduction"):
            path = self.context.attempt_dir / f"{stage}.json"
            path.write_text(json.dumps({"stage": stage, "task": task.target.value}), encoding="utf-8")
            stages[stage] = {
                "path": str(path.relative_to(self.context.shard_dir)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return {
            "accepted_pairs": 1,
            "accepted_pair_hashes": [self.result_hash_override or pair_hash],
            "stage_evidence": stages,
        }


def test_missing_accepted_hashes_and_canonical_artifact_is_final_failure(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 21, 1, 1, 60)
    plan = RunPlan("missing-hashes", 2, (task,), 1, SourceRevision(None, None, None))

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(plan, lambda _task: {"accepted_pairs": 1})

    assert summary.failed == (task,)
    assert summary.completed == ()


def test_canonical_and_result_hash_sets_must_match_exactly(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 22, 1, 1, 60)
    plan = RunPlan("hash-mismatch", 2, (task,), 1, SourceRevision(None, None, None))

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(
        plan,
        _DurableStageWorker(result_hash_override=_hash_for("different")),
    )

    assert summary.failed == (task,)
    assert summary.completed == ()


def test_complete_requires_durable_validation_and_reduction_stage_evidence(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 23, 1, 1, 60)
    plan = RunPlan("full-chain", 2, (task,), 1, SourceRevision(None, None, None))

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(plan, _DurableStageWorker())

    events = [
        json.loads(line)
        for line in (tmp_path / "shards" / "neo4j-000" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert summary.completed == (task,)
    assert [event["state"] for event in events][-3:] == ["validating", "reducing", "completed"]


def test_resume_does_not_treat_attempt_number_as_infrastructure_retry_count(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 24, 1, 1, 60)
    plan = RunPlan("attempt-exhausted", 2, (task,), 1, SourceRevision(None, None, None))
    shard_dir = tmp_path / "shards" / "neo4j-000"
    journal = ShardJournal(shard_dir, manifest_hash=_manifest_hash(plan), task_identity=_task_identity(task))
    journal.transition(ShardState.GENERATING, attempt=3)
    (shard_dir / "attempt-003").mkdir()

    summary = ShardSupervisor(tmp_path, retry_limit=3).resume(plan, _DurableStageWorker())

    checkpoint = json.loads((shard_dir / "checkpoint.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (shard_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary.completed == (task,)
    assert checkpoint["state"] == "completed"
    assert checkpoint["attempt"] == 4
    assert events[-1]["state"] == "completed"


def test_status_rejects_checkpoint_copied_from_another_manifest_task(tmp_path) -> None:
    tasks = (
        ShardTask(TargetSystem.NEO4J, 0, 31, 1, 1, 60),
        ShardTask(TargetSystem.MEMGRAPH, 0, 32, 1, 1, 60),
    )
    plan = RunPlan("status-identity", 2, tasks, 2, SourceRevision(None, None, None))
    run_id = "copied-checkpoint"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_resolved_manifest(plan, run_dir / "resolved_manifest.json")
    summary = ShardSupervisor(run_dir, retry_limit=0).run(plan, _DurableStageWorker())
    assert len(summary.completed) == 2
    neo = run_dir / "shards" / "neo4j-000"
    memgraph = run_dir / "shards" / "memgraph-000"
    for name in ("checkpoint.json", "events.jsonl", "accepted_pairs.jsonl"):
        shutil.copy2(neo / name, memgraph / name)

    status = ExperimentLifecycle(tmp_path).status(run_id)

    assert status.completed == 1
    assert status.failed == 1
    assert status.invalid == 1


def test_terminal_resume_revalidates_missing_canonical_artifact_before_completed(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 41, 1, 1, 60)
    plan = RunPlan("terminal-revalidate", 2, (task,), 1, SourceRevision(None, None, None))
    run_id = "terminal-revalidate"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_resolved_manifest(plan, run_dir / "resolved_manifest.json")
    supervisor = ShardSupervisor(run_dir, retry_limit=0)
    assert supervisor.run(plan, _DurableStageWorker()).completed == (task,)
    accepted_path = run_dir / "shards" / "neo4j-000" / "accepted_pairs.jsonl"
    accepted_path.unlink()

    summary = supervisor.resume(plan, lambda _task: pytest.fail("invalid terminal evidence must not rerun"))

    assert summary.completed == ()
    assert summary.failed == (task,)
    assert supervisor.resume(plan, lambda _task: pytest.fail("typed terminal failure must be stable")).failed == (task,)
    status = ExperimentLifecycle(tmp_path).status(run_id)
    assert (status.completed, status.failed, status.invalid) == (0, 1, 1)
    failure = json.loads((run_dir / "shards" / "neo4j-000" / "failure.json").read_text(encoding="utf-8"))
    assert failure["state"] == "failed_final"
    assert failure["error_kind"] == "INFRASTRUCTURE_ERROR"
    (run_dir / "shards" / "neo4j-000" / "checkpoint.json").unlink()
    marker_only_status = ExperimentLifecycle(tmp_path).status(run_id)
    assert (marker_only_status.active, marker_only_status.failed, marker_only_status.invalid) == (0, 1, 1)


def test_corrupt_checkpoint_and_events_are_preserved_while_other_shard_continues(tmp_path) -> None:
    bad = ShardTask(TargetSystem.NEO4J, 0, 51, 1, 1, 60)
    good = ShardTask(TargetSystem.MEMGRAPH, 0, 52, 1, 1, 60)
    plan = RunPlan("corrupt-journal", 2, (bad, good), 2, SourceRevision(None, None, None))
    run_id = "corrupt-journal"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_resolved_manifest(plan, run_dir / "resolved_manifest.json")
    bad_dir = run_dir / "shards" / "neo4j-000"
    bad_dir.mkdir(parents=True)
    checkpoint_bytes = b"{broken-checkpoint}\n"
    event_bytes = b"{broken-event}\n"
    (bad_dir / "checkpoint.json").write_bytes(checkpoint_bytes)
    (bad_dir / "events.jsonl").write_bytes(event_bytes)

    summary = ShardSupervisor(
        run_dir,
        retry_limit=0,
        process_isolation=True,
        max_parallel_generation_systems=2,
    ).resume(plan, _ParallelRecoveryWorker())

    assert summary.failed == (bad,)
    assert summary.completed == (good,)
    assert (bad_dir / "checkpoint.json").read_bytes() == checkpoint_bytes
    assert (bad_dir / "events.jsonl").read_bytes() == event_bytes
    failure = json.loads((bad_dir / "failure.json").read_text(encoding="utf-8"))
    assert failure["state"] == "failed_final"
    assert failure["error_kind"] == "INFRASTRUCTURE_ERROR"
    repeated = ShardSupervisor(run_dir, retry_limit=0).resume(plan, _ParallelRecoveryWorker())
    assert repeated.failed == (bad,)
    assert repeated.completed == (good,)
    status = ExperimentLifecycle(tmp_path).status(run_id)
    assert (status.completed, status.failed, status.invalid) == (1, 1, 1)


def test_marker_only_failure_identity_mismatch_is_run_level_refusal_without_rewrite(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 61, 1, 1, 60)
    original = RunPlan("marker-original", 2, (task,), 1, SourceRevision("original", None, None))
    changed = RunPlan("marker-changed", 2, (task,), 1, SourceRevision("changed", None, None))
    supervisor = ShardSupervisor(tmp_path, retry_limit=0)
    assert supervisor.run(original, _DurableStageWorker()).completed == (task,)
    shard_dir = tmp_path / "shards" / "neo4j-000"
    (shard_dir / "accepted_pairs.jsonl").unlink()
    assert supervisor.resume(original, lambda _task: pytest.fail("invalid terminal evidence must not rerun")).failed == (
        task,
    )
    (shard_dir / "checkpoint.json").unlink()
    marker_before = (shard_dir / "failure.json").read_bytes()
    events_before = (shard_dir / "events.jsonl").read_bytes()

    with pytest.raises(ManifestMismatchError):
        supervisor.resume(changed, lambda _task: pytest.fail("identity drift must refuse before worker execution"))

    assert not (shard_dir / "checkpoint.json").exists()
    assert (shard_dir / "failure.json").read_bytes() == marker_before
    assert (shard_dir / "events.jsonl").read_bytes() == events_before


def test_retry_degrades_raw_diagnostics_from_all_attempts_and_preserves_final_budget(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 71, 1, 1, 60)
    plan = RunPlan("budget-retry", 2, (task,), 1, SourceRevision(None, None, None))

    class BudgetRetryWorker:
        context = None

        def bind_attempt_context(self, context: AttemptContext) -> None:
            self.context = context

        def __call__(self, current_task):
            assert self.context is not None
            if self.context.attempt == 0:
                raw = self.context.attempt_dir / "gdsmith_raw"
                raw.mkdir()
                (raw / "failed.log").write_bytes(b"x" * (90 * 1024))
                raise RuntimeError("injected failed generation")
            return _durable_result(self.context, current_task)

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=1,
        shard_budget_bytes=128 * 1024,
    ).run(plan, BudgetRetryWorker())

    shard = tmp_path / "shards" / "neo4j-000"
    assert summary.completed == (task,)
    assert not (shard / "attempt-000" / "gdsmith_raw").exists()
    assert "SHARD_ARTIFACT_BUDGET_DEGRADED" in (shard / "artifact_warnings.jsonl").read_text()
    assert sum(path.stat().st_size for path in shard.rglob("*") if path.is_file()) <= 128 * 1024
