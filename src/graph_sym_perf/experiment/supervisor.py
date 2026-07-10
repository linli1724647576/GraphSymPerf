"""Failure-isolating supervisor with append-only event and checkpoint files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from graph_sym_perf.experiment.plan_models import RunPlan
from graph_sym_perf.experiment.state import ShardState


@dataclass(frozen=True)
class RunSummary:
    completed: tuple[object, ...]
    incomplete: tuple[object, ...]
    failed: tuple[object, ...]


class ShardSupervisor:
    def __init__(self, run_dir: str | Path, *, retry_limit: int = 3) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.retry_limit = retry_limit

    def run(self, plan: RunPlan, worker: Callable[[object], dict]) -> RunSummary:
        completed: list[object] = []
        incomplete: list[object] = []
        failed: list[object] = []
        for task in plan.tasks:
            for attempt in range(self.retry_limit + 1):
                self._event(task, ShardState.GENERATING, attempt)
                try:
                    result = worker(task)
                except Exception as exc:
                    if attempt < self.retry_limit:
                        self._event(task, ShardState.FAILED_RETRYABLE, attempt, str(exc))
                        continue
                    self._event(task, ShardState.FAILED_FINAL, attempt, str(exc))
                    failed.append(task)
                    break
                if int(result.get("accepted_pairs", 0)) < 1000:
                    self._event(task, ShardState.INCOMPLETE, attempt)
                    incomplete.append(task)
                else:
                    self._event(task, ShardState.COMPLETED, attempt)
                    completed.append(task)
                break
        return RunSummary(tuple(completed), tuple(incomplete), tuple(failed))

    def _event(self, task: object, state: ShardState, attempt: int, reason: str | None = None) -> None:
        payload = {"task": str(task), "state": state.value, "attempt": attempt, "reason": reason}
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        checkpoint = self.run_dir / "checkpoint.json"
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(checkpoint)
