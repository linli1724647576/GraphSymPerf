from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactReference:
    content_hash: str
    relative_path: str
    schema_version: int = 1
