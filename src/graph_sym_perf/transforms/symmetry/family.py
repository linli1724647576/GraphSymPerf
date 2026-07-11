"""Audited symmetry family backed by certificate-bound CORE rules."""

from __future__ import annotations

from graph_sym_perf.core import QueryCase, SymmetryFamily, SymmetryVariant, TargetSystem, TransformConfig
from graph_sym_perf.transforms.engine import RuleNotApplicable, apply_certified_rule
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


class TransformSkipError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def generate_symmetry_family(
    query_case: QueryCase,
    config: TransformConfig | None = None,
    target: TargetSystem | None = None,
) -> SymmetryFamily:
    transform_config = config or TransformConfig(enabled_families=("symmetry",))
    query = query_case.query.rstrip().rstrip(";")
    dbms = target.value if target is not None else "neo4j"
    registry = RuleRegistry.default()
    available = tuple(
        rule
        for rule in registry.rules_for(dbms, SafetyTier.CORE)
        if rule.rule_id.endswith("_symmetry")
    )
    requested = tuple(rule for rule in available if rule.rule_id in transform_config.enabled_rules)
    if transform_config.max_variants_per_case is not None:
        requested = requested[: max(0, transform_config.max_variants_per_case)]
    variants: list[SymmetryVariant] = []
    skipped: list[SymmetryVariant] = []
    seen = {query}
    for rule in requested:
        transformed = None
        selected_certificate = None
        for certificate in enumerate_safety_points(query, rule, f"legacy-{dbms}"):
            try:
                candidate = apply_certified_rule(query, rule, certificate)
            except (RuleNotApplicable, ValueError):
                continue
            if candidate not in seen:
                transformed, selected_certificate = candidate, certificate
                break
        if transformed is None or selected_certificate is None:
            skipped.append(
                SymmetryVariant(
                    variant_id=f"{query_case.case_id}_{rule.rule_id}",
                    query=query,
                    transform_rule=rule.rule_id,
                    proof_obligation=rule.proof_theorem,
                    static_safe=False,
                    safety_tier=rule.tier.value,
                    skip_reason="RULE_NOT_APPLICABLE",
                )
            )
            continue
        seen.add(transformed)
        variants.append(
            SymmetryVariant(
                variant_id=f"{query_case.case_id}_{rule.rule_id}",
                query=transformed,
                transform_rule=rule.rule_id,
                proof_obligation=rule.proof_theorem,
                safety_tier=rule.tier.value,
                metadata={
                    "transform_family": "symmetry",
                    "rule_version": rule.version,
                    "safety_certificate": selected_certificate,
                },
            )
        )
    return SymmetryFamily(
        case_id=query_case.case_id,
        seed_query=query,
        parameters=query_case.parameters,
        variants=tuple(variants),
        skipped_variants=tuple(skipped),
        source=query_case.source,
        generator_case_id=query_case.generator_case_id,
        raw_query=query_case.raw_query,
    )


__all__ = ["TransformSkipError", "generate_symmetry_family"]
