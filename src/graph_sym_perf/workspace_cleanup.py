"""Fail-closed cleanup audit; deletion requires a user-edited final manifest."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path


_PROTECTED_ROOTS = (
    Path(".git"),
    Path(".venv"),
    Path("issues"),
    Path("tests/fixtures/regressions"),
)
_GENERATED_ROOTS = frozenset(
    {
        "runs",
        "logs",
        "results",
        "reports",
        "build",
        "dist",
        "htmlcov",
        ".pytest_cache",
        ".mypy_cache",
        "BUG list",
    }
)
_GENERATED_FILES = frozenset({".coverage", "coverage.xml", "doctor.json"})
_TEMP_FILE_PATTERNS = (
    "runtime-*.csv",
    "runtime-*.jsonl",
    "tmp_*.html",
    "tmp_*.pdf",
    "tmp_*.png",
    "tmp_*.jpg",
    "tmp_*.jpeg",
)
_AUDIT_ITEM_FIELDS = frozenset(
    {
        "path",
        "bytes",
        "file_count",
        "tracked",
        "uncommitted",
        "evidence_references",
        "migration_target",
        "deletion_reason",
        "status",
    }
)


def discover_cleanup_paths(root: str | Path) -> tuple[Path, ...]:
    """Return existing paths from the fixed Task 12 cleanup scope."""

    root_path = Path(root).absolute()
    paths: set[Path] = set()
    for name in sorted(_GENERATED_ROOTS | _GENERATED_FILES):
        candidate = root_path / name
        if candidate.exists() or candidate.is_symlink():
            paths.add(candidate)
    for directory, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
        directory_path = Path(directory)
        retained_dirs: list[str] = []
        for name in dirnames:
            candidate = directory_path / name
            relative = _relative_path(candidate, root_path)
            if _is_inside_protected(relative) or candidate.is_symlink():
                continue
            if name == "__pycache__":
                paths.add(candidate)
                continue
            if len(relative.parts) == 1 and name in _GENERATED_ROOTS:
                continue
            retained_dirs.append(name)
        dirnames[:] = retained_dirs
        for name in filenames:
            candidate = directory_path / name
            relative = _relative_path(candidate, root_path)
            if _is_inside_protected(relative):
                continue
            if candidate.suffix in {".pyc", ".pyo"} or _matches_temp_file(relative):
                paths.add(candidate)
    ordered = sorted(paths, key=lambda path: (len(path.parts), str(_relative_path(path, root_path))))
    non_overlapping: list[Path] = []
    for candidate in ordered:
        if any(parent == candidate or parent in candidate.parents for parent in non_overlapping):
            continue
        non_overlapping.append(candidate)
    return tuple(sorted(non_overlapping, key=lambda path: str(_relative_path(path, root_path))))


def audit_paths(
    paths: tuple[Path, ...],
    *,
    root: str | Path | None = None,
    migrations: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[dict[str, object], ...]:
    """Describe exact paths without modifying them."""

    root_path = Path(root or Path.cwd()).absolute()
    migration_records = migrations or {}
    records: list[dict[str, object]] = []
    for path in paths:
        absolute = path if path.is_absolute() else root_path / path
        relative = _relative_path(absolute, root_path)
        relative_text = relative.as_posix()
        tracked, uncommitted = _git_state(absolute, root_path)
        metadata = dict(_default_migration(relative))
        metadata.update(migration_records.get(relative_text, {}))
        if _overlaps_protected(relative):
            status = "PROTECTED"
            reason = "protected_root"
        elif tracked and uncommitted:
            status = "PROTECTED"
            reason = "uncommitted_tracked_path"
        else:
            status = "CANDIDATE"
            reason = str(metadata["deletion_reason"])
        records.append(
            {
                "path": relative_text,
                "bytes": _size(absolute),
                "file_count": _file_count(absolute),
                "tracked": tracked,
                "uncommitted": uncommitted,
                "evidence_references": list(metadata["evidence_references"]),
                "migration_target": metadata["migration_target"],
                "deletion_reason": reason,
                "status": status,
            }
        )
    return tuple(records)


def delete_approved(manifest_path: str | Path, *, root: str | Path) -> tuple[dict[str, object], ...]:
    """Delete exact APPROVED entries from a final manifest, without globbing."""

    root_path = Path(root).resolve(strict=True)
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expected a final approved deletion manifest")
    items = payload.get("items") if isinstance(payload, dict) else None
    if payload.get("schema_version") != 1 or payload.get("dry_run") is not False or not isinstance(items, list):
        raise ValueError("expected a final approved deletion manifest")
    manifest_root = payload.get("root")
    if not isinstance(manifest_root, str) or not manifest_root or not Path(manifest_root).is_absolute():
        raise ValueError("deletion manifest root binding is missing or invalid")
    try:
        bound_root = Path(manifest_root).resolve(strict=True)
    except OSError as exc:
        raise ValueError("deletion manifest root binding does not exist") from exc
    if bound_root != root_path:
        raise ValueError(f"deletion manifest root does not match execution root: {bound_root} != {root_path}")
    for item in items:
        _validate_manifest_item(item)
    item_paths = [item["path"] for item in items]
    if len(item_paths) != len(set(item_paths)):
        raise ValueError("deletion manifest contains duplicate item paths")
    approved_paths = [item["path"] for item in items if item["status"] == "APPROVED"]

    validated: list[tuple[Path, Path]] = []
    for relative_text in approved_paths:
        relative = Path(relative_text)
        _validate_approved_path(relative, root_path)
        target = root_path / relative
        tracked, uncommitted = _git_state(target, root_path)
        if tracked and uncommitted:
            raise ValueError(f"approved path became tracked-uncommitted and is PROTECTED: {relative.as_posix()}")
        validated.append((relative, target))

    for relative, _target in validated:
        _validate_approved_path(relative, root_path)

    deleted: list[dict[str, object]] = []
    for relative, target in validated:
        _validate_approved_path(relative, root_path)
        before = _size(target)
        file_count = _file_count(target)
        _delete_exact(root_path, relative)
        deleted.append(
            {
                "path": relative.as_posix(),
                "bytes": before,
                "file_count": file_count,
                "status": "DELETED",
            }
        )
    return tuple(deleted)


def _validate_approved_path(relative: Path, root: Path) -> None:
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe approved path: {relative}")
    if _overlaps_protected(relative):
        raise ValueError(f"approved path overlaps a protected root: {relative}")
    if not _in_cleanup_scope(relative):
        raise ValueError(f"approved path is outside the cleanup scope: {relative}")
    target = root / relative
    try:
        target.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ValueError(f"unsafe approved path: {relative}") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"approved path has a symlink component: {relative}")


def _in_cleanup_scope(relative: Path) -> bool:
    if relative.parts[0] in _GENERATED_ROOTS or relative.as_posix() in _GENERATED_FILES:
        return True
    return relative.name == "__pycache__" or relative.suffix in {".pyc", ".pyo"} or _matches_temp_file(relative)


def _overlaps_protected(relative: Path) -> bool:
    return any(
        relative == protected
        or protected in relative.parents
        or relative in protected.parents
        for protected in _PROTECTED_ROOTS
    )


def _is_inside_protected(relative: Path) -> bool:
    return any(relative == protected or protected in relative.parents for protected in _PROTECTED_ROOTS)


def _matches_temp_file(relative: Path) -> bool:
    return any(fnmatch.fnmatchcase(relative.name, pattern) for pattern in _TEMP_FILE_PATTERNS)


def _validate_manifest_item(item: object) -> None:
    if not isinstance(item, dict) or not _AUDIT_ITEM_FIELDS <= set(item):
        raise ValueError("deletion manifest item is missing required audit fields")
    if not isinstance(item["path"], str) or not item["path"]:
        raise ValueError("deletion manifest item path must be a non-empty string")
    if type(item["bytes"]) is not int or item["bytes"] < 0:
        raise ValueError("deletion manifest item bytes must be a non-negative integer")
    if type(item["file_count"]) is not int or item["file_count"] < 0:
        raise ValueError("deletion manifest item file_count must be a non-negative integer")
    if type(item["tracked"]) is not bool or type(item["uncommitted"]) is not bool:
        raise ValueError("deletion manifest tracked and uncommitted fields must be boolean")
    references = item["evidence_references"]
    if not isinstance(references, list) or any(not isinstance(reference, str) for reference in references):
        raise ValueError("deletion manifest evidence_references must be a list of strings")
    migration_target = item["migration_target"]
    if migration_target is not None and not isinstance(migration_target, str):
        raise ValueError("deletion manifest migration_target must be a string or null")
    if not isinstance(item["deletion_reason"], str) or not item["deletion_reason"]:
        raise ValueError("deletion manifest deletion_reason must be a non-empty string")
    if not isinstance(item["status"], str) or item["status"] not in {"APPROVED", "CANDIDATE", "PROTECTED"}:
        raise ValueError("deletion manifest item status is invalid")
    if item["status"] == "APPROVED" and item["uncommitted"]:
        raise ValueError("tracked-uncommitted manifest items cannot be APPROVED")


def _default_migration(relative: Path) -> dict[str, object]:
    if relative.parts and relative.parts[0] == "BUG list":
        return {
            "evidence_references": ["issues/evidence_inventory.json", "issues/evidence/"],
            "migration_target": "issues/evidence/",
            "deletion_reason": "migrated_legacy_issue_artifacts",
        }
    if relative.name == "doctor.json":
        reason = "generated_doctor_report"
    elif relative.name == "__pycache__" or relative.suffix in {".pyc", ".pyo"}:
        reason = "python_cache"
    elif relative.parts and relative.parts[0] in {".pytest_cache", ".mypy_cache"}:
        reason = "tool_cache"
    else:
        reason = "historical_generated_artifact"
    return {"evidence_references": [], "migration_target": None, "deletion_reason": reason}


def _git_state(path: Path, root: Path) -> tuple[bool, bool]:
    relative = _relative_path(path, root).as_posix()
    tracked_result = subprocess.run(
        ("git", "-C", str(root), "ls-files", "--", relative),
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked_result.returncode != 0:
        raise RuntimeError(f"git ls-files failed for {relative}: {tracked_result.stderr.strip()}")
    tracked = bool(tracked_result.stdout.strip())
    dirty_result = subprocess.run(
        ("git", "-C", str(root), "status", "--porcelain", "--untracked-files=no", "--", relative),
        capture_output=True,
        text=True,
        check=False,
    )
    if dirty_result.returncode != 0:
        raise RuntimeError(f"git status failed for {relative}: {dirty_result.stderr.strip()}")
    uncommitted = tracked and bool(dirty_result.stdout.strip())
    return tracked, uncommitted


def _relative_path(path: Path, root: Path) -> Path:
    try:
        return path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ValueError(f"cleanup path is outside root: {path}") from exc


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def _file_count(path: Path) -> int:
    if path.is_file():
        return 1
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def _delete_exact(root: Path, relative: Path) -> None:
    """Delete relative to no-follow directory handles rooted at *root*."""

    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("platform cannot provide symlink-safe recursive deletion")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        name = relative.parts[-1]
        target_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(target_stat.st_mode):
            raise ValueError(f"approved path became a symlink before deletion: {relative}")
        if stat.S_ISDIR(target_stat.st_mode):
            shutil.rmtree(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)
    except FileNotFoundError:
        return
    finally:
        os.close(descriptor)


__all__ = ["audit_paths", "delete_approved", "discover_cleanup_paths"]
