from dataclasses import dataclass, field
from .base import ASTNode, Variable, identifier


@dataclass
class NodePattern(ASTNode):
    variable: Variable
    labels: list = field(default_factory=list)
    properties: list = field(default_factory=list)

    def to_cypher(self):
        labels = "".join(":" + identifier(label) for label in self.labels)
        props = ", ".join(identifier(key) + ": " + value.to_cypher() for key, value in self.properties)
        return f"({self.variable.name}{labels}" + (" {" + props + "}" if props else "") + ")"


@dataclass
class RelationshipPattern(ASTNode):
    types: list
    direction: str = "out"
    minimum: int = 1
    maximum: int = 1
    variable: Variable | None = None

    def to_cypher(self):
        name = self.variable.name if self.variable else ""
        types = ":" + "|".join(identifier(value) for value in self.types) if self.types else ""
        length = f"*{self.minimum}..{self.maximum}" if (self.minimum, self.maximum) != (1, 1) else ""
        middle = f"[{name}{types}{length}]"
        return {"out": "-" + middle + "->", "in": "<-" + middle + "-", "both": "-" + middle + "-"}[self.direction]


@dataclass
class PathPattern(ASTNode):
    nodes: list
    relationships: list
    variable: Variable | None = None

    def to_cypher(self):
        text = self.nodes[0].to_cypher()
        for relationship, node in zip(self.relationships, self.nodes[1:]):
            text += relationship.to_cypher() + node.to_cypher()
        return (self.variable.name + " = " if self.variable else "") + text

    def reverse(self):
        # Reverse written positions and arrowheads, preserving variables and stored edges.
        # 反转书写位置和箭头方向，保留变量身份，不修改图中实际存储的关系。
        self.nodes.reverse()
        self.relationships.reverse()
        for relationship in self.relationships:
            relationship.direction = {"out": "in", "in": "out", "both": "both"}[relationship.direction]

    def variables(self):
        values = [node.variable for node in self.nodes]
        values += [edge.variable for edge in self.relationships if edge.variable]
        return values + ([self.variable] if self.variable else [])
