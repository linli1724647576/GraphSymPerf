"""Failure-isolating supervisor with append-only event and checkpoint files."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from graph_sym_perf.experiment.plan_models import RunPlan
from graph_sym_perf.experiment.process_control import terminate_process_group
from graph_sym_perf.experiment.state import ShardState


@dataclass(frozen=True)
class RunSummary:
    completed: tuple[object, ...]
    incomplete: tuple[object, ...]
    failed: tuple[object, ...]


class ShardSupervisor:
    def __init__(
        self,
        run_dir: str | Path,
        *,
        retry_limit: int = 3,
        process_isolation: bool = False,
        hard_timeout_seconds: float | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.retry_limit = retry_limit
        self.process_isolation = process_isolation
        self.hard_timeout_seconds = hard_timeout_seconds

    def run(self, plan: RunPlan, worker: Callable[[object], dict]) -> RunSummary:
        completed: list[object] = []
        incomplete: list[object] = []
        failed: list[object] = []
        for task in plan.tasks:
            for attempt in range(self.retry_limit + 1):
                self._event(task, ShardState.GENERATING, attempt)
                try:
                    result = self._run_worker(worker, task)
                except Exception as exc:
                    if attempt < self.retry_limit:
                        self._event(task, ShardState.FAILED_RETRYABLE, attempt, str(exc))
                        continue
                    self._event(task, ShardState.FAILED_FINAL, attempt, str(exc))
                    failed.append(task)
                    break
                required_pairs = int(getattr(task, "accepted_query_pairs", 1000))
                if int(result.get("accepted_pairs", 0)) < required_pairs:
                    self._event(task, ShardState.INCOMPLETE, attempt)
                    incomplete.append(task)
                else:
                    self._event(task, ShardState.COMPLETED, attempt)
                    completed.append(task)
                break
        return RunSummary(tuple(completed), tuple(incomplete), tuple(failed))

    def _run_worker(self, worker: Callable[[object], dict], task: object) -> dict:
        if not self.process_isolation:
            return worker(task)
        return self._run_worker_in_process(worker, task)

    def _run_worker_in_process(self, worker: Callable[[object], dict], task: object) -> dict:
        context = _process_context()
        result_queue = context.Queue()
        process = context.Process(target=_isolated_worker_entry, args=(worker, task, result_queue))
        process.start()
        timeout = self.hard_timeout_seconds
        if timeout is None:
            timeout = float(getattr(task, "max_runtime_seconds", 0) or 0)
        if timeout <= 0:
            timeout = None
        process.join(timeout)
        if process.is_alive():
            try:
                terminate_process_group(process.pid)
            except (OSError, ProcessLookupError, PermissionError):
                process.terminate()
            process.join()
            result_queue.close()
            result_queue.join_thread()
            raise RuntimeError("WORKER_HUNG: hard deadline exceeded; process group terminated")
        try:
            status, payload = result_queue.get(timeout=0.2)
        except queue.Empty as exc:
            raise RuntimeError(f"WORKER_CRASHED: exited with code {process.exitcode} without a result") from exc
        finally:
            result_queue.close()
            result_queue.join_thread()
        if status == "ok":
            if not isinstance(payload, dict):
                raise TypeError("worker must return a dictionary")
            return payload
        raise RuntimeError(f"WORKER_CRASHED: {payload}")

    def _event(self, task: object, state: ShardState, attempt: int, reason: str | None = None) -> None:
        payload = {"task": str(task), "state": state.value, "attempt": attempt, "reason": reason}
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        checkpoint = self.run_dir / "checkpoint.json"
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(checkpoint)


def _process_context() -> multiprocessing.context.BaseContext:
    try:
        return multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - Windows fallback
        return multiprocessing.get_context()


def _isolated_worker_entry(worker: Callable[[object], dict], task: object, result_queue) -> None:
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass
    try:
        result_queue.put(("ok", worker(task)))
    except BaseException:  # child failures must be serialized to the parent
        result_queue.put(("error", traceback.format_exc()))
