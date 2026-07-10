from graph_sym_perf.cli.runtime_first import main


class _ReadyReport:
    ready = True


class _LifecycleResult:
    run_id = "formal-run"
    summary = type("Summary", (), {"completed": (), "incomplete": (), "failed": ()})()


class _RunStatus:
    run_id = "resume-me"
    total = 112
    completed = 100
    incomplete = 2
    failed = 1
    active = 9
    invalid = 0


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


def test_runtime_first_resume_calls_durable_lifecycle_instead_of_echoing_success(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Lifecycle:
        def __init__(self, output_root) -> None:
            calls["output_root"] = output_root

        def resume(self, spec, worker, *, run_id):
            calls.update(spec=spec, worker=worker, run_id=run_id)
            return _LifecycleResult()

    monkeypatch.setattr("graph_sym_perf.cli.runtime_first.ExperimentLifecycle", _Lifecycle)
    monkeypatch.setattr("graph_sym_perf.cli.runtime_first.RuntimeFirstShardWorker", lambda spec: ("worker", spec.name))

    exit_code = main(
        [
            "experiment",
            "resume",
            "--run-id",
            "resume-me",
            "--config",
            "config/experiment.yaml",
            "--output-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert calls["run_id"] == "resume-me"
    assert calls["worker"][0] == "worker"
    assert "formal-run" in capsys.readouterr().out


def test_runtime_first_status_reads_checkpoint_summary(monkeypatch, tmp_path, capsys) -> None:
    class _Lifecycle:
        def __init__(self, _output_root) -> None:
            return None

        def status(self, run_id):
            assert run_id == "resume-me"
            return _RunStatus()

    monkeypatch.setattr("graph_sym_perf.cli.runtime_first.ExperimentLifecycle", _Lifecycle)

    assert main(["experiment", "status", "--run-id", "resume-me", "--output-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "completed=100" in output
    assert "active=9" in output
