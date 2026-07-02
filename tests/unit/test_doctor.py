from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.experiment.doctor import run_doctor


def test_doctor_dry_run_skips_adapter_checks(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "runs" / "doctor_dry_run"
    manifest_path = tmp_path / "doctor_dry_run.yaml"
    manifest_path.write_text(
        "\n".join(
            [
                "experiment_id: doctor_dry_run",
                "description: dry-run smoke",
                f"output_root: {output_root.as_posix()}",
                "systems:",
                "  targets:",
                "    - neo4j",
                "source:",
                "  kind: gdsmith",
                "  base_run_config: config/graph_sym_perf.yaml",
                "",
            ]
        ),
        encoding="utf-8",
    )

    def fail_create_adapter(*args, **kwargs):
        raise AssertionError("dry-run must not create adapters")

    monkeypatch.setattr("graph_sym_perf.experiment.doctor.create_adapter", fail_create_adapter)

    exit_code = run_doctor(manifest_path, dry_run=True)

    assert exit_code == 0
    preflight_dir = output_root / "preflight"
    assert preflight_dir.exists()
    payload = json.loads((preflight_dir / "preflight.json").read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["statuses"] == []
    markdown = (preflight_dir / "preflight.md").read_text(encoding="utf-8")
    assert "Dry-run mode validated manifest loading" in markdown
    assert "Adapter creation and database capability checks were skipped." in markdown
