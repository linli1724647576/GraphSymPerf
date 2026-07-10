"""Redundant boolean predicate injection."""

from __future__ import annotations

import re
from typing import Iterable

from graph_sym_perf.core import InjectionSite, QueryCase, QueryFamily, QueryPair, QueryVariant, TargetSystem, TransformConfig
from graph_sym_perf.cypher.ast import (
    EdgePattern as AstEdgePattern,
    NodePattern as AstNodePattern,
    PatternList as AstPatternList,
    VarLengthPathPattern as AstVarLengthPathPattern,
)
from graph_sym_perf.cypher.parser import CypherParseError, parse_match_clause, parse_query
from graph_sym_perf.cypher.proof import ProofFailure, prove_redundant_boolean_predicate
from graph_sym_perf.query_ir import select_expression, wrap_as_case_true


TRANSFORM_RULE = "and_true_or"
GDSMITH_TRANSFORM_RULE = "gdsmith_ir_redundant_predicate"

SUPPORTED_GDSMITH_RULES = (
    "and_true_or",
    "and_expr_or_true",
    "or_false_and",
    "or_expr_and_false",
    "and_not_false_and",
    "and_case_true",
    "and_expr_null_partition",
    "or_expr_null_contradiction",
    "and_not_expr_null_contradiction",
    "and_excluded_middle_or_null",
    "or_guarded_contradiction",
    "and_double_negated_true_or",
    "or_duplicate_predicate",
    "and_null_guarded_true",
    "and_distributive_absorption",
    "and_multi_layer_redundancy",
    "arcade_and_true",
    "arcade_or_false",
    "arcade_and_not_false",
    "arcade_and_true_or_false_expr",
)

RULE_SPACE_METADATA: dict[str, dict[str, str]] = {
    "and_true_or": {
        "rule_space_id": "B1",
        "rule_type": "constant_folding_stress",
        "optimizer_target": "constant_folding_failure",
    },
    "and_expr_or_true": {
        "rule_space_id": "B1",
        "rule_type": "constant_folding_stress",
        "optimizer_target": "constant_folding_failure",
    },
    "or_false_and": {
        "rule_space_id": "B2",
        "rule_type": "dead_branch_amplification",
        "optimizer_target": "dead_code_elimination",
    },
    "or_expr_and_false": {
        "rule_space_id": "B2",
        "rule_type": "dead_branch_amplification",
        "optimizer_target": "dead_code_elimination",
    },
    "and_expr_null_partition": {
        "rule_space_id": "B3",
        "rule_type": "null_aware_contradiction",
        "optimizer_target": "null_propagation",
    },
    "or_expr_null_contradiction": {
        "rule_space_id": "B3",
        "rule_type": "null_aware_contradiction",
        "optimizer_target": "null_propagation",
    },
    "and_not_expr_null_contradiction": {
        "rule_space_id": "B3",
        "rule_type": "null_aware_contradiction",
        "optimizer_target": "null_propagation",
    },
    "or_duplicate_predicate": {
        "rule_space_id": "B4",
        "rule_type": "predicate_explosion",
        "optimizer_target": "predicate_deduplication",
    },
    "and_case_true": {
        "rule_space_id": "B5",
        "rule_type": "conditional_flattening",
        "optimizer_target": "logical_equivalence_detection",
    },
    "and_excluded_middle_or_null": {
        "rule_space_id": "B6",
        "rule_type": "boolean_distribution_stress",
        "optimizer_target": "algebraic_rewrite_and_pushdown",
    },
    "and_distributive_absorption": {
        "rule_space_id": "B6",
        "rule_type": "boolean_distribution_stress",
        "optimizer_target": "algebraic_rewrite_and_pushdown",
    },
    "or_guarded_contradiction": {
        "rule_space_id": "B7",
        "rule_type": "guarded_contradiction",
        "optimizer_target": "partial_evaluation",
    },
    "and_not_false_and": {
        "rule_space_id": "B8",
        "rule_type": "multi_layer_redundancy",
        "optimizer_target": "multi_stage_simplification",
    },
    "and_double_negated_true_or": {
        "rule_space_id": "B8",
        "rule_type": "multi_layer_redundancy",
        "optimizer_target": "multi_stage_simplification",
    },
    "and_multi_layer_redundancy": {
        "rule_space_id": "B8",
        "rule_type": "multi_layer_redundancy",
        "optimizer_target": "multi_stage_simplification",
    },
    "and_null_guarded_true": {
        "rule_space_id": "B9",
        "rule_type": "null_guarded_predicate_shift",
        "optimizer_target": "null_elimination_path",
    },
}

SKIP_UNSUPPORTED_SCOPE_START = "UNSUPPORTED_SCOPE_START"
SKIP_UNSAFE_OPTIONAL_MATCH_SITE = "UNSAFE_OPTIONAL_MATCH_SITE"
SKIP_CALL_NOT_SUPPORTED = "CALL_NOT_SUPPORTED"
SKIP_EXISTS_NOT_SUPPORTED = "EXISTS_NOT_SUPPORTED"
SKIP_PATTERN_COMPREHENSION_NOT_SUPPORTED = "PATTERN_COMPREHENSION_NOT_SUPPORTED"
SKIP_VARIABLE_LENGTH_PATH_NOT_SUPPORTED = "VARIABLE_LENGTH_PATH_NOT_SUPPORTED"
SKIP_WRITE_STATEMENT = "WRITE_STATEMENT"
SKIP_NO_MATCH_CLAUSE = "NO_MATCH_CLAUSE"
SKIP_NO_BOUND_NODE_VARIABLE = "NO_BOUND_NODE_VARIABLE"
SKIP_NO_ENABLED_TRANSFORM_RULE = "NO_ENABLED_TRANSFORM_RULE"
SKIP_EXPRESSION_GENERATION_FAILED = "EXPRESSION_GENERATION_FAILED"

_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_PROPERTY_TEMPLATE = r"\b{}\s*\.\s*([A-Za-z][A-Za-z0-9_]*)\b"
_WRITE_RE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP)\b", re.IGNORECASE)
_EXISTS_RE = re.compile(r"\bEXISTS\b", re.IGNORECASE)
_CALL_RE = re.compile(r"\bCALL\b", re.IGNORECASE)


class TransformSkipError(ValueError):
    """Raised when a query is deliberately excluded from safe transformation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def inject_redundant_predicate(
    query_case: QueryCase,
    transform_config: TransformConfig | None = None,
    target: TargetSystem | None = None,
) -> QueryPair:
    family = generate_bool_redundancy_family(query_case, transform_config, target)
    if not family.variants:
        skipped = family.skipped_variants[0] if family.skipped_variants else None
        raise TransformSkipError(
            skipped.skip_reason if skipped is not None else SKIP_NO_ENABLED_TRANSFORM_RULE,
            skipped.metadata.get("error", "no redundant predicate variant was generated") if skipped else "no redundant predicate variant was generated",
        )
    variant = family.variants[0]
    metadata = dict(variant.metadata)
    return QueryPair(
        case_id=query_case.case_id,
        original_query=family.seed_query,
        transformed_query=variant.query,
        parameters=family.parameters,
        transform_rule=variant.transform_rule,
        proof_obligation=variant.proof_obligation,
        expression_record=metadata.get("expression_record"),
        injection_site=metadata.get("injection_site"),
        transform_metadata=metadata,
    )


def generate_bool_redundancy_family(
    query_case: QueryCase,
    transform_config: TransformConfig | None = None,
    target: TargetSystem | None = None,
) -> QueryFamily:
    config = transform_config or TransformConfig(enabled_families=("bool_redundancy",))
    query = query_case.query.rstrip().rstrip(";")
    variants: list[QueryVariant] = []
    skipped: list[QueryVariant] = []

    if query_case.source != "gdsmith" and query_case.base_predicate:
        rules = (query_case.transform_rule,) if query_case.transform_rule else _enabled_rules(config.enabled_rules)
        if config.max_variants_per_case is not None:
            rules = rules[: max(0, config.max_variants_per_case)]
        for rule in rules:
            try:
                variants.append(_named_variant(query_case.case_id, len(variants) + 1, _portable_variant(query_case, query, rule=rule)))
            except TransformSkipError as exc:
                skipped.append(_skipped_variant(query_case.case_id, rule, query, exc))
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

    site = analyze_gdsmith_injection_site(query)
    rules = _enabled_rules(config.enabled_rules)
    if target is TargetSystem.ARCADEDB:
        rules = tuple(rule for rule in rules if rule.startswith("arcade_")) or (
            "arcade_and_true",
            "arcade_or_false",
            "arcade_and_not_false",
            "arcade_and_true_or_false_expr",
        )
    if config.max_variants_per_case is not None:
        rules = rules[: max(0, config.max_variants_per_case)]
    property_names = _property_names(query, site.selected_variable or "")
    if not property_names:
        property_names = ("id",)
    expression_dialect = _expression_dialect(target)
    case_index = _case_index(query_case.case_id)

    for index, rule in enumerate(rules, start=1):
        try:
            expression = select_expression(
                site.selected_variable or "n",
                property_names=property_names,
                index=case_index + index - 1,
                max_complexity=config.max_expr_complexity,
                dialect=expression_dialect,
            )
            rendered_expr = expression.rendered_cypher
            expression_record = expression
            if rule == "and_case_true":
                expression_record = wrap_as_case_true(expression)
                rendered_expr = expression.rendered_cypher
            proof, ast_fragment = _prove_static_redundant_transform(
                query,
                site,
                rule=rule,
                expression=expression_record,
                referenced_variables=expression_record.referenced_variables,
            )
            variants.append(
                QueryVariant(
                    variant_id=f"{query_case.case_id}_{index:02d}_{rule}",
                    query=render_gdsmith_transform(query, site, rule=rule, expression=rendered_expr),
                    transform_rule=rule,
                    proof_obligation=proof.obligation,
                    metadata={
                        "transform_family": "bool_redundancy",
                        "source": "gdsmith",
                        "rule": rule,
                        "rule_family": "conservative_tautology",
                        "base_predicate": site.base_predicate,
                        "expression_family": expression_record.family,
                        "expression_id": expression_record.expr_id,
                        "expression_record": expression_record,
                        "injection_site": site,
                        "referenced_variables": expression_record.referenced_variables,
                        "selected_variable": site.selected_variable,
                        "safe_only": config.gdsmith_safe_only,
                        "target": target.value if target is not None else None,
                        "expression_dialect": expression_dialect,
                        **_rule_space_metadata(rule),
                        **_proof_metadata(proof, ast_fragment),
                    },
                )
            )
        except TransformSkipError as exc:
            skipped.append(_skipped_variant(query_case.case_id, rule, query, exc))

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


def analyze_gdsmith_injection_site(query: str) -> InjectionSite:
    original = query.rstrip().rstrip(";")
    _reject_unsafe_scope(original)
    clauses = parse_query(original).ordinary_match_clauses()
    if not clauses:
        raise TransformSkipError(SKIP_NO_MATCH_CLAUSE, "query does not contain a supported non-optional MATCH clause")
    try:
        match_clause = parse_match_clause(clauses[0])
    except CypherParseError as exc:
        raise TransformSkipError(exc.code, str(exc)) from exc
    variables = _node_variables_from_pattern(match_clause.pattern)
    if not variables:
        raise TransformSkipError(SKIP_NO_BOUND_NODE_VARIABLE, "MATCH clause does not expose a bound node variable")
    base_predicate = match_clause.where_region.text if match_clause.where_region else "true"
    return InjectionSite(
        clause_kind="MATCH",
        start=match_clause.clause.start,
        end=match_clause.clause.end,
        has_where=match_clause.where_region is not None,
        bound_variables=variables,
        selected_variable=variables[0],
        base_predicate=base_predicate,
    )


def render_gdsmith_transform(
    original_query: str,
    injection_site: InjectionSite,
    *,
    rule: str,
    expression: str,
) -> str:
    original = original_query.rstrip().rstrip(";")
    clause = original[injection_site.start : injection_site.end]
    predicate = _render_rule(rule, injection_site.base_predicate, expression)
    where_match = _WHERE_RE.search(clause)
    if where_match is None:
        transformed_clause = f"{clause.rstrip()} WHERE {predicate}"
    else:
        before_where = clause[: where_match.start()].rstrip()
        transformed_clause = f"{before_where} WHERE {predicate}"
    suffix = original[injection_site.end :]
    separator = " " if suffix else ""
    return original[: injection_site.start] + transformed_clause.rstrip() + separator + suffix.lstrip()


def _inject_gdsmith_redundant_predicate(
    query_case: QueryCase,
    config: TransformConfig,
    target: TargetSystem | None,
) -> QueryPair:
    original = query_case.query.rstrip().rstrip(";")
    site = analyze_gdsmith_injection_site(original)
    if site.selected_variable is None:
        raise TransformSkipError(SKIP_NO_BOUND_NODE_VARIABLE, "safe injection site has no selected variable")
    rules = _enabled_rules(config.enabled_rules)
    if target is TargetSystem.ARCADEDB:
        rules = ("arcade_and_true", "arcade_or_false", "arcade_and_not_false", "arcade_and_true_or_false_expr")
    case_index = _case_index(query_case.case_id)
    rule = rules[case_index % len(rules)]
    property_names = _property_names(original, site.selected_variable)
    if not property_names:
        property_names = ("id",)
    expression_dialect = _expression_dialect(target)
    try:
        expression = select_expression(
            site.selected_variable,
            property_names=property_names,
            index=case_index // len(rules),
            max_complexity=config.max_expr_complexity,
            dialect=expression_dialect,
        )
    except ValueError as exc:
        raise TransformSkipError(SKIP_EXPRESSION_GENERATION_FAILED, str(exc)) from exc

    rendered_expr = expression.rendered_cypher
    expression_record = expression
    if rule == "and_case_true":
        expression_record = wrap_as_case_true(expression)
        rendered_expr = expression.rendered_cypher

    proof, ast_fragment = _prove_static_redundant_transform(
        original,
        site,
        rule=rule,
        expression=expression_record,
        referenced_variables=expression_record.referenced_variables,
    )
    transformed = render_gdsmith_transform(original, site, rule=rule, expression=rendered_expr)
    return QueryPair(
        case_id=query_case.case_id,
        original_query=original,
        transformed_query=transformed,
        parameters=query_case.parameters,
        transform_rule=rule,
        proof_obligation=proof.obligation,
        expression_record=expression_record,
        injection_site=site,
        transform_metadata={
            "source": "gdsmith",
            "rule": rule,
            "rule_family": "conservative_tautology",
            "base_predicate": site.base_predicate,
            "expression_family": expression_record.family,
            "expression_id": expression_record.expr_id,
            "referenced_variables": expression_record.referenced_variables,
            "selected_variable": site.selected_variable,
            "safe_only": config.gdsmith_safe_only,
            "target": target.value if target is not None else None,
            "expression_dialect": expression_dialect,
            **_rule_space_metadata(rule),
            **_proof_metadata(proof, ast_fragment),
        },
    )


def _expression_dialect(target: TargetSystem | None) -> str:
    if target in (None, TargetSystem.NEO4J):
        return "neo4j"
    return "portable"


def _reject_unsafe_scope(query: str) -> None:
    stripped = query.lstrip()
    upper = stripped.upper()
    if upper.startswith("WITH "):
        raise TransformSkipError(SKIP_UNSUPPORTED_SCOPE_START, "query starts with WITH and may rebind scope")
    if upper.startswith("OPTIONAL MATCH"):
        raise TransformSkipError(SKIP_UNSAFE_OPTIONAL_MATCH_SITE, "query starts with OPTIONAL MATCH")
    if _CALL_RE.search(query):
        raise TransformSkipError(SKIP_CALL_NOT_SUPPORTED, "CALL and subquery scopes are not supported")
    if _EXISTS_RE.search(query):
        raise TransformSkipError(SKIP_EXISTS_NOT_SUPPORTED, "EXISTS predicates are not supported")
    if _WRITE_RE.search(query):
        raise TransformSkipError(SKIP_WRITE_STATEMENT, "write statements are not transformed")
    if _has_pattern_comprehension(query):
        raise TransformSkipError(SKIP_PATTERN_COMPREHENSION_NOT_SUPPORTED, "pattern comprehension is not supported")
    if _has_variable_length_path(query):
        raise TransformSkipError(SKIP_VARIABLE_LENGTH_PATH_NOT_SUPPORTED, "variable-length paths are not supported")


def _property_names(query: str, variable: str) -> tuple[str, ...]:
    pattern = re.compile(_PROPERTY_TEMPLATE.format(re.escape(variable)))
    properties: list[str] = []
    for match in pattern.finditer(query):
        prop = match.group(1)
        if prop not in properties:
            properties.append(prop)
    return tuple(properties)


def _enabled_rules(raw_rules: Iterable[str]) -> tuple[str, ...]:
    rules = tuple(rule for rule in raw_rules if rule in SUPPORTED_GDSMITH_RULES)
    return rules or SUPPORTED_GDSMITH_RULES


def _case_index(case_id: str) -> int:
    match = re.search(r"(\d+)$", case_id)
    if not match:
        return 0
    return max(int(match.group(1)) - 1, 0)


def _render_rule(rule: str, base_predicate: str, expression: str) -> str:
    base = base_predicate.strip() or "true"
    expr = expression.strip()
    if rule in {"where_and_true_or_expr", "and_true_or"}:
        return f"({base}) AND (true OR ({expr}))"
    if rule == "and_expr_or_true":
        return f"({base}) AND (({expr}) OR true)"
    if rule == "or_false_and":
        return f"({base}) OR (false AND ({expr}))"
    if rule == "or_expr_and_false":
        return f"({base}) OR (({expr}) AND false)"
    if rule == "and_not_false_and":
        return f"({base}) AND NOT(false AND ({expr}))"
    if rule == "and_case_true":
        return f"({base}) AND CASE WHEN ({expr}) THEN true ELSE true END"
    if rule == "and_expr_null_partition":
        return f"({base}) AND (({expr}) IS NULL OR ({expr}) IS NOT NULL)"
    if rule == "or_expr_null_contradiction":
        return f"({base}) OR (({expr}) IS NULL AND ({expr}) IS NOT NULL)"
    if rule == "and_not_expr_null_contradiction":
        return f"({base}) AND NOT((({expr}) IS NULL) AND (({expr}) IS NOT NULL))"
    if rule == "and_excluded_middle_or_null":
        return f"({base}) AND ((({expr}) OR NOT({expr})) OR (({expr}) IS NULL))"
    if rule == "or_guarded_contradiction":
        return f"({base}) OR ((({expr}) AND NOT({expr})) AND (({expr}) IS NOT NULL))"
    if rule == "and_double_negated_true_or":
        return f"({base}) AND NOT(NOT(true OR ({expr})))"
    if rule == "or_duplicate_predicate":
        return f"({base}) OR ({base}) OR ({base})"
    if rule == "and_null_guarded_true":
        return f"({base}) AND ((({expr}) IS NOT NULL) OR true)"
    if rule == "and_distributive_absorption":
        return f"(({base}) AND ((({expr}) OR NOT({expr})) OR (({expr}) IS NULL))) OR (({base}) AND false)"
    if rule == "and_multi_layer_redundancy":
        return f"(({base}) AND (true OR ({expr}))) OR (false AND ({expr}))"
    if rule == "arcade_and_true":
        return f"({base}) AND true"
    if rule == "arcade_or_false":
        # Fully parenthesized: explicit precedence avoids soundness ambiguity
        return f"({base}) OR (false AND ({expr}))"
    if rule == "arcade_and_not_false":
        return f"({base}) AND (NOT false)"
    if rule == "arcade_and_true_or_false_expr":
        # Fully parenthesized: explicit precedence avoids soundness ambiguity
        return f"(({base}) AND true) OR (false AND ({expr}))"
    raise TransformSkipError(SKIP_NO_ENABLED_TRANSFORM_RULE, f"unsupported transform rule: {rule}")


def _prove_static_redundant_transform(
    query: str,
    site: InjectionSite,
    *,
    rule: str,
    expression,
    referenced_variables: tuple[str, ...],
):
    try:
        match_clause = _match_clause_for_site(query.rstrip().rstrip(";"), site)
        proof = prove_redundant_boolean_predicate(
            match_clause,
            base_predicate=site.base_predicate,
            rule=rule,
            expression=expression,
            referenced_variables=referenced_variables,
        )
    except (CypherParseError, ProofFailure) as exc:
        raise TransformSkipError(getattr(exc, "code", "UNPROVEN_TRANSFORM"), str(exc)) from exc
    return proof, type(match_clause.pattern).__name__


def _match_clause_for_site(query: str, site: InjectionSite):
    for clause in parse_query(query).ordinary_match_clauses():
        if clause.start == site.start and clause.end == site.end:
            return parse_match_clause(clause)
    raise TransformSkipError(SKIP_NO_MATCH_CLAUSE, "selected MATCH clause was not found")


def _proof_metadata(proof, ast_fragment: str) -> dict[str, object]:
    return {
        "proof_tier": "strict",
        "semantic_proof": proof.to_metadata(),
        "ast_fragment": ast_fragment,
        "ra_canonical": proof.original_ra,
    }


def _node_variables_from_pattern(pattern: object) -> tuple[str, ...]:
    variables: list[str] = []

    def add(value: str) -> None:
        if value not in variables:
            variables.append(value)

    def visit(item: object) -> None:
        if isinstance(item, AstNodePattern):
            add(item.variable)
            return
        if isinstance(item, AstEdgePattern | AstVarLengthPathPattern):
            add(item.left.variable)
            add(item.right.variable)
            return
        if isinstance(item, AstPatternList):
            for atom in item.atoms:
                visit(atom)

    visit(pattern)
    return tuple(variables)


def _referenced_variables_from_expression(expression: str) -> tuple[str, ...]:
    variables: list[str] = []
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]*)\s*\.", expression):
        variable = match.group(1)
        if variable not in variables:
            variables.append(variable)
    return tuple(variables)


def _normalize_predicate_text(predicate: str) -> str:
    return re.sub(r"\s+", " ", predicate.strip())


def _has_pattern_comprehension(query: str) -> bool:
    for match in re.finditer(r"\[[^\]]*\|[^\]]*\]", query, re.DOTALL):
        snippet = match.group(0)
        if "(" in snippet and ")" in snippet:
            return True
    return False


def _has_variable_length_path(query: str) -> bool:
    return bool(re.search(r"\[[^\]]*\*", query))


def _portable_variant(query_case: QueryCase, query: str, *, rule: str | None = None) -> QueryVariant:
    site = analyze_gdsmith_injection_site(query)
    if _normalize_predicate_text(site.base_predicate) != _normalize_predicate_text(query_case.base_predicate):
        raise TransformSkipError(
            "UNSAFE_PREDICATE_PROOF_FRAGMENT",
            "query case base_predicate does not match the selected MATCH WHERE predicate",
        )
    rule = query_case.transform_rule or rule or TRANSFORM_RULE
    proof, ast_fragment = _prove_static_redundant_transform(
        query,
        site,
        rule=rule,
        expression=query_case.safe_expression,
        referenced_variables=_referenced_variables_from_expression(query_case.safe_expression),
    )
    return QueryVariant(
        variant_id="",
        query=render_gdsmith_transform(query, site, rule=rule, expression=query_case.safe_expression),
        transform_rule=rule,
        proof_obligation=proof.obligation,
        metadata={
            "transform_family": "bool_redundancy",
            "source": query_case.source,
            "rule": rule,
            "rule_family": "conservative_tautology",
            "base_predicate": site.base_predicate,
            "expression_cypher": query_case.safe_expression,
            "injection_site": site,
            "referenced_variables": _referenced_variables_from_expression(query_case.safe_expression),
            **_rule_space_metadata(rule),
            **_proof_metadata(proof, ast_fragment),
        },
    )


def _rule_space_metadata(rule: str) -> dict[str, str]:
    metadata = RULE_SPACE_METADATA.get(rule)
    if metadata is not None:
        return dict(metadata)
    if rule.startswith("arcade_"):
        return {
            "rule_space_id": "B8",
            "rule_type": "portable_arcadedb_redundancy",
            "optimizer_target": "portable_boolean_simplification",
        }
    return {
        "rule_space_id": "B0",
        "rule_type": "legacy_bool_redundancy",
        "optimizer_target": "boolean_simplification",
    }


def _named_variant(case_id: str, index: int, variant: QueryVariant) -> QueryVariant:
    return QueryVariant(
        variant_id=f"{case_id}_{index:02d}_{variant.transform_rule}",
        query=variant.query,
        transform_rule=variant.transform_rule,
        proof_obligation=variant.proof_obligation,
        static_safe=variant.static_safe,
        safety_tier=variant.safety_tier,
        metadata=variant.metadata,
    )


def _skipped_variant(case_id: str, rule: str, query: str, exc: TransformSkipError) -> QueryVariant:
    return QueryVariant(
        variant_id=f"{case_id}_{rule}",
        query=query,
        transform_rule=rule,
        proof_obligation="No static proof obligation because this rule was skipped.",
        static_safe=False,
        safety_tier="strict",
        skip_reason=exc.code,
        metadata={"error": str(exc), "transform_family": "bool_redundancy"},
    )
