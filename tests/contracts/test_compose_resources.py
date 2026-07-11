from __future__ import annotations

from pathlib import Path

import yaml


def test_postgres_graph_services_have_shared_memory_headroom() -> None:
    compose = yaml.safe_load(Path("infra/docker-compose.graphdbs.yml").read_text(encoding="utf-8"))

    for service_name in ("agensgraph", "agensgraph-age-proxy", "apache-age"):
        assert compose["services"][service_name]["shm_size"] == "512mb"
