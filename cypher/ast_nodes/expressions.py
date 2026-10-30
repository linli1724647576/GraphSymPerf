from dataclasses import dataclass
from data_structures.ast import ASTNode, Variable


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


@dataclass
class Reference(Expression):
    variable: Variable

    @property
    def kind(self):
        return self.variable.kind


@dataclass
class Property(Expression):
    variable: Variable
    key: object
    value_type: str

    @property
    def kind(self):
        return self.value_type


@dataclass
class Unary(Expression):
    operator: str
    operand: Expression

    @property
    def kind(self):
        return "bool"


@dataclass
class Binary(Expression):
    left: Expression
    operator: str
    right: Expression

    @property
    def kind(self):
        return self.left.kind if self.operator in {"+", "-", "*"} else "bool"


@dataclass
class Function(Expression):
    name: str
    arguments: list
    value_type: str
    distinct: bool = False

    @property
    def kind(self):
        return self.value_type


@dataclass
class CaseExpression(Expression):
    branches: list
    otherwise: Expression

    @property
    def kind(self):
        return self.otherwise.kind


@dataclass
class ListExpression(Expression):
    items: list
    element_type: str = "int"

    @property
    def kind(self):
        return "list_" + self.element_type


@dataclass
class ListIndex(Expression):
    source: Expression
    index: Expression

    @property
    def kind(self):
        return self.source.kind.removeprefix("list_")


@dataclass
class Quantifier(Expression):
    name: str
    variable: Variable
    source: Expression
    predicate: Expression

    @property
    def kind(self):
        return "bool"


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


@dataclass
class PatternComprehension(Expression):
    pattern: ASTNode
    where: Expression
    projection: Expression

    @property
    def kind(self):
        return "list_" + self.projection.kind
