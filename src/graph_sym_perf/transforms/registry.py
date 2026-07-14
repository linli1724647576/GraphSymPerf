"""Registry for transform families."""

from __future__ import annotations

from graph_sym_perf.core import QueryCase, TargetSystem, TransformConfig
from graph_sym_perf.transforms.base import TransformFamily, TransformGenerator
from graph_sym_perf.transforms.bool_redundancy import generate_bool_redundancy_family
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.symmetry import generate_symmetry_family

__all__ = [
    "RuleRegistry",
    "available_families",
    "family_generator",
    "generate_transform_family",
    "select_family_name",
]

_FAMILIES: dict[str, TransformGenerator] = {
    "bool_redundancy": generate_bool_redundancy_family,
    "symmetry": generate_symmetry_family,
}


def available_families() -> tuple[str, ...]:
    return tuple(sorted(_FAMILIES))


def family_generator(name: str) -> TransformGenerator:
    try:
        return _FAMILIES[name]
    except KeyError as exc:
        allowed = ", ".join(available_families())
        raise ValueError(f"unknown transform family {name!r}; expected one of: {allowed}") from exc


def select_family_name(query_case: QueryCase, config: TransformConfig | None = None) -> str:
    transform_config = config or TransformConfig()
    enabled = tuple(name for name in transform_config.enabled_families if name in _FAMILIES)
    if enabled:
        if query_case.source == "gdsmith" and "symmetry" in enabled:
            return "symmetry"
        if query_case.base_predicate and "bool_redundancy" in enabled:
            return "bool_redundancy"
        return enabled[0]
    return "bool_redundancy" if query_case.base_predicate else "symmetry"


def generate_transform_family(
    query_case: QueryCase,
    config: TransformConfig | None = None,
    *,
    target: TargetSystem | None = None,
    family_name: str | None = None,
) -> TransformFamily:
    transform_config = config or TransformConfig()
    chosen = family_name or select_family_name(query_case, transform_config)
    generator = family_generator(chosen)
    if chosen == "symmetry":
        return generator(query_case, transform_config, None)
    return generator(query_case, transform_config, target)
