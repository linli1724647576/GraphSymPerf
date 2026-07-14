"""Audited boolean-equivalence family backed by certificate-bound CORE rules."""

from __future__ import annotations

from graph_sym_perf.core import QueryCase, QueryFamily, QueryPair, QueryVariant, TargetSystem, TransformConfig
from graph_sym_perf.transforms.engine import RuleNotApplicable, apply_certified_rule_with_proof
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


TRANSFORM_RULE = "null_complete_excluded_middle"
GDSMITH_TRANSFORM_RULE = "certified_boolean_equivalence"


class TransformSkipError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def generate_bool_redundancy_family(
    query_case: QueryCase,
    transform_config: TransformConfig | None = None,
    target: TargetSystem | None = None,
) -> QueryFamily:
    config = transform_config or TransformConfig(enabled_families=("bool_redundancy",))
    query = query_case.query.rstrip().rstrip(";")
    registry = RuleRegistry.default()
    dbms = target.value if target is not None else "neo4j"
    available = tuple(
        rule
        for tier in (SafetyTier.CORE, SafetyTier.CONTROL)
        for rule in registry.rules_for(dbms, tier)
        if rule.family.value == "bool" and rule.site_classes == ("where_boolean",)
    )
    requested = tuple(rule for rule in available if rule.rule_id in config.enabled_rules)
    if config.max_variants_per_case is not None:
        requested = requested[: max(0, config.max_variants_per_case)]
    variants: list[QueryVariant] = []
    skipped: list[QueryVariant] = []
    seen = {query}
    for rule in requested:
        certificates = enumerate_safety_points(query, rule, f"legacy-{dbms}")
        transformed = None
        selected_certificate = None
        selected_proof = None
        for certificate in certificates:
            try:
                result = apply_certified_rule_with_proof(query, rule, certificate)
                candidate = result.query
            except RuleNotApplicable:
                continue
            if candidate not in seen:
                transformed, selected_certificate = candidate, result.certificate
                selected_proof = result.semantic_proof
                break
        if transformed is None or selected_certificate is None or selected_proof is None:
            skipped.append(
                QueryVariant(
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
            QueryVariant(
                variant_id=f"{query_case.case_id}_{rule.rule_id}",
                query=transformed,
                transform_rule=rule.rule_id,
                proof_obligation=rule.proof_theorem,
                safety_tier=rule.tier.value,
                metadata={
                    "transform_family": "bool_redundancy",
                    "rule_version": rule.version,
                    "safety_certificate": selected_certificate,
                    "semantic_proof": selected_proof,
                },
            )
        )
    return QueryFamily(
        case_id=query_case.case_id,
        seed_query=query,
        parameters=query_case.parameters,
        variants=tuple(variants),
        skipped_variants=tuple(skipped),
        source=query_case.source,
        generator_case_id=query_case.generator_case_id,
        raw_query=query_case.raw_query,
    )


def inject_redundant_predicate(
    query_case: QueryCase,
    transform_config: TransformConfig | None = None,
    target: TargetSystem | None = None,
) -> QueryPair:
    family = generate_bool_redundancy_family(query_case, transform_config, target)
    if not family.variants:
        raise TransformSkipError("RULE_NOT_APPLICABLE", "no certified boolean rule applies")
    variant = family.variants[0]
    return QueryPair(
        case_id=query_case.case_id,
        original_query=family.seed_query,
        transformed_query=variant.query,
        parameters=family.parameters,
        transform_rule=variant.transform_rule,
        proof_obligation=variant.proof_obligation,
        transform_metadata=variant.metadata,
    )


__all__ = [
    "GDSMITH_TRANSFORM_RULE",
    "TRANSFORM_RULE",
    "TransformSkipError",
    "generate_bool_redundancy_family",
    "inject_redundant_predicate",
]
