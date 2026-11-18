from dataclasses import dataclass
from data_structures.ast import ASTNode


@dataclass
class MatchBody(ASTNode):
    patterns: list
    where: ASTNode | None = None


@dataclass
class Query(ASTNode):
    clauses: list

    def output(self):
        return [item.alias for item in self.clauses[-1].items]


@dataclass
class Union(ASTNode):
    branches: list
    all: bool = True

    def output(self):
        return self.branches[0].output()


@dataclass
class ConditionalQuery(ASTNode):
    branches: list
    otherwise: ASTNode

    def output(self):
        return self.otherwise.output()
