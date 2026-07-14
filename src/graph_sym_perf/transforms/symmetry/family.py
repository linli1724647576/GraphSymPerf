"""Audited symmetry family backed by certificate-bound CORE rules."""

from __future__ import annotations

import re

from graph_sym_perf.core import QueryCase, SymmetryFamily, SymmetryVariant, TargetSystem, TransformConfig
from graph_sym_perf.cypher.ast import PatternList
from graph_sym_perf.cypher.parser import CypherParseError, parse_match_clause, parse_query
from graph_sym_perf.cypher.proof import SemanticProof, prove_pattern_list_order
from graph_sym_perf.transforms.engine import RuleNotApplicable, apply_certified_rule
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


class TransformSkipError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_PATTERN_COMPONENT_PATH = re.compile(r"^clauses\[(?P<clause>\d+)]\.pattern\.components\[\d+]$")


def _prove_certified_pattern_order(
    query: str,
    transformed: str,
    ast_path: str,
) -> tuple[SemanticProof, int]:
    path = _PATTERN_COMPONENT_PATH.fullmatch(ast_path)
    if path is None:
        raise ValueError("pattern-order certificate does not identify one MATCH pattern component")
    clause_index = int(path.group("clause"))
    original_query = parse_query(query)
    transformed_query = parse_query(transformed)
    if clause_index >= len(original_query.clauses) or clause_index >= len(transformed_query.clauses):
        raise ValueError("pattern-order certificate clause is outside the parsed query")
    original = parse_match_clause(original_query.clauses[clause_index])
    replacement = parse_match_clause(transformed_query.clauses[clause_index])
    if not isinstance(original.pattern, PatternList) or not isinstance(replacement.pattern, PatternList):
        raise ValueError("pattern-order proof requires one comma-separated MATCH pattern list")
    return prove_pattern_list_order(original.pattern, replacement.pattern), len(original.pattern.atoms)


def _has_ordinary_match_comma_pattern_list(query: str) -> bool | None:
    parse_failed = False
    for clause in parse_query(query).clauses:
        if clause.keyword != "MATCH":
            continue
        try:
            match = parse_match_clause(clause)
        except CypherParseError:
            parse_failed = True
            continue
        if (
            isinstance(match.pattern, PatternList)
            and match.pattern.separator == "comma"
            and len(match.pattern.atoms) >= 2
        ):
            return True
    return None if parse_failed else False


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
        selected_proof = None
        selected_pattern_count = None
        for certificate in enumerate_safety_points(query, rule, f"legacy-{dbms}"):
            try:
                candidate = apply_certified_rule(query, rule, certificate)
                proof_result = (
                    _prove_certified_pattern_order(query, candidate, certificate.ast_path)
                    if rule.rule_id == "pattern_order_symmetry"
                    else None
                )
                proof, pattern_count = proof_result if proof_result is not None else (None, None)
            except (RuleNotApplicable, ValueError):
                continue
            if candidate not in seen:
                transformed, selected_certificate = candidate, certificate
                selected_proof, selected_pattern_count = proof, pattern_count
                break
        if transformed is None or selected_certificate is None:
            skip_reason = (
                "NO_REORDERABLE_PATTERN_LIST"
                if rule.rule_id == "pattern_order_symmetry"
                and _has_ordinary_match_comma_pattern_list(query) is False
                else "RULE_NOT_APPLICABLE"
            )
            skipped.append(
                SymmetryVariant(
                    variant_id=f"{query_case.case_id}_{rule.rule_id}",
                    query=query,
                    transform_rule=rule.rule_id,
                    proof_obligation=rule.proof_theorem,
                    static_safe=False,
                    safety_tier=rule.tier.value,
                    skip_reason=skip_reason,
                )
            )
            continue
        seen.add(transformed)
        variants.append(
            SymmetryVariant(
                variant_id=f"{query_case.case_id}_{rule.rule_id}",
                query=transformed,
                transform_rule=rule.rule_id,
                proof_obligation=selected_proof.obligation if selected_proof is not None else rule.proof_theorem,
                safety_tier=rule.tier.value,
                metadata={
                    "transform_family": "symmetry",
                    "rule_version": rule.version,
                    "safety_certificate": selected_certificate,
                    **(
                        {"semantic_proof": selected_proof.to_metadata()}
                        if selected_proof is not None
                        else {}
                    ),
                    **(
                        {
                            "pattern_count": selected_pattern_count,
                            "site_class": "ordinary_match_comma_pattern_list",
                        }
                        if selected_pattern_count is not None
                        else {}
                    ),
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
