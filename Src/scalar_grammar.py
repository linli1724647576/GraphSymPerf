"""Typed scalar productions composed independently of graph population.

The local law selects an operator and a mutation port. The same productions
then embed that port in Boolean, CASE, and coalesce contexts. No production
assigns matching quantities or decides which graph read should grow.
"""

from dataclasses import dataclass
import random

import ir
from row_grammar import RowGrammar


@dataclass(frozen=True)
class GeneratedScalar:
    source: object
    marked: object
    suffix: tuple
    replacement: object | None
    contexts: tuple


class ScalarGrammar:
    def __init__(self, anchor, *, seed, symbol, flag, region, length=1):
        self.anchor, self.symbol, self.flag = anchor, symbol, flag
        self.random = random.Random(seed)
        self.rows = RowGrammar(anchor, seed=seed, symbol=symbol, flag=flag, region=region)
        self.length = length

    def value(self, name, kind):
        if kind == "boolean":
            return self.flag(self.anchor.variable, name, port=f"scalar:{name}")
        return ir.Property(self.anchor.variable, self.symbol(f"property:scalar:{name}", "identifier", (name,)))

    @staticmethod
    def literal(kind):
        return ir.Literal({"boolean": False, "integer": 0, "float": 0.0, "string": ""}[kind])

    def read(self, name, kind, *, exists=False):
        body = self.rows.matching(self.length, name=name, projection="constant")
        if exists:
            # The direct one-atom form and a multi-clause body share exactly
            # the same row interface, including lexical input bindings.
            if hasattr(body, "candidates"):
                query = body.candidates[0]
                return ir.Exists(query.pattern, query.where)
            return ir.Exists(body=body)
        counted = ir.Count(body=body)
        if kind == "integer":
            return counted
        positive = ir.Comparison(counted, ">", ir.Literal(0))
        if kind == "boolean":
            return positive
        if kind == "float":
            return ir.Case(positive, ir.Literal(1.5), ir.Literal(0.0))
        return ir.Case(positive, ir.Literal("present"), ir.Literal("absent"))

    def expand(self, operator, action, *, kind, depth=0, contexts=None):
        if operator in {"and", "or"}:
            if kind != "boolean":
                raise ValueError("Boolean productions require a Boolean result interface")
            expensive = self.read("scalar_work", "boolean", exists=True)
            second = expensive if action == "commute" else self.literal("boolean")
            if operator == "and" and action != "commute":
                second = ir.Literal(True)
            marked = ir.Boolean(operator.upper(), (self.value("decisive", "boolean"), second))
            suffix = () if action == "commute" else ("terms", 1)
        elif operator == "case":
            second = self.read("scalar_work", kind) if action == "dual" else self.literal(kind)
            marked = ir.Case(self.value("decisive", "boolean"), self.value("payload", kind), second)
            suffix = () if action == "dual" else ("otherwise",)
        elif operator == "coalesce":
            marked = ir.Call("coalesce", (self.value("prefix", kind), self.value("payload", kind), self.literal(kind)))
            suffix = ("args", 2)
        else:
            raise ValueError("Unknown scalar production")
        replacement = self.read("scalar_replacement", kind) if action == "replace" else None
        choices = ("and", "or", "case_then", "case_else", "coalesce") if kind == "boolean" else ("case_then", "case_else", "coalesce")
        selected = tuple(contexts) if contexts is not None else tuple(self.random.choice(choices) for _ in range(depth))
        if len(selected) > 4 or any(context not in choices for context in selected):
            raise ValueError("Scalar contexts must preserve their scalar type interface")
        result = marked
        for index, context in enumerate(selected):
            if context in {"and", "or"}:
                result = ir.Boolean(context.upper(), (result, self.value(f"neutral_{index}", "boolean")))
            elif context in {"case_then", "case_else"}:
                branch = (result, self.literal(kind)) if context == "case_then" else (self.literal(kind), result)
                result = ir.Case(self.value(f"guard_{index}", "boolean"), *branch)
            else:
                result = ir.Call("coalesce", (self.value(f"null_prefix_{index}", kind), result, self.literal(kind)))
        return GeneratedScalar(result, marked, suffix, replacement, selected)
