"""Adapter factory."""

from __future__ import annotations

from typing import Mapping

from graph_sym_perf.adapters.base import GraphAdapter
from graph_sym_perf.adapters.kuzu import KuzuAdapter
from graph_sym_perf.adapters.memgraph import MemgraphAdapter
from graph_sym_perf.adapters.neo4j import Neo4jAdapter
from graph_sym_perf.core import ConfigError, RunConfig, SystemConfig, TargetSystem


def create_adapter(
    system_config: SystemConfig,
    run_config: RunConfig,
    env: Mapping[str, str],
    *,
    allow_create: bool = False,
) -> GraphAdapter:
    if system_config.target is TargetSystem.NEO4J:
        return Neo4jAdapter(system_config, run_config, env, allow_create=allow_create)
    if system_config.target is TargetSystem.MEMGRAPH:
        return MemgraphAdapter(system_config, run_config, env, allow_create=allow_create)
    if system_config.target is TargetSystem.KUZU:
        return KuzuAdapter(system_config, run_config, env, allow_create=allow_create)
    raise ConfigError(f"unsupported target: {system_config.target.value}")
