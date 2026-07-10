from graph_sym_perf.experiment.doctor_v2 import run_doctor


def test_doctor_reports_all_required_systems_and_refuses_missing_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GRAPHSYMPERF_GDSMITH_JAR", raising=False)
    report = run_doctor("config/experiment.yaml", environment={}, output_root=tmp_path)
    assert len(report.systems) == 7
    assert not report.ready
    assert (tmp_path / "doctor.json").exists()


def test_doctor_treats_proxy_generation_and_full_matrix_disk_budget_as_hard_gates(tmp_path) -> None:
    report = run_doctor("config/experiment.yaml", environment={}, output_root=tmp_path)
    age = next(system for system in report.systems if system.system == "apache_age")
    ladybug = next(system for system in report.systems if system.system == "ladybugdb")

    assert not age.generation_env_present
    assert not ladybug.generation_env_present
    assert report.disk_required_bytes == 7 * 16 * 500 * 1024 * 1024
