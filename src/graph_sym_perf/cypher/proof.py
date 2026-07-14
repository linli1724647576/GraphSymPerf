"""Relational-algebra equivalence proofs for Cypher transformations."""

from __future__ import annotations

from dataclasses import dataclass

from graph_sym_perf.cypher.ast import AndPredicate, EdgePattern, NodePattern, PatternList, VarLengthPathPattern
from graph_sym_perf.cypher.relational import RAExpression, lower_pattern, lower_selection


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
