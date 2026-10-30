from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Symbol:
    # A terminal stays symbolic until graph construction assigns its name or value.
    # 标签、常量等终端先保留为符号，由构图阶段分配具体名称或取值。
    name: str
    kind: str


@dataclass(frozen=True)
class Variable:
    # Label provenance supports anchor selection; a variable is not one concrete graph node.
    # 标签来源用于选择候选锚点；查询变量并不固定对应一个图节点。
    name: str
    kind: str
    labels: tuple = ()
    nullable: bool = False


class ASTNode:
    """Marker for tree traversal; language modules own query semantics and rendering."""


def walk(value, path=()):
    # Field names and list indices locate a subtree without parent pointers or node IDs.
    # 用字段名和列表下标定位子树，无需维护父指针或额外节点编号。
    if isinstance(value, ASTNode):
        yield path, value
        for field in fields(value):
            yield from walk(getattr(value, field.name), path + (field.name,))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from walk(item, path + (index,))


def replace_at(root, path, replacement):
    if not path:
        return replacement
    parent = root
    for key in path[:-1]:
        parent = parent[key] if isinstance(key, int) else getattr(parent, key)
    key = path[-1]
    if isinstance(key, int):
        if isinstance(parent, tuple):
            # CASE branches contain tuples, so replace the containing tuple rather than a slot.
            # CASE 分支包含元组，需要重建该元组，不能直接给元组元素赋值。
            values = list(parent)
            values[key] = replacement
            return replace_at(root, path[:-1], tuple(values))
        parent[key] = replacement
    else:
        setattr(parent, key, replacement)
    return root


def at(root, path):
    for key in path:
        root = root[key] if isinstance(key, int) else getattr(root, key)
    return root
