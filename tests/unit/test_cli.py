from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.cli.__main__ import main


def test_cli_cluster_subcommand_writes_summary(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text('{"run_id":"cli-run"}\n', encoding="utf-8")
    (tmp_path / "cases.jsonl").write_text("", encoding="utf-8")

    exit_code = main(["cluster", "--run-dir", str(tmp_path)])

    assert exit_code == 0
    assert (tmp_path / "clustered_summary.json").exists()


def test_cli_gdsmith_flags_are_forwarded(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_gdsmith_mining(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("graph_sym_perf.cli.__main__.run_gdsmith_mining", fake_run_gdsmith_mining)

    exit_code = main(
        [
            "run-gdsmith-mining",
            "--config",
            "config/graph_sym_perf.yaml",
            "--systems",
            "neo4j",
            "--feedback",
            "--max-rules-per-case",
            "3",
            "--transform-family",
            "bool_redundancy",
        ]
    )

    assert exit_code == 0
    assert captured["feedback_enabled"] is True
    assert captured["max_rules_per_case"] == 3
    assert captured["transform_family"] == "bool_redundancy"


def test_cli_experiment_run_flags_are_forwarded(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_experiment(
        manifest_path,
        *,
        shard=None,
        num_shards=None,
        systems=None,
        max_cases=None,
        feedback_enabled=None,
        resume=None,
    ):
        captured["manifest_path"] = manifest_path
        captured["shard"] = shard
        captured["num_shards"] = num_shards
        captured["systems"] = systems
        captured["max_cases"] = max_cases
        captured["feedback_enabled"] = feedback_enabled
        captured["resume"] = resume
        return 0

    monkeypatch.setattr("graph_sym_perf.cli.__main__.run_experiment", fake_run_experiment)

    exit_code = main(
        [
            "experiment",
            "run",
            "--manifest",
            "config/experiments/main_effectiveness.yaml",
            "--shard",
            "1",
            "--num-shards",
            "16",
            "--systems",
            "neo4j,memgraph",
            "--max-cases",
            "25",
            "--no-feedback",
            "--no-resume",
        ]
    )

    assert exit_code == 0
    assert captured["manifest_path"] == "config/experiments/main_effectiveness.yaml"
    assert captured["shard"] == 1
    assert captured["num_shards"] == 16
    assert captured["systems"] == "neo4j,memgraph"
    assert captured["max_cases"] == 25
    assert captured["feedback_enabled"] is False
    assert captured["resume"] is False


def test_cli_validate_issue_search_flag_is_forwarded(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_validate_triage(triage_path, *, runtime_repeats=15, reduce=False, issue_search=False):
        captured["triage_path"] = triage_path
        captured["runtime_repeats"] = runtime_repeats
        captured["reduce"] = reduce
        captured["issue_search"] = issue_search
        return {"validated": 1, "passed": 1}

    monkeypatch.setattr("graph_sym_perf.cli.__main__.validate_triage", fake_validate_triage)

    exit_code = main(["validate", "--triage", "triage_top100.json", "--runtime-repeats", "3", "--reduce", "--issue-search"])

    assert exit_code == 0
    assert captured == {
        "triage_path": "triage_top100.json",
        "runtime_repeats": 3,
        "reduce": True,
        "issue_search": True,
    }


def test_cli_replay_dry_run(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_known_replay(config_path, *, case_ids=(), dry_run=False):
        captured["config_path"] = config_path
        captured["case_ids"] = case_ids
        captured["dry_run"] = dry_run
        return 0, {"case_count": 0}

    monkeypatch.setattr("graph_sym_perf.cli.__main__.run_known_replay", fake_run_known_replay)

    exit_code = main(["replay", "--all-known", "--config", "config/experiments/replay_known.yaml", "--dry-run"])

    assert exit_code == 0
    assert captured["config_path"] == "config/experiments/replay_known.yaml"
    assert captured["dry_run"] is True


def test_cli_doctor_dry_run(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_doctor(config_path, *, strict=False, dry_run=False):
        captured["config_path"] = config_path
        captured["strict"] = strict
        captured["dry_run"] = dry_run
        return 0

    monkeypatch.setattr("graph_sym_perf.cli.__main__.run_doctor", fake_run_doctor)

    exit_code = main(["doctor", "--config", "config/experiments/calibration.yaml", "--dry-run"])

    assert exit_code == 0
    assert captured["config_path"] == "config/experiments/calibration.yaml"
    assert captured["strict"] is False
    assert captured["dry_run"] is True


def test_cli_report_exports(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_export_run_report(run_dir):
        captured["run_dir"] = run_dir
        return {"run_dir": run_dir}

    monkeypatch.setattr("graph_sym_perf.cli.__main__.export_run_report", fake_export_run_report)

    exit_code = main(["report", "--run-dir", "runs/main_effectiveness_2026_01"])

    assert exit_code == 0
    assert captured["run_dir"] == "runs/main_effectiveness_2026_01"
