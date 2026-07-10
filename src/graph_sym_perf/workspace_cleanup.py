"""Non-destructive cleanup audit; deletion requires an approved manifest."""

from __future__ import annotations

import subprocess
import json
import shutil
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


def approve_candidates(manifest_path: str | Path) -> tuple[dict[str, object], ...]:
    """Mark only dry-run candidates as approved after explicit user consent."""
    path = Path(manifest_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items")
    if payload.get("dry_run") is not True or not isinstance(items, list):
        raise ValueError("expected a dry-run deletion manifest")
    for item in items:
        if item.get("status") == "CANDIDATE":
            item["status"] = "APPROVED"
    payload["dry_run"] = False
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return tuple(items)


def delete_approved(manifest_path: str | Path, *, root: str | Path) -> tuple[dict[str, object], ...]:
    """Delete only exact APPROVED entries inside *root*, never globbing paths."""
    root_path = Path(root).resolve()
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    deleted: list[dict[str, object]] = []
    for item in payload.get("items", []):
        if item.get("status") != "APPROVED":
            continue
        relative = Path(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe approved path: {relative}")
        target = (root_path / relative).resolve()
        if target.parent != root_path:
            raise ValueError(f"approved path escapes root: {relative}")
        before = _size(target)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        deleted.append({"path": str(relative), "bytes": before, "status": "DELETED"})
    return tuple(deleted)
