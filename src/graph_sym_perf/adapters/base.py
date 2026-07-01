"""Common adapter protocol and helpers."""

from __future__ import annotations

import importlib.util
from typing import Any, Mapping, Protocol

from graph_sym_perf.core import ExecutionResult, PlanRecord, RunConfig, SystemConfig, TargetSystem
from graph_sym_perf.datasets import MiniLdbcGraph
from graph_sym_perf.gdsmith.replay import GdsmithReplayGraph


class GraphAdapter(Protocol):
    target: TargetSystem

    def check_dependency(self) -> tuple[bool, str]:
        ...

    def check_environment(self) -> tuple[bool, tuple[str, ...]]:
        ...

    def connectivity_check(self) -> tuple[bool, str]:
        ...

    def ensure_schema(self) -> None:
        ...

    def load_mini_ldbc(self, graph: MiniLdbcGraph) -> None:
        ...

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph | None:
        ...

    def execute(
        self,
        query: str,
        parameters: Mapping[str, Any],
        query_kind: str,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        ...

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        ...

    def profile(
        self,
        query: str,
        parameters: Mapping[str, Any],
        query_kind: str,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        ...

    def close(self) -> None:
        ...


class BaseAdapter:
    dependency_modules: tuple[str, ...] = ()

    def __init__(
        self,
        system_config: SystemConfig,
        run_config: RunConfig,
        env: Mapping[str, str],
        *,
        allow_create: bool = False,
    ) -> None:
        self.system_config = system_config
        self.run_config = run_config
        self.env = env
        self.allow_create = allow_create
        self.target = system_config.target

    def check_dependency(self) -> tuple[bool, str]:
        missing = [name for name in self.dependency_modules if importlib.util.find_spec(name) is None]
        if missing:
            return False, f"missing Python module(s): {', '.join(missing)}"
        return True, "dependency available"

    def check_environment(self) -> tuple[bool, tuple[str, ...]]:
        missing = tuple(name for name in self.system_config.required_env if not self.env.get(name))
        return not missing, missing

    def env_value(self, name: str, default: str | None = None) -> str | None:
        value = self.env.get(name)
        return value if value not in (None, "") else default

    def optional_env_value(self, key: str, default: str | None = None) -> str | None:
        env_name = self.system_config.optional_env.get(key)
        if env_name is None:
            return default
        return self.env_value(env_name, default)
