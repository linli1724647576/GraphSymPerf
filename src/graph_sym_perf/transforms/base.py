"""Minimal transform-family interfaces shared by bool and symmetry families."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from graph_sym_perf.core import QueryCase, QueryFamily, QueryVariant, TargetSystem, TransformConfig

TransformFamily = QueryFamily
TransformVariant = QueryVariant
TransformGenerator = Callable[[QueryCase, TransformConfig | None, TargetSystem | None], TransformFamily]


@dataclass(frozen=True)
class TransformContext:
    query_case: QueryCase
    config: TransformConfig
    target: TargetSystem | None = None
