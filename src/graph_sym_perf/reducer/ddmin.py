"""Small deterministic delta-debugging primitive."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def ddmin(items: tuple[T, ...], preserves: Callable[[tuple[T, ...]], bool]) -> tuple[T, ...]:
    current = items
    granularity = 2
    while len(current) >= 2:
        chunk_size = max(1, len(current) // granularity)
        changed = False
        for start in range(0, len(current), chunk_size):
            candidate = current[:start] + current[start + chunk_size :]
            if candidate and preserves(candidate):
                current = candidate
                granularity = max(2, granularity - 1)
                changed = True
                break
        if not changed:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)
    return current
