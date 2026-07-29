from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from graph_sym_perf.core import GdsmithConfig
from graph_sym_perf.gdsmith import check_gdsmith_tooling, neo4j_uri_to_host_port, parse_gdsmith_log
from graph_sym_perf.gdsmith import runner as gdsmith_runner
from graph_sym_perf.gdsmith.runner import _compact_large_text_artifact, _prune_rotated_dbms_logs, _redact_sensitive_command_parts
from graph_sym_perf.gdsmith.source import query_cases_from_gdsmith


def test_parse_gdsmith_log_splits_graph_and_queries() -> None:
    parsed = parse_gdsmith_log(
        """
CREATE (n0 :L0{id : 1});
MATCH (n0 :L0) RETURN n0.id;
MATCH (n0) SET n0.x = 1 RETURN n0;
WITH 1 AS x RETURN x;
"""
    )

    assert len(parsed.graph_statements) == 1
    assert [record.accepted for record in parsed.query_records] == [True, False, True]
    assert query_cases_from_gdsmith(parsed.query_records)[0].case_id == "gdsmith_case_0001"


def test_neo4j_uri_to_host_port() -> None:
    assert neo4j_uri_to_host_port("bolt://127.0.0.1:7687") == ("127.0.0.1", 7687)
    assert neo4j_uri_to_host_port("neo4j://localhost") == ("localhost", 7687)


def test_gdsmith_tooling_requires_destructive_guard(tmp_path: Path) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("placeholder", encoding="utf-8")
    status = check_gdsmith_tooling(GdsmithConfig(), {"GRAPHSYMPERF_GDSMITH_JAR": str(jar)})

    assert not status.ready
    assert "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE=1" in status.reason


def test_gdsmith_command_redaction_hides_password() -> None:
    command = ("java", "-jar", "GDsmith.jar", "neo4j", "--username", "neo4j", "--password", "secret")

    assert _redact_sensitive_command_parts(command) == (
        "java",
        "-jar",
        "GDsmith.jar",
        "neo4j",
        "--username",
        "neo4j",
        "--password",
        "<redacted>",
    )


def test_run_gdsmith_streams_process_output_to_files(tmp_path: Path, monkeypatch) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):
        assert "capture_output" not in kwargs
        assert kwargs["stdout"].name.endswith("gdsmith_stdout.txt")
        assert kwargs["stderr"].name.endswith("gdsmith_stderr.txt")
        kwargs["stdout"].write("stdout payload")
        kwargs["stderr"].write("stderr payload")
        log_dir = Path(kwargs["cwd"]) / "logs" / "neo4j"
        log_dir.mkdir(parents=True)
        (log_dir / "database0-cur.log").write_text(
            "CREATE (n0:L0 {id: 1});\nMATCH (n0:L0) RETURN n0.id;\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gdsmith_runner.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(gdsmith_runner.subprocess, "run", fake_run)

    result = gdsmith_runner.run_gdsmith(
        config=GdsmithConfig(timeout_seconds=1),
        env={
            "GRAPHSYMPERF_GDSMITH_JAR": str(jar),
            "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE": "1",
        },
        raw_dir=tmp_path / "raw",
        backend="neo4j",
        host="127.0.0.1",
        port=7687,
        username="neo4j",
        password="secret",
        num_queries=1,
        seed=1,
    )

    assert Path(result.stdout_path).read_text(encoding="utf-8") == "stdout payload"
    assert Path(result.stderr_path).read_text(encoding="utf-8") == "stderr payload"
    assert result.parsed.accepted_queries


def test_prune_rotated_dbms_logs_keeps_current_log(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs" / "arcadedb"
    log_dir.mkdir(parents=True)
    keep = log_dir / "database0-cur.log"
    rotated = log_dir / "database0-1.log"
    keep.write_text("current", encoding="utf-8")
    rotated.write_text("rotated", encoding="utf-8")

    _prune_rotated_dbms_logs(tmp_path, "arcadedb", keep_path=keep)

    assert keep.read_text(encoding="utf-8") == "current"
    assert not rotated.exists()


def test_run_gdsmith_parses_partial_logs_after_timeout(tmp_path: Path, monkeypatch) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):
        log_dir = Path(kwargs["cwd"]) / "logs" / "memgraph"
        log_dir.mkdir(parents=True)
        (log_dir / "database0-cur.log").write_text(
            "CREATE (n0:L0 {id: 1});\nMATCH (n0:L0) RETURN n0.id;\n",
            encoding="utf-8",
        )
        raise subprocess.TimeoutExpired(command, timeout=16)

    monkeypatch.setattr(gdsmith_runner.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(gdsmith_runner.subprocess, "run", fake_run)

    result = gdsmith_runner.run_gdsmith(
        config=GdsmithConfig(timeout_seconds=1),
        env={
            "GRAPHSYMPERF_GDSMITH_JAR": str(jar),
            "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE": "1",
        },
        raw_dir=tmp_path / "raw",
        backend="memgraph",
        host="127.0.0.1",
        port=7688,
        username="",
        password="",
        num_queries=1,
        seed=1,
    )

    assert result.returncode == 124
    assert len(result.parsed.accepted_queries) == 1


def test_run_gdsmith_marks_fatal_stderr_as_failure(tmp_path: Path, monkeypatch) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):
        kwargs["stdout"].write("stdout payload")
        kwargs["stderr"].write("java.lang.OutOfMemoryError: Java heap space\n")
        log_dir = Path(kwargs["cwd"]) / "logs" / "neo4j"
        log_dir.mkdir(parents=True)
        (log_dir / "database0-cur.log").write_text(
            "CREATE (n0:L0 {id: 1});\nMATCH (n0:L0) RETURN n0.id;\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gdsmith_runner.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(gdsmith_runner.subprocess, "run", fake_run)

    result = gdsmith_runner.run_gdsmith(
        config=GdsmithConfig(timeout_seconds=1),
        env={
            "GRAPHSYMPERF_GDSMITH_JAR": str(jar),
            "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE": "1",
        },
        raw_dir=tmp_path / "raw",
        backend="neo4j",
        host="127.0.0.1",
        port=7687,
        username="neo4j",
        password="secret",
        num_queries=1,
        seed=1,
    )

    assert result.process_returncode == 0
    assert result.returncode == 1
    assert result.fatal_stderr_markers == ("OutOfMemoryError",)


def test_run_gdsmith_marks_neo4j_java_heap_space_as_failure(tmp_path: Path, monkeypatch) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):
        kwargs["stderr"].write(
            "org.neo4j.driver.exceptions.TransientException: Java heap space\n"
        )
        log_dir = Path(kwargs["cwd"]) / "logs" / "neo4j"
        log_dir.mkdir(parents=True)
        (log_dir / "database0-cur.log").write_text(
            "CREATE (n0:L0 {id: 1});\nMATCH (n0:L0) RETURN n0.id;\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gdsmith_runner.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(gdsmith_runner.subprocess, "run", fake_run)

    result = gdsmith_runner.run_gdsmith(
        config=GdsmithConfig(timeout_seconds=1),
        env={
            "GRAPHSYMPERF_GDSMITH_JAR": str(jar),
            "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE": "1",
        },
        raw_dir=tmp_path / "raw",
        backend="neo4j",
        host="127.0.0.1",
        port=7687,
        username="neo4j",
        password="secret",
        num_queries=1,
        seed=1,
    )

    assert result.process_returncode == 0
    assert result.returncode == 1
    assert result.fatal_stderr_markers == ("Java heap space",)


def test_compact_large_text_artifact_keeps_head_and_tail(tmp_path: Path) -> None:
    path = tmp_path / "gdsmith_stderr.txt"
    path.write_bytes(b"a" * 20 + b"middle" * 20 + b"z" * 20)

    _compact_large_text_artifact(path, max_bytes=40)

    compacted = path.read_bytes()
    assert len(compacted) < 200
    assert compacted.startswith(b"a" * 20)
    assert compacted.endswith(b"z" * 20)
    assert b"truncated this GDsmith stream artifact" in compacted
