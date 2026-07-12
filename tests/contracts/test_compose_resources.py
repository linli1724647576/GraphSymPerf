from __future__ import annotations

from pathlib import Path

import yaml


def test_postgres_graph_services_have_shared_memory_headroom() -> None:
    compose = yaml.safe_load(Path("infra/docker-compose.graphdbs.yml").read_text(encoding="utf-8"))

    for service_name in ("agensgraph", "agensgraph-age-proxy", "apache-age"):
        assert compose["services"][service_name]["shm_size"] == "512mb"


def test_gdsmith_neo4j_proxies_use_independent_containers_and_bolt_ports() -> None:
    compose = yaml.safe_load(Path("infra/docker-compose.graphdbs.yml").read_text(encoding="utf-8"))

    ladybug = compose["services"]["neo4j-ladybug-proxy"]
    arcadedb = compose["services"]["neo4j-arcadedb-proxy"]
    assert ladybug["container_name"] != arcadedb["container_name"]
    assert "7697:7687" in ladybug["ports"]
    assert "7698:7687" in arcadedb["ports"]
    assert arcadedb["environment"]["NEO4J_AUTH"] == "none"


def test_memgraph_has_a_bounded_memory_and_query_runtime_envelope() -> None:
    compose = yaml.safe_load(Path("infra/docker-compose.graphdbs.yml").read_text(encoding="utf-8"))

    command = compose["services"]["memgraph"]["command"]
    assert "--memory-limit=1024" in command
    assert "--query-execution-timeout-sec=30" in command


def test_arcadedb_has_a_bounded_heap_and_server_side_command_timeout() -> None:
    compose = yaml.safe_load(Path("infra/docker-compose.graphdbs.yml").read_text(encoding="utf-8"))

    environment = compose["services"]["arcadedb"]["environment"]
    assert environment["ARCADEDB_OPTS_MEMORY"] == "-Xms1G -Xmx2G"
    assert "-Darcadedb.command.timeout=30000" in environment["JAVA_TOOL_OPTIONS"]
