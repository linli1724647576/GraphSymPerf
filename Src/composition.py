"""Typed, recursive query contexts independent of mutation-pattern identifiers.

Each operator consumes rows or a scalar. The two coercions below are shared
by every nesting depth; there is no registry of complete query combinations.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import random

from ir import (Alias, Comparison, Count, Exists, Literal, Match, Node, Property,
                Query, Variable, With, validate_query)


OPERATORS = ("exists", "count", "with")


def composition_options(value: dict, seed: int = 0) -> dict:
    """Resolve a bounded syntax choice, not a topology or population recipe."""
    if not isinstance(value, dict) or set(value) - {"contexts", "depth", "threshold", "path_bounds", "row_limit", "form", "match_state", "chain_length", "conditional_depth", "guard_kind", "branch_path", "scalar_depth", "scalar_type", "scalar_value", "scalar_contexts"}:
        raise ValueError("Unknown composition option")
    if "row_limit" in value and (type(value["row_limit"]) is not int or not 1 <= value["row_limit"] <= 64):
        raise ValueError("composition.row_limit must lie between 1 and 64")
    threshold = value.get("threshold", 0)
    if type(threshold) is not int or threshold < 0:
        raise ValueError("composition.threshold must be a nonnegative integer")
    if "contexts" in value and "depth" in value:
        raise ValueError("choose composition.contexts or composition.depth, not both")
    contexts = value.get("contexts")
    if contexts is None:
        depth = value.get("depth", 2)
        if type(depth) is not int or not 0 <= depth <= 8:
            raise ValueError("composition.depth must be between 0 and 8")
        rng = random.Random(seed)
        contexts = [rng.choice(OPERATORS) for _ in range(depth)]
    if (not isinstance(contexts, (tuple, list)) or len(contexts) > 8
            or any(context not in OPERATORS for context in contexts)):
        raise ValueError("composition.contexts must contain at most eight supported operators")
    bounds = value.get("path_bounds", (1, 3))
    if (not isinstance(bounds, (tuple, list)) or len(bounds) != 2
            or any(type(bound) is not int for bound in bounds)
            or not 1 <= bounds[0] <= bounds[1] <= 6):
        raise ValueError("composition.path_bounds must satisfy 1 <= min <= max <= 6")
    result = {"contexts": list(contexts), "threshold": threshold, "path_bounds": list(bounds)}
    if "row_limit" in value:
        result["row_limit"] = value["row_limit"]
    for key in ("form", "match_state"):
        if key in value:
            if not isinstance(value[key], str):
                raise ValueError(f"composition.{key} requires a string")
            result[key] = value[key]
    for key, lower, upper in (("chain_length", 1, 6), ("conditional_depth", 0, 3)):
        if key in value:
            if type(value[key]) is not int or not lower <= value[key] <= upper:
                raise ValueError(f"composition.{key} must lie between {lower} and {upper}")
            result[key] = value[key]
    if "guard_kind" in value:
        if not isinstance(value["guard_kind"], str) or value["guard_kind"] not in {"property", "count", "exists"}:
            raise ValueError("guard_kind requires property, count or exists")
        result["guard_kind"] = value["guard_kind"]
    if "branch_path" in value:
        path = value["branch_path"]
        if not isinstance(path, (list, tuple)) or len(path) > 2 or any(not isinstance(branch, str) or branch not in {"then", "otherwise"} for branch in path):
            raise ValueError("branch_path requires at most two then/otherwise child selections")
        result["branch_path"] = list(path)
    if "scalar_depth" in value and "scalar_contexts" in value:
        raise ValueError("Choose scalar_depth or scalar_contexts, not both")
    if "scalar_depth" in value:
        if type(value["scalar_depth"]) is not int or not 0 <= value["scalar_depth"] <= 4:
            raise ValueError("scalar_depth must lie between zero and four")
        result["scalar_depth"] = value["scalar_depth"]
    if "scalar_contexts" in value:
        contexts = value["scalar_contexts"]
        if (not isinstance(contexts, (list, tuple)) or len(contexts) > 4
                or any(not isinstance(context, str) or context not in {"and", "or", "case_then", "case_else", "coalesce"} for context in contexts)):
            raise ValueError("Invalid scalar_contexts")
        result["scalar_contexts"] = list(contexts)
    if "scalar_type" in value:
        if not isinstance(value["scalar_type"], str) or value["scalar_type"] not in {"boolean", "integer", "float", "string"}:
            raise ValueError("scalar_type requires boolean, integer, float or string")
        result["scalar_type"] = value["scalar_type"]
    if "scalar_value" in value:
        validate_query(Query(None, None, (Literal(value["scalar_value"]),)))
        result["scalar_value"] = value["scalar_value"]
    return result


def compose_query(kernel: Query, anchor: Node, contexts, *, threshold: int = 0) -> Query:
    validate_query(kernel)
    result = compose_fragment(kernel, "rows", anchor, contexts, threshold=threshold)
    validate_query(result)
    return result


def compose_fragment(kernel, kind, anchor: Node, contexts, *, threshold: int = 0):
    """Apply inner-to-outer operators, preserving one correlated input binding.

    EXISTS and COUNT consume a row-producing query. WITH names the current
    scalar and exposes it to filtering and projection. Rows are counted when
    WITH needs a scalar; a scalar becomes a filter when an operator needs rows.
    """
    options = composition_options({"contexts": contexts, "threshold": threshold})
    if not isinstance(anchor, Node):
        raise ValueError("composition requires a node input binding")
    names = set()

    def collect(value):
        if isinstance(value, (Variable, Alias)):
            names.add(value.name)
        if hasattr(value, "variable") and isinstance(value.variable, str):
            names.add(value.variable)
        if hasattr(value, "path_variable") and value.path_variable:
            names.add(value.path_variable)
        if isinstance(value, tuple):
            for item in value:
                collect(item)
        elif is_dataclass(value):
            for field in fields(value):
                collect(getattr(value, field.name))

    collect(kernel)

    def fresh_name():
        index = 0
        while f"gsp_value_{index}" in names:
            index += 1
        name = f"gsp_value_{index}"
        names.add(name)
        return name

    def condition(expression, kind):
        return expression if kind == "boolean" else Comparison(expression, ">", Literal(threshold))

    def as_query(fragment, kind):
        if kind == "rows":
            return fragment
        return Query(anchor, condition(fragment, kind), (Property(anchor.variable, "uid"),))

    if kind not in {"rows", "boolean", "integer"}:
        raise ValueError("A context requires rows, boolean or integer input")
    fragment = kernel
    for operator in options["contexts"]:
        if operator in {"exists", "count"}:
            body = as_query(fragment, kind)
            fragment = Exists(body=body) if operator == "exists" else Count(body=body)
            kind = "boolean" if operator == "exists" else "integer"
        else:
            if kind == "rows":
                fragment, kind = Count(body=fragment), "integer"
            name = fresh_name()
            fragment = Query(None, None, (Property(anchor.variable, "uid"), Variable(name)),
                             pipeline=(Match(anchor), With(
                                 (Variable(anchor.variable), Alias(fragment, name)),
                                 condition(Variable(name), kind))))
            kind = "rows"
    result = as_query(fragment, kind)
    return result
