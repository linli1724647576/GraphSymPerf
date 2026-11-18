from dataclasses import dataclass, field
from data_structures.ast import ASTNode


@dataclass
class Traversal(ASTNode):
    steps: list = field(default_factory=list)
    root: bool = False
