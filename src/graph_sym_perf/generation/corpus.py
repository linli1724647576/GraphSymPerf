from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphCorpus:
    statements: tuple[str, ...]


@dataclass(frozen=True)
class PortableQuery:
    text: str
    source: str = "gdsmith"


@dataclass(frozen=True)
class QueryCorpus:
    queries: tuple[PortableQuery, ...]
