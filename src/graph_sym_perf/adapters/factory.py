"""Adapter factory."""

from __future__ import annotations

from typing import Mapping

from graph_sym_perf.adapters.agensgraph import AgensGraphAdapter
from graph_sym_perf.adapters.apache_age import ApacheAgeAdapter
from graph_sym_perf.adapters.arcadedb import ArcadeDBAdapter
from graph_sym_perf.adapters.base import GraphAdapter
from graph_sym_perf.adapters.falkordb import FalkorDBAdapter
from graph_sym_perf.adapters.ladybugdb import LadybugDBAdapter
from graph_sym_perf.adapters.memgraph import MemgraphAdapter
from graph_sym_perf.adapters.neo4j import Neo4jAdapter
from graph_sym_perf.core import ConfigError, RunConfig, SystemConfig, TargetSystem

_ADAPTERS: dict[TargetSystem, type[GraphAdapter]] = {
    TargetSystem.NEO4J: Neo4jAdapter,
    TargetSystem.MEMGRAPH: MemgraphAdapter,
    TargetSystem.FALKORDB: FalkorDBAdapter,
    TargetSystem.AGENSGRAPH: AgensGraphAdapter,
    TargetSystem.APACHE_AGE: ApacheAgeAdapter,
    TargetSystem.ARCADEDB: ArcadeDBAdapter,
    TargetSystem.LADYBUGDB: LadybugDBAdapter,
}


def create_adapter(
    system_config: SystemConfig,
    run_config: RunConfig,
    env: Mapping[str, str],
    *,
    allow_create: bool = False,
) -> GraphAdapter:
    adapter_cls = _ADAPTERS.get(system_config.target)
    if adapter_cls is None:
        raise ConfigError(f"unsupported target: {system_config.target.value}")
    return adapter_cls(system_config, run_config, env, allow_create=allow_create)
