from graph_sym_perf.cli.runtime_first import main


def test_issue_export_requires_a_submit_ready_payload(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.json"
    assert main(["issue", "export", "--issue", str(missing), "--output", str(tmp_path / "out.json")]) != 0
    assert "missing gate" in capsys.readouterr().out
