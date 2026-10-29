from dataclasses import dataclass
from .base import ASTNode, Variable, identifier, literal


class Expression(ASTNode):
    @property
    def kind(self):
        raise NotImplementedError


@dataclass
class Literal(Expression):
    # Typed nulls retain the expected expression type during generation.
    # NULL 也携带预期表达式类型，便于生成时保持类型一致。
    value: object
    value_type: str = "int"

    @property
    def kind(self):
        return self.value_type

    def to_cypher(self):
        return literal(self.value)


@dataclass
class Reference(Expression):
    variable: Variable

    @property
    def kind(self):
        return self.variable.kind

    def to_cypher(self):
        return self.variable.name


@dataclass
class Property(Expression):
    variable: Variable
    key: object
    value_type: str

    @property
    def kind(self):
        return self.value_type

    def to_cypher(self):
        return f"{self.variable.name}.{identifier(self.key)}"


@dataclass
class Unary(Expression):
    operator: str
    operand: Expression

    @property
    def kind(self):
        return "bool"

    def to_cypher(self):
        expression = self.operand.to_cypher()
        return f"({expression} {self.operator})" if self.operator.startswith("IS ") else f"(NOT {expression})"


@dataclass
class Binary(Expression):
    left: Expression
    operator: str
    right: Expression

    @property
    def kind(self):
        return self.left.kind if self.operator in {"+", "-", "*"} else "bool"

    def to_cypher(self):
        return f"({self.left.to_cypher()} {self.operator} {self.right.to_cypher()})"


@dataclass
class Function(Expression):
    name: str
    arguments: list
    value_type: str
    distinct: bool = False

    @property
    def kind(self):
        return self.value_type

    def to_cypher(self):
        args = ", ".join(value.to_cypher() for value in self.arguments)
        return f"{self.name}({'DISTINCT ' if self.distinct else ''}{args})"


@dataclass
class CaseExpression(Expression):
    branches: list
    otherwise: Expression

    @property
    def kind(self):
        return self.otherwise.kind

    def to_cypher(self):
        branches = " ".join(f"WHEN {condition.to_cypher()} THEN {value.to_cypher()}" for condition, value in self.branches)
        return f"CASE {branches} ELSE {self.otherwise.to_cypher()} END"


@dataclass
class ListExpression(Expression):
    items: list
    element_type: str = "int"

    @property
    def kind(self):
        return "list_" + self.element_type

    def to_cypher(self):
        return "[" + ", ".join(item.to_cypher() for item in self.items) + "]"


@dataclass
class ListIndex(Expression):
    source: Expression
    index: Expression

    @property
    def kind(self):
        return self.source.kind.removeprefix("list_")

    def to_cypher(self):
        return f"({self.source.to_cypher()})[{self.index.to_cypher()}]"


@dataclass
class Quantifier(Expression):
    name: str
    variable: Variable
    source: Expression
    predicate: Expression

    @property
    def kind(self):
        return "bool"

    def to_cypher(self):
        return f"{self.name}({self.variable.name} IN {self.source.to_cypher()} WHERE {self.predicate.to_cypher()})"


@dataclass
class SubqueryExpression(Expression):
    mode: str
    query: ASTNode

    @property
    def kind(self):
        if self.mode == "EXISTS":
            return "bool"
        if self.mode == "COUNT":
            return "int"
        return "list_" + self.query.output()[0].kind

    def to_cypher(self):
        return f"{self.mode} {{\n{self.query.to_cypher()}\n}}"
