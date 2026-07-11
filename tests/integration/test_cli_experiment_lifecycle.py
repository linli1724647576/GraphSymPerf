from graph_sym_perf.cli.__main__ import main


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


class _GateLifecycleResult:
    run_id = "neo4j-gate-10"
    summary = type("Summary", (), {"completed": ("neo4j-000",), "incomplete": (), "failed": ()})()


def test_runtime_first_run_calls_only_the_public_experiment_interface(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Commands:
        def run(self, *, config, output_root):
            calls.update(config=config, output_root=output_root)
            return _LifecycleResult()

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)

    assert main(["experiment", "run", "--config", "config/experiment.yaml", "--output-root", str(tmp_path)]) == 0
    assert calls == {"config": "config/experiment.yaml", "output_root": str(tmp_path)}
    assert "formal-run" in capsys.readouterr().out


def test_runtime_first_plan_prints_exact_formal_budget(capsys) -> None:
    assert main(["experiment", "plan", "--config", "config/experiment.yaml"]) == 0
    assert "shards=112" in capsys.readouterr().out


def test_runtime_first_resume_calls_public_durable_interface(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Commands:
        def resume(self, *, config, output_root, run_id):
            calls.update(config=config, output_root=output_root, run_id=run_id)
            return _LifecycleResult()

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)

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
    assert "formal-run" in capsys.readouterr().out


def test_runtime_first_status_reads_checkpoint_summary(monkeypatch, tmp_path, capsys) -> None:
    class _Commands:
        def status(self, *, output_root, run_id):
            assert run_id == "resume-me"
            return _RunStatus()

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)

    assert main(["experiment", "status", "--run-id", "resume-me", "--output-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "completed=100" in output
    assert "active=9" in output


def test_advanced_commands_are_explicitly_marked(capsys) -> None:
    assert main(["experiment", "--help"]) == 0
    output = capsys.readouterr().out
    assert "advanced" in output
    assert "doctor" in output and "plan" in output and "gate" in output and "status" in output


def test_live_gate_calls_the_public_single_target_interface(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Commands:
        def gate(self, *, config, target, accepted_pairs, output_root):
            calls.update(
                config=config,
                target=target,
                accepted_pairs=accepted_pairs,
                output_root=output_root,
            )
            return _GateLifecycleResult()

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)

    assert main(
        [
            "experiment",
            "gate",
            "--target",
            "neo4j",
            "--accepted-pairs",
            "10",
            "--output-root",
            str(tmp_path),
        ]
    ) == 0
    assert calls["target"] == "neo4j"
    assert calls["accepted_pairs"] == 10
    assert "completed=1" in capsys.readouterr().out
