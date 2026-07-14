"""Registry for transform families."""

from __future__ import annotations

from graph_sym_perf.core import QueryCase, TargetSystem, TransformConfig
from graph_sym_perf.transforms.base import TransformFamily, TransformGenerator
from graph_sym_perf.transforms.bool_redundancy import generate_bool_redundancy_family
from graph_sym_perf.transforms.rule_models import RuleRegistry, RuleSpec
from graph_sym_perf.transforms.symmetry import generate_symmetry_family

__all__ = [
    "RuleRegistry",
    "available_families",
    "family_generator",
    "generate_transform_family",
    "generate_rule_transform",
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


def generate_rule_transform(
    rule: RuleSpec,
    query_case: QueryCase,
    config: TransformConfig,
    target: TargetSystem,
) -> TransformFamily:
    """Invoke one registry rule through its family adapter.

    Boolean rules deliberately keep their public canonical IDs separate from
    the low-level implementation IDs.  Rule-plan execution names the latter in
    ``TransformConfig``; this boundary translates it back exactly once for the
    legacy family generator and prevents the family from selecting a sibling
    rule.
    """

    if config.enabled_rules != (rule.implementation_rule_id,):
        raise ValueError("single-rule transform config does not match the registry implementation id")
    family_name = "bool_redundancy" if rule.family.value == "bool" else "symmetry"
    if config.enabled_families != (family_name,):
        raise ValueError("single-rule transform family does not match registry facts")
    canonical_config = TransformConfig(
        enabled_families=config.enabled_families,
        enabled_rules=(rule.canonical_rule_id,),
        max_expr_complexity=config.max_expr_complexity,
        gdsmith_safe_only=config.gdsmith_safe_only,
        max_variants_per_case=config.max_variants_per_case,
    )
    return rule.transform_factory(query_case, canonical_config, target)
