from __future__ import annotations

from enum import Enum


class ArtifactLevel(str, Enum):
    MINIMAL = "minimal"
    CANDIDATE = "candidate"
    ISSUE = "issue"


class ArtifactPolicy:
    def __init__(self, *, shard_budget_mb: int | None = None) -> None:
        self.shard_budget_mb = shard_budget_mb or 500

    def level_for(self, stage: str) -> ArtifactLevel:
        return ArtifactLevel.ISSUE if stage == "issue" else ArtifactLevel.CANDIDATE if stage == "candidate" else ArtifactLevel.MINIMAL
