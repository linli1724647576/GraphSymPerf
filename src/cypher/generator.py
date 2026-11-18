from cypher.ast_nodes import *
from data_structures.ast import sample_constant
from cypher.scope import Scope


class Generator:
    def __init__(self, rng, options, support):
        self.rng = rng
        self.options = options
        self.support = support
        self.number = 0
        # This expansion budget favors terminal choices; it is not an exact AST node count.
        self.remaining = options["nodes"]
        self.labels = [Symbol(f"Random_label_{i}", "label") for i in range(4)]
        self.types = [Symbol(f"Random_relationship_{i}", "relationship") for i in range(3)]
        self.keys = {kind: Symbol("property_" + kind, "key_" + kind) for kind in ("int", "float", "bool", "string")}

    def variable(self, kind, labels=(), nullable=False):
        self.number += 1
        return Variable("v" + str(self.number), kind, tuple(labels), nullable)

    def constant(self, kind, symbolic=True):
        value = sample_constant(self.rng, kind) if symbolic else {"int": 1, "bool": True, "string": "x", "float": 1.0}[kind]
        return Literal(value, kind)

    def query(self, scope, depth=0, outputs=None, compound=True, allow_call=True):
        self.remaining -= 1
        if compound and self.remaining > 12 and self.rng.random() < .18:
            # Generate each branch against the same output names and types.
            outputs = outputs or [self.variable(self.rng.choice(["int", "float", "bool", "string"]))]
            branches = [self.query(scope.child(), depth, outputs, False, allow_call) for _ in range(2)]
            if self.support["conditional"] and self.options["conditional_queries"] and self.rng.random() < .35:
                guard = self.expression("bool", scope, 1, depth)
                return ConditionalQuery([(guard, branches[0])], branches[1])
            return Union(branches, all=self.rng.random() < .7)

        clauses = []
        clause_count = self.rng.randint(0, self.options["clauses"] - 1) if self.remaining > 5 else 0
        for _ in range(clause_count):
            choices = ["match", "match", "with", "unwind"]
            if allow_call and self.support["call"] and depth < self.options["subquery_depth"] and self.remaining > 10:
                choices.append("call")
            choice = self.rng.choice(choices)
            if choice == "match":
                clause = self.match(scope)
                if self.rng.random() < .4:
                    clause.where = self.expression("bool", scope, self.options["expression_depth"], depth)
            elif choice == "with":
                clause = self.projection("WITH", scope, depth)
                scope.project([item.alias for item in clause.items])
                if self.rng.random() < .25:
                    clause.where = self.expression("bool", scope, 1, depth)
            elif choice == "call":
                # Only explicitly selected outer variables enter a CALL subquery.
                visible = list(scope.visible().values())
                imported = self.rng.sample(visible, self.rng.randint(0, len(visible)))
                child = Scope(imported={v.name: v for v in imported}, persistent_imports=scope.persistent_imports)
                query = self.query(child, depth + 1)
                clause = Call(imported, query)
                for variable in query.output():
                    scope.add(variable)
            else:
                variable = self.variable("int")
                clause = Unwind(ListExpression([self.constant("int") for _ in range(self.rng.randint(1, 3))]), variable)
                scope.add(variable)
            clauses.append(clause)
        clauses.append(self.projection("RETURN", scope, depth, outputs))
        return Query(clauses)

    def match(self, scope, require_edge=False):
        self.remaining -= 2
        optional = bool(scope.visible()) and self.rng.random() < .2
        shapes = ["node", "edge", "chain", "branch", "join", "cycle", "disconnected"]
        # FalkorDB comprehensions require a relationship; ordinary MATCH still allows nodes.
        shape = self.rng.choice(shapes[1:] if require_edge else shapes)
        visible_nodes = [v for v in scope.visible().values() if v.kind == "node"]
        # Reusing variables creates shared anchors, joins, and cycles without fixed query templates.
        if visible_nodes and self.rng.random() < .5:
            a = self.rng.choice(visible_nodes)
            first = NodePattern(a)
        else:
            labels = self.rng.sample(self.labels, 2 if self.support["multi_label"] and self.rng.random() < .15 else 1)
            a = self.variable("node", labels, optional)
            scope.add(a)
            first = NodePattern(a, labels)
        if self.rng.random() < .15:
            first.properties.append((self.keys["int"], self.constant("int")))
        if shape == "node":
            return Match([PathPattern([first], [])], optional)

        def node():
            labels = [self.rng.choice(self.labels)]
            variable = self.variable("node", labels, optional)
            scope.add(variable)
            return NodePattern(variable, labels)

        def relationship(bounded=False):
            types = self.rng.sample(self.types, 2 if self.rng.random() < .15 else 1)
            variable = None
            if not bounded and self.rng.random() < .15:
                variable = self.variable("relationship", nullable=optional)
                scope.add(variable)
            return RelationshipPattern(types, self.rng.choice(["out", "in", "both"]),
                                       1, self.rng.randint(2, 3) if bounded else 1, variable)

        b = node()
        if shape == "edge":
            bounded = self.rng.random() < .25
            path_var = self.variable("path", nullable=optional) if self.rng.random() < .2 else None
            if path_var:
                scope.add(path_var)
            paths = [PathPattern([first, b], [relationship(bounded)], path_var)]
        else:
            c = node()
            if shape == "chain":
                paths = [PathPattern([first, b, c], [relationship(), relationship()])]
            elif shape == "cycle":
                paths = [PathPattern([first, b, c, NodePattern(a)], [relationship(), relationship(), relationship()])]
            elif shape == "branch":
                paths = [PathPattern([first, b], [relationship()]), PathPattern([NodePattern(a), c], [relationship()])]
            elif shape == "join":
                paths = [PathPattern([first, c], [relationship()]), PathPattern([b, NodePattern(c.variable)], [relationship()])]
            else:
                paths = [PathPattern([first, b], [relationship()]), PathPattern([c, node()], [relationship()])]
        return Match(paths, optional)

    def projection(self, keyword, scope, depth, outputs=None):
        visible = list(scope.visible().values())
        items = []
        aggregate = outputs is None and self.rng.random() < .15
        if aggregate:
            # Constant aggregate inputs keep collect order-insensitive in this grammar.
            name = self.rng.choice(["count", "sum", "min", "max", "collect"])
            value = self.constant("int", symbolic=False)
            kind = "list_int" if name == "collect" else "int"
            items = [ProjectionItem(Function(name, [value], kind), self.variable(kind))]
        elif outputs:
            for variable in outputs:
                expr = self.expression(variable.kind, scope, self.options["expression_depth"], depth)
                items.append(ProjectionItem(expr, variable))
        else:
            if visible and self.rng.random() < .55:
                selected = self.rng.choice(visible)
                alias = self.variable(selected.kind, selected.labels, selected.nullable)
                items.append(ProjectionItem(Reference(selected), alias))
            kind = self.rng.choice(["int", "float", "bool", "string"])
            items.append(ProjectionItem(self.expression(kind, scope, self.options["expression_depth"], depth), self.variable(kind)))
        result = Projection(keyword, items, distinct=self.rng.random() < .15)
        sortable = all(item.alias.kind in {"int", "float", "bool", "string"} for item in items)
        if sortable and self.rng.random() < .2:
            # Sort all projected scalar columns before pagination; tied rows have equal values.
            result.order = [(Reference(item.alias), self.rng.random() < .5) for item in items]
            if self.rng.random() < .6:
                result.limit = Literal(self.rng.randint(0, 5))
                if self.rng.random() < .3:
                    result.skip = Literal(self.rng.randint(0, 2))
            if keyword == "WITH" and result.limit is None and not self.support.get("with_order_without_page", True):
                # Without SKIP/LIMIT, WITH ... ORDER BY does not change the row multiset; LadybugDB rejects it.
                result.order = []
        return result

    def expression(self, kind, scope, depth, query_depth):
        self.remaining -= 1
        variables = list(scope.visible().values())
        if depth <= 0 or self.remaining <= 0 or self.rng.random() < .3:
            same_type = [v for v in variables if v.kind == kind]
            nodes = [v for v in variables if v.kind in {"node", "relationship"}]
            if same_type and self.rng.random() < .35:
                return Reference(self.rng.choice(same_type))
            if nodes and kind in self.keys and self.rng.random() < .55:
                return Property(self.rng.choice(nodes), self.keys[kind], kind)
            return self.constant(kind)
        subqueries = query_depth < self.options["subquery_depth"] and self.remaining > 8
        # Choose productions by the expected type; nested queries recurse through query().
        choices = ["case", "coalesce", "leaf"]
        if kind == "bool":
            choices += ["compare", "and", "or", "not", "null", "quantifier", "membership"]
            if subqueries:
                choices.append("exists")
        elif kind == "int":
            choices += ["arithmetic", "size", "index"]
            if any(variable.kind == "path" for variable in variables):
                choices.append("length")
            if subqueries:
                choices.append("count")
        elif kind == "float":
            choices.append("arithmetic")
        choice = self.rng.choice(choices)
        child = lambda value_type=kind: self.expression(value_type, scope, depth - 1, query_depth)
        if choice == "case":
            return CaseExpression([(child("bool"), child())], child())
        if choice == "coalesce":
            return Function("coalesce", [Literal(None, kind), child(), child()], kind)
        if choice in {"and", "or"}:
            return Binary(child(), choice.upper(), child())
        if choice == "not":
            return Unary("NOT", child())
        if choice == "null":
            return Unary(self.rng.choice(["IS NULL", "IS NOT NULL"]), child("int"))
        if choice == "compare":
            return Binary(child("int"), self.rng.choice(["=", "<>", ">", "<", ">=", "<="]), child("int"))
        if choice == "membership":
            values = ListExpression([child("int") for _ in range(3)])
            return Binary(child("int"), "IN", values)
        if choice == "index":
            values = ListExpression([child() for _ in range(3)])
            index = self.rng.randint(-1, 3)
            if self.support.get("list_index_base", 0) == 1 and index == 0:
                # 1-based targets reject index 0; -1..3 then stays within the three-element list.
                index = 1
            return ListIndex(values, Literal(index))
        if choice == "arithmetic":
            return Binary(child(), self.rng.choice(["+", "-", "*"]), child())
        if choice == "length":
            path = self.rng.choice([variable for variable in variables if variable.kind == "path"])
            return Function("length", [Reference(path)], "int")
        if choice in {"exists", "count"}:
            if self.support["bodies"] != "full":
                child_scope = scope.child()
                match = self.match(child_scope, require_edge=self.support["bodies"] == "pattern_list")
                if self.support["bodies"] == "pattern_list":
                    child_scope = scope.child()
                    for variable in match.paths[0].variables():
                        child_scope.add(variable)
                predicate = self.expression("bool", child_scope, depth - 1, query_depth + 1)
                if self.support["bodies"] == "pattern_list":
                    values = PatternComprehension(match.paths[0], predicate, Literal(1))
                    count = Function("size", [values], "int")
                    return Binary(count, ">", Literal(0)) if choice == "exists" else count
                return SubqueryExpression(choice.upper(), MatchBody(match.paths, predicate))
            output = self.variable("int")
            return SubqueryExpression(choice.upper(), self.query(scope.child(), query_depth + 1, [output],
                                                              allow_call=self.support["subquery_call"]))
        if choice == "size":
            if self.support["bodies"] == "full" and subqueries and self.rng.random() < .5:
                output = self.variable("int")
                query = self.query(scope.child(), query_depth + 1, [output], False,
                                   allow_call=self.support["subquery_call"])
                # COLLECT exposes element order, so its single output column is sorted.
                query.clauses[-1].order = [(Reference(output), False)]
                values = SubqueryExpression("COLLECT", query)
            else:
                values = ListExpression([child() for _ in range(2)])
            return Function("size", [values], "int")
        if choice == "quantifier":
            paths = [variable for variable in variables if variable.kind == "path"]
            if paths and self.rng.random() < .5:
                variable = self.variable("node")
                values = Function("nodes", [Reference(self.rng.choice(paths))], "list_node")
                predicate = Binary(Property(variable, "keep", "bool"), "=", Literal(True, "bool"))
            else:
                variable = self.variable("int")
                values = ListExpression([self.constant("int") for _ in range(3)])
                predicate = Binary(Reference(variable), ">", self.constant("int"))
            return Quantifier(self.rng.choice(["all", "any", "none", "single"]), variable, values, predicate)
        return self.expression(kind, scope, 0, query_depth)


def generate_ast(rng, options, support=None):
    if support is None:
        from databases.neo4j import SUPPORT
        support = SUPPORT
    return Generator(rng, options, support).query(Scope(persistent_imports=support.get("persistent_imports", True)))
