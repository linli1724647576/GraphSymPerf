from dataclasses import dataclass, field, fields, is_dataclass
from data_structures.ast import Symbol


class Inapplicable(Exception):
    """The sampled context does not satisfy this construction recipe."""


@dataclass
class Site:
    path: tuple
    node: object
    variables: dict


@dataclass
class Case:
    pattern: str
    seed: int
    query: object
    target: tuple
    host_symbols: set
    # Pattern-local graph requirements and the prepared mutation; names are already final.
    # Pattern 的构图要求与预先生成的变异内容；其中名字已是最终名字。
    payload: dict = field(default_factory=dict)


def symbols(value):
    if isinstance(value, Symbol):
        yield value
    elif is_dataclass(value):
        for item in fields(value):
            yield from symbols(getattr(value, item.name))
    elif isinstance(value, dict):
        for item in value.values():
            yield from symbols(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from symbols(item)

