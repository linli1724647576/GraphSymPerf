import json

from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.supervisor import RunSummary


def test_lifecycle_writes_immutable_manifest_and_summary(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    result = ExperimentLifecycle(tmp_path).run(spec, lambda task: {"accepted_pairs": task.accepted_query_pairs})
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
