from data_structures.ast import Symbol, sample_constant
from .ast_nodes import Traversal, Step, Value, Predicate
from .scope import analyze


SCALARS = ("int", "float", "bool", "string")


class Generator:
    def __init__(self, rng, options):
        self.rng, self.options = rng, options
        self.remaining, self.number = options["nodes"], 0
        self.labels = [Symbol(f"Random_label_{i}", "label") for i in range(4)]
        self.edges = [Symbol(f"Random_relationship_{i}", "relationship") for i in range(3)]
        self.keys = {kind: Symbol("property_" + kind, "key_" + kind) for kind in SCALARS}

    def value(self, kind):
        return Value(sample_constant(self.rng, kind), kind)

    def predicate(self, scope, depth):
        self.remaining -= 1
        choices = ["identity", "not"]
        if scope.current.kind in {"node", "relationship"}:
            choices += ["has", "has"]
        elif scope.current.kind in SCALARS:
            choices.append("is")
        if depth < self.options["expression_depth"] and self.remaining > 5:
            choices += ["and", "or", "filter"]
        name = self.rng.choice(choices)
        if name == "has":
            kind = self.rng.choice(SCALARS)
            args = [self.keys[kind], self.value(kind)]
        elif name == "is":
            args = [Predicate(self.rng.choice(["eq", "neq"]), [self.value(scope.current.kind)])]
        elif name in {"and", "or", "filter"}:
            args = [self.predicate(scope, depth+1) for _ in range(2 if name in {"and", "or"} else 1)]
        elif name == "not" and depth < self.options["expression_depth"] and self.remaining > 2:
            args = [self.predicate(scope, depth+1)]
        else:
            args = [Traversal([Step("identity")])] if name == "not" else []
        return Traversal([Step(name, args)])

    def scalar(self, scope, kind, depth):
        self.remaining -= 1
        choices = ["constant"]
        if kind == "int":
            choices.append("count")
        if scope.current.kind in {"node", "relationship"}:
            choices.append("values")
        if depth < self.options["subquery_depth"] and self.remaining > 8:
            if self.options["conditional_queries"]:
                choices.append("choose")
            choices += ["coalesce", "map"]
        name = self.rng.choice(choices)
        if name == "constant":
            args = [self.value(kind)]
        elif name == "values":
            args = [self.keys[kind], kind]
        elif name == "choose":
            args = [self.predicate(scope, 0)] + [self.scalar(scope, kind, depth+1) for _ in range(2)]
        elif name in {"coalesce", "map"}:
            args = [self.scalar(scope, kind, depth+1) for _ in range(2 if name == "coalesce" else 1)]
        else:
            args = []
        steps = []
        if self.remaining > 2 and self.rng.random() < .3:
            steps.append(Step("filter", [self.predicate(scope, 0)]))
        return Traversal(steps + [Step(name, args)])

    def query(self):
        tree = Traversal([Step("V"), Step("hasLabel", [self.rng.choice(self.labels)])], root=True)
        scope = analyze(tree)
        for _ in range(self.rng.randint(0, self.options["clauses"])):
            if self.remaining <= 0:
                break
            self.remaining -= 1
            choices = ["filter", "filter", "as", "map", "union", "identity"]
            if scope.current.kind == "node":
                choices += ["out", "in", "both", "outE"]
            if scope.current.kind == "relationship":
                choices += ["inV", "outV"]
            if scope.bindings:
                choices.append("select")
            name = self.rng.choice(choices)
            if name == "filter":
                args = [self.predicate(scope, 0)]
            elif name == "as":
                self.number += 1
                args = ["v" + str(self.number)]
            elif name == "select":
                args = [self.rng.choice(list(scope.bindings))]
            elif name == "map":
                args = [self.scalar(scope, self.rng.choice(SCALARS), 0)]
            elif name == "union":
                # Branches retain the same input kind while independently filtering rows.
                args = [Traversal([Step("filter", [self.predicate(scope, 0)])]) for _ in range(2)]
            elif name in {"out", "in", "both", "outE"}:
                args = [self.rng.choice(self.edges)]
            else:
                args = []
            tree.steps.append(Step(name, args))
            if name in {"out", "in", "both", "inV", "outV"}:
                tree.steps.append(Step("hasLabel", [self.rng.choice(self.labels)]))
            scope = analyze(tree)
        final = self.rng.choice(["map", "constant", "identity", "fold"])
        if final == "map":
            tree.steps.append(Step("map", [self.scalar(scope, self.rng.choice(SCALARS), 0)]))
        elif final in {"constant", "fold"}:
            tree.steps.append(Step("constant", [self.value(self.rng.choice(SCALARS))]))
            if final == "fold":
                # Only repeated constants are collected, so graph iteration order is unobservable.
                tree.steps.append(Step("fold"))
        else:
            tree.steps.append(Step("identity"))
        return tree


def generate_ast(rng, options, support):
    return Generator(rng, options).query()
