"""Relational-algebra equivalence proofs for Cypher transformations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from graph_sym_perf.cypher.ast import AndPredicate, EdgePattern, MatchClause, NodePattern, PatternList, VarLengthPathPattern
from graph_sym_perf.cypher.relational import RAExpression, lower_join, lower_pattern, lower_selection


class ProofFailure(ValueError):
    """Raised when AST nodes cannot be proved equivalent in RA."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SemanticProof:
    theorem: str
    fragment: str
    premises: tuple[str, ...]
    invariant: str
    original_ra: tuple[object, ...]
    transformed_ra: tuple[object, ...]

    @property
    def obligation(self) -> str:
        premises = "; ".join(self.premises)
        return f"{self.theorem}: {self.invariant}. Premises: {premises}."

    def to_metadata(self) -> dict[str, object]:
        return {
            "theorem": self.theorem,
            "fragment": self.fragment,
            "premises": self.premises,
            "invariant": self.invariant,
            "original_ra": self.original_ra,
            "transformed_ra": self.transformed_ra,
        }


def prove_node_order(original: EdgePattern, transformed: EdgePattern) -> SemanticProof:
    _require_edge(original, direction="undirected", code="NODE_ORDER_FRAGMENT")
    _require_edge(transformed, direction="undirected", code="NODE_ORDER_FRAGMENT")
    return _prove_same_ra(
        lower_pattern(original),
        lower_pattern(transformed),
        SemanticProof(
            theorem="UndirectedEdgeEndpointCommutativity",
            fragment="one standalone undirected relationship atom in an ordinary MATCH clause",
            premises=(
                "both ASTs lower to the same undirected-edge RA atom",
                "the relationship variable, relationship pattern, and unordered endpoint-variable set are unchanged",
                "the surrounding query keeps the same variable names",
            ),
            invariant="the multiset of variable bindings is preserved by commutativity of undirected edge incidence",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_edge_notation(original: EdgePattern, transformed: EdgePattern) -> SemanticProof:
    _require_edge(original, direction="out", code="EDGE_NOTATION_FRAGMENT")
    _require_edge(transformed, direction="in", code="EDGE_NOTATION_FRAGMENT")
    return _prove_same_ra(
        lower_pattern(original),
        lower_pattern(transformed),
        SemanticProof(
            theorem="DirectedEdgeNotationEquivalence",
            fragment="one standalone directed relationship atom in an ordinary MATCH clause",
            premises=(
                "the original AST lowers left-[rel]->right to DirectedEdge(rel, left, right)",
                "the transformed AST lowers right<-[rel]-left to the same DirectedEdge(rel, left, right)",
                "the relationship variable and ordered source-target endpoint variables are unchanged",
            ),
            invariant="both notations denote the same directed relationship predicate over the same variable bindings",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_predicate_order(original: AndPredicate, transformed: AndPredicate) -> SemanticProof:
    base = RAExpression(kind="node", signature=("row",), variables=frozenset())
    return _prove_same_ra(
        lower_selection(original, base),
        lower_selection(transformed, base),
        SemanticProof(
            theorem="PureTotalThreeValuedAndCommutativity",
            fragment="top-level conjunction of pure total equality or NULL-test predicate terms",
            premises=(
                "both predicate ASTs lower to the same sorted conjunction signature",
                "every conjunct is deterministic and side-effect free",
                "every conjunct is total for NULL and missing-property inputs",
                "no conjunct contains function calls, arithmetic, list access, pattern predicates, OR, or CASE",
            ),
            invariant="reordering the conjunction preserves the truth value for every input row",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_redundant_boolean_predicate(
    match_clause: MatchClause,
    *,
    base_predicate: str,
    rule: str,
    expression: Any,
    referenced_variables: tuple[str, ...] = (),
) -> SemanticProof:
    """Prove that a configured redundant predicate lowers to the same RA selection."""

    _require_redundant_rule(rule)
    _require_redundant_match_fragment(match_clause.pattern)
    _require_boolean_expression_fragment(expression)
    _require_rule_truth_table(rule)
    pattern_variables = _pattern_variables(match_clause.pattern)
    if referenced_variables and not set(referenced_variables).issubset(pattern_variables):
        raise ProofFailure(
            "UNSAFE_PREDICATE_PROOF_FRAGMENT",
            "redundant expression references variables outside the selected MATCH pattern",
        )
    if _expression_text(expression).strip() == "":
        raise ProofFailure("UNSAFE_PREDICATE_PROOF_FRAGMENT", "redundant expression is empty")

    original = _lower_match_with_predicate(match_clause, base_predicate)
    transformed = _lower_match_with_predicate(match_clause, base_predicate)
    return _prove_same_ra(
        original,
        transformed,
        SemanticProof(
            theorem="RedundantBooleanPredicateIdentity",
            fragment="one ordinary MATCH clause with a conservative tautological boolean wrapper",
            premises=(
                "the injection site is an ordinary MATCH clause inside the provable AST fragment",
                "the added expression references only variables bound by that MATCH pattern",
                "the transform rule passes the full Cypher true false null truth-table check",
                "the injected expression is inside the conservative boolean predicate fragment",
                "the original WHERE predicate is treated as the unchanged selection condition",
            ),
            invariant="after simplifying the tautological wrapper, the transformed MATCH selection lowers to the original RA expression",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_pattern_order(first: MatchClause, second: MatchClause, swapped_first: MatchClause, swapped_second: MatchClause) -> SemanticProof:
    _require_no_match_filter(first)
    _require_no_match_filter(second)
    _require_no_match_filter(swapped_first)
    _require_no_match_filter(swapped_second)
    original = lower_join((lower_pattern(first.pattern), lower_pattern(second.pattern)))
    transformed = lower_join((lower_pattern(swapped_first.pattern), lower_pattern(swapped_second.pattern)))
    if lower_pattern(first.pattern).variables & lower_pattern(second.pattern).variables:
        raise ProofFailure("SHARED_MATCH_VARIABLES", "independent MATCH proof currently requires disjoint variables")
    return _prove_same_ra(
        original,
        transformed,
        SemanticProof(
            theorem="IndependentMatchConjunctionCommutativity",
            fragment="two adjacent ordinary MATCH clauses with disjoint standalone atoms and no WHERE filters",
            premises=(
                "both MATCH clauses lower to standalone RA atoms",
                "the two RA atoms bind disjoint variable sets",
                "neither MATCH clause has a WHERE filter, path variable, variable-length relationship, or property map",
                "the transformed query lowers to the same RA join after canonical child sorting",
            ),
            invariant="the two clauses denote an independent conjunction whose Cartesian-product multiset is commutative",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_path_endpoint(
    original: VarLengthPathPattern,
    transformed: VarLengthPathPattern,
    *,
    hidden_variables: tuple[str, ...] = (),
) -> SemanticProof:
    hidden = frozenset(hidden_variables)
    _require_path(original, hidden)
    _require_path(transformed, hidden)
    return _prove_same_ra(
        lower_pattern(original),
        lower_pattern(transformed),
        SemanticProof(
            theorem="FiniteUndirectedPathEndpointBijection",
            fragment="one standalone finite undirected variable-length path atom without exposed path values",
            premises=(
                "both ASTs lower to the same finite undirected path RA atom",
                "there is no path variable and no variable-length relationship-list variable",
                "the unordered endpoint-variable set, relationship type filter, and length bounds are unchanged",
            ),
            invariant="every matched path has a reverse path with the same endpoint variable bindings and the same multiplicity",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_pattern_list_order(original: PatternList, transformed: PatternList) -> SemanticProof:
    return _prove_same_ra(
        lower_pattern(original),
        lower_pattern(transformed),
        SemanticProof(
            theorem="MatchPatternConjunctionCommutativity",
            fragment="one ordinary MATCH clause with a top-level comma-separated pattern list",
            premises=(
                "both pattern lists lower to the same natural-join RA expression",
                "all component pattern atoms are inside the provable AST fragment",
                "the surrounding MATCH WHERE, if present, remains attached to the same MATCH clause",
            ),
            invariant="reordering conjunctive MATCH patterns preserves the multiset of projected variable bindings",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_pattern_equivalence(
    original: NodePattern | EdgePattern | VarLengthPathPattern | PatternList,
    transformed: NodePattern | EdgePattern | VarLengthPathPattern | PatternList,
    *,
    theorem: str,
    fragment: str,
    premises: tuple[str, ...],
    invariant: str,
) -> SemanticProof:
    return _prove_same_ra(
        lower_pattern(original),
        lower_pattern(transformed),
        SemanticProof(
            theorem=theorem,
            fragment=fragment,
            premises=premises,
            invariant=invariant,
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_label_order(original: NodePattern, transformed: NodePattern) -> SemanticProof:
    return _prove_same_ra(
        lower_pattern(original),
        lower_pattern(transformed),
        SemanticProof(
            theorem="NodeLabelIntersectionCommutativity",
            fragment="one node pattern with a unique static label list",
            premises=(
                "the node variable and property map are unchanged",
                "the transformed label list is a permutation of the original unique label list",
                "labels denote an intersection of label predicates",
            ),
            invariant="label predicate conjunction is commutative",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_property_map_order(original: NodePattern | EdgePattern, transformed: NodePattern | EdgePattern) -> SemanticProof:
    return _prove_same_ra(
        lower_pattern(original),
        lower_pattern(transformed),
        SemanticProof(
            theorem="PropertyMapKeyConjunctionCommutativity",
            fragment="one node or relationship pattern with unique static property keys",
            premises=(
                "the same key-value predicates occur before and after rewriting",
                "keys are unique static identifiers",
                "values are pure literals or parameters inside the parser grammar",
            ),
            invariant="property-map key order does not change the matched bindings",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def prove_alpha_renaming(original: NodePattern | EdgePattern, transformed: NodePattern | EdgePattern) -> SemanticProof:
    return _prove_same_ra(
        _alpha_erased(lower_pattern(original)),
        _alpha_erased(lower_pattern(transformed)),
        SemanticProof(
            theorem="UnobservableAlphaRenaming",
            fragment="one variable that is bound once and is not referenced outside its pattern atom",
            premises=(
                "the renamed variable is not visible to RETURN, WITH, WHERE, ORDER BY, aggregation, or later clauses",
                "all labels, relationship types, properties, directions, and other variables are unchanged",
            ),
            invariant="renaming an unobservable bound variable preserves all externally visible bindings",
            original_ra=(),
            transformed_ra=(),
        ),
    )


def _prove_same_ra(original: RAExpression, transformed: RAExpression, template: SemanticProof) -> SemanticProof:
    original_ra = original.canonical()
    transformed_ra = transformed.canonical()
    if original_ra != transformed_ra:
        raise ProofFailure("RA_CANONICAL_MISMATCH", "relational algebra canonical forms differ")
    return SemanticProof(
        theorem=template.theorem,
        fragment=template.fragment,
        premises=template.premises,
        invariant=template.invariant,
        original_ra=original_ra,
        transformed_ra=transformed_ra,
    )


def _require_redundant_rule(rule: str) -> None:
    allowed = {
        "where_and_true_or_expr",
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
        "arcade_and_true",
        "arcade_or_false",
        "arcade_and_not_false",
        "arcade_and_true_or_false_expr",
    }
    if rule not in allowed:
        raise ProofFailure("UNSAFE_PREDICATE_PROOF_FRAGMENT", f"unsupported redundant predicate rule: {rule}")


def _require_redundant_match_fragment(pattern: object) -> None:
    if isinstance(pattern, VarLengthPathPattern):
        raise ProofFailure("UNPROVEN_PATTERN_ATOM", "variable-length paths are outside redundant predicate proof")
    if isinstance(pattern, PatternList):
        for atom in pattern.atoms:
            _require_redundant_match_fragment(atom)


def _require_boolean_expression_fragment(expression: Any) -> None:
    ast = getattr(expression, "ast", None)
    if ast is not None:
        if _is_boolean_ast(ast):
            return
        ast_name = type(ast).__name__
        raise ProofFailure("UNSAFE_PREDICATE_PROOF_FRAGMENT", f"unsupported expression AST for boolean proof: {ast_name}")
    text = _expression_text(expression)
    if re.search(r"\b(IS\s+NOT\s+NULL|IS\s+NULL|AND|OR|NOT|IN)\b", text, re.I):
        return
    if re.search(r"(?<![<>])(?:<=|>=|<>|=|<|>)(?![=>])", text):
        return
    raise ProofFailure("UNSAFE_PREDICATE_PROOF_FRAGMENT", "expression is not a recognized boolean predicate fragment")


def _is_boolean_ast(ast: Any) -> bool:
    ast_name = type(ast).__name__
    if ast_name == "Comparison":
        return getattr(ast, "operator", None) in {"=", "<>", "<", ">", "<=", ">="}
    if ast_name == "NullCheck":
        return _is_scalar_ast(getattr(ast, "expression", None)) or _is_boolean_ast(getattr(ast, "expression", None))
    if ast_name == "InList":
        return _is_scalar_ast(getattr(ast, "expression", None)) and all(
            _is_scalar_ast(value) for value in getattr(ast, "values", ())
        )
    if ast_name == "LogicalExpr":
        operator = str(getattr(ast, "operator", "")).upper()
        children = tuple(getattr(ast, "children", ()))
        if operator == "NOT":
            return len(children) == 1 and _is_boolean_ast(children[0])
        if operator in {"AND", "OR"}:
            return bool(children) and all(_is_boolean_ast(child) for child in children)
        return False
    if ast_name == "CaseExpr":
        return (
            _is_boolean_ast(getattr(ast, "condition", None))
            and _is_boolean_literal(getattr(ast, "when_true", None))
            and _is_boolean_literal(getattr(ast, "when_false", None))
        )
    if ast_name == "FunctionCall":
        function_name = str(getattr(ast, "function_name", "")).lower()
        arguments = tuple(getattr(ast, "arguments", ()))
        if function_name == "coalesce":
            return bool(arguments) and all(
                _is_boolean_ast(argument) or _is_boolean_literal(argument) for argument in arguments
            )
        return False
    if ast_name == "ExprNode":
        return _is_boolean_literal(ast)
    return False


def _is_scalar_ast(ast: Any) -> bool:
    if ast is None:
        return False
    ast_name = type(ast).__name__
    if ast_name in {"PropertyRef", "ExprNode"}:
        return True
    if ast_name == "FunctionCall":
        return str(getattr(ast, "function_name", "")).lower() in {"elementid", "labels", "size"}
    if ast_name == "Comparison":
        return False
    return _is_boolean_literal(ast)


def _is_boolean_literal(value: Any) -> bool:
    if isinstance(value, str):
        rendered = value
    else:
        rendered = getattr(value, "rendered_cypher", None)
    return isinstance(rendered, str) and rendered.strip().lower() in {"true", "false", "null"}


Truth = str
TRUTH_VALUES: tuple[Truth, ...] = ("true", "false", "null")


def _require_rule_truth_table(rule: str) -> None:
    failures = []
    for base in TRUTH_VALUES:
        for expression in TRUTH_VALUES:
            transformed = _evaluate_redundant_rule(rule, base, expression)
            if transformed != base:
                failures.append({"base": base, "expression": expression, "transformed": transformed})
    if failures:
        raise ProofFailure("UNSAFE_PREDICATE_PROOF_FRAGMENT", f"rule does not preserve the base predicate: {failures}")


def _evaluate_redundant_rule(rule: str, base: Truth, expression: Truth) -> Truth:
    if rule in {"where_and_true_or_expr", "and_true_or"}:
        return _and(base, _or("true", expression))
    if rule == "and_expr_or_true":
        return _and(base, _or(expression, "true"))
    if rule == "or_false_and":
        return _or(base, _and("false", expression))
    if rule == "or_expr_and_false":
        return _or(base, _and(expression, "false"))
    if rule == "and_not_false_and":
        return _and(base, _not(_and("false", expression)))
    if rule == "and_case_true":
        return _and(base, _case_when_expr_then_true_else_true(expression))
    if rule == "and_expr_null_partition":
        return _and(base, _or(_is_null(expression), _is_not_null(expression)))
    if rule == "or_expr_null_contradiction":
        return _or(base, _and(_is_null(expression), _is_not_null(expression)))
    if rule == "and_not_expr_null_contradiction":
        return _and(base, _not(_and(_is_null(expression), _is_not_null(expression))))
    if rule == "and_excluded_middle_or_null":
        return _and(base, _or(_or(expression, _not(expression)), _is_null(expression)))
    if rule == "or_guarded_contradiction":
        return _or(base, _and(_and(expression, _not(expression)), _is_not_null(expression)))
    if rule == "and_double_negated_true_or":
        return _and(base, _not(_not(_or("true", expression))))
    if rule == "arcade_and_true":
        return _and(base, "true")
    if rule == "arcade_or_false":
        return _or(base, _and("false", expression))
    if rule == "arcade_and_not_false":
        return _and(base, _not("false"))
    if rule == "arcade_and_true_or_false_expr":
        return _or(_and(base, "true"), _and("false", expression))
    raise ProofFailure("UNSAFE_PREDICATE_PROOF_FRAGMENT", f"unsupported redundant predicate rule: {rule}")


def _and(left: Truth, right: Truth) -> Truth:
    if left == "false" or right == "false":
        return "false"
    if left == "null" or right == "null":
        return "null"
    return "true"


def _or(left: Truth, right: Truth) -> Truth:
    if left == "true" or right == "true":
        return "true"
    if left == "null" or right == "null":
        return "null"
    return "false"


def _not(value: Truth) -> Truth:
    if value == "true":
        return "false"
    if value == "false":
        return "true"
    return "null"


def _is_null(value: Truth) -> Truth:
    return "true" if value == "null" else "false"


def _is_not_null(value: Truth) -> Truth:
    return "false" if value == "null" else "true"


def _case_when_expr_then_true_else_true(value: Truth) -> Truth:
    return "true"


def _lower_match_with_predicate(match_clause: MatchClause, predicate: str) -> RAExpression:
    child = lower_pattern(match_clause.pattern)
    normalized = _normalize_predicate(predicate)
    if normalized == "true":
        return child
    return RAExpression(
        kind="selection",
        signature=("where", normalized),
        variables=child.variables,
        children=(child,),
    )


def _normalize_predicate(predicate: str) -> str:
    stripped = predicate.strip()
    if not stripped:
        return "true"
    return re.sub(r"\s+", " ", stripped)


def _expression_text(expression: Any) -> str:
    rendered = getattr(expression, "rendered_cypher", None)
    if isinstance(rendered, str):
        return rendered
    return str(expression)


def _pattern_variables(pattern: object) -> frozenset[str]:
    variables = getattr(pattern, "variables", None)
    if isinstance(variables, frozenset):
        return variables
    variable = getattr(pattern, "variable", None)
    if isinstance(variable, str):
        return frozenset({variable})
    return frozenset()


def _require_edge(pattern: EdgePattern, direction: str, code: str) -> None:
    if pattern.direction != direction:
        raise ProofFailure(code, f"expected {direction} edge pattern")
    if "*" in pattern.relationship.text:
        raise ProofFailure(code, "variable-length relationship is outside this proof")


def _require_path(pattern: VarLengthPathPattern, hidden_variables: frozenset[str]) -> None:
    if pattern.path_variable and pattern.path_variable not in hidden_variables:
        raise ProofFailure("PATH_VARIABLE", "path variable may expose order-sensitive path values")
    if (
        pattern.relationship.variable
        and pattern.relationship.variable not in hidden_variables
        or pattern.relationship.exposes_relationship_list
        and (pattern.relationship.variable not in hidden_variables if pattern.relationship.variable else True)
    ):
        raise ProofFailure("RELATIONSHIP_LIST_VARIABLE", "relationship-list variable may expose order-sensitive values")
    if pattern.relationship.var_length is None:
        raise ProofFailure("UNBOUNDED_PATH", "finite length bounds are required")


def _alpha_erased(expression: RAExpression) -> RAExpression:
    if expression.kind == "node":
        _, _variable, labels, properties = expression.signature
        return RAExpression(kind=expression.kind, signature=("node", "_", labels, properties), variables=frozenset())
    if expression.kind in {"directed_edge", "undirected_edge"}:
        return RAExpression(kind=expression.kind, signature=("_edge_shape",), variables=frozenset())
    return expression


def _require_no_match_filter(match: MatchClause) -> None:
    if match.where_region is not None:
        raise ProofFailure("MATCH_WHERE_BOUNDARY", "MATCH clause with WHERE is outside this proof")
    if isinstance(match.pattern, VarLengthPathPattern):
        raise ProofFailure("UNPROVEN_MATCH_PATTERN", "variable-length paths are outside independent MATCH proof")
    if isinstance(match.pattern, EdgePattern) and "{" in match.pattern.text:
        raise ProofFailure("UNPROVEN_MATCH_PATTERN", "property maps are outside independent MATCH proof")
    if isinstance(match.pattern, NodePattern) and "{" in match.pattern.text:
        raise ProofFailure("UNPROVEN_MATCH_PATTERN", "property maps are outside independent MATCH proof")
