from dataclasses import dataclass
from .base import ASTNode


@dataclass
class Query(ASTNode):
    clauses: list

    def output(self):
        return [item.alias for item in self.clauses[-1].items]

    def to_cypher(self):
        return "\n".join(clause.to_cypher() for clause in self.clauses)


@dataclass
class Union(ASTNode):
    branches: list
    all: bool = True

    def output(self):
        return self.branches[0].output()

    def to_cypher(self):
        separator = "\nUNION ALL\n" if self.all else "\nUNION\n"
        # Conditional branches need grouping when used as UNION operands.
        # 条件查询作为 UNION 分支时需要保留分组边界。
        rendered = ["{\n" + branch.to_cypher() + "\n}" if isinstance(branch, ConditionalQuery)
                    else branch.to_cypher() for branch in self.branches]
        return separator.join(rendered)


@dataclass
class ConditionalQuery(ASTNode):
    branches: list
    otherwise: ASTNode

    def output(self):
        return self.otherwise.output()

    def to_cypher(self):
        text = "\n".join(f"WHEN {condition.to_cypher()} THEN {{\n{query.to_cypher()}\n}}" for condition, query in self.branches)
        return text + "\nELSE {\n" + self.otherwise.to_cypher() + "\n}"
