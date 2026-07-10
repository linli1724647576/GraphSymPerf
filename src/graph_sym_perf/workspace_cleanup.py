"""Non-destructive cleanup audit; deletion requires an approved manifest."""

from __future__ import annotations

import subprocess
from pathlib import Path


def audit_paths(paths: tuple[Path, ...]) -> tuple[dict[str, object], ...]:
    records = []
    for path in paths:
        tracked, dirty = _git_state(path)
        records.append(
            {
                "path": str(path),
                "bytes": _size(path),
                "tracked": tracked,
                "dirty": dirty,
                "status": "PROTECTED" if tracked and dirty else "CANDIDATE",
                "reason": "uncommitted_tracked_path" if tracked and dirty else "historical_generated_artifact",
            }
        )
    return tuple(records)


def _git_state(path: Path) -> tuple[bool, bool]:
    root = Path.cwd()
    relative = str(path.resolve().relative_to(root.resolve()))
    tracked = subprocess.run(("git", "ls-files", "--error-unmatch", relative), capture_output=True, text=True).returncode == 0
    dirty = subprocess.run(("git", "status", "--porcelain", "--", relative), capture_output=True, text=True).stdout != ""
    return tracked, dirty


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
