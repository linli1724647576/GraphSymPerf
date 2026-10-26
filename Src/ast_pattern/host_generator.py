"""Pattern-independent, typed host generation before graph construction.

The two passes deliberately have no rule-registry or graph-state dependency.
Only the declared insertion port and graph-dependent terminals survive M1.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
import random

import ir

from .contracts import (ClauseSkeleton, EmbeddingPort, HostAst, Port, ScopeState,
                        StageFailure, TerminalSlot, derive_seed)


DEFAULT_BUDGET = {"clauses": 4, "nodes": 180, "expression_depth": 3,
                  "subquery_depth": 3, "path_hops": 3, "port_nodes": 128,
                  "match_edges": 3, "projections": 2, "expression_arity": 3}
CLAUSES = ("MATCH", "OPTIONAL MATCH", "WITH", "WITH DISTINCT", "RETURN", "RETURN DISTINCT")
RETURN_CLAUSES = ("RETURN", "RETURN DISTINCT")
MATCH_SHAPES = ("node", "edge", "chain", "branch", "converge", "cycle", "disconnected")
MATCH_FEATURES = ("multi_label", "relationship_alternatives", "property_maps", "named_relationship")
ROW_OPERATORS = ("query", "union_all", "conditional", "limit")
COMPARISON_OPERATORS = ("=", "<>", "<", "<=", ">", ">=")
EXPRESSION_OPERATORS = ("leaf", "comparison", "and", "or", "not", "case",
                        "coalesce", "count", "exists", "null_test")


def _failure(code, message, **evidence):
    raise StageFailure("M1", code, message, evidence=evidence)


def _budget(value):
    if value is None:
        return dict(DEFAULT_BUDGET)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_BUDGET):
        _failure("invalid_configuration", "Unknown host budget field")
    result = DEFAULT_BUDGET | value
    for key, amount in result.items():
        lower = 0 if key in {"expression_depth", "subquery_depth"} else 2 if key == "expression_arity" else 1
        if type(amount) is not int or amount < lower:
            _failure("invalid_configuration", f"{key} requires an integer >= {lower}")
    if result["clauses"] > 32 or result["expression_depth"] > 8 or result["subquery_depth"] > 8:
        _failure("invalid_configuration", "Host recursion or clause budget exceeds supported bounds")
    if result["match_edges"] > 6 or result["projections"] > 8 or result["expression_arity"] > 8:
        _failure("invalid_configuration", "Host matching or expression width exceeds supported bounds")
    return result


def _grammar(value):
    if value is None:
        value = {}
    allowed = {"clauses", "clause_kinds", "port_kinds", "return_type", "expression_operators",
               "match_shapes", "match_features", "row_operators", "comparison_operators"}
    if not isinstance(value, dict) or set(value) - allowed:
        _failure("invalid_configuration", "Unknown host grammar field")
    result = dict(value)
    for key, choices in (("clause_kinds", CLAUSES), ("port_kinds", ("rows", "predicate", "scalar")),
                         ("expression_operators", EXPRESSION_OPERATORS), ("match_shapes", MATCH_SHAPES),
                         ("match_features", MATCH_FEATURES), ("row_operators", ROW_OPERATORS),
                         ("comparison_operators", COMPARISON_OPERATORS)):
        options = result.get(key, choices)
        if (not isinstance(options, (tuple, list)) or (not options and key != "match_features")
                or any(x not in choices for x in options)):
            _failure("invalid_configuration", f"Invalid {key}")
        result[key] = tuple(dict.fromkeys(options))
    if not set(RETURN_CLAUSES) & set(result["clause_kinds"]):
        _failure("invalid_configuration", "The host grammar requires RETURN")
    if "query" not in result["row_operators"]:
        _failure("invalid_configuration", "The row grammar requires the terminal query production")
    if result.get("return_type", "integer") not in {"integer", "boolean"}:
        _failure("unsupported_fragment", "Host return_type supports integer and boolean")
    if "clauses" in result:
        if not isinstance(result["clauses"], (tuple, list)):
            _failure("invalid_configuration", "Explicit host clauses require a sequence")
        kinds = tuple(result["clauses"])
        if (not kinds or kinds[-1] not in RETURN_CLAUSES or any(k not in CLAUSES for k in kinds)
                or any(k in RETURN_CLAUSES for k in kinds[:-1]) or kinds[0] == "OPTIONAL MATCH"):
            _failure("invalid_configuration", "Invalid explicit host clause sequence")
        if any(k not in result["clause_kinds"] for k in kinds):
            _failure("invalid_configuration", "Explicit clause is disabled by the grammar")
        result["clauses"] = kinds
    return result


def _capabilities(profile):
    if profile is None:
        return {}
    if not isinstance(profile, dict):
        _failure("invalid_configuration", "capability_profile must be a mapping")
    # Capabilities constrain productions, never provide concrete graph data.
    allowed = {"clause_kinds", "expression_operators", "subqueries", "optional_match", "with",
               "distinct", "match_shapes", "match_features", "row_operators", "comparison_operators"}
    if set(profile) - allowed:
        _failure("invalid_configuration", "Unknown host capability field")
    result = dict(profile)
    for key in ("subqueries", "optional_match", "with", "distinct"):
        if key in result and type(result[key]) is not bool:
            _failure("invalid_configuration", f"{key} requires a boolean")
    for key, choices in (("clause_kinds", CLAUSES), ("expression_operators", EXPRESSION_OPERATORS),
                         ("match_shapes", MATCH_SHAPES), ("match_features", MATCH_FEATURES),
                         ("row_operators", ROW_OPERATORS), ("comparison_operators", COMPARISON_OPERATORS)):
        if key in result and (not isinstance(result[key], (tuple, list))
                              or (not result[key] and key != "match_features")
                              or any(item not in choices for item in result[key])):
            _failure("invalid_configuration", f"Invalid capability {key}")
    return result


def _choices(grammar, capabilities):
    clauses = tuple(k for k in grammar["clause_kinds"]
                    if k in capabilities.get("clause_kinds", CLAUSES)
                    and (k != "OPTIONAL MATCH" or capabilities.get("optional_match", True))
                    and (not k.startswith("WITH") or capabilities.get("with", True))
                    and ("DISTINCT" not in k or capabilities.get("distinct", True)))
    operators = tuple(k for k in grammar["expression_operators"]
                      if k in capabilities.get("expression_operators", EXPRESSION_OPERATORS)
                      and (k not in {"count", "exists"} or capabilities.get("subqueries", True)))
    if not set(RETURN_CLAUSES) & set(clauses) or not operators:
        _failure("unsupported_fragment", "Capabilities leave no complete host grammar")
    return clauses, operators


def sample_clause_skeleton(grammar=None, capability_profile=None, clause_budget=None, seed=0):
    """Sample a bounded clause sequence without filling expressions or matches."""
    derive_seed(seed, "validate")
    grammar, capabilities = _grammar(grammar), _capabilities(capability_profile)
    allowed, _ = _choices(grammar, capabilities)
    limit = DEFAULT_BUDGET["clauses"] if clause_budget is None else clause_budget
    if type(limit) is not int or not 1 <= limit <= 32:
        _failure("invalid_configuration", "clause_budget must lie between 1 and 32")
    rng = random.Random(seed)
    kinds = grammar.get("clauses")
    if kinds is None:
        count = rng.randint(1, limit)
        prefix = []
        for index in range(count - 1):
            candidates = [k for k in allowed if k not in RETURN_CLAUSES
                          and (k != "OPTIONAL MATCH" or index > 0)]
            if not candidates:
                break
            prefix.append(rng.choice(candidates))
        kinds = (*prefix, rng.choice([k for k in allowed if k in RETURN_CLAUSES]))
    if len(kinds) > limit or any(k not in allowed for k in kinds):
        _failure("generation_exhausted", "Clause skeleton does not fit the capability/budget")
    slots = {"MATCH": ("matching", "predicate"), "OPTIONAL MATCH": ("matching", "predicate"),
             "WITH": ("projection", "predicate"), "WITH DISTINCT": ("projection", "predicate"),
             "RETURN": ("projection",), "RETURN DISTINCT": ("projection",)}
    clauses = tuple({"clause_id": f"clause:{i}", "kind": kind, "syntax_slots": slots[kind]}
                    for i, kind in enumerate(kinds))
    trace = tuple({"production_id": f"clause/{kind}", "position": i,
                   "predecessor": kinds[i - 1] if i else None} for i, kind in enumerate(kinds))
    return ClauseSkeleton(clauses, trace, seed)


def _nullable(kind):
    return kind == "null" or kind.endswith("?")


def _join_scalar_types(kinds, *, nullable):
    concrete = {kind.rstrip("?") for kind in kinds if kind != "null"}
    if not concrete:
        return "null"
    if len(concrete) == 1:
        result = next(iter(concrete))
    elif concrete <= {"integer", "float", "number"}:
        result = "number"
    else:
        result = "any"
    return result + ("?" if nullable else "")


def _symbol_type(expression, scope):
    available = dict(scope.visible_symbols)
    if isinstance(expression, ir.Variable):
        if expression.name not in available:
            _failure("unbound_symbol", f"Unbound host variable: {expression.name}")
        return available[expression.name]
    if isinstance(expression, ir.Literal):
        value = expression.value
        if isinstance(value, TerminalSlot):
            return _join_scalar_types((value.type,), nullable=_nullable(value.type) or None in value.candidates)
        return {bool: "boolean", int: "integer", type(None): "null", str: "string"}.get(type(value), "any")
    if isinstance(expression, Port):
        return expression.expected_type
    if isinstance(expression, ir.Property):
        if available.get(expression.variable, "").rstrip("?") not in {"node", "relationship"}:
            _failure("unbound_symbol", "A property requires a visible entity binding")
        # A typed property-key slot constrains the property's non-NULL value
        # type, not its existence. M3 has not yet established the property on
        # each reachable entity; even a mandatory node can lack the key.
        kind = expression.key.type if isinstance(expression.key, TerminalSlot) else "any"
        return _join_scalar_types((kind,), nullable=True)
    if isinstance(expression, ir.Count):
        return "integer"
    if isinstance(expression, ir.Exists):
        return "boolean"
    if isinstance(expression, ir.Comparison):
        if expression.operator in {"IS NULL", "IS NOT NULL"}:
            return "boolean"
        kinds = (_symbol_type(expression.left, scope), _symbol_type(expression.right, scope))
        return "boolean?" if any(_nullable(kind) for kind in kinds) else "boolean"
    if isinstance(expression, (ir.Boolean, ir.Negation)):
        operands = expression.terms if isinstance(expression, ir.Boolean) else (expression.expression,)
        return "boolean?" if any(_nullable(_symbol_type(item, scope)) for item in operands) else "boolean"
    if isinstance(expression, ir.Case):
        left, right = (_symbol_type(e, scope) for e in (expression.then, expression.otherwise))
        return _join_scalar_types((left, right), nullable=_nullable(left) or _nullable(right))
    if isinstance(expression, ir.Call) and expression.name == "coalesce":
        kinds = [_symbol_type(e, scope) for e in expression.args]
        if not kinds:
            _failure("interface_mismatch", "coalesce requires at least one argument")
        reachable = []
        for kind in kinds:
            reachable.append(kind)
            if not _nullable(kind):
                break
        return _join_scalar_types(reachable, nullable=all(_nullable(kind) for kind in reachable))
    _failure("unsupported_fragment", f"No host type rule for {type(expression).__name__}")


def advance_scope(clause, incoming_scope):
    """Apply MATCH/OPTIONAL/WITH definition and projection lifecycle rules."""
    if not isinstance(incoming_scope, ScopeState):
        _failure("invalid_configuration", "advance_scope requires ScopeState")
    available = dict(incoming_scope.visible_symbols)
    definitions, aliases = list(incoming_scope.definitions), list(incoming_scope.alias_sources)
    if isinstance(clause, ir.Match):
        # The concrete IR helper validates label spellings; M1 still carries
        # symbolic labels, so extract structural declarations directly.
        variables = {}
        for _, atom in _located(clause.pattern):
            if isinstance(atom, ir.Node):
                variables[atom.variable] = "node"
            elif isinstance(atom, ir.Relationship) and atom.variable:
                variables[atom.variable] = "relationship"
            elif isinstance(atom, ir.Edge) and atom.path_variable:
                variables[atom.path_variable] = "path"
        for variable, kind in variables.items():
            if variable in available:
                if available[variable].rstrip("?") != kind:
                    _failure("binding_capture", f"Host symbol {variable} has incompatible kind")
                continue
            value_type = kind + ("?" if clause.optional else "")
            available[variable] = value_type
            definitions.append({"symbol_id": f"{incoming_scope.scope_id}/{variable}", "name": variable,
                                "type": value_type, "source": "optional_match" if clause.optional else "match"})
    elif isinstance(clause, ir.With):
        outgoing = {}
        for item in clause.projections:
            if isinstance(item, ir.Alias):
                name, expression = item.name, item.expression
                kind = _symbol_type(expression, incoming_scope)
                referenced = {part.name if isinstance(part, ir.Variable) else part.variable
                              for _, part in _located(expression) if isinstance(part, (ir.Variable, ir.Property))}
                aliases.append({"name": name, "source_symbols": tuple(sorted(referenced & available.keys())),
                                "scope_id": incoming_scope.scope_id})
            elif isinstance(item, ir.Variable):
                name, expression = item.name, item
                kind = _symbol_type(expression, incoming_scope)
            else:
                _failure("interface_mismatch", "Host WITH projects variables or explicit aliases")
            if name in outgoing:
                _failure("binding_capture", f"Duplicate host projection {name}")
            outgoing[name] = kind
        available = outgoing
    else:
        _failure("unsupported_fragment", "Only MATCH and WITH advance host scope")
    return replace(incoming_scope, visible_symbols=tuple(available.items()), definitions=tuple(definitions),
                   exports=tuple(available), alias_sources=tuple(aliases),
                   nullable_types=tuple(name for name, kind in available.items() if _nullable(kind)))


class _Builder:
    def __init__(self, grammar, capabilities, budget, seed):
        self.grammar, self.capabilities, self.budget = grammar, capabilities, budget
        self.rng = random.Random(seed)
        self.clause_kinds, self.operators = _choices(grammar, capabilities)
        self.used_nodes, self.next_symbol, self.next_slot, self.next_scope = 0, 0, 0, 0
        self.slots, self.trace, self.scopes, self.uses = [], [], [], []
        self.port_kind = self.rng.choice(grammar["port_kinds"])
        self.port_scope = None

    def node(self, cls, *args, **kwargs):
        self.used_nodes += 1
        if self.used_nodes > self.budget["nodes"]:
            _failure("generation_exhausted", "Host AST node budget exhausted", used=self.used_nodes)
        return cls(*args, **kwargs)

    def slot(self, kind, value_type, values, scope):
        name = f"host:terminal:{self.next_slot}"
        self.next_slot += 1
        result = TerminalSlot(name, kind, tuple(values), value_type, scope.scope_id)
        self.slots.append(result)
        return result

    def literal(self, value_type, scope):
        values = (0, 1, 2) if value_type == "integer" else (True, False)
        values = tuple(self.rng.sample(list(values), len(values)))
        return self.node(ir.Literal, self.slot("literal", value_type, values, scope))

    def fresh(self, prefix="h"):
        name = f"{prefix}{self.next_symbol}"
        self.next_symbol += 1
        return name

    def new_scope(self, parent=None):
        value = ScopeState(f"scope:{self.next_scope}", parent.scope_id if parent else None,
                           parent.visible_symbols if parent else ())
        self.next_scope += 1
        return value

    def port(self, scope, kind=None):
        if self.port_scope is not None:
            _failure("interface_mismatch", "M1 may declare only one unfilled port")
        self.port_scope = scope
        kind = kind or self.port_kind
        return self.node(Port, "port:0", kind, "rows" if kind == "rows" else
                         "boolean" if kind == "predicate" else "integer", ())

    def _match_features(self):
        defaults = ("multi_label", "relationship_alternatives", "property_maps", "named_relationship")
        return set(self.grammar.get("match_features", defaults)) & set(self.capabilities.get("match_features", defaults))

    def _match_node(self, scope, *, reuse=False):
        visible = [name for name, kind in scope.visible_symbols if kind.rstrip("?") == "node"]
        if reuse and visible and self.rng.random() < .45:
            return self.node(ir.Node, self.rng.choice(visible))
        name = self.fresh()
        labels = [self.slot("label", "string", (f"AstHost{name}",), scope)]
        features = self._match_features()
        if "multi_label" in features and self.rng.random() < .3:
            labels.append(self.slot("label", "string", (f"AstHost{name}Extra",), scope))
        properties = (("host_match", self.literal("integer", scope)),) if (
            "property_maps" in features and self.rng.random() < .25) else ()
        return self.node(ir.Node, name, tuple(labels), properties)

    def _match_reference(self, node):
        # A repeated binding is another AST occurrence, with its own budget
        # charge, rather than a shared object hidden from the node counter.
        return self.node(ir.Node, node.variable)

    def _match_edge(self, scope, left, right, *, direction=None):
        features = self._match_features()
        relation_type = self.slot("relationship_type", "string", (f"AST_HOST_{self.next_slot}",), scope)
        alternatives = ()
        if "relationship_alternatives" in features and self.rng.random() < .3:
            alternatives = (self.slot("relationship_type", "string", (f"AST_HOST_ALT_{self.next_slot}",), scope),)
        properties = (("host_edge", self.literal("integer", scope)),) if (
            "property_maps" in features and self.rng.random() < .2) else ()
        variable = self.fresh("rel") if "named_relationship" in features and self.rng.random() < .35 else None
        relation = self.node(ir.Relationship, variable=variable, type=relation_type,
                             properties=properties, alternatives=alternatives)
        return self.node(ir.Edge, left, relation, right,
                         direction or self.rng.choice(("out", "in", "undirected")))

    def matches(self, scope, optional=False, max_clauses=None):
        """Generate topology as separate MATCH atoms with explicit shared nodes.

        Their semantics include cross-clause relationship reuse and, for
        OPTIONAL, independent NULL extension. They are not a single Cypher
        path pattern with implicit relationship uniqueness.
        """
        minimum = {"node": 1, "edge": 1, "chain": 2, "branch": 2,
                   "converge": 2, "cycle": 3, "disconnected": 2}
        limit = min(self.budget.get("match_edges", 3), max_clauses) if max_clauses is not None else self.budget.get("match_edges", 3)
        allowed = self.capabilities.get("match_shapes", tuple(minimum))
        candidates = [shape for shape in self.grammar.get("match_shapes", tuple(minimum))
                      if shape in allowed and minimum[shape] <= limit]
        if not candidates:
            _failure("generation_exhausted", "No matching shape fits the remaining clause/edge budget")
        shape = self.rng.choice(candidates)
        count = self.rng.randint(minimum[shape], limit) if minimum[shape] > 1 else 1
        parts, imported = [], set()
        incoming = {name for name, kind in scope.visible_symbols if kind.rstrip("?") == "node"}

        def append(pattern):
            parts.append(self.node(ir.Match, pattern, None, optional))
            nodes = (pattern,) if isinstance(pattern, ir.Node) else (pattern.left, pattern.right)
            imported.update(node.variable for node in nodes if node.variable in incoming)

        if shape == "node":
            append(self._match_node(scope, reuse=True))
        elif shape == "disconnected":
            for _ in range(count):
                append(self._match_edge(scope, self._match_node(scope), self._match_node(scope)))
        else:
            root = self._match_node(scope, reuse=True)
            if shape == "edge":
                append(self._match_edge(scope, root, self._match_node(scope)))
            elif shape == "converge":
                target = self._match_node(scope)
                append(self._match_edge(scope, root, target, direction="out"))
                for _ in range(count - 1):
                    append(self._match_edge(scope, self._match_node(scope), self._match_reference(target), direction="out"))
            else:
                previous = root
                for index in range(count):
                    left = root if index == 0 else self._match_reference(root if shape == "branch" else previous)
                    target = self._match_reference(root) if shape == "cycle" and index == count - 1 else self._match_node(scope)
                    append(self._match_edge(scope, left, target, direction="out" if shape in {"branch", "cycle"} else None))
                    previous = target
        self.trace.append({"production_id": f"match/{shape}", "scope_id": scope.scope_id,
                           "optional": optional, "imports": tuple(sorted(imported)), "clauses": len(parts)})
        return tuple(parts)

    def match(self, scope, optional=False):
        """Keep the single-atom boundary for callers with one MATCH slot."""
        return self.matches(scope, optional, max_clauses=1)[0]

    def leaf(self, expected, scope, depth, *, ordered=False):
        # NULL is syntax, not an alternative inside every typed terminal: a
        # later binding must not silently change the terminal's scalar type.
        if self.rng.random() < .15:
            return self.node(ir.Literal, None)
        # The shared concrete IR has no graph-property schema. An integer
        # host alias can also originate in such a property, so ordering uses
        # only operands whose numeric type survives concrete IR validation.
        available = [] if ordered else [name for name, kind in scope.visible_symbols if kind.rstrip("?") == expected]
        entities = [] if ordered else [name for name, kind in scope.visible_symbols if kind.rstrip("?") in {"node", "relationship"}]
        if available and self.rng.random() < .3:
            name = self.rng.choice(available)
            self.uses.append({"scope_id": scope.scope_id, "name": name, "kind": "scalar"})
            return self.node(ir.Variable, name)
        boolean_comparisons = tuple(op for op in self.comparison_choices() if op in {"=", "<>"})
        if entities and (expected != "boolean" or depth > 0 and boolean_comparisons
                         and "comparison" in self.operators) and self.rng.random() < .35:
            name = self.rng.choice(entities)
            key = self.slot("property_key", expected, ("host_number" if expected == "integer" else "host_flag",), scope)
            prop = self.node(ir.Property, name, key)
            self.uses.append({"scope_id": scope.scope_id, "name": name, "kind": "property"})
            if expected == "boolean":
                return self.node(ir.Comparison, prop, self.rng.choice(boolean_comparisons), self.literal("boolean", scope))
            return prop
        return self.literal(expected, scope)

    def comparison_choices(self):
        enabled = self.capabilities.get("comparison_operators", ("=", "<>", "<", "<=", ">", ">="))
        return tuple(op for op in self.grammar.get("comparison_operators", ("=", "<>", "<", "<=", ">", ">="))
                     if op in enabled)

    def port_fits(self, expected, depth, subquery_depth):
        """Whether a port has a type-correct path within the remaining budgets."""
        can_query = (subquery_depth < self.budget["subquery_depth"]
                     and ("count" if expected == "integer" else "exists") in self.operators)
        if self.port_kind == "rows":
            return can_query
        port_type = "boolean" if self.port_kind == "predicate" else "integer"
        if expected == port_type:
            return True
        adapters = ("case",) if expected == "integer" else ("comparison", "null_test")
        return can_query or depth > 0 and any(adapter in self.operators
            and (adapter != "comparison" or self.comparison_choices()) for adapter in adapters)

    def expression(self, expected, scope, depth, subquery_depth=0, need_port=False, *, ordered=False):
        candidates = ["leaf"]
        if depth > 0:
            candidates += ["case", "coalesce"]
            candidates += ["comparison", "null_test", "and", "or", "not"] if expected == "boolean" else []
        if subquery_depth < self.budget["subquery_depth"]:
            candidates += ["count" if expected == "integer" else "exists"]
        candidates = [operator for operator in candidates if operator in self.operators]
        if not self.comparison_choices():
            candidates = [operator for operator in candidates if operator != "comparison"]
        # A typed port may be nested beneath any compatible production. Its
        # path depends only on the host grammar and host RNG.
        if need_port and depth <= 0:
            if self.port_kind == "rows":
                operator = "count" if expected == "integer" else "exists"
                if operator not in candidates:
                    _failure("generation_exhausted", "A row port requires an enabled COUNT/EXISTS consumer")
                return self.node(ir.Count if expected == "integer" else ir.Exists, body=self.port(scope))
            if (self.port_kind == "predicate") == (expected == "boolean"):
                return self.port(scope)
            operator = "count" if expected == "integer" else "exists"
            if operator not in candidates:
                _failure("generation_exhausted", "Typed port cannot fit the expression/subquery depth budget")
            # COUNT/EXISTS consume a separate query block. Its expression
            # depth starts at zero while the shared subquery budget advances.
            body = self.rows(scope, 0, subquery_depth + 1, True)
            return self.node(ir.Count if expected == "integer" else ir.Exists, body=body)
        if not candidates:
            _failure("generation_exhausted", "No terminal production for requested host type")
        if need_port:
            candidates = [op for op in candidates if op != "leaf"]
            child_depth = max(0, depth - 1)
            valid_children = {"comparison": ("integer",), "null_test": ("integer", "boolean"),
                              "and": ("boolean",), "or": ("boolean",),
                              "not": ("boolean",), "case": ("boolean", expected), "coalesce": (expected,)}
            candidates = [op for op in candidates if op not in valid_children or any(
                self.port_fits(kind, child_depth, subquery_depth) for kind in valid_children[op])]
            if self.port_kind != "rows" and ((self.port_kind == "predicate") == (expected == "boolean")):
                candidates.append("port")
            if not candidates:
                return self.expression(expected, scope, 0, subquery_depth, True, ordered=ordered)
        operator = self.rng.choice(candidates)
        self.trace.append({"production_id": f"expression/{operator}", "expected_type": expected,
                           "scope_id": scope.scope_id, "depth": depth, "contains_port": need_port})
        child_depth = max(0, depth - 1)
        if operator == "port":
            return self.port(scope)
        if operator == "leaf":
            return self.leaf(expected, scope, depth, ordered=ordered)
        if operator in {"count", "exists"}:
            if need_port and self.port_kind == "rows" and (
                    depth == 0 or subquery_depth + 1 >= self.budget["subquery_depth"] or self.rng.random() < .7):
                body = self.port(scope)
            else:
                body = self.rows(scope, child_depth, subquery_depth + 1, need_port)
            return self.node(ir.Count if operator == "count" else ir.Exists, body=body)
        if operator == "comparison":
            comparator = self.rng.choice(self.comparison_choices())
            needs_numeric = comparator in {"<", "<=", ">", ">="}
            left_port = need_port and self.rng.choice((True, False))
            left = self.expression("integer", scope, child_depth, subquery_depth, left_port, ordered=needs_numeric)
            right = self.expression("integer", scope, child_depth, subquery_depth, need_port and not left_port,
                                    ordered=needs_numeric)
            return self.node(ir.Comparison, left, comparator, right)
        if operator == "null_test":
            kinds = [kind for kind in ("integer", "boolean") if not need_port
                     or self.port_fits(kind, child_depth, subquery_depth)]
            operand = self.expression(self.rng.choice(kinds), scope, child_depth, subquery_depth, need_port)
            return self.node(ir.Comparison, operand, self.rng.choice(("IS NULL", "IS NOT NULL")),
                             self.node(ir.Literal, None))
        if operator in {"and", "or"}:
            arity = self.rng.randint(2, self.budget.get("expression_arity", 3))
            side = self.rng.randrange(arity) if need_port else -1
            operands = tuple(self.expression("boolean", scope, child_depth, subquery_depth, need_port and index == side)
                             for index in range(arity))
            return self.node(ir.Boolean, operator.upper(), operands)
        if operator == "not":
            return self.node(ir.Negation, self.expression("boolean", scope, child_depth, subquery_depth, need_port))
        if operator == "case":
            # The port may occur in the guard or either same-typed branch.
            sides = [index for index, kind in enumerate(("boolean", expected, expected))
                     if self.port_fits(kind, child_depth, subquery_depth)]
            side = self.rng.choice(sides) if need_port else -1
            condition = self.expression("boolean", scope, child_depth, subquery_depth, need_port and side == 0)
            yes = self.expression(expected, scope, child_depth, subquery_depth, need_port and side == 1, ordered=ordered)
            no = self.expression(expected, scope, child_depth, subquery_depth, need_port and side == 2, ordered=ordered)
            return self.node(ir.Case, condition, yes, no)
        arity = self.rng.randint(2, self.budget.get("expression_arity", 3))
        side = self.rng.randrange(arity) if need_port else -1
        arguments = tuple(self.expression(expected, scope, child_depth, subquery_depth, need_port and side == index,
                                          ordered=ordered) for index in range(arity))
        return self.node(ir.Call, "coalesce", arguments)

    def rows(self, parent, depth, subquery_depth, need_port=False, output_type=None):
        # All branches share an explicit one-column interface. In particular,
        # a raw row port cannot be a UNION branch: M1 does not yet know the
        # columns of the local rule that M2 will insert there.
        expected = output_type or (("boolean" if self.port_kind == "predicate" else "integer")
                                  if need_port and self.port_kind != "rows"
                                  else self.rng.choice(("integer", "boolean")))
        allowed = self.capabilities.get("row_operators", ROW_OPERATORS)
        operators = [kind for kind in self.grammar["row_operators"] if kind in allowed
                     and (kind == "query" or depth > 0)
                     and (kind != "limit" or not need_port)]
        if not operators:
            _failure("generation_exhausted", "No row production fits the remaining grammar/budget")
        operator = self.rng.choice(operators)
        self.trace.append({"production_id": f"rows/{operator}", "expected_type": expected,
                           "depth": depth, "contains_port": need_port})
        if operator == "union_all":
            count = self.rng.randint(2, self.budget["expression_arity"])
            side = self.rng.randrange(count) if need_port else -1
            branches = tuple(self.rows(parent, depth - 1, subquery_depth, need_port and i == side, expected)
                             for i in range(count))
            return self.node(ir.UnionAll, branches)
        if operator == "conditional":
            side = self.rng.randrange(3) if need_port else -1
            condition = self.expression("boolean", parent, depth - 1, subquery_depth,
                                        need_port and side == 0)
            yes = self.rows(parent, depth - 1, subquery_depth, need_port and side == 1, expected)
            no = self.rows(parent, depth - 1, subquery_depth, need_port and side == 2, expected)
            return self.node(ir.ConditionalQuery, condition, yes, no)
        if operator == "limit":
            # An enclosing unordered LIMIT has no general equivalence proof.
            # Limits therefore occur only in target-independent row subtrees.
            body = self.rows(parent, depth - 1, subquery_depth, False, expected)
            return self.node(ir.RowLimit, body, self.rng.randint(0, 2))
        scope = self.new_scope(parent)
        pipeline = []
        if "MATCH" in self.clause_kinds and self.budget["clauses"] > 1 and depth > 0 and self.rng.random() < .4:
            for clause in self.matches(scope, max_clauses=self.budget["clauses"] - 1):
                scope = advance_scope(clause, scope)
                pipeline.append(clause)
        predicate_port = need_port and bool(pipeline) and self.rng.random() < .5
        if predicate_port:
            condition = self.expression("boolean", scope, depth, subquery_depth, True)
            pipeline[-1] = replace(pipeline[-1], where=condition)
        expression = self.expression(expected, scope, depth, subquery_depth, need_port and not predicate_port)
        self.scopes.append(scope)
        distinct = "RETURN DISTINCT" in self.clause_kinds and (
            "RETURN" not in self.clause_kinds or self.rng.random() < .25)
        return self.node(ir.Query, None, None, (self.node(ir.Alias, expression, "row_value"),),
                         pipeline=tuple(pipeline), distinct=distinct)


def _located(value, address=()):
    yield address, value
    if isinstance(value, (TerminalSlot, Port)):
        return
    if is_dataclass(value):
        for field in fields(value):
            yield from _located(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from _located(item, (*address, index))


def _validation_tree(value):
    if isinstance(value, TerminalSlot):
        return value.candidates[0]
    if isinstance(value, Port):
        if value.kind == "rows":
            return ir.Query(None, None, (ir.Alias(ir.Literal(1), "port_value"),))
        return ir.Literal(True if value.expected_type == "boolean" else 1)
    if is_dataclass(value):
        return replace(value, **{field.name: _validation_tree(getattr(value, field.name)) for field in fields(value)})
    if isinstance(value, tuple):
        return tuple(_validation_tree(item) for item in value)
    return value


def validate_abstract_types_and_scopes(host):
    """Validate symbolic hosts using typed terminal witnesses, never graph data."""
    try:
        ir.validate_query(_validation_tree(host.tree))
    except ir.QueryError as error:
        _failure("interface_mismatch", str(error))
    ports = [(address, value) for address, value in _located(host.tree) if isinstance(value, Port)]
    if len(ports) != 1 or len(host.ports) != 1 or host.ports[0].address != ports[0][0]:
        _failure("interface_mismatch", "A host requires exactly one accurately located insertion port")
    return host


def expand_host_structure(skeleton, grammar=None, capability_profile=None, budget=None, seed=0):
    """Expand every ordinary skeleton slot, retaining one typed insertion port."""
    derive_seed(seed, "validate")
    if not isinstance(skeleton, ClauseSkeleton):
        _failure("invalid_configuration", "Expected ClauseSkeleton")
    grammar, capabilities, limits = _grammar(grammar), _capabilities(capability_profile), _budget(budget)
    builder = _Builder(grammar, capabilities, limits, seed)
    scope, pipeline = builder.new_scope(), []
    stages = []
    if (not skeleton.clauses or any(not isinstance(item, dict) or "kind" not in item or "clause_id" not in item
                                    for item in skeleton.clauses)
            or skeleton.clauses[-1]["kind"] not in RETURN_CLAUSES):
        _failure("interface_mismatch", "A host clause skeleton must end in RETURN")
    if len(skeleton.clauses) > limits["clauses"]:
        _failure("generation_exhausted", "Clause skeleton exceeds the expansion clause budget")
    if any(item["kind"] not in builder.clause_kinds for item in skeleton.clauses):
        _failure("unsupported_fragment", "Clause skeleton uses a disabled host capability")
    for index, item in enumerate(skeleton.clauses[:-1]):
        kind = item["kind"]
        if kind in {"MATCH", "OPTIONAL MATCH"}:
            remaining = limits["clauses"] - len(pipeline) - (len(skeleton.clauses) - index - 1)
            expanded = builder.matches(scope, kind == "OPTIONAL MATCH", max_clauses=remaining)
        elif kind in {"WITH", "WITH DISTINCT"}:
            # Projection reads the incoming scope, not sibling aliases.
            retained = [name for name, _ in scope.visible_symbols if builder.rng.random() < .6]
            width = builder.rng.randint(1, limits["projections"])
            projections = [builder.node(ir.Variable, name) for name in retained[:width - 1]]
            alias = builder.node(ir.Alias, builder.expression("integer", scope, min(1, limits["expression_depth"])),
                                 builder.fresh("alias"))
            projections.append(alias)
            expanded = (builder.node(ir.With, tuple(projections), distinct=kind == "WITH DISTINCT"),)
        else:
            _failure("interface_mismatch", f"Unsupported skeleton clause {kind}")
        for part, clause in enumerate(expanded):
            incoming = scope
            scope = advance_scope(clause, scope)
            if builder.rng.random() < .3:
                condition = builder.expression("boolean", scope, min(1, limits["expression_depth"]))
                clause = replace(clause, where=condition)
            stages.append({"clause_id": f"{item['clause_id']}/{part}", "kind": kind,
                           "input_symbols": incoming.visible_symbols, "output_symbols": scope.visible_symbols})
            pipeline.append(clause)
    expected = grammar.get("return_type", builder.rng.choice(("integer", "boolean")))
    result = builder.expression(expected, scope, limits["expression_depth"], need_port=True)
    projections = [builder.node(ir.Alias, result, "value")]
    for index in range(1, builder.rng.randint(1, limits["projections"])):
        expression = builder.expression(builder.rng.choice(("integer", "boolean")), scope,
                                         limits["expression_depth"])
        projections.append(builder.node(ir.Alias, expression, f"value_{index}"))
    query = builder.node(ir.Query, None, None, tuple(projections), pipeline=tuple(pipeline),
                         distinct=skeleton.clauses[-1]["kind"] == "RETURN DISTINCT")
    builder.scopes.append(scope)
    found = [(address, value) for address, value in _located(query) if isinstance(value, Port)]
    if len(found) != 1:
        _failure("interface_mismatch", "Host expansion did not produce one insertion port")
    address, port = found[0]
    visible = builder.port_scope.visible_symbols
    descriptor = EmbeddingPort(port.port_id, address, port.kind, tuple(name for name, _ in visible),
                               visible, (), "row_count_or_existence" if port.kind == "rows" else "scalar_value",
                               limits["port_nodes"])
    host = HostAst(query, (descriptor,), tuple(builder.slots),
                   {"scopes": tuple(builder.scopes), "uses": tuple(builder.uses), "clauses": tuple(stages)},
                   skeleton, tuple(builder.trace), seed, budget=limits)
    return validate_abstract_types_and_scopes(host)


def generate_random_ast(grammar=None, capability_profile=None, budget=None, host_seed=0):
    """Public M1 boundary: no Pattern, graph, schema values or data-seed inputs."""
    limits = _budget(budget)
    skeleton = sample_clause_skeleton(grammar, capability_profile, limits["clauses"], derive_seed(host_seed, "clauses"))
    host = expand_host_structure(skeleton, grammar, capability_profile, limits, derive_seed(host_seed, "structure"))
    return replace(host, host_seed=host_seed)
