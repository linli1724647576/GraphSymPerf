"""Append-only shard lifecycle states."""

from __future__ import annotations

from enum import Enum


class ShardState(str, Enum):
    PLANNED = "planned"
    GENERATING = "generating"
    LOADING = "loading"
    BUILDING_ACCEPTED_PAIRS = "building_accepted_pairs"
    SCREENING = "screening"
    VALIDATING = "validating"
    REDUCING = "reducing"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"


_NEXT = {
    ShardState.PLANNED: {ShardState.GENERATING, ShardState.FAILED_RETRYABLE},
    ShardState.GENERATING: {ShardState.LOADING, ShardState.FAILED_RETRYABLE},
    ShardState.LOADING: {ShardState.BUILDING_ACCEPTED_PAIRS, ShardState.FAILED_RETRYABLE},
    ShardState.BUILDING_ACCEPTED_PAIRS: {ShardState.SCREENING, ShardState.INCOMPLETE, ShardState.FAILED_RETRYABLE},
    ShardState.SCREENING: {ShardState.VALIDATING, ShardState.COMPLETED, ShardState.FAILED_RETRYABLE},
    ShardState.VALIDATING: {ShardState.REDUCING, ShardState.COMPLETED, ShardState.FAILED_RETRYABLE},
    ShardState.REDUCING: {ShardState.COMPLETED, ShardState.FAILED_RETRYABLE},
    ShardState.FAILED_RETRYABLE: {ShardState.GENERATING, ShardState.FAILED_FINAL},
}


def can_transition(current: ShardState, target: ShardState) -> bool:
    return target in _NEXT.get(current, set())
