"""Run GDsmith as an external Java process."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import threading
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from graph_sym_perf.core import ConfigError, GdsmithConfig
from graph_sym_perf.gdsmith.parser import GdsmithParsedLog, parse_gdsmith_log

_MAX_CAPTURE_ARTIFACT_BYTES = 10 * 1024 * 1024
_CAPTURE_ARTIFACT_EDGE_BYTES = 2 * 1024 * 1024
_FATAL_STDERR_RETURNCODE = 1
_QUERY_RECORD_LIMIT_SENTINEL = "java.lang.RuntimeException: total number reached"
_FATAL_STDERR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("OutOfMemoryError", "OutOfMemoryError"),
    ("StackOverflowError", "StackOverflowError"),
    ("NoClassDefFoundError", "NoClassDefFoundError"),
    ("UnsupportedClassVersionError", "UnsupportedClassVersionError"),
    ("Exception in thread", "Exception in thread"),
    ("DatabaseConnectionLost", "ServiceUnavailableException"),
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
    jar_sha256: str | None
    build_manifest_sha256: str | None
    build_manifest_ok: bool
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
    query_record_limit_reached: bool = False
    artifact_budget_exceeded: bool = False
    generation_stalled: bool = False


def check_gdsmith_tooling(config: GdsmithConfig, env: Mapping[str, str]) -> GdsmithToolStatus:
    java_path = shutil.which("java")
    jar_value = env.get(config.jar_env)
    jar_path = Path(jar_value).expanduser() if jar_value else None
    java_ok = java_path is not None
    jar_ok = jar_path is not None and jar_path.is_file()
    jar_sha256, manifest_sha256, manifest_ok = _verify_gdsmith_artifact(
        jar_path,
        env.get(config.jar_sha256_env),
        expected_source_commit=config.expected_source_commit,
    )
    destructive_ok = env.get(config.allow_destructive_env) == "1"

    missing: list[str] = []
    if not java_ok:
        missing.append("java executable")
    if not jar_ok:
        missing.append(config.jar_env)
    elif not manifest_ok:
        missing.append(f"verified {config.jar_sha256_env} and build manifest")
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
        jar_sha256=jar_sha256,
        build_manifest_sha256=manifest_sha256,
        build_manifest_ok=manifest_ok,
        allow_destructive_env=config.allow_destructive_env,
    )


def _verify_gdsmith_artifact(
    jar_path: Path | None,
    expected_digest: str | None,
    *,
    expected_source_commit: str | None,
) -> tuple[str | None, str | None, bool]:
    if jar_path is None or not jar_path.is_file():
        return None, None, False
    actual = "sha256:" + _file_sha256(jar_path)
    if expected_digest != actual:
        return actual, None, False
    manifest_path = jar_path.with_suffix(jar_path.suffix + ".manifest.json")
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return actual, None, False
    required_guards = {
        "reject_multi_label_node_patterns",
        "render_safe_double_quoted_literals",
        "reject_unsafe_double_quoted_tokens",
        "jdbc_query_timeout_60s",
        "jdbc_result_row_cap_1000",
        "neo4j_transaction_timeout_30s",
        "neo4j_result_row_cap_1000",
        "memgraph_transaction_timeout_30s",
        "exclude_unbuildable_tugraph_provider",
        "shade_runnable_jar",
    }
    valid = bool(
        isinstance(manifest, dict)
        and manifest.get("sha256") == actual.removeprefix("sha256:")
        and (
            expected_source_commit is None
            or manifest.get("source_commit") == expected_source_commit
        )
        and required_guards <= set(manifest.get("compatibility_guards", ()))
    )
    return actual, "sha256:" + hashlib.sha256(raw).hexdigest(), valid


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            generation_stalled = False
        else:
            process_returncode, artifact_budget_exceeded, generation_stalled = _run_bounded_process(
                command,
                cwd=raw_dir,
                env=dict(env),
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=config.timeout_seconds + 15.0,
                stall_timeout=config.stall_timeout_seconds,
                max_raw_artifact_bytes=config.max_raw_artifact_bytes,
            )

    fatal_stderr_markers = _detect_fatal_stderr_markers(stderr_path)
    query_record_limit_reached = _detect_query_record_limit_sentinel(stderr_path)
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
        query_record_limit_reached=query_record_limit_reached,
        artifact_budget_exceeded=artifact_budget_exceeded,
        generation_stalled=generation_stalled,
    )


def _run_bounded_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout,
    stderr,
    timeout: float,
    stall_timeout: float,
    max_raw_artifact_bytes: int,
) -> tuple[int, bool, bool]:
    if max_raw_artifact_bytes <= 0:
        raise ValueError("GDsmith raw artifact budget must be positive")
    if stall_timeout <= 0:
        raise ValueError("GDsmith stall timeout must be positive")
    capture_limit = min(_MAX_CAPTURE_ARTIFACT_BYTES, max(1, max_raw_artifact_bytes // 16))
    stdout_capture = _BoundedStreamCapture(capture_limit)
    stderr_capture = _BoundedStreamCapture(capture_limit)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("GDsmith process pipes are unavailable")
    readers = (
        threading.Thread(target=_drain_stream, args=(process.stdout, stdout_capture), daemon=True),
        threading.Thread(target=_drain_stream, args=(process.stderr, stderr_capture), daemon=True),
    )
    for reader in readers:
        reader.start()

    def finish(returncode: int, exceeded: bool, stalled: bool) -> tuple[int, bool, bool]:
        for reader in readers:
            reader.join(timeout=5.0)
            if reader.is_alive():
                raise RuntimeError("GDsmith output capture did not terminate")
        stdout.write(stdout_capture.render())
        stderr.write(stderr_capture.render())
        stdout.flush()
        stderr.flush()
        return returncode, exceeded, stalled

    deadline = time.monotonic() + timeout
    last_activity = time.monotonic()
    progress = _GenerationProgress(cwd)
    last_progress = progress.snapshot()
    while True:
        now = time.monotonic()
        returncode = process.poll()
        if returncode is not None:
            return finish(int(returncode), False, False)
        _prune_rotated_logs_during_run(cwd)
        current_size = _raw_directory_size(cwd)
        current_progress = progress.snapshot()
        if current_progress != last_progress:
            last_progress = current_progress
            last_activity = now
        if current_size > max_raw_artifact_bytes:
            _terminate_process_group(process)
            return finish(125, True, False)
        if now >= deadline:
            _terminate_process_group(process)
            return finish(124, False, False)
        if now - last_activity >= stall_timeout:
            _terminate_process_group(process)
            return finish(124, False, True)
        time.sleep(0.1)


class _GenerationProgress:
    """Track semantic GDsmith progress while ignoring background log noise."""

    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir
        self._offsets: dict[Path, int] = {}
        self._partials: dict[Path, bytes] = {}
        self._statement_records = 0

    def snapshot(self) -> tuple[int, int]:
        coverage_size = sum(_existing_size(self.raw_dir / name) for name in ("coverage_log", "coverage_info"))
        for path in sorted(self.raw_dir.glob("logs/*/*-cur.log")):
            self._scan_appended_statements(path)
        return coverage_size, self._statement_records

    def _scan_appended_statements(self, path: Path) -> None:
        size = _existing_size(path)
        offset = self._offsets.get(path, 0)
        if size < offset:
            offset = 0
            self._partials.pop(path, None)
        if size == offset:
            return
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                appended = stream.read()
        except FileNotFoundError:
            return
        self._offsets[path] = offset + len(appended)
        payload = self._partials.pop(path, b"") + appended
        lines = payload.splitlines(keepends=True)
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            self._partials[path] = lines.pop()
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            parsed = parse_gdsmith_log(line.decode("utf-8", errors="replace"))
            if parsed.graph_statements or parsed.accepted_queries:
                self._statement_records += 1


def _existing_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


class _BoundedStreamCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_space = self.head_limit - len(self.head)
        if head_space > 0:
            self.head.extend(chunk[:head_space])
            chunk = chunk[head_space:]
        if chunk and self.tail_limit > 0:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    def render(self) -> str:
        if self.total <= self.limit:
            payload = bytes(self.head + self.tail)
        else:
            marker = f"\n... truncated {self.total - self.limit} GDsmith stream bytes ...\n".encode()
            payload = bytes(self.head) + marker + bytes(self.tail)
        return payload.decode("utf-8", errors="replace")


def _drain_stream(stream, capture: _BoundedStreamCapture) -> None:
    try:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            capture.add(chunk)
    finally:
        stream.close()


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
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _prune_rotated_logs_during_run(root: Path) -> None:
    log_root = root / "logs"
    if not log_root.is_dir():
        return
    for path in log_root.glob("*/*.log"):
        if path.name.endswith("-cur.log"):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


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


def _detect_query_record_limit_sentinel(path: Path) -> bool:
    try:
        marker = _QUERY_RECORD_LIMIT_SENTINEL.encode("utf-8")
        overlap = b""
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                window = overlap + chunk
                if marker in window:
                    return True
                overlap = window[-(len(marker) - 1) :]
        return False
    except FileNotFoundError:
        return False


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
