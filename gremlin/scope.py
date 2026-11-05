from dataclasses import dataclass, field, replace
from data_structures.ast import Variable
from .ast_nodes import Traversal, Value, Predicate


@dataclass
class Scope:
    current: Variable = field(default_factory=lambda: Variable("it", "unknown"))
    bindings: dict = field(default_factory=dict)
    constant: bool = False


@dataclass
class Site:
    path: tuple
    node: object
    scope: Scope
    output: Scope
    terminal: bool


def analyze(query, scope=None, path=(), sites=None):
    scope = scope or Scope()
    for index, step in enumerate(query.steps):
        location = path + ("steps", index)
        before = scope
        # match() patterns are declarative; they are typed by match_scope, not as nested sites.
        # match() 模式是声明式的，由 match_scope 推断类型，不作为嵌套变异位置。
        children = [] if step.name == "match" else [analyze(arg, before, location + ("args", i), sites)
                                                    for i, arg in enumerate(step.args) if isinstance(arg, Traversal)]
        name, args = step.name, step.args
        if name == "V":
            assert query.root and index == 0, "Only root traversals may restart at V"
            scope = Scope(Variable("it", "node"))
        elif name == "hasLabel":
            assert scope.current.kind in {"node", "relationship"}
            scope = replace(scope, current=replace(scope.current, labels=tuple(args)))
        elif name == "has":
            assert scope.current.kind in {"node", "relationship"}
        elif name == "as":
            scope = replace(scope, bindings={**scope.bindings, args[0]: scope.current})
        elif name == "select":
            assert args[0] in scope.bindings, f"Unbound Gremlin label: {args[0]}"
            scope = replace(scope, current=scope.bindings[args[0]], constant=False)
        elif name in {"out", "in", "both", "outE"}:
            assert scope.current.kind == "node"
            scope = replace(scope, current=Variable("it", "relationship" if name == "outE" else "node"), constant=False)
        elif name in {"inV", "outV"}:
            assert scope.current.kind == "relationship"
            scope = replace(scope, current=Variable("it", "node"), constant=False)
        elif name == "values":
            assert scope.current.kind in {"node", "relationship"}
            scope = replace(scope, current=Variable("it", args[1]), constant=False)
        elif name == "constant":
            scope = replace(scope, current=Variable("it", args[0].kind), constant=True)
        elif name in {"count", "fold"}:
            # Reductions replace traversers; old label bindings must not survive them.
            # 归约会替换遍历对象，不能继续使用归约前的标签绑定。
            if name == "fold":
                assert scope.constant and scope.current.kind in {"int", "float", "bool", "string"}
            scope = Scope(Variable("it", "int" if name == "count" else "list_" + scope.current.kind))
        elif name == "map":
            scope = replace(scope, current=children[0].current, constant=children[0].constant)
        elif name in {"union", "coalesce", "choose"}:
            branches = children[1:] if name == "choose" else children
            assert len({branch.current.kind for branch in branches}) == 1, "Branch output kinds differ"
            labels = tuple(label for label in branches[0].current.labels
                           if all(label in branch.current.labels for branch in branches))
            bindings = {key: value for key, value in before.bindings.items()
                        if all(branch.bindings.get(key) == value for branch in branches)}
            scope = Scope(replace(branches[0].current, labels=labels), bindings)
        elif name == "match":
            scope = match_scope(args, scope)
        elif name == "is":
            value = args[0].args[0] if isinstance(args[0], Predicate) else args[0]
            assert isinstance(value, Value) and value.kind == scope.current.kind
        elif name not in {"identity", "filter", "not", "and", "or", "limit"}:
            raise ValueError(f"Unsupported Gremlin step: {name}")
        # Predicate children read the outer bindings but never export their local labels.
        # 谓词子遍历可读外层绑定，但不向外导出自己的局部标签。
        if sites is not None:
            sites.append(Site(location, step, before, scope, query.root and index == len(query.steps)-1))
    return scope


def match_scope(patterns, scope):
    """Bind every match() label; the step emits a map of these bindings."""
    starts = [p.steps[0].args[0] for p in patterns]
    ends = {step.args[0] for p in patterns for step in p.steps[1:] if step.name == "as"}
    # Like TinkerPop, the incoming object binds the one start label that no pattern produces.
    # 与 TinkerPop 一致，输入对象绑定到唯一一个不由其他模式产生的起始标签。
    unbound = {label for label in starts if label not in ends and label not in scope.bindings}
    assert len(unbound) <= 1, "match() needs one computed start label"
    bindings = {**scope.bindings, **{label: scope.current for label in unbound}}
    pending = list(patterns)
    while pending:
        ready = [p for p in pending if p.steps[0].args[0] in bindings]
        assert ready, "Unsolvable match() pattern"
        for pattern in ready:
            pending.remove(pattern)
            current = bindings[pattern.steps[0].args[0]]
            for step in pattern.steps[1:]:
                if step.name == "as":
                    bindings[step.args[0]] = current
                elif step.name == "hasLabel":
                    current = replace(current, labels=tuple(step.args))
                elif step.name in {"out", "in", "both", "V"}:
                    current = Variable("it", "node")
                elif step.name != "has":
                    raise ValueError(f"Unsupported step inside match(): {step.name}")
    return Scope(Variable("it", "map"), bindings)
