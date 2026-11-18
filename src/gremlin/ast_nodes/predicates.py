from dataclasses import dataclass
from data_structures.ast import ASTNode


@dataclass
class Value(ASTNode):
    value: object
    kind: str


@dataclass
class Predicate(ASTNode):
    name: str
    args: list
