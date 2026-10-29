from dataclasses import dataclass, field
from .case import Inapplicable


@dataclass
class Graph:
    nodes: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    indexes: list = field(default_factory=list)

    def node(self, labels=(), **properties):
        uid = len(self.nodes)
        self.nodes.append({"labels": list(dict.fromkeys(labels)), "properties": {**properties, "uid": uid}})
        return uid

    def edge(self, start, end, relation_type, **properties):
        self.relationships.append({"start": start, "end": end, "type": relation_type, "properties": properties})

    def candidates(self, variable, create=False):
        # Labels identify candidate anchors, not the exact rows produced by outer filters.
        # 标签确定候选锚点范围，不代表已经求出了外层过滤后的实际输入行。
        if variable.nullable or not variable.labels:
            raise Inapplicable("Correlated anchors require a traceable, non-null node source")
        required = set(variable.labels)
        values = [i for i, node in enumerate(self.nodes) if required <= set(node["labels"])]
        if not values and create:
            values = [self.node(variable.labels, keep=True, value=1)]
        return values

    def index(self, label, key):
        index = [label, key]
        if index not in self.indexes:
            self.indexes.append(index)


def shares(total, count):
    # Split one case-wide budget; the remainder gives some regions one extra unit.
    # 分配整个用例的总预算，余数使部分区域多获得一个扩张单元。
    quotient, remainder = divmod(total, count)
    return [quotient + (i < remainder) for i in range(count)]
