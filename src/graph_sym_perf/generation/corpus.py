from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class GraphCorpus:
    statements: tuple[str, ...]
    source: str = "gdsmith"

    def __post_init__(self) -> None:
        if self.source != "gdsmith":
            raise ValueError("GDsmith is the only supported graph source")
        if not isinstance(self.statements, tuple) or not all(isinstance(item, str) and item.strip() for item in self.statements):
            raise TypeError("graph corpus statements must be a tuple of non-empty strings")

    @property
    def content_hash(self) -> str:
        payload = "\n".join(statement.strip() for statement in self.statements)
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PortableQuery:
    text: str
    source: str = "gdsmith"
    parameters: Mapping[str, Any] = MappingProxyType({})
    ast: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("portable query text must be non-empty")
        if self.source != "gdsmith":
            raise ValueError("GDsmith is the only supported query source")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @property
    def ast_hash(self) -> str:
        return portable_ast_hash(self.text)


@dataclass(frozen=True)
class QueryCorpus:
    queries: tuple[PortableQuery, ...]
    generation_seed: int | None = None
    duplicate_ast_hashes: tuple[str, ...] = ()


def portable_ast_hash(text: str) -> str:
    from graph_sym_perf.cypher.parser import alpha_normalize_query

    canonical = alpha_normalize_query(text)
    return sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["GraphCorpus", "PortableQuery", "QueryCorpus", "portable_ast_hash"]
