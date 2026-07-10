import pytest

from graph_sym_perf.issues.exporter import export_issue


def test_exporter_rejects_untyped_raw_candidate(tmp_path) -> None:
    with pytest.raises(TypeError):
        export_issue({"stage": "submit_ready"}, tmp_path / "issue.json")  # type: ignore[arg-type]
