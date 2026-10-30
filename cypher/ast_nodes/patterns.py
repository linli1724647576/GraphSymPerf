from dataclasses import dataclass, field
from data_structures.ast import ASTNode, Variable


@dataclass
class NodePattern(ASTNode):
    variable: Variable
    labels: list = field(default_factory=list)
    properties: list = field(default_factory=list)


@dataclass
class RelationshipPattern(ASTNode):
    types: list
    direction: str = "out"
    minimum: int = 1
    maximum: int = 1
    variable: Variable | None = None


@dataclass
class PathPattern(ASTNode):
    nodes: list
    relationships: list
    variable: Variable | None = None


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
