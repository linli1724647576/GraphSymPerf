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
