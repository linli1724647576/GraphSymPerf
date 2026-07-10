from graph_sym_perf.experiment.doctor_v2 import run_doctor


def test_doctor_reports_all_required_systems_and_refuses_missing_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GRAPHSYMPERF_GDSMITH_JAR", raising=False)
    report = run_doctor("config/experiment.yaml", environment={}, output_root=tmp_path)
    assert len(report.systems) == 7
    assert not report.ready
    assert (tmp_path / "doctor.json").exists()
