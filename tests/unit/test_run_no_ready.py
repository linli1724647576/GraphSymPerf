from __future__ import annotations

from pathlib import Path

from graph_sym_perf.app.gdsmith_mining import run_gdsmith_mining


def test_gdsmith_run_without_env_writes_preflight_and_summary(tmp_path: Path) -> None:
    run_id = "unit-no-env"
    config_path = tmp_path / "graph_sym_perf.yaml"
    config_text = Path("config/graph_sym_perf.yaml").read_text(encoding="utf-8").replace(
        "artifact_dir: logs/{run_id}",
        f'artifact_dir: "{tmp_path.as_posix()}/{{run_id}}"',
    )
    config_path.write_text(config_text, encoding="utf-8")

    exit_code = run_gdsmith_mining(
        config_path=config_path,
        systems="neo4j",
        max_cases=1,
        run_id=run_id,
        env={},
    )

    assert exit_code == 3
    assert (tmp_path / run_id / "preflight.json").exists()
    assert (tmp_path / run_id / "summary.json").exists()
