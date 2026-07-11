from __future__ import annotations

from pathlib import Path
import subprocess
import hashlib
import json
from types import SimpleNamespace

from graph_sym_perf.core import GdsmithConfig
from graph_sym_perf.gdsmith import check_gdsmith_tooling, neo4j_uri_to_host_port, parse_gdsmith_log
from graph_sym_perf.gdsmith import runner as gdsmith_runner
from graph_sym_perf.gdsmith.runner import (
    _compact_large_text_artifact,
    _detect_fatal_stderr_markers,
    _prune_rotated_dbms_logs,
    _redact_sensitive_command_parts,
    _run_bounded_process,
)
from graph_sym_perf.gdsmith.source import query_cases_from_gdsmith


def _gdsmith_artifact_env(jar: Path) -> dict[str, str]:
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    jar.with_suffix(jar.suffix + ".manifest.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "source_commit": "a" * 40,
                "compatibility_guards": [
                    "reject_multi_label_node_patterns",
                    "render_safe_double_quoted_literals",
                    "reject_unsafe_double_quoted_tokens",
                    "jdbc_query_timeout_60s",
                    "exclude_unbuildable_tugraph_provider",
                    "shade_runnable_jar",
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "GRAPHSYMPERF_GDSMITH_JAR": str(jar),
        "GRAPHSYMPERF_GDSMITH_JAR_SHA256": "sha256:" + digest,
    }


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


def test_parse_gdsmith_log_never_puts_schema_ddl_in_the_graph_snapshot() -> None:
    parsed = parse_gdsmith_log(
        """
CREATE INDEX i0 IF NOT EXISTS FOR (n:L0) ON (n.k0);
CREATE VLABEL L0;
CREATE ELABEL T0;
CREATE (n0:L0 {id: 1});
MATCH (n0 {id: 1}), (n1 {id: 2}) MERGE (n0)-[r:T0 {id: 3}]->(n1);
MATCH (n0:L0) RETURN n0.id;
"""
    )

    assert parsed.graph_statements == (
        "CREATE (n0:L0 {id: 1})",
        "MATCH (n0 {id: 1}), (n1 {id: 2}) MERGE (n0)-[r:T0 {id: 3}]->(n1)",
    )
    assert [record.skip_reason for record in parsed.query_records[:3]] == [
        "SCHEMA_SETUP_NOT_REPLAYED",
        "SCHEMA_SETUP_NOT_REPLAYED",
        "SCHEMA_SETUP_NOT_REPLAYED",
    ]


def test_neo4j_uri_to_host_port() -> None:
    assert neo4j_uri_to_host_port("bolt://127.0.0.1:7687") == ("127.0.0.1", 7687)
    assert neo4j_uri_to_host_port("neo4j://localhost") == ("localhost", 7687)


def test_gdsmith_tooling_requires_destructive_guard(tmp_path: Path) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("placeholder", encoding="utf-8")
    status = check_gdsmith_tooling(GdsmithConfig(), {"GRAPHSYMPERF_GDSMITH_JAR": str(jar)})

    assert not status.ready
    assert "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE=1" in status.reason


def test_gdsmith_tooling_binds_jar_hash_manifest_and_source_commit(tmp_path: Path, monkeypatch) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("verified-jar", encoding="utf-8")
    environment = {
        **_gdsmith_artifact_env(jar),
        "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE": "1",
    }
    monkeypatch.setattr(gdsmith_runner.shutil, "which", lambda name: "/usr/bin/java")

    ready = check_gdsmith_tooling(
        GdsmithConfig(expected_source_commit="a" * 40),
        environment,
    )
    wrong_commit = check_gdsmith_tooling(
        GdsmithConfig(expected_source_commit="b" * 40),
        environment,
    )

    assert ready.ready
    assert ready.jar_sha256 == environment["GRAPHSYMPERF_GDSMITH_JAR_SHA256"]
    assert ready.build_manifest_ok
    assert not wrong_commit.ready


def test_gdsmith_tooling_requires_the_per_statement_timeout_guard(tmp_path: Path, monkeypatch) -> None:
    jar = tmp_path / "GDsmith.jar"
    jar.write_text("verified-jar", encoding="utf-8")
    environment = _gdsmith_artifact_env(jar)
    manifest_path = jar.with_suffix(jar.suffix + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["compatibility_guards"].remove("jdbc_query_timeout_60s")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(gdsmith_runner.shutil, "which", lambda name: "/usr/bin/java")

    status = check_gdsmith_tooling(
        GdsmithConfig(expected_source_commit="a" * 40),
        {**environment, "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE": "1"},
    )

    assert not status.ready
    assert not status.build_manifest_ok


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
            **_gdsmith_artifact_env(jar),
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
            **_gdsmith_artifact_env(jar),
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
            **_gdsmith_artifact_env(jar),
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


def test_gdsmith_database_connection_loss_is_fatal_even_when_upstream_exits_zero(tmp_path: Path) -> None:
    stderr = tmp_path / "gdsmith_stderr.txt"
    stderr.write_text(
        "org.neo4j.driver.exceptions.ServiceUnavailableException: Connection to the database terminated\n",
        encoding="utf-8",
    )

    assert _detect_fatal_stderr_markers(stderr) == ("DatabaseConnectionLost",)


def test_compact_large_text_artifact_keeps_head_and_tail(tmp_path: Path) -> None:
    path = tmp_path / "gdsmith_stderr.txt"
    path.write_bytes(b"a" * 20 + b"middle" * 20 + b"z" * 20)

    _compact_large_text_artifact(path, max_bytes=40)

    compacted = path.read_bytes()
    assert len(compacted) < 200
    assert compacted.startswith(b"a" * 20)
    assert compacted.endswith(b"z" * 20)
    assert b"truncated this GDsmith stream artifact" in compacted


def test_bounded_gdsmith_process_is_killed_before_raw_artifacts_fill_the_shard(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()

    class FakeProcess:
        pid = 999_999

        def poll(self):
            (raw / "growing.log").write_bytes(b"x" * 101)
            return None

    killed = []
    monkeypatch.setattr(gdsmith_runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(gdsmith_runner, "_terminate_process_group", lambda process: killed.append(process.pid))

    with (raw / "stdout").open("w") as stdout, (raw / "stderr").open("w") as stderr:
        returncode, exceeded, stalled = _run_bounded_process(
            ("java",),
            cwd=raw,
            env={},
            stdout=stdout,
            stderr=stderr,
            timeout=1,
            stall_timeout=1,
            max_raw_artifact_bytes=100,
        )

    assert (returncode, exceeded, stalled) == (125, True, False)
    assert killed == [999_999]


def test_bounded_gdsmith_process_returns_partial_timeout_after_no_artifact_progress(
    tmp_path,
    monkeypatch,
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()

    class FakeProcess:
        pid = 999_998

        def poll(self):
            return None

    ticks = iter((0.0, 0.0, 2.0))
    killed = []
    monkeypatch.setattr(gdsmith_runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(gdsmith_runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(gdsmith_runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(gdsmith_runner, "_terminate_process_group", lambda process: killed.append(process.pid))

    with (raw / "stdout").open("w") as stdout, (raw / "stderr").open("w") as stderr:
        returncode, exceeded, stalled = _run_bounded_process(
            ("java",),
            cwd=raw,
            env={},
            stdout=stdout,
            stderr=stderr,
            timeout=10,
            stall_timeout=1,
            max_raw_artifact_bytes=100,
        )

    assert (returncode, exceeded, stalled) == (124, False, True)
    assert killed == [999_998]
