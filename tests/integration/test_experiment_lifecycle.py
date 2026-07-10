import json
import hashlib

import pytest

from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.supervisor import RunSummary


class _CompleteLifecycleWorker:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        hashes = [
            hashlib.sha256(f"{task.target.value}:{task.shard_index}:{index}".encode()).hexdigest()
            for index in range(task.accepted_query_pairs)
        ]
        (self.context.shard_dir / "accepted_pairs.jsonl").write_text(
            "".join(json.dumps({"pair_hash": value}) + "\n" for value in hashes),
            encoding="utf-8",
        )
        stages = {}
        for stage in ("screening", "validation", "reduction"):
            path = self.context.attempt_dir / f"{stage}.json"
            path.write_text(json.dumps({"stage": stage}), encoding="utf-8")
            stages[stage] = {
                "path": str(path.relative_to(self.context.shard_dir)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return {"accepted_pairs": len(hashes), "accepted_pair_hashes": hashes, "stage_evidence": stages}


def test_lifecycle_writes_immutable_manifest_and_summary(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    result = ExperimentLifecycle(tmp_path, process_isolation=False).run(spec, _CompleteLifecycleWorker())
    assert (result.run_dir / "resolved_manifest.json").exists()
    summary = json.loads((result.run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["completed_count"] == 112


def test_formal_lifecycle_enables_process_isolation(monkeypatch, tmp_path) -> None:
    captured = {}

    class _Supervisor:
        def __init__(self, _run_dir, **kwargs) -> None:
            captured.update(kwargs)

        def run(self, _plan, _worker) -> RunSummary:
            return RunSummary((), (), ())

    monkeypatch.setattr("graph_sym_perf.experiment.lifecycle.ShardSupervisor", _Supervisor)
    spec = load_experiment_spec("config/experiment.yaml")

    ExperimentLifecycle(tmp_path).run(spec, lambda _task: {"accepted_pairs": 0})

    assert captured["process_isolation"] is True
    assert captured["heartbeat_seconds"] == 10
    assert captured["stale_after_seconds"] == 90
    assert captured["max_parallel_generation_systems"] == 3
    assert captured["max_parallel_measurement_systems"] == 1
    assert captured["max_parallel_shards_per_system"] == 1


def test_lifecycle_binds_the_immutable_run_directory_to_a_shard_worker(tmp_path) -> None:
    class _Worker:
        bound_run_dir = None

        def bind_run_dir(self, run_dir) -> None:
            self.bound_run_dir = run_dir

        def __call__(self, task):
            return {"accepted_pairs": task.accepted_query_pairs}

    worker = _Worker()
    result = ExperimentLifecycle(tmp_path, process_isolation=False).run(
        load_experiment_spec("config/experiment.yaml"),
        worker,
    )

    assert worker.bound_run_dir == result.run_dir


def test_lifecycle_resume_reads_existing_terminal_checkpoints_without_rerunning_workers(tmp_path) -> None:
    lifecycle = ExperimentLifecycle(tmp_path, process_isolation=False)
    spec = load_experiment_spec("config/experiment.yaml")
    original = lifecycle.run(spec, _CompleteLifecycleWorker(), run_id="resume-me")

    def must_not_run(_task):
        raise AssertionError("completed shards must not rerun during resume")

    resumed = lifecycle.resume(spec, must_not_run, run_id=original.run_id)
    status = lifecycle.status(original.run_id)

    assert len(resumed.summary.completed) == 112
    assert resumed.summary.failed == ()
    assert status.total == 112
    assert status.completed == 112
    assert status.active == 0
    assert status.invalid == 0


def test_lifecycle_resume_rejects_tampered_resolved_manifest(tmp_path) -> None:
    lifecycle = ExperimentLifecycle(tmp_path, process_isolation=False)
    spec = load_experiment_spec("config/experiment.yaml")
    original = lifecycle.run(spec, lambda task: {"accepted_pairs": task.accepted_query_pairs}, run_id="tampered")
    manifest_path = original.run_dir / "resolved_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["spec_name"] = "changed-after-run"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable manifest"):
        lifecycle.resume(spec, lambda _task: {"accepted_pairs": 0}, run_id=original.run_id)
