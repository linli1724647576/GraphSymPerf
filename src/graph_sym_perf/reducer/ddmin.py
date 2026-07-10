"""Bounded hierarchical delta debugging with monotonic attempt evidence."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.runtime.models import EquivalentQueryPair


T = TypeVar("T")


@dataclass(frozen=True)
class DDMinCandidate(Generic[T]):
    canonical_id: str
    value: T
    level: str


@dataclass(frozen=True)
class DDMinEvaluation:
    preserved: bool
    reason: str
    evidence_hash: str


@dataclass(frozen=True)
class DDMinAttempt:
    attempt_id: str
    index: int
    proposal_id: str
    level: str
    cache_key: str
    outcome: str
    evidence_hash: str


@dataclass(frozen=True)
class DDMinRunResult(Generic[T]):
    best: T
    attempts: tuple[DDMinAttempt, ...]
    exhausted: bool
    stop_reason: str


class HierarchicalDDMin(Generic[T]):
    def __init__(
        self,
        *,
        max_attempts: int = 500,
        deadline_seconds: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts <= 0 or deadline_seconds <= 0:
            raise ValueError("ddmin bounds must be positive")
        self.max_attempts = min(max_attempts, 500)
        self.deadline_seconds = min(deadline_seconds, 1800.0)
        self.clock = clock

    def run(
        self,
        initial: T,
        *,
        proposals: Callable[[T], tuple[DDMinCandidate[T], ...]],
        evaluate: Callable[[T], DDMinEvaluation],
        cache_key: Callable[[T], str],
        checkpoint: Callable[[T, DDMinAttempt], None] | None = None,
    ) -> DDMinRunResult[T]:
        started = self.clock()
        best = initial
        attempts: list[DDMinAttempt] = []
        seen: set[str] = set()
        while True:
            progressed = False
            available = proposals(best)
            for proposal in available:
                if len(attempts) >= self.max_attempts:
                    return DDMinRunResult(best, tuple(attempts), False, "attempt_limit")
                if self.clock() - started >= self.deadline_seconds:
                    return DDMinRunResult(best, tuple(attempts), False, "deadline")
                key = cache_key(proposal.value)
                if key in seen or proposal.value == best:
                    continue
                seen.add(key)
                index = len(attempts) + 1
                attempt_id = _attempt_id(index, proposal.canonical_id, key)
                try:
                    evaluation = evaluate(proposal.value)
                    outcome = evaluation.reason
                    evidence_hash = evaluation.evidence_hash
                except Exception as exc:  # an evaluation/reload failure is evidence, not control flow
                    evaluation = DDMinEvaluation(False, f"evaluation_error:{type(exc).__name__}", _exception_hash(exc))
                    outcome = evaluation.reason
                    evidence_hash = evaluation.evidence_hash
                attempt = DDMinAttempt(
                    attempt_id=attempt_id,
                    index=index,
                    proposal_id=proposal.canonical_id,
                    level=proposal.level,
                    cache_key=key,
                    outcome=outcome,
                    evidence_hash=evidence_hash,
                )
                attempts.append(attempt)
                if evaluation.preserved:
                    best = proposal.value
                    progressed = True
                    if checkpoint is not None:
                        checkpoint(best, attempt)
                    break
            if not progressed:
                return DDMinRunResult(best, tuple(attempts), True, "exhausted")


def ddmin(items: tuple[T, ...], preserves: Callable[[tuple[T, ...]], bool]) -> tuple[T, ...]:
    """Compatibility helper for tuple ddmin; exceptions still advance the scan."""

    current = items
    granularity = 2
    while len(current) >= 2:
        chunk_size = max(1, len(current) // granularity)
        changed = False
        for start in range(0, len(current), chunk_size):
            candidate = current[:start] + current[start + chunk_size :]
            if not candidate:
                continue
            try:
                accepted = preserves(candidate)
            except Exception:
                accepted = False
            if accepted:
                current = candidate
                granularity = max(2, granularity - 1)
                changed = True
                break
        if not changed:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)
    return current


def reduction_cache_key(pair: EquivalentQueryPair, *, policy_hash: str) -> str:
    payload = {
        "graph_snapshot_hash": pair.graph_snapshot_hash,
        "query_a_hash": hashlib.sha256(pair.query_a.encode("utf-8")).hexdigest(),
        "query_b_hash": hashlib.sha256(pair.query_b.encode("utf-8")).hexdigest(),
        "policy_hash": policy_hash,
    }
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


def _attempt_id(index: int, proposal_id: str, key: str) -> str:
    suffix = hashlib.sha256(f"{proposal_id}\0{key}".encode("utf-8")).hexdigest()[:12]
    return f"attempt-{index:06d}-{suffix}"


def _exception_hash(exc: Exception) -> str:
    return hashlib.sha256(f"{type(exc).__name__}:{exc}".encode("utf-8")).hexdigest()
