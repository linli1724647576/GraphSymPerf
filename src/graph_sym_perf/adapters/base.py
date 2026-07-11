"""Common adapter protocol and helpers."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from graph_sym_perf.core import ExecutionResult, PlanRecord, RunConfig, SystemConfig, TargetSystem
from graph_sym_perf.generation.corpus import GraphCorpus
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

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph | None:
        ...

    def load_graph(self, corpus: GraphCorpus) -> GdsmithReplayGraph | None:
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

    def dbms_fingerprint(self) -> "DBMSFingerprint":
        ...

    def dbms_health_epoch(self) -> "DBMSHealthEpoch":
        ...


@dataclass(frozen=True)
class DBMSFingerprint:
    value: str | None
    reason: str | None = None
    server_version: str | None = None
    driver_version: str | None = None

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class DBMSHealthEpoch:
    value: str | None
    source: str | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.value is not None


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
        self._server_version: str | None = None
        self._driver_version: str | None = None

    def check_dependency(self) -> tuple[bool, str]:
        missing = [name for name in self.dependency_modules if importlib.util.find_spec(name) is None]
        if missing:
            return False, f"missing Python module(s): {', '.join(missing)}"
        return True, "dependency available"

    def check_environment(self) -> tuple[bool, tuple[str, ...]]:
        missing = tuple(name for name in self.system_config.required_env if not self.env.get(name))
        return not missing, missing

    def load_graph(self, corpus: GraphCorpus) -> GdsmithReplayGraph | None:
        """Target-neutral graph loading seam; legacy method stays internal."""

        return self.load_gdsmith_graph(corpus.statements)  # type: ignore[attr-defined]

    def env_value(self, name: str, default: str | None = None) -> str | None:
        value = self.env.get(name)
        return value if value not in (None, "") else default

    def optional_env_value(self, key: str, default: str | None = None) -> str | None:
        env_name = self.system_config.optional_env.get(key)
        if env_name is None:
            return default
        return self.env_value(env_name, default)

    def dbms_fingerprint(self) -> DBMSFingerprint:
        server_version = getattr(self, "_server_version", None)
        if not server_version:
            return DBMSFingerprint(None, "server_version_unavailable")
        driver_package = self.dependency_modules[0] if self.dependency_modules else None
        driver_version = getattr(self, "_driver_version", None) or _package_version(driver_package)
        if not driver_package or not driver_version:
            return DBMSFingerprint(None, "driver_version_unavailable")
        endpoint = self._fingerprint_endpoint_identity()
        if not endpoint:
            return DBMSFingerprint(None, "endpoint_identity_unavailable")
        payload = {
            "target": self.target.value,
            "adapter_type": f"{type(self).__module__}.{type(self).__qualname__}",
            "driver_package": driver_package,
            "driver_version": str(driver_version),
            "endpoint": endpoint,
            "server_version": str(server_version),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return DBMSFingerprint(
            sha256(canonical.encode("utf-8")).hexdigest(),
            server_version=str(server_version),
            driver_version=str(driver_version),
        )

    def _fingerprint_endpoint_identity(self) -> Mapping[str, object]:
        return {}

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        try:
            probe = self._probe_health_epoch_identity()
        except Exception as exc:
            return DBMSHealthEpoch(None, reason=f"health_epoch_probe_failed:{type(exc).__name__}")
        if (
            not isinstance(probe, tuple)
            or len(probe) != 2
            or not isinstance(probe[0], str)
            or not probe[0]
            or probe[1] in (None, "")
        ):
            return DBMSHealthEpoch(None, reason="health_epoch_identity_unavailable")
        source, identity = probe
        endpoint = self._fingerprint_endpoint_identity()
        if not endpoint:
            return DBMSHealthEpoch(None, source=source, reason="endpoint_identity_unavailable")
        payload = {"target": self.target.value, "endpoint": endpoint, "source": source, "identity": identity}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
        return DBMSHealthEpoch(sha256(canonical.encode("utf-8")).hexdigest(), source=source)

    def _probe_health_epoch_identity(self) -> tuple[str, object] | None:
        return None

    def _cache_server_version(self, value: object) -> None:
        if value not in (None, ""):
            self._server_version = str(value)


def normalized_uri_endpoint(uri: str | None, *, default_port: int, database: str | None = None) -> Mapping[str, object]:
    if not uri:
        return {}
    parsed = urlparse(uri)
    return {
        "scheme": parsed.scheme.lower(),
        "host": (parsed.hostname or "").lower(),
        "port": parsed.port or default_port,
        "database": database or parsed.path.lstrip("/") or None,
    }


def normalized_file_endpoint(path: str | None) -> Mapping[str, object]:
    if not path:
        return {}
    return {"database_path": str(Path(path).expanduser().resolve(strict=False))}


def docker_container_epoch_identity(
    environment: Mapping[str, str],
    target: TargetSystem,
) -> tuple[str, object] | None:
    container = environment.get(f"GRAPHSYMPERF_{target.value.upper()}_CONTAINER", "")
    if not container:
        return None
    try:
        completed = subprocess.run(
            (
                "docker",
                "inspect",
                "--format={{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}",
                container,
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identity = completed.stdout.strip() if completed.returncode == 0 else ""
    return ("docker_container_epoch", identity) if identity else None


def _package_version(package: str | None) -> str | None:
    if not package:
        return None
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None
