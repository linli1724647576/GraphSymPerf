from dataclasses import dataclass, field
from data_structures.ast import ASTNode


@dataclass
class Step(ASTNode):
    name: str
    args: list = field(default_factory=list)
