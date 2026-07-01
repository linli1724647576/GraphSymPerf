"""Small Cypher AST for the provable GraphSymPerf fragment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Direction = Literal["out", "in", "undirected"]


@dataclass(frozen=True)
class Clause:
    keyword: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class NodePattern:
    text: str
    variable: str
    labels: tuple[str, ...] = ()
    properties: tuple[tuple[str, str], ...] = ()

    def render(self) -> str:
        return self.text


@dataclass(frozen=True)
class RelationshipPattern:
    text: str
    variable: str | None
    types: tuple[str, ...] = ()
    var_length: tuple[int, int] | None = None
    exposes_relationship_list: bool = False
    properties: tuple[tuple[str, str], ...] = ()

    def render(self) -> str:
        return self.text


@dataclass(frozen=True)
class EdgePattern:
    text: str
    left: NodePattern
    relationship: RelationshipPattern
    right: NodePattern
    direction: Direction

    @property
    def variables(self) -> frozenset[str]:
        variables = {self.left.variable, self.right.variable}
        if self.relationship.variable:
            variables.add(self.relationship.variable)
        return frozenset(variables)

    def render(self) -> str:
        if self.direction == "out":
            return f"{self.left.render()}-{self.relationship.render()}->{self.right.render()}"
        if self.direction == "in":
            return f"{self.left.render()}<-{self.relationship.render()}-{self.right.render()}"
        return f"{self.left.render()}-{self.relationship.render()}-{self.right.render()}"


@dataclass(frozen=True)
class VarLengthPathPattern:
    text: str
    left: NodePattern
    relationship: RelationshipPattern
    right: NodePattern
    direction: Literal["undirected"]
    path_variable: str | None = None

    @property
    def variables(self) -> frozenset[str]:
        variables = {self.left.variable, self.right.variable}
        if self.path_variable:
            variables.add(self.path_variable)
        if self.relationship.variable:
            variables.add(self.relationship.variable)
        return frozenset(variables)

    def render(self) -> str:
        body = f"{self.left.render()}-{self.relationship.render()}-{self.right.render()}"
        return f"{self.path_variable} = {body}" if self.path_variable else body


@dataclass(frozen=True)
class PatternList:
    text: str
    atoms: tuple["PatternAtom", ...]
    separator: str = "comma"

    @property
    def variables(self) -> frozenset[str]:
        variables: set[str] = set()
        for atom in self.atoms:
            variables.update(atom.variables if hasattr(atom, "variables") else {atom.variable})
        return frozenset(variables)

    def render(self) -> str:
        if self.separator == "chain":
            if not self.atoms:
                return ""
            first = self.atoms[0]
            if not isinstance(first, EdgePattern):
                return self.text
            parts = [first.left.render()]
            for atom in self.atoms:
                if not isinstance(atom, EdgePattern):
                    return self.text
                if atom.direction == "out":
                    parts.append(f"-{atom.relationship.render()}->{atom.right.render()}")
                elif atom.direction == "in":
                    parts.append(f"<-{atom.relationship.render()}-{atom.right.render()}")
                else:
                    parts.append(f"-{atom.relationship.render()}-{atom.right.render()}")
            return "".join(parts)
        return ", ".join(atom.render() for atom in self.atoms)


PatternAtom = NodePattern | EdgePattern | VarLengthPathPattern | PatternList


@dataclass(frozen=True)
class MatchClause:
    clause: Clause
    pattern_region: Region
    pattern: PatternAtom
    where_region: Region | None = None

    def replace_pattern(self, replacement: str) -> str:
        return self.clause.text[: self.pattern_region.start] + replacement + self.clause.text[self.pattern_region.end :]


@dataclass(frozen=True)
class PredicateTerm:
    text: str
    operator: str
    operands: tuple[str, ...]

    def signature(self) -> tuple[str, str, tuple[str, ...]]:
        return self.operator, self.text.upper() if self.operator == "literal" else "", self.operands


@dataclass(frozen=True)
class NullPredicate(PredicateTerm):
    pass


@dataclass(frozen=True)
class LiteralPredicate(PredicateTerm):
    pass


@dataclass(frozen=True)
class AndPredicate:
    text: str
    terms: tuple[PredicateTerm, ...]

    def render_reversed(self) -> str:
        return " AND ".join(term.text for term in reversed(self.terms))

    def multiset_signature(self) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        return tuple(sorted(term.signature() for term in self.terms))


@dataclass(frozen=True)
class CypherQuery:
    text: str
    clauses: tuple[Clause, ...]

    def ordinary_match_clauses(self) -> tuple[Clause, ...]:
        return tuple(clause for clause in self.clauses if clause.keyword == "MATCH")
