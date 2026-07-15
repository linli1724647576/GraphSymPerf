"""One isolated worker process, monitored entirely from its supervisor."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from graph_sym_perf.experiment.process_control import terminate_process_group
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import ErrorKind, _atomic_write_json
from graph_sym_perf.sanitization import (
    SanitizationIntegrityError,
    _restore_verified_evidence_envelopes,
    _sanitize_structure_for_persistence,
    sanitize_structure,
    sanitize_text,
)


class WorkerProcessError(RuntimeError):
    def __init__(self, kind: ErrorKind, message: str, *, inflight: Mapping[str, object] | None = None) -> None:
        safe_message = sanitize_terminal_error(message)
        super().__init__(f"{kind.value}: {safe_message}")
        self.kind = kind
        self.inflight = dict(inflight or {})


class ProcessMonitor:
    def __init__(
        self,
        *,
        heartbeat_seconds: float,
        stale_after_seconds: float,
        hard_timeout_seconds: float | None,
        bootstrap_timeout_seconds: float = 5.0,
        hard_timeout_error_kind: ErrorKind = ErrorKind.WORKER_HUNG,
    ) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_after_seconds = stale_after_seconds
        self.hard_timeout_seconds = hard_timeout_seconds
        self.bootstrap_timeout_seconds = bootstrap_timeout_seconds
        self.hard_timeout_error_kind = hard_timeout_error_kind

    def run(self, worker: Callable[[object], dict], task: object, attempt: AttemptContext) -> dict:
        attempt_dir = attempt.attempt_dir
        process_context = _process_context()
        control_queue = process_context.Queue()
        process = process_context.Process(
            target=_isolated_worker_entry,
            args=(worker, task, attempt, control_queue, self.heartbeat_seconds),
        )
        process.start()
        timeout = self.hard_timeout_seconds
        if timeout is None:
            timeout = float(getattr(task, "max_runtime_seconds", 0) or 0)
        bootstrap_deadline = time.monotonic() + self.bootstrap_timeout_seconds
        shard_deadline: float | None = None
        verified_pgid: int | None = None
        ready_at: float | None = None
        final_message: tuple[str, object] | None = None
        failure: WorkerProcessError | None = None

        while process.is_alive():
            for message in _drain_control_queue(control_queue):
                kind, payload = message
                if kind == "ready":
                    try:
                        verified_pgid = _validate_ready(payload, expected_pid=process.pid)
                    except WorkerProcessError as exc:
                        failure = exc
                        break
                    ready_at = time.time()
                    shard_deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
                elif kind in {"result_ready", "typed_error", "error", "bootstrap_error"}:
                    final_message = message
            if failure is not None:
                break
            if verified_pgid is None:
                if final_message is not None and final_message[0] == "bootstrap_error":
                    failure = WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, f"bootstrap failed: {final_message[1]}")
                    break
                if time.monotonic() >= bootstrap_deadline:
                    failure = WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "worker bootstrap handshake timed out")
                    break
            else:
                inflight = _read_json(attempt_dir / "inflight.json")
                query_deadline = inflight.get("deadline") if inflight is not None else None
                if isinstance(query_deadline, int | float) and time.time() >= float(query_deadline):
                    failure = WorkerProcessError(
                        ErrorKind.QUERY_TIMEOUT,
                        "in-flight query hard deadline exceeded",
                        inflight=inflight,
                    )
                    break
                heartbeat = _read_json(attempt_dir / "heartbeat.json")
                heartbeat_time = heartbeat.get("updated_at") if heartbeat else ready_at
                if isinstance(heartbeat_time, int | float) and time.time() - float(heartbeat_time) >= self.stale_after_seconds:
                    failure = WorkerProcessError(ErrorKind.WORKER_HUNG, "heartbeat became stale", inflight=inflight)
                    break
                if shard_deadline is not None and time.monotonic() >= shard_deadline:
                    failure = WorkerProcessError(
                        self.hard_timeout_error_kind,
                        (
                            "worker hard runtime budget exhausted"
                            if self.hard_timeout_error_kind is ErrorKind.RESOURCE_EXHAUSTED
                            else "shard hard deadline exceeded"
                        ),
                        inflight=inflight,
                    )
                    break
            process.join(min(0.01, self.heartbeat_seconds))

        if process.is_alive():
            reaped = _terminate_join_close(process, control_queue, verified_pgid=verified_pgid)
            if not reaped:
                raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "worker process could not be reaped")
            assert failure is not None
            raise failure

        exitcode = process.exitcode
        control_deadline = time.monotonic() + 0.2
        while time.monotonic() < control_deadline and (verified_pgid is None or final_message is None):
            try:
                message = control_queue.get(timeout=max(0.001, control_deadline - time.monotonic()))
            except queue.Empty:
                break
            if not isinstance(message, tuple) or len(message) != 2 or not isinstance(message[0], str):
                continue
            kind, payload = message
            if kind == "ready":
                verified_pgid = _validate_ready(payload, expected_pid=process.pid)
            elif kind in {"result_ready", "typed_error", "error", "bootstrap_error"}:
                final_message = (kind, payload)
        _close_queue(control_queue)
        process.close()
        if verified_pgid is None:
            detail = final_message[1] if final_message is not None else f"exit code {exitcode}"
            raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, f"bootstrap was not verified: {detail}")
        if final_message is None:
            raise WorkerProcessError(
                ErrorKind.WORKER_CRASHED,
                f"exited with code {exitcode} without a result reference",
                inflight=_read_json(attempt_dir / "inflight.json"),
            )
        status, payload = final_message
        if status == "result_ready":
            return _load_result_reference(attempt_dir, payload)
        if status == "typed_error" and isinstance(payload, Mapping):
            try:
                kind = ErrorKind(str(payload["kind"]))
            except (KeyError, ValueError) as exc:
                raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "child returned invalid typed error") from exc
            raise WorkerProcessError(kind, str(payload.get("message", kind.value)))
        if status == "bootstrap_error":
            raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, f"bootstrap failed: {payload}")
        raise WorkerProcessError(ErrorKind.WORKER_CRASHED, str(payload))


def bind_worker_attempt(worker: object, context: AttemptContext) -> None:
    bind_attempt_context = getattr(worker, "bind_attempt_context", None)
    if callable(bind_attempt_context):
        bind_attempt_context(context)
    bind_attempt_dir = getattr(worker, "bind_attempt_dir", None)
    if callable(bind_attempt_dir):
        bind_attempt_dir(context.attempt_dir)


class _Heartbeat:
    def __init__(self, path: Path, interval_seconds: float) -> None:
        self.path = path
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="shard-heartbeat", daemon=True)

    def __enter__(self) -> "_Heartbeat":
        self._write()
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(min(0.5, self.interval_seconds * 2))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._write()
            except OSError:
                return

    def _write(self) -> None:
        _atomic_write_json(self.path, {"pid": os.getpid(), "updated_at": time.time()})


def _process_context() -> multiprocessing.context.BaseContext:
    if threading.current_thread() is not threading.main_thread():
        return multiprocessing.get_context("spawn")
    try:
        return multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - Windows fallback
        return multiprocessing.get_context()


def _establish_process_group() -> tuple[int, int]:
    if os.name != "posix":
        raise RuntimeError("formal worker isolation requires POSIX process groups")
    os.setsid()
    pid = os.getpid()
    pgid = os.getpgid(pid)
    if pgid != pid:
        raise RuntimeError(f"worker process group is not isolated: pid={pid} pgid={pgid}")
    return pid, pgid


def _isolated_worker_entry(
    worker: Callable[[object], dict],
    task: object,
    context: AttemptContext,
    control_queue: Any,
    heartbeat_seconds: float,
) -> None:
    try:
        pid, pgid = _establish_process_group()
        _publish_control(control_queue, "ready", {"pid": pid, "pgid": pgid})
    except BaseException as exc:
        _publish_control(control_queue, "bootstrap_error", _bounded_error(exc))
        return
    try:
        bind_worker_attempt(worker, context)
        with _Heartbeat(context.attempt_dir / "heartbeat.json", heartbeat_seconds):
            result = worker(task)
            if not isinstance(result, Mapping):
                raise TypeError("worker must return a mapping")
            result_path = context.attempt_dir / "result.json"
            safe_result, evidence_attestations = _sanitize_structure_for_persistence(
                result
            )
            _atomic_write_json(result_path, safe_result)
            digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
            _publish_control(
                control_queue,
                "result_ready",
                {
                    "name": result_path.name,
                    "sha256": digest,
                    "evidence_envelopes": evidence_attestations,
                },
            )
    except BaseException as exc:
        kind = getattr(exc, "kind", None)
        if isinstance(kind, ErrorKind):
            _publish_control(
                control_queue,
                "typed_error",
                {
                    "kind": kind.value,
                    "message": sanitize_terminal_error(str(exc)),
                },
            )
        elif isinstance(exc, ValueError):
            _publish_control(
                control_queue,
                "typed_error",
                {
                    "kind": ErrorKind.EVIDENCE_INTEGRITY_ERROR.value,
                    "message": sanitize_terminal_error(str(exc)),
                },
            )
        else:
            _publish_control(control_queue, "error", _bounded_error(exc))


def _bounded_error(exc: BaseException) -> str:
    return sanitize_terminal_error(f"{type(exc).__name__}: {exc}")


_TERMINAL_ERROR_LIMIT = 512


def sanitize_terminal_error(value: object, *, limit: int = _TERMINAL_ERROR_LIMIT) -> str:
    """Render one bounded, single-line, secret-free terminal diagnostic."""

    return sanitize_text(value, limit=limit, single_line=True)


def _publish_control(control_queue: Any, kind: str, payload: object) -> None:
    control_queue.put((kind, sanitize_structure(payload)))


def _validate_ready(payload: object, *, expected_pid: int) -> int:
    if not isinstance(payload, Mapping):
        raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "invalid bootstrap READY payload")
    pid = payload.get("pid")
    pgid = payload.get("pgid")
    if pid != expected_pid or pgid != expected_pid:
        raise WorkerProcessError(
            ErrorKind.INFRASTRUCTURE_ERROR,
            f"unverified worker process group: expected={expected_pid} pid={pid} pgid={pgid}",
        )
    return expected_pid


def _load_result_reference(attempt_dir: Path, payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping) or payload.get("name") != "result.json":
        raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "invalid worker result reference")
    expected_hash = payload.get("sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "invalid worker result digest")
    path = attempt_dir / "result.json"
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "worker result digest mismatch")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "cannot load worker result reference") from exc
    if not isinstance(value, dict):
        raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "worker result must be a JSON object")
    try:
        restored = _restore_verified_evidence_envelopes(
            value,
            payload.get("evidence_envelopes", ()),
        )
    except SanitizationIntegrityError as exc:
        raise WorkerProcessError(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            str(exc),
        ) from exc
    if not isinstance(restored, dict):  # pragma: no cover - guarded above
        raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "worker result must be a JSON object")
    return restored


def _drain_control_queue(control_queue: Any) -> tuple[tuple[str, object], ...]:
    messages = []
    while True:
        try:
            value = control_queue.get_nowait()
        except queue.Empty:
            break
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
            messages.append((value[0], value[1]))
    return tuple(messages)


def _terminate_join_close(process: multiprocessing.Process, control_queue: Any, *, verified_pgid: int | None) -> bool:
    if verified_pgid is not None:
        try:
            terminate_process_group(verified_pgid)
        except (OSError, ProcessLookupError, PermissionError):
            process.kill()
    else:
        process.kill()
    process.join(0.5)
    reaped = not process.is_alive()
    _close_queue(control_queue)
    if reaped:
        process.close()
    return reaped


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _close_queue(control_queue: Any) -> None:
    try:
        control_queue.cancel_join_thread()
    except (AttributeError, OSError):
        pass
    try:
        control_queue.close()
    except (AttributeError, OSError):
        pass


__all__ = ["ProcessMonitor", "WorkerProcessError", "bind_worker_attempt"]
