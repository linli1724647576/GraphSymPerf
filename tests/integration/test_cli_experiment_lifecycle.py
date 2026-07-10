from graph_sym_perf.cli.runtime_first import main


class _ReadyReport:
    ready = True


class _LifecycleResult:
    run_id = "formal-run"
    summary = type("Summary", (), {"completed": (), "incomplete": (), "failed": ()})()


def test_runtime_first_run_starts_lifecycle_only_after_doctor_passes(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Lifecycle:
        def __init__(self, output_root) -> None:
            calls["output_root"] = output_root

        def run(self, spec, worker) -> _LifecycleResult:
            calls["spec"] = spec
            calls["worker"] = worker
            return _LifecycleResult()

    monkeypatch.setattr("graph_sym_perf.cli.runtime_first.run_doctor", lambda _config, **_kwargs: _ReadyReport())
    monkeypatch.setattr("graph_sym_perf.cli.runtime_first.ExperimentLifecycle", _Lifecycle)
    monkeypatch.setattr("graph_sym_perf.cli.runtime_first.RuntimeFirstShardWorker", lambda spec: object())

    assert main(["experiment", "run", "--config", "config/experiment.yaml", "--output-root", str(tmp_path)]) == 0
    assert calls["worker"]
    assert "formal-run" in capsys.readouterr().out


def test_runtime_first_plan_prints_exact_formal_budget(capsys) -> None:
    assert main(["experiment", "plan", "--config", "config/experiment.yaml"]) == 0
    assert "shards=112" in capsys.readouterr().out
