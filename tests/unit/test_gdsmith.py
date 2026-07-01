from __future__ import annotations

from pathlib import Path

from graph_sym_perf.core import GdsmithConfig
from graph_sym_perf.gdsmith import check_gdsmith_tooling, neo4j_uri_to_host_port, parse_gdsmith_log
from graph_sym_perf.gdsmith.runner import _redact_sensitive_command_parts
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
