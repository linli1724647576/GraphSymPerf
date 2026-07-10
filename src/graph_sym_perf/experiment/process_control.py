"""Bounded POSIX process-group cleanup and cross-process file leases."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import time
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - GraphSymPerf formal runs are POSIX-only
    fcntl = None  # type: ignore[assignment]


class LeaseTimeoutError(TimeoutError):
    pass


class FileLeasePool:
    """A small flock-backed semaphore shared by independent worker processes."""

    def __init__(self, root: str | Path, name: str, slots: int) -> None:
        if slots <= 0:
            raise ValueError("lease slots must be positive")
        if fcntl is None:
            raise RuntimeError("cross-process leases require POSIX fcntl")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-") or "lease"
        self.paths = tuple(self.root / f"{safe_name}-{slot:03d}.lock" for slot in range(slots))

    @contextlib.contextmanager
    def acquire(self, *, timeout_seconds: float | None = None) -> Iterator[Path]:
        started = time.monotonic()
        descriptor = -1
        acquired_path: Path | None = None
        while acquired_path is None:
            for path in self.paths:
                candidate = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(candidate)
                    continue
                descriptor = candidate
                acquired_path = path
                break
            if acquired_path is not None:
                break
            if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                raise LeaseTimeoutError(f"timed out acquiring lease pool {self.paths[0].stem}")
            time.sleep(0.005)
        try:
            yield acquired_path
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def terminate_process_group(pid: int, *, term_grace_seconds: float = 0.05) -> None:
    """Send TERM, wait a bounded grace period, then KILL the remaining group."""

    pgid = os.getpgid(pid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.0, term_grace_seconds)
    while time.monotonic() < deadline:
        if not _process_group_exists(pgid):
            return
        time.sleep(0.005)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ["FileLeasePool", "LeaseTimeoutError", "terminate_process_group"]
