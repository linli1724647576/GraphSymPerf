"""Graph-snapshot fingerprints required by independent validation sessions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class GraphSnapshot:
    statements: tuple[str, ...]
    content_hash: str

    @classmethod
    def from_statements(cls, statements: tuple[str, ...] | list[str]) -> "GraphSnapshot":
        normalized = tuple(statement.strip() for statement in statements if statement.strip())
        payload = "\n".join(normalized).encode("utf-8")
        return cls(normalized, hashlib.sha256(payload).hexdigest())
