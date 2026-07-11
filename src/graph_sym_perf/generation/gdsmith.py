from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from graph_sym_perf.generation.corpus import PortableQuery, QueryCorpus


@dataclass(frozen=True)
class GenerationRequest:
    seed: int
    limit: int

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("generation seed must be an integer")
        if self.seed < 0:
            raise ValueError("generation seed must be non-negative")
        if not isinstance(self.limit, int) or isinstance(self.limit, bool):
            raise TypeError("generation limit must be an integer")
        if self.limit <= 0:
            raise ValueError("generation limit must be positive")


def derive_generation_seed(experiment_seed: int, target: str, shard_index: int, batch_index: int) -> int:
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in (experiment_seed, shard_index, batch_index)):
        raise ValueError("experiment seed, shard index, and batch index must be non-negative integers")
    if not isinstance(target, str) or not target:
        raise ValueError("target must be non-empty")
    payload = json.dumps(
        {"batch": batch_index, "experiment_seed": experiment_seed, "shard": shard_index, "target": target},
        sort_keys=True,
        separators=(",", ":"),
    )
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big", signed=False)


class GdsmithGenerator:
    """The only query-generation seam; alternate backends are invalid."""

    def __init__(self, generator: Callable[[GenerationRequest], tuple[str, ...]], *, source: str = "gdsmith") -> None:
        if source != "gdsmith":
            raise ValueError("GDsmith is the only supported generator")
        self._generator = generator

    def generate(self, request: GenerationRequest) -> QueryCorpus:
        generated = self._generator(request)
        if not isinstance(generated, tuple) or not all(isinstance(query, str) and query.strip() for query in generated):
            raise TypeError("GDsmith generator must return a tuple of non-empty query strings")
        queries: list[PortableQuery] = []
        seen: set[str] = set()
        duplicates: list[str] = []
        for text in generated[: request.limit]:
            query = PortableQuery(text)
            if query.ast_hash in seen:
                duplicates.append(query.ast_hash)
                continue
            seen.add(query.ast_hash)
            queries.append(query)
        return QueryCorpus(tuple(queries), generation_seed=request.seed, duplicate_ast_hashes=tuple(duplicates))


__all__ = ["GdsmithGenerator", "GenerationRequest", "derive_generation_seed"]
