from dataclasses import dataclass, field
from .case import Inapplicable


@dataclass
class Graph:
    nodes: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    indexes: list = field(default_factory=list)
    # Declarations can describe empty regions independently of stored instances.
    schema: dict = field(default_factory=lambda: {"nodes": {}, "relationships": {}})

    def node(self, labels=(), **properties):
        uid = len(self.nodes)
        # Store plain strings so typed Symbol names never reach backend drivers.
        self.nodes.append({"labels": list(dict.fromkeys(map(str, labels))),
                           "properties": {**{str(k): v for k, v in properties.items()}, "uid": uid}})
        return uid

    def edge(self, start, end, relation_type, **properties):
        self.relationships.append({"start": start, "end": end, "type": str(relation_type),
                                   "properties": {str(k): v for k, v in properties.items()}})

    def candidates(self, variable, create=False):
        # Labels identify candidate anchors, not the exact rows produced by outer filters.
        if variable.nullable or not variable.labels:
            raise Inapplicable("Correlated anchors require a traceable, non-null node source")
        required = set(variable.labels)
        values = [i for i, node in enumerate(self.nodes) if required <= set(node["labels"])]
        if not values and create:
            values = [self.node(variable.labels, keep=True, value=1)]
        return values

    def index(self, label, key):
        index = [str(label), str(key)]
        if index not in self.indexes:
            self.indexes.append(index)


def shares(total, count):
    # Split one case-wide budget; the remainder gives some regions one extra unit.
    quotient, remainder = divmod(total, count)
    return [quotient + (i < remainder) for i in range(count)]
