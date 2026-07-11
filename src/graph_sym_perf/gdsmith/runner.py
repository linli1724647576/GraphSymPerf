"""Run GDsmith as an external Java process."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from graph_sym_perf.core import ConfigError, GdsmithConfig
from graph_sym_perf.gdsmith.parser import GdsmithParsedLog, parse_gdsmith_log

_MAX_CAPTURE_ARTIFACT_BYTES = 10 * 1024 * 1024
_CAPTURE_ARTIFACT_EDGE_BYTES = 2 * 1024 * 1024
_FATAL_STDERR_RETURNCODE = 1
_FATAL_STDERR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("OutOfMemoryError", "OutOfMemoryError"),
    ("StackOverflowError", "StackOverflowError"),
    ("NoClassDefFoundError", "NoClassDefFoundError"),
    ("UnsupportedClassVersionError", "UnsupportedClassVersionError"),
    ("Exception in thread", "Exception in thread"),
)


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
    process_returncode: int
    raw_dir: str
    stdout_path: str
    stderr_path: str
    log_path: str | None
    parsed: GdsmithParsedLog
    fatal_stderr_markers: tuple[str, ...] = ()
    artifact_budget_exceeded: bool = False


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
    extra_args: tuple[str, ...] = (),
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
        # Upstream GDsmith currently returns the configured error exit code once
        # the worker thread terminates, even on successful completion. Keep the
        # process-level code at 0 and derive an effective failure code from the
        # observed stderr below.
        "--exit-code-error",
        "0",
        "--print-progress-information=false",
        "--timeout-seconds",
        str(int(config.timeout_seconds)),
        *extra_args,
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
        extra_args=config.backend_extra_args.get(backend, ()),
    )
    artifact_command = _redact_sensitive_command_parts(command)
    (raw_dir / "gdsmith_command.json").write_text(
        json.dumps({"command": artifact_command}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    stdout_path = raw_dir / "gdsmith_stdout.txt"
    stderr_path = raw_dir / "gdsmith_stderr.txt"
    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
        if config.max_raw_artifact_bytes is None:
            try:
                completed = subprocess.run(
                    command,
                    cwd=raw_dir,
                    env=dict(env),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    timeout=config.timeout_seconds + 15.0,
                    check=False,
                )
                process_returncode = completed.returncode
            except subprocess.TimeoutExpired:
                process_returncode = 124
            artifact_budget_exceeded = False
        else:
            process_returncode, artifact_budget_exceeded = _run_bounded_process(
                command,
                cwd=raw_dir,
                env=dict(env),
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=config.timeout_seconds + 15.0,
                max_raw_artifact_bytes=config.max_raw_artifact_bytes,
            )

    fatal_stderr_markers = _detect_fatal_stderr_markers(stderr_path)
    returncode = _effective_returncode(process_returncode, fatal_stderr_markers)

    log_path = _latest_dbms_log(raw_dir, backend)
    parsed = parse_gdsmith_log(log_path.read_text(encoding="utf-8") if log_path else "", max_cases=num_queries)
    if not parsed.accepted_queries and log_path is None:
        raise ConfigError(f"GDsmith completed without producing logs/{backend}/*-cur.log")
    _compact_large_text_artifact(stdout_path)
    _compact_large_text_artifact(stderr_path)
    _prune_rotated_dbms_logs(raw_dir, backend, keep_path=log_path)
    return GdsmithRunResult(
        command=artifact_command,
        returncode=returncode,
        process_returncode=process_returncode,
        raw_dir=str(raw_dir),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        log_path=str(log_path) if log_path is not None else None,
        parsed=parsed,
        fatal_stderr_markers=fatal_stderr_markers,
        artifact_budget_exceeded=artifact_budget_exceeded,
    )


def _run_bounded_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout,
    stderr,
    timeout: float,
    max_raw_artifact_bytes: int,
) -> tuple[int, bool]:
    if max_raw_artifact_bytes <= 0:
        raise ValueError("GDsmith raw artifact budget must be positive")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=stdout,
        stderr=stderr,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while True:
        returncode = process.poll()
        if returncode is not None:
            return int(returncode), False
        if _raw_directory_size(cwd) > max_raw_artifact_bytes:
            _terminate_process_group(process)
            return 125, True
        if time.monotonic() >= deadline:
            _terminate_process_group(process)
            return 124, False
        time.sleep(0.1)


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2.0)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2.0)


def _raw_directory_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


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


def _prune_rotated_dbms_logs(raw_dir: Path, backend: str, *, keep_path: Path | None) -> None:
    log_dir = raw_dir / "logs" / backend
    if not log_dir.exists() or keep_path is None:
        return
    keep = keep_path.resolve()
    for path in log_dir.glob("*.log"):
        if path.resolve() == keep:
            continue
        path.unlink(missing_ok=True)


def _effective_returncode(process_returncode: int, fatal_stderr_markers: tuple[str, ...]) -> int:
    if process_returncode != 0:
        return process_returncode
    if fatal_stderr_markers:
        return _FATAL_STDERR_RETURNCODE
    return 0


def _detect_fatal_stderr_markers(path: Path) -> tuple[str, ...]:
    try:
        text = _read_text_excerpt(path)
    except FileNotFoundError:
        return ()
    seen: list[str] = []
    for marker, pattern in _FATAL_STDERR_PATTERNS:
        if pattern in text and marker not in seen:
            seen.append(marker)
    return tuple(seen)


def _read_text_excerpt(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_text(encoding="utf-8", errors="ignore")
    edge = min(_CAPTURE_ARTIFACT_EDGE_BYTES, max_bytes // 2)
    with path.open("rb") as handle:
        head = handle.read(edge)
        handle.seek(max(0, size - edge))
        tail = handle.read(edge)
    return (head + b"\n...\n" + tail).decode("utf-8", errors="ignore")


def _compact_large_text_artifact(path: Path, *, max_bytes: int = _MAX_CAPTURE_ARTIFACT_BYTES) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size <= max_bytes:
        return
    edge = min(_CAPTURE_ARTIFACT_EDGE_BYTES, max_bytes // 2)
    with path.open("rb") as handle:
        head = handle.read(edge)
        handle.seek(max(0, size - edge))
        tail = handle.read(edge)
    marker = (
        f"\n\n[GraphSymPerf truncated this GDsmith stream artifact from {size} bytes "
        f"to preserve disk space; retained first {len(head)} and last {len(tail)} bytes.]\n\n"
    ).encode("utf-8")
    path.write_bytes(head + marker + tail)
