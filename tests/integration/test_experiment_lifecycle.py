import json

from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle
from graph_sym_perf.experiment.spec import load_experiment_spec


def test_lifecycle_writes_immutable_manifest_and_summary(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    result = ExperimentLifecycle(tmp_path).run(spec, lambda task: {"accepted_pairs": task.accepted_query_pairs})
    assert (result.run_dir / "resolved_manifest.json").exists()
    summary = json.loads((result.run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["completed_count"] == 112
