from types import SimpleNamespace

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.doctor_v2 import run_doctor
from graph_sym_perf.experiment.provenance import (
    verified_container_image_digest,
    verified_execution_provenance,
)


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
    assert age.generation_connectivity == "not_attempted"
    assert ladybug.generation_connectivity == "not_attempted"
    assert report.disk_required_bytes == 7 * 16 * 500 * 1024 * 1024


def test_container_digest_must_match_docker_inspect_not_only_environment_format(monkeypatch) -> None:
    expected = "sha256:" + "a" * 64
    environment = {
        "GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-under-test",
        "GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": expected,
    }
    observed = {}

    def inspect(command, **_kwargs):
        observed["command"] = command
        return SimpleNamespace(returncode=0, stdout=expected + "\n")

    monkeypatch.setattr("graph_sym_perf.experiment.provenance.subprocess.run", inspect)
    assert verified_container_image_digest(environment, TargetSystem.NEO4J) == expected
    assert observed["command"][-1] == "neo4j-under-test"

    monkeypatch.setattr(
        "graph_sym_perf.experiment.provenance.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="sha256:" + "b" * 64),
    )
    assert verified_container_image_digest(environment, TargetSystem.NEO4J) is None


def test_ladybugdb_provenance_binds_the_installed_embedded_package(monkeypatch) -> None:
    expected = "sha256:" + "c" * 64
    monkeypatch.setattr(
        "graph_sym_perf.experiment.provenance.installed_distribution_digest",
        lambda package: expected if package == "ladybug" else None,
    )

    provenance = verified_execution_provenance(
        {"GRAPHSYMPERF_LADYBUGDB_PACKAGE_DIGEST": expected},
        TargetSystem.LADYBUGDB,
    )

    assert provenance is not None
    assert provenance.mode == "embedded_package"
    assert provenance.digest == expected
    assert verified_execution_provenance(
        {"GRAPHSYMPERF_LADYBUGDB_PACKAGE_DIGEST": "sha256:" + "d" * 64},
        TargetSystem.LADYBUGDB,
    ) is None
