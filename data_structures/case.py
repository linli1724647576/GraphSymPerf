from dataclasses import dataclass, field, fields, is_dataclass
from ast_nodes.base import Symbol


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
    # Pattern-local source data and the prepared mutation share one binding map.
    # Pattern 的局部构造信息与预先生成的变异内容共用同一份绑定。
    payload: dict = field(default_factory=dict)
    bindings: dict = field(default_factory=dict)


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


def bind(value, bindings):
    # Rebuild values recursively, replacing terminals without changing AST operators.
    # 递归替换符号终端并重建对象，不改变 AST 中的算子结构。
    if isinstance(value, Symbol):
        return bindings[value.name]
    if is_dataclass(value):
        return type(value)(**{item.name: bind(getattr(value, item.name), bindings) for item in fields(value)})
    if isinstance(value, list):
        return [bind(item, bindings) for item in value]
    if isinstance(value, tuple):
        return tuple(bind(item, bindings) for item in value)
    if isinstance(value, dict):
        return {key: bind(item, bindings) for key, item in value.items()}
    return value


def instantiate(case, bindings):
    # Bind the mutation payload too, so mutation cannot choose new names or constants.
    # 同时实例化变异内容，防止变异时重新选择名称或常量。
    case.query = bind(case.query, bindings)
    case.payload = bind(case.payload, bindings)
    case.bindings = bindings
    return case
