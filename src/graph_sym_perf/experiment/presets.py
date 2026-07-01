"""Experiment preset discovery."""

from __future__ import annotations

from pathlib import Path

DEFAULT_EXPERIMENT_DIR = Path("config/experiments")


def list_presets(root: str | Path = DEFAULT_EXPERIMENT_DIR) -> tuple[Path, ...]:
    path = Path(root)
    if not path.exists():
        return ()
    return tuple(sorted(candidate for candidate in path.glob("*.yaml") if candidate.is_file()))


def resolve_preset_path(name_or_path: str | Path) -> Path:
    candidate = Path(name_or_path)
    if candidate.exists():
        return candidate
    preset = DEFAULT_EXPERIMENT_DIR / f"{candidate.stem}.yaml"
    return preset
