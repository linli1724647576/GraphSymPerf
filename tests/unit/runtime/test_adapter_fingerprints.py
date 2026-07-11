from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph_sym_perf.adapters import base as adapter_base
from graph_sym_perf.adapters.agensgraph import AgensGraphAdapter
from graph_sym_perf.adapters.base import docker_container_epoch_identity
from graph_sym_perf.adapters.apache_age import ApacheAgeAdapter
from graph_sym_perf.adapters.arcadedb import ArcadeDBAdapter
from graph_sym_perf.adapters.falkordb import FalkorDBAdapter
from graph_sym_perf.adapters.ladybugdb import LadybugDBAdapter
from graph_sym_perf.adapters.memgraph import MemgraphAdapter
from graph_sym_perf.adapters.neo4j import Neo4jAdapter
from graph_sym_perf.core import TargetSystem


CASES = (
    (
        Neo4jAdapter,
        TargetSystem.NEO4J,
        {"GRAPHSYMPERF_NEO4J_URI": "bolt://db.example:7687", "GRAPHSYMPERF_NEO4J_DATABASE": "neo", "GRAPHSYMPERF_NEO4J_PASSWORD": "secret-a"},
        {"database": "GRAPHSYMPERF_NEO4J_DATABASE"},
        "GRAPHSYMPERF_NEO4J_URI",
        "bolt://other.example:7687",
        "GRAPHSYMPERF_NEO4J_PASSWORD",
    ),
    (
        MemgraphAdapter,
        TargetSystem.MEMGRAPH,
        {"GRAPHSYMPERF_MEMGRAPH_HOST": "db.example", "GRAPHSYMPERF_MEMGRAPH_PORT": "7687", "GRAPHSYMPERF_MEMGRAPH_PASSWORD": "secret-a"},
        {"password": "GRAPHSYMPERF_MEMGRAPH_PASSWORD"},
        "GRAPHSYMPERF_MEMGRAPH_PORT",
        "7688",
        "GRAPHSYMPERF_MEMGRAPH_PASSWORD",
    ),
    (
        FalkorDBAdapter,
        TargetSystem.FALKORDB,
        {"GRAPHSYMPERF_FALKORDB_HOST": "db.example", "GRAPHSYMPERF_FALKORDB_PORT": "6379", "GRAPHSYMPERF_FALKORDB_GRAPH": "graph-a", "GRAPHSYMPERF_FALKORDB_PASSWORD": "secret-a"},
        {"password": "GRAPHSYMPERF_FALKORDB_PASSWORD"},
        "GRAPHSYMPERF_FALKORDB_GRAPH",
        "graph-b",
        "GRAPHSYMPERF_FALKORDB_PASSWORD",
    ),
    (
        AgensGraphAdapter,
        TargetSystem.AGENSGRAPH,
        {"GRAPHSYMPERF_AGENSGRAPH_URI": "postgresql://user:secret-a@db.example:5432/agens", "GRAPHSYMPERF_AGENSGRAPH_GRAPH": "graph-a"},
        {},
        "GRAPHSYMPERF_AGENSGRAPH_GRAPH",
        "graph-b",
        "GRAPHSYMPERF_AGENSGRAPH_URI",
    ),
    (
        ApacheAgeAdapter,
        TargetSystem.APACHE_AGE,
        {"GRAPHSYMPERF_APACHE_AGE_URI": "postgresql://user:secret-a@db.example:5432/age", "GRAPHSYMPERF_APACHE_AGE_GRAPH": "graph-a"},
        {},
        "GRAPHSYMPERF_APACHE_AGE_GRAPH",
        "graph-b",
        "GRAPHSYMPERF_APACHE_AGE_URI",
    ),
    (
        ArcadeDBAdapter,
        TargetSystem.ARCADEDB,
        {"GRAPHSYMPERF_ARCADEDB_HTTP_URL": "http://db.example:2480", "GRAPHSYMPERF_ARCADEDB_DATABASE": "graph-a", "GRAPHSYMPERF_ARCADEDB_PASSWORD": "secret-a"},
        {},
        "GRAPHSYMPERF_ARCADEDB_DATABASE",
        "graph-b",
        "GRAPHSYMPERF_ARCADEDB_PASSWORD",
    ),
    (
        LadybugDBAdapter,
        TargetSystem.LADYBUGDB,
        {"GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH": "/tmp/graph-a.lbug"},
        {},
        "GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH",
        "/tmp/graph-b.lbug",
        None,
    ),
)


def _adapter(adapter_type, target, env, optional_env, *, server_version="server-1", driver_version="driver-1"):
    adapter = object.__new__(adapter_type)
    adapter.target = target
    adapter.env = dict(env)
    adapter.system_config = SimpleNamespace(optional_env=optional_env)
    adapter._server_version = server_version
    adapter._driver_version = driver_version
    return adapter


@pytest.mark.parametrize(
    ("adapter_type", "target", "env", "optional_env", "endpoint_key", "endpoint_value", "secret_key"),
    CASES,
    ids=["neo4j", "memgraph", "falkordb", "agensgraph", "apache-age", "arcadedb", "ladybugdb"],
)
def test_fingerprint_is_stable_sensitive_and_secret_free(
    adapter_type,
    target,
    env,
    optional_env,
    endpoint_key,
    endpoint_value,
    secret_key,
) -> None:
    adapter = _adapter(adapter_type, target, env, optional_env)

    first = adapter.dbms_fingerprint()
    second = adapter.dbms_fingerprint()
    endpoint_changed = _adapter(adapter_type, target, {**env, endpoint_key: endpoint_value}, optional_env).dbms_fingerprint()
    server_changed = _adapter(adapter_type, target, env, optional_env, server_version="server-2").dbms_fingerprint()
    driver_changed = _adapter(adapter_type, target, env, optional_env, driver_version="driver-2").dbms_fingerprint()

    assert first.available
    assert first == second
    assert len(first.value) == 64
    assert endpoint_changed.value != first.value
    assert server_changed.value != first.value
    assert driver_changed.value != first.value
    assert "secret-a" not in first.value
    if secret_key:
        secret_value = env[secret_key].replace("secret-a", "secret-b") if "://" in env[secret_key] else "secret-b"
        secret_changed = _adapter(adapter_type, target, {**env, secret_key: secret_value}, optional_env).dbms_fingerprint()
        assert secret_changed.value == first.value


@pytest.mark.parametrize(
    ("adapter_type", "target", "env", "optional_env", "_endpoint_key", "_endpoint_value", "_secret_key"),
    CASES,
    ids=["neo4j", "memgraph", "falkordb", "agensgraph", "apache-age", "arcadedb", "ladybugdb"],
)
def test_fingerprint_is_unavailable_without_server_version(
    adapter_type,
    target,
    env,
    optional_env,
    _endpoint_key,
    _endpoint_value,
    _secret_key,
) -> None:
    fingerprint = _adapter(adapter_type, target, env, optional_env, server_version=None).dbms_fingerprint()

    assert not fingerprint.available
    assert fingerprint.value is None
    assert fingerprint.reason == "server_version_unavailable"


@pytest.mark.parametrize(
    ("adapter_type", "target", "env", "optional_env", "_endpoint_key", "_endpoint_value", "_secret_key"),
    CASES,
    ids=["neo4j", "memgraph", "falkordb", "agensgraph", "apache-age", "arcadedb", "ladybugdb"],
)
def test_health_epoch_is_stable_and_changes_with_boot_identity(
    adapter_type,
    target,
    env,
    optional_env,
    _endpoint_key,
    _endpoint_value,
    _secret_key,
) -> None:
    adapter = _adapter(adapter_type, target, env, optional_env)
    adapter._probe_health_epoch_identity = lambda: ("test_boot_id", "boot-one")

    first = adapter.dbms_health_epoch()
    second = adapter.dbms_health_epoch()
    adapter._probe_health_epoch_identity = lambda: ("test_boot_id", "boot-two")
    restarted = adapter.dbms_health_epoch()

    assert first.available
    assert first == second
    assert first.source == "test_boot_id"
    assert restarted.value != first.value


def test_health_epoch_fails_closed_when_probe_has_no_stable_identity() -> None:
    adapter_type, target, env, optional_env, *_ = CASES[0]
    adapter = _adapter(adapter_type, target, env, optional_env)
    adapter._probe_health_epoch_identity = lambda: None

    epoch = adapter.dbms_health_epoch()

    assert not epoch.available
    assert epoch.reason == "health_epoch_identity_unavailable"


def test_docker_container_epoch_is_stable_and_restart_sensitive(monkeypatch) -> None:
    observed = {}

    def inspect(command, **_kwargs):
        observed["command"] = command
        return SimpleNamespace(returncode=0, stdout="container-id|2026-07-11T12:00:00Z|2\n")

    monkeypatch.setattr("graph_sym_perf.adapters.base.subprocess.run", inspect)

    epoch = docker_container_epoch_identity(
        {"GRAPHSYMPERF_MEMGRAPH_CONTAINER": "graphsymperf-memgraph"},
        TargetSystem.MEMGRAPH,
    )

    assert epoch == ("docker_container_epoch", "container-id|2026-07-11T12:00:00Z|2")
    assert observed["command"][-1] == "graphsymperf-memgraph"


def test_docker_container_image_identity_is_observed_from_the_runtime(monkeypatch) -> None:
    def inspect(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="sha256:" + "a" * 64 + "\n")

    monkeypatch.setattr("graph_sym_perf.adapters.base.subprocess.run", inspect)

    identity = getattr(adapter_base, "docker_container_image_identity")(
        {"GRAPHSYMPERF_ARCADEDB_CONTAINER": "graphsymperf-arcadedb"},
        TargetSystem.ARCADEDB,
    )

    assert identity == "sha256:" + "a" * 64
