from dataclasses import dataclass, field
from data_structures.ast import ASTNode, Variable
from .expressions import Expression


@dataclass
class Match(ASTNode):
    paths: list
    optional: bool = False
    where: Expression | None = None


@dataclass
class ProjectionItem(ASTNode):
    expression: Expression
    alias: Variable


@dataclass
class Projection(ASTNode):
    keyword: str
    items: list
    distinct: bool = False
    where: Expression | None = None
    order: list = field(default_factory=list)
    skip: Expression | None = None
    limit: Expression | None = None


@dataclass
class Call(ASTNode):
    imports: list
    query: ASTNode


@dataclass
class Unwind(ASTNode):
    expression: Expression
    variable: Variable
