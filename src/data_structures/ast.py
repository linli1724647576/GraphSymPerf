from dataclasses import dataclass, fields


class Symbol(str):
    """A label, relationship type, or property key with a fixed name and kind."""
    # The generator fixes names once; the kind lets schema and background data collect terminals.

    def __new__(cls, name, kind):
        value = super().__new__(cls, name)
        value.kind = kind
        return value

    def __getnewargs__(self):
        return str(self), self.kind

    @property
    def name(self):
        return str(self)

    def __repr__(self):
        return f"Symbol({str(self)!r}, {self.kind!r})"


def sample_constant(rng, kind):
    # Constants share the value domain of random background properties.
    return {"int": lambda: rng.randint(-2, 4), "bool": lambda: rng.choice([True, False]),
            "string": lambda: rng.choice(["a", "b", "c"]), "float": lambda: rng.choice([0.5, 1.5, 2.5])}[kind]()


@dataclass(frozen=True)
class Variable:
    # Label provenance supports anchor selection; a variable is not one concrete graph node.
    name: str
    kind: str
    labels: tuple = ()
    nullable: bool = False


class ASTNode:
    """Marker for tree traversal; language modules own query semantics and rendering."""


def walk(value, path=()):
    # Field names and list indices locate a subtree without parent pointers or node IDs.
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
