import json
from data_structures.query import Query
from .ast_nodes import Traversal, Value, Predicate


def render(query, *, groovy=False):
    parameters = {}

    def emit(value, bound=False):
        if isinstance(value, Traversal):
            parts = ["g" if value.root else "__"]
            for step in value.steps:
                # values() carries a static output kind used only by scope analysis.
                args = step.args[:1] if step.name == "values" else step.args
                text = ",".join(emit(arg, step.name in {"has", "is"}) for arg in args)
                parts.append(f"{step.name}({text})")
            return ".".join(parts)
        if isinstance(value, Predicate):
            return "P." + value.name + "(" + ",".join(emit(arg, True) for arg in value.args) + ")"
        if isinstance(value, Value):
            if bound:
                name = "p" + str(len(parameters))
                parameters[name] = value.value
                return name
            literal = json.dumps(value.value, ensure_ascii=True)
            # Groovy decimal literals otherwise become BigDecimal rather than Double.
            return literal + "d" if groovy and value.kind == "float" else literal
        return json.dumps(value, ensure_ascii=True)

    return Query(emit(query), parameters, ("result",))
