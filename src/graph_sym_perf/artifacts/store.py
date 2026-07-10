from __future__ import annotations

import hashlib
from pathlib import Path

from graph_sym_perf.artifacts.schema import ArtifactReference


class ContentAddressedStore:
    def __init__(self, root: str | Path, *, budget_bytes: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget_bytes = budget_bytes

    def put(self, name: str, payload: bytes) -> ArtifactReference:
        current = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        if current + len(payload) > self.budget_bytes:
            raise ValueError("artifact budget exceeded")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / digest
        path.write_bytes(payload)
        return ArtifactReference(digest, path.name)
