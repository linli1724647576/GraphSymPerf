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


class _CampaignResult:
    run_id = "p1-run"
    passed = True
    summary = type(
        "Summary",
        (),
        {
            "completed": ("cell",),
            "underfilled": (),
            "quarantined": (),
            "resource_exhausted": (),
            "infrastructure_failed": (),
        },
    )()


def test_runtime_first_run_calls_only_the_public_experiment_interface(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Commands:
        def run(self, **kwargs):
            calls.update(kwargs)
            return _LifecycleResult()

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)

    assert main(["experiment", "run", "--config", "config/experiment.yaml", "--output-root", str(tmp_path)]) == 0
    assert calls == {
        "config": "config/experiment.yaml",
        "output_root": str(tmp_path),
        "phase": None,
        "run_id": None,
        "parent_run": None,
        "selection": None,
    }
    assert "formal-run" in capsys.readouterr().out


def test_cli_never_drops_parent_or_selection_when_phase_is_absent(monkeypatch, tmp_path) -> None:
    calls = {}

    class _Commands:
        def run(self, **kwargs):
            calls.update(kwargs)
            raise ValueError("phase ancestry arguments require a schema-v3 config")

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)

    assert main(
        [
            "experiment",
            "run",
            "--config",
            "config/experiment.yaml",
            "--parent-run",
            "p0-run",
            "--selection",
            str(tmp_path / "selection.json"),
        ]
    ) == 2
    assert calls["phase"] is None
    assert calls["parent_run"] == "p0-run"
    assert calls["selection"].endswith("selection.json")


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


def test_schema_v3_plan_routes_phase_and_selection_through_public_interface(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Plan:
        entries = (1, 2, 3)
        discovery_accepted_pair_budget = 3000
        control_accepted_pair_budget = 0

    class _Commands:
        def plan(self, *, config, output, phase, selection):
            calls.update(config=config, output=output, phase=phase, selection=selection)
            return _Plan()

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)
    selection = tmp_path / "pilot_selection.json"
    assert main(
        [
            "experiment",
            "plan",
            "--config",
            "config/neo4j_validated_runtime_yield.yaml",
            "--phase",
            "p2",
            "--selection",
            str(selection),
        ]
    ) == 0
    assert calls["phase"] == "p2"
    assert calls["selection"] == str(selection)
    assert "entries=3" in capsys.readouterr().out


def test_schema_v3_run_routes_ancestry_without_legacy_parallel_entrypoint(monkeypatch, tmp_path, capsys) -> None:
    calls = {}

    class _Commands:
        def run(self, **kwargs):
            calls.update(kwargs)
            return _CampaignResult()

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)
    assert main(
        [
            "experiment",
            "run",
            "--config",
            "config/neo4j_validated_runtime_yield.yaml",
            "--phase",
            "p1",
            "--run-id",
            "p1-run",
            "--parent-run",
            "p0-run",
            "--output-root",
            str(tmp_path),
        ]
    ) == 0
    assert calls == {
        "config": "config/neo4j_validated_runtime_yield.yaml",
        "output_root": str(tmp_path),
        "phase": "p1",
        "run_id": "p1-run",
        "parent_run": "p0-run",
        "selection": None,
    }
    assert "phase-run" in capsys.readouterr().out


def test_schema_v3_select_and_report_use_unified_experiment_cli(monkeypatch, tmp_path, capsys) -> None:
    calls = []

    class _Selection:
        status = "selected"
        cells = (1, 2)

    class _Commands:
        def select(self, *, config, campaign_root, p1_run, output):
            calls.append(("select", config, campaign_root, p1_run, output))
            return _Selection()

        def report(self, *, config, campaign_root):
            calls.append(("report", config, campaign_root))
            return {"phases": [{"run_id": "p1-run"}]}

    monkeypatch.setattr("graph_sym_perf.cli.__main__.ExperimentCommands", _Commands)
    output = tmp_path / "pilot_selection.json"
    assert main(
        [
            "experiment",
            "select",
            "--config",
            "config/neo4j_validated_runtime_yield.yaml",
            "--campaign-root",
            str(tmp_path),
            "--p1-run",
            "p1-run",
            "--output",
            str(output),
        ]
    ) == 0
    assert main(
        [
            "experiment",
            "report",
            "--config",
            "config/neo4j_validated_runtime_yield.yaml",
            "--campaign-root",
            str(tmp_path),
        ]
    ) == 0
    assert calls[0][0] == "select"
    assert calls[1][0] == "report"
    output_text = capsys.readouterr().out
    assert "selected=2" in output_text
    assert "phases=1" in output_text
