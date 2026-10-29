from dataclasses import dataclass, field
from .base import ASTNode, Variable
from .expressions import Expression


@dataclass
class Match(ASTNode):
    paths: list
    optional: bool = False
    where: Expression | None = None

    def to_cypher(self):
        text = ("OPTIONAL MATCH " if self.optional else "MATCH ") + ", ".join(path.to_cypher() for path in self.paths)
        return text + ("\nWHERE " + self.where.to_cypher() if self.where else "")


@dataclass
class ProjectionItem(ASTNode):
    expression: Expression
    alias: Variable

    def to_cypher(self):
        return self.expression.to_cypher() + " AS " + self.alias.name


@dataclass
class Projection(ASTNode):
    keyword: str
    items: list
    distinct: bool = False
    where: Expression | None = None
    order: list = field(default_factory=list)
    skip: Expression | None = None
    limit: Expression | None = None

    def to_cypher(self):
        text = self.keyword + (" DISTINCT " if self.distinct else " ") + ", ".join(item.to_cypher() for item in self.items)
        if self.where:
            text += "\nWHERE " + self.where.to_cypher()
        if self.order:
            text += "\nORDER BY " + ", ".join(expr.to_cypher() + (" DESC" if descending else " ASC") for expr, descending in self.order)
        if self.skip:
            text += "\nSKIP " + self.skip.to_cypher()
        if self.limit:
            text += "\nLIMIT " + self.limit.to_cypher()
        return text


@dataclass
class Call(ASTNode):
    imports: list
    query: ASTNode

    def to_cypher(self):
        return "CALL (" + ", ".join(var.name for var in self.imports) + ") {\n" + self.query.to_cypher() + "\n}"


@dataclass
class Unwind(ASTNode):
    expression: Expression
    variable: Variable

    def to_cypher(self):
        return f"UNWIND {self.expression.to_cypher()} AS {self.variable.name}"
