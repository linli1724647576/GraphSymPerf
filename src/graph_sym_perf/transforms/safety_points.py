from __future__ import annotations

import re
from dataclasses import dataclass

from graph_sym_perf.cypher.ast import CypherQuery, PatternList
from graph_sym_perf.cypher.parser import CypherParseError, parse_match_clause, parse_query
from graph_sym_perf.equivalence.purity import assess_query_purity
from graph_sym_perf.transforms.rule_models import RuleSpec, SafetyTier


@dataclass(frozen=True)
class SafetyCertificate:
    rule_id: str
    ast_path: str
    variables_in_scope: tuple[str, ...]
    expression_type: str
    nullability: str
    clause_context: str
    preconditions: tuple[str, ...]
    proof_id: str
    capability_hash: str

    def __post_init__(self) -> None:
        if not all((self.rule_id, self.ast_path, self.expression_type, self.nullability, self.clause_context, self.proof_id, self.capability_hash)):
            raise ValueError("safety certificate fields must be complete")
        if not self.preconditions:
            raise ValueError("safety certificate preconditions must be explicit")


def enumerate_safety_points(
    ast: str | CypherQuery,
    rule: RuleSpec,
    capability_hash: str,
) -> tuple[SafetyCertificate, ...]:
    """Enumerate every provable site in the supported AST fragment.

    Unsupported clauses are skipped rather than guessed. The caller records an
    explicit not-applicable outcome when no certificate is returned.
    """

    if not capability_hash:
        raise ValueError("capability hash is required")
    source = ast if isinstance(ast, str) else ast.text
    if rule.tier in {SafetyTier.CORE, SafetyTier.CONTROL} and not assess_query_purity(source).safe:
        return ()
    query = parse_query(ast) if isinstance(ast, str) else ast
    certificates: list[SafetyCertificate] = []
    bound: set[str] = set()
    for clause_index, clause in enumerate(query.clauses):
        if clause.keyword != "MATCH":
            continue
        try:
            match = parse_match_clause(clause)
        except CypherParseError:
            continue
        bound.update(match.pattern.variables if hasattr(match.pattern, "variables") else {match.pattern.variable})
        variables = tuple(sorted(bound))
        if "mandatory_match_clause" in rule.site_classes:
            certificates.append(
                _certificate(rule, f"clauses[{clause_index}].mandatory_match", variables, "graph_pattern", "non_null_introducing", "match", capability_hash)
            )
        if "match_pattern_component" in rule.site_classes:
            for component_index, _component in enumerate(_pattern_components(match.pattern)):
                certificates.append(
                    _certificate(
                        rule,
                        f"clauses[{clause_index}].pattern.components[{component_index}]",
                        variables,
                        "graph_pattern",
                        "pattern_defined",
                        "match",
                        capability_hash,
                    )
                )
        if "bound_variable" in rule.site_classes:
            for variable in variables:
                certificates.append(
                    _certificate(
                        rule,
                        f"clauses[{clause_index}].variables[{variable}]",
                        variables,
                        "bound_variable",
                        "scope_bound",
                        "match",
                        capability_hash,
                    )
                )
        if (
            "where_boolean" in rule.site_classes
            and match.where_region is not None
            and _conservative_boolean_expression(match.where_region.text)
        ):
            for suffix in _boolean_subtree_paths(match.where_region.text):
                certificates.append(
                    _certificate(
                        rule,
                        f"clauses[{clause_index}].where{suffix}",
                        variables,
                        "boolean",
                        "three_valued",
                        "where",
                        capability_hash,
                    )
                )
    return tuple(certificates)


def _certificate(
    rule: RuleSpec,
    ast_path: str,
    variables: tuple[str, ...],
    expression_type: str,
    nullability: str,
    context: str,
    capability_hash: str,
) -> SafetyCertificate:
    return SafetyCertificate(
        rule_id=rule.rule_id,
        ast_path=ast_path,
        variables_in_scope=variables,
        expression_type=expression_type,
        nullability=nullability,
        clause_context=context,
        preconditions=rule.purity_requirements,
        proof_id=rule.proof_id,
        capability_hash=capability_hash,
    )


def _pattern_components(pattern: object) -> tuple[object, ...]:
    if isinstance(pattern, PatternList):
        return tuple(pattern.atoms)
    return (pattern,)


def _boolean_subtree_paths(expression: str) -> tuple[str, ...]:
    paths: list[str] = [""]

    def visit(text: str, prefix: str) -> None:
        split = _split_top_level_boolean(text)
        if split is None:
            return
        operator, parts = split
        for index, part in enumerate(parts):
            child = f"{prefix}.{operator.lower()}[{index}]"
            paths.append(child)
            visit(_strip_outer_parentheses(part), child)

    visit(_strip_outer_parentheses(expression.strip()), "")
    return tuple(paths)


def _split_top_level_boolean(expression: str) -> tuple[str, tuple[str, ...]] | None:
    for operator in ("OR", "AND"):
        parts = _split_top_level_keyword(expression, operator)
        if len(parts) > 1:
            return operator, parts
    return None


def _split_top_level_keyword(expression: str, keyword: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    upper = expression.upper()
    while index < len(expression):
        char = expression[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            index += 1
            continue
        end = index + len(keyword)
        if (
            depth == 0
            and upper[index:end] == keyword
            and (index == 0 or not (expression[index - 1].isalnum() or expression[index - 1] == "_"))
            and (end == len(expression) or not (expression[end].isalnum() or expression[end] == "_"))
        ):
            parts.append(expression[start:index].strip())
            start = end
            index = end
            continue
        index += 1
    if not parts:
        return (expression.strip(),)
    parts.append(expression[start:].strip())
    return tuple(part for part in parts if part)


def _strip_outer_parentheses(expression: str) -> str:
    value = expression.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        encloses_all = True
        for index, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _conservative_boolean_expression(expression: str) -> bool:
    # CORE rules may duplicate a predicate. Keep the automatic fragment to
    # property/parameter/literal comparisons, NULL tests and boolean connectives.
    if re.search(r"\b(CASE|WHEN|THEN|ELSE|END|EXISTS|CALL|IN)\b", expression, re.IGNORECASE):
        return False
    if re.search(r"[+*/%]|\[[^\]]*]", expression):
        return False
    if re.search(r"\b[A-Za-z_]\w*\s*\(", expression):
        return False
    scrubbed = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "'literal'", expression)
    return re.fullmatch(r"[A-Za-z0-9_.$'\"\s()=<>!-]+", scrubbed) is not None


__all__ = ["SafetyCertificate", "enumerate_safety_points"]
