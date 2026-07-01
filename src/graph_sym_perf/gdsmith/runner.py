"""Run GDsmith as an external Java process."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from graph_sym_perf.core import ConfigError, GdsmithConfig
from graph_sym_perf.gdsmith.parser import GdsmithParsedLog, parse_gdsmith_log


@dataclass(frozen=True)
class GdsmithToolStatus:
    state: str
    java_ok: bool
    jar_ok: bool
    destructive_ok: bool
    reason: str
    java_path: str | None
    jar_env: str
    jar_path: str | None
    allow_destructive_env: str

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True)
class GdsmithRunResult:
    command: tuple[str, ...]
    returncode: int
    raw_dir: str
    stdout_path: str
    stderr_path: str
    log_path: str | None
    parsed: GdsmithParsedLog


def check_gdsmith_tooling(config: GdsmithConfig, env: Mapping[str, str]) -> GdsmithToolStatus:
    java_path = shutil.which("java")
    jar_value = env.get(config.jar_env)
    jar_path = Path(jar_value).expanduser() if jar_value else None
    java_ok = java_path is not None
    jar_ok = jar_path is not None and jar_path.is_file()
    destructive_ok = env.get(config.allow_destructive_env) == "1"

    missing: list[str] = []
    if not java_ok:
        missing.append("java executable")
    if not jar_ok:
        missing.append(config.jar_env)
    if not destructive_ok:
        missing.append(f"{config.allow_destructive_env}=1")
    return GdsmithToolStatus(
        state="ready" if not missing else "skipped",
        java_ok=java_ok,
        jar_ok=jar_ok,
        destructive_ok=destructive_ok,
        reason="ready" if not missing else "missing " + ", ".join(missing),
        java_path=java_path,
        jar_env=config.jar_env,
        jar_path=str(jar_path) if jar_path is not None else None,
        allow_destructive_env=config.allow_destructive_env,
    )


def neo4j_uri_to_host_port(uri: str) -> tuple[str, int]:
    parsed = urlparse(uri)
    if not parsed.hostname:
        raise ConfigError(f"invalid Neo4j URI for GDsmith: {uri!r}")
    return parsed.hostname, parsed.port or 7687


def build_gdsmith_command(
    *,
    java_path: str,
    jar_path: str,
    config: GdsmithConfig,
    backend: str,
    host: str,
    port: int,
    username: str,
    password: str,
    num_queries: int,
    seed: int,
) -> tuple[str, ...]:
    return (
        java_path,
        "-jar",
        jar_path,
        "--algorithm",
        config.algorithm,
        "--num-tries",
        str(config.num_tries),
        "--num-queries",
        str(num_queries),
        "--random-seed",
        str(seed),
        "--exit-code-error",
        "0",
        "--print-progress-information=false",
        "--timeout-seconds",
        str(int(config.timeout_seconds)),
        backend,
        "--host",
        host,
        "--port",
        str(port),
        "--username",
        username,
        "--password",
        password,
    )


def run_gdsmith(
    *,
    config: GdsmithConfig,
    env: Mapping[str, str],
    raw_dir: Path,
    backend: str,
    host: str,
    port: int,
    username: str,
    password: str,
    num_queries: int,
    seed: int,
) -> GdsmithRunResult:
    status = check_gdsmith_tooling(config, env)
    if not status.ready or status.java_path is None or status.jar_path is None:
        raise ConfigError(f"GDsmith preflight failed: {status.reason}")

    raw_dir.mkdir(parents=True, exist_ok=True)
    command = build_gdsmith_command(
        java_path=status.java_path,
        jar_path=status.jar_path,
        config=config,
        backend=backend,
        host=host,
        port=port,
        username=username,
        password=password,
        num_queries=num_queries,
        seed=seed,
    )
    artifact_command = _redact_sensitive_command_parts(command)
    (raw_dir / "gdsmith_command.json").write_text(
        json.dumps({"command": artifact_command}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        command,
        cwd=raw_dir,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=config.timeout_seconds + 15.0,
        check=False,
    )
    stdout_path = raw_dir / "gdsmith_stdout.txt"
    stderr_path = raw_dir / "gdsmith_stderr.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    log_path = _latest_dbms_log(raw_dir, backend)
    parsed = parse_gdsmith_log(log_path.read_text(encoding="utf-8") if log_path else "", max_cases=num_queries)
    if not parsed.accepted_queries and log_path is None:
        raise ConfigError(f"GDsmith completed without producing logs/{backend}/*-cur.log")
    return GdsmithRunResult(
        command=artifact_command,
        returncode=completed.returncode,
        raw_dir=str(raw_dir),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        log_path=str(log_path) if log_path is not None else None,
        parsed=parsed,
    )


def _redact_sensitive_command_parts(command: tuple[str, ...]) -> tuple[str, ...]:
    redacted = list(command)
    sensitive_flags = {"--password"}
    for index, part in enumerate(redacted[:-1]):
        if part in sensitive_flags:
            redacted[index + 1] = "<redacted>"
    return tuple(redacted)


def _latest_dbms_log(raw_dir: Path, backend: str) -> Path | None:
    log_dir = raw_dir / "logs" / backend
    if not log_dir.exists():
        return None
    candidates = sorted(log_dir.glob("*-cur.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None
