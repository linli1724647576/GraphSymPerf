import json

from graph_sym_perf.cli.runtime_first import main


def test_issue_export_requires_a_submit_ready_payload(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.json"
    assert main(["issue", "export", "--issue", str(missing), "--output", str(tmp_path / "out.json")]) != 0
    assert "missing gate" in capsys.readouterr().out


def test_issue_export_rejects_raw_json_that_only_claims_submit_ready(tmp_path, capsys) -> None:
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({"stage": "submit_ready", "reduction_status": "reduced_valid"}), encoding="utf-8")
    output = tmp_path / "out.json"

    assert main(["issue", "export", "--issue", str(forged), "--output", str(output)]) != 0
    assert not output.exists()
    assert "typed SubmitReadyIssue" in capsys.readouterr().out
