from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from graph_sym_perf.generation.corpus import PortableQuery, QueryCorpus


@dataclass(frozen=True)
class GenerationRequest:
    seed: int
    limit: int


class GdsmithGenerator:
    def __init__(self, generator: Callable[[GenerationRequest], tuple[str, ...]], *, source: str = "gdsmith") -> None:
        if source != "gdsmith":
            raise ValueError("GDsmith is the only supported generator")
        self._generator = generator

    def generate(self, request: GenerationRequest) -> QueryCorpus:
        return QueryCorpus(tuple(PortableQuery(query) for query in self._generator(request)[: request.limit]))
