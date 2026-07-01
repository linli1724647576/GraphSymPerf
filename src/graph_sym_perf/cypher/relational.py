"""Relational-algebra lowering for the provable Cypher AST fragment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from graph_sym_perf.cypher.ast import AndPredicate, EdgePattern, NodePattern, PatternList, PredicateTerm, VarLengthPathPattern


RAKind = Literal["node", "directed_edge", "undirected_edge", "finite_undirected_path", "selection", "join"]


@dataclass(frozen=True)
class RAExpression:
    kind: RAKind
    signature: tuple[object, ...]
    variables: frozenset[str]
    children: tuple["RAExpression", ...] = ()

    def canonical(self) -> tuple[object, ...]:
        if self.kind == "join":
            child_signatures = tuple(sorted((child.canonical() for child in self.children), key=repr))
            return self.kind, child_signatures
        if self.kind == "selection":
            child = self.children[0].canonical() if self.children else ()
            return self.kind, self.signature, child
        return self.kind, self.signature


def lower_pattern(pattern: NodePattern | EdgePattern | VarLengthPathPattern | PatternList) -> RAExpression:
    if isinstance(pattern, PatternList):
        return lower_join(tuple(lower_pattern(atom) for atom in pattern.atoms))
    if isinstance(pattern, NodePattern):
        return RAExpression(
            kind="node",
            signature=("node", pattern.variable, tuple(sorted(pattern.labels)), tuple(sorted(pattern.properties))),
            variables=frozenset({pattern.variable}),
        )
    if isinstance(pattern, EdgePattern):
        rel = _relationship_signature(pattern.relationship.text)
        if pattern.direction == "out":
            source = pattern.left.variable
            target = pattern.right.variable
            kind: RAKind = "directed_edge"
            signature = (pattern.relationship.variable, source, target, rel)
        elif pattern.direction == "in":
            source = pattern.right.variable
            target = pattern.left.variable
            kind = "directed_edge"
            signature = (pattern.relationship.variable, source, target, rel)
        else:
            endpoints = tuple(sorted((pattern.left.variable, pattern.right.variable)))
            kind = "undirected_edge"
            signature = (pattern.relationship.variable, endpoints, rel)
        return RAExpression(kind=kind, signature=signature, variables=pattern.variables)
    endpoints = tuple(sorted((pattern.left.variable, pattern.right.variable)))
    rel = _relationship_type_prefix(pattern.relationship.text)
    signature = (endpoints, pattern.relationship.var_length, rel)
    return RAExpression(
        kind="finite_undirected_path",
        signature=signature,
        variables=pattern.variables,
    )


def lower_selection(predicate: AndPredicate, child: RAExpression) -> RAExpression:
    return RAExpression(
        kind="selection",
        signature=("and", predicate.multiset_signature()),
        variables=child.variables,
        children=(child,),
    )


def lower_join(children: tuple[RAExpression, ...]) -> RAExpression:
    variables: set[str] = set()
    for child in children:
        variables.update(child.variables)
    return RAExpression(kind="join", signature=("join", len(children)), variables=frozenset(variables), children=children)


def predicate_term_signature(term: PredicateTerm) -> tuple[str, str, tuple[str, ...]]:
    return term.signature()


def _relationship_signature(raw: str) -> str:
    return re.sub(r"\s+", "", _canonical_property_map_text(raw))


def _relationship_type_prefix(raw: str) -> str:
    body = raw.strip()[1:-1].strip()
    star = body.find("*")
    prefix = body[:star].strip() if star >= 0 else body
    return re.sub(r"\s+", "", prefix)


def _canonical_property_map_text(raw: str) -> str:
    match = re.search(r"\{(?P<body>[^{}]*)\}", raw)
    if match is None:
        return raw
    entries = []
    for item in match.group("body").split(","):
        if ":" not in item:
            entries.append(item.strip())
            continue
        key, value = item.split(":", 1)
        entries.append(f"{key.strip()}:{value.strip()}")
    replacement = "{" + ",".join(sorted(entries)) + "}"
    return raw[: match.start()] + replacement + raw[match.end() :]
