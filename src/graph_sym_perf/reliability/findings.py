from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReliabilityFinding:
    kind: str
    query_hash: str
    graph_hash: str
    dbms_fingerprint: str
    reproductions: int
