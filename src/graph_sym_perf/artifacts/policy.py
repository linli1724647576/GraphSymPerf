from __future__ import annotations

from enum import Enum


class ArtifactLevel(str, Enum):
    MINIMAL = "minimal"
    CANDIDATE = "candidate"
    VALIDATION = "validation"
    ISSUE = "issue"


class ArtifactPolicy:
    _LEVELS = {
        "accepted": ArtifactLevel.MINIMAL,
        "rejected": ArtifactLevel.MINIMAL,
        "candidate": ArtifactLevel.CANDIDATE,
        "validation": ArtifactLevel.VALIDATION,
        "issue": ArtifactLevel.ISSUE,
    }

    def __init__(self, *, shard_budget_mb: int = 500) -> None:
        if not isinstance(shard_budget_mb, int) or isinstance(shard_budget_mb, bool) or shard_budget_mb <= 0:
            raise ValueError("shard artifact budget must be a positive integer")
        self.shard_budget_mb = shard_budget_mb

    @property
    def shard_budget_bytes(self) -> int:
        return self.shard_budget_mb * 1024 * 1024

    def level_for(self, stage: str) -> ArtifactLevel:
        try:
            return self._LEVELS[stage]
        except KeyError as exc:
            raise ValueError(f"unknown artifact stage: {stage}") from exc


__all__ = ["ArtifactLevel", "ArtifactPolicy"]
