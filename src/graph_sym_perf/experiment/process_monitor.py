"""One isolated worker process, monitored entirely from its supervisor."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from graph_sym_perf.experiment.process_control import terminate_process_group
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import ErrorKind, _atomic_write_json


class WorkerProcessError(RuntimeError):
    def __init__(self, kind: ErrorKind, message: str, *, inflight: Mapping[str, object] | None = None) -> None:
        super().__init__(f"{kind.value}: {message}")
        self.kind = kind
        self.inflight = dict(inflight or {})


class ProcessMonitor:
    def __init__(
        self,
        *,
        heartbeat_seconds: float,
        stale_after_seconds: float,
        hard_timeout_seconds: float | None,
    ) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_after_seconds = stale_after_seconds
        self.hard_timeout_seconds = hard_timeout_seconds

    def run(self, worker: Callable[[object], dict], task: object, attempt: AttemptContext) -> dict:
        attempt_dir = attempt.attempt_dir
        process_context = _process_context()
        result_queue = process_context.Queue()
        process = process_context.Process(
            target=_isolated_worker_entry,
            args=(worker, task, attempt, result_queue, self.heartbeat_seconds),
        )
        process.start()
        timeout = self.hard_timeout_seconds
        if timeout is None:
            timeout = float(getattr(task, "max_runtime_seconds", 0) or 0)
        shard_deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        started_at = time.time()
        failure: WorkerProcessError | None = None
        while process.is_alive():
            process.join(min(0.01, self.heartbeat_seconds))
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
            heartbeat_time = heartbeat.get("updated_at") if heartbeat else started_at
            if isinstance(heartbeat_time, int | float) and time.time() - float(heartbeat_time) >= self.stale_after_seconds:
                failure = WorkerProcessError(ErrorKind.WORKER_HUNG, "heartbeat became stale", inflight=inflight)
                break
            if shard_deadline is not None and time.monotonic() >= shard_deadline:
                failure = WorkerProcessError(ErrorKind.WORKER_HUNG, "shard hard deadline exceeded", inflight=inflight)
                break
        if process.is_alive():
            reaped = _terminate_join_close(process, result_queue)
            if not reaped:
                raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "worker process could not be reaped")
            assert failure is not None
            raise failure
        try:
            status, payload = result_queue.get(timeout=0.2)
        except queue.Empty as exc:
            raise WorkerProcessError(
                ErrorKind.WORKER_CRASHED,
                f"exited with code {process.exitcode} without a result",
                inflight=_read_json(attempt_dir / "inflight.json"),
            ) from exc
        finally:
            _close_queue(result_queue)
            process.close()
        if status == "ok":
            if not isinstance(payload, dict):
                raise TypeError("worker must return a dictionary")
            return payload
        if status == "typed_error" and isinstance(payload, Mapping):
            try:
                kind = ErrorKind(str(payload["kind"]))
            except (KeyError, ValueError) as exc:
                raise WorkerProcessError(ErrorKind.INFRASTRUCTURE_ERROR, "child returned invalid typed error") from exc
            raise WorkerProcessError(kind, str(payload.get("message", kind.value)))
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


def _isolated_worker_entry(
    worker: Callable[[object], dict],
    task: object,
    context: AttemptContext,
    result_queue: Any,
    heartbeat_seconds: float,
) -> None:
    attempt_dir = context.attempt_dir
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass
    try:
        bind_worker_attempt(worker, context)
        with _Heartbeat(attempt_dir / "heartbeat.json", heartbeat_seconds):
            result_queue.put(("ok", worker(task)))
    except BaseException as exc:
        kind = getattr(exc, "kind", None)
        if isinstance(kind, ErrorKind):
            result_queue.put(
                (
                    "typed_error",
                    {"kind": kind.value, "message": str(exc), "traceback": traceback.format_exc()},
                )
            )
        else:
            result_queue.put(("error", traceback.format_exc()))


def _terminate_join_close(process: multiprocessing.Process, result_queue: Any) -> bool:
    try:
        terminate_process_group(process.pid)
    except (OSError, ProcessLookupError, PermissionError):
        process.terminate()
    process.join(0.1)
    if process.is_alive():
        process.kill()
        process.join(0.4)
    reaped = not process.is_alive()
    _close_queue(result_queue)
    if reaped:
        process.close()
    return reaped


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _close_queue(result_queue: Any) -> None:
    try:
        result_queue.cancel_join_thread()
    except (AttributeError, OSError):
        pass
    try:
        result_queue.close()
    except (AttributeError, OSError):
        pass


__all__ = ["ProcessMonitor", "WorkerProcessError", "bind_worker_attempt"]
