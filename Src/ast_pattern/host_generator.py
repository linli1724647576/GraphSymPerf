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
                  "subquery_depth": 3, "path_hops": 3, "port_nodes": 128}
CLAUSES = ("MATCH", "OPTIONAL MATCH", "WITH", "RETURN")
EXPRESSION_OPERATORS = ("leaf", "comparison", "and", "or", "not", "case",
                        "coalesce", "count", "exists")


def _failure(code, message, **evidence):
    raise StageFailure("M1", code, message, evidence=evidence)


def _budget(value):
    if value is None:
        return dict(DEFAULT_BUDGET)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_BUDGET):
        _failure("invalid_configuration", "Unknown host budget field")
    result = DEFAULT_BUDGET | value
    for key, amount in result.items():
        lower = 0 if key in {"expression_depth", "subquery_depth"} else 1
        if type(amount) is not int or amount < lower:
            _failure("invalid_configuration", f"{key} requires an integer >= {lower}")
    if result["clauses"] > 32 or result["expression_depth"] > 8 or result["subquery_depth"] > 8:
        _failure("invalid_configuration", "Host recursion or clause budget exceeds supported bounds")
    return result


def _grammar(value):
    if value is None:
        value = {}
    allowed = {"clauses", "clause_kinds", "port_kinds", "return_type", "expression_operators"}
    if not isinstance(value, dict) or set(value) - allowed:
        _failure("invalid_configuration", "Unknown host grammar field")
    result = dict(value)
    for key, choices in (("clause_kinds", CLAUSES), ("port_kinds", ("rows", "predicate", "scalar")),
                         ("expression_operators", EXPRESSION_OPERATORS)):
        options = result.get(key, choices)
        if not isinstance(options, (tuple, list)) or not options or any(x not in choices for x in options):
            _failure("invalid_configuration", f"Invalid {key}")
        result[key] = tuple(dict.fromkeys(options))
    if "RETURN" not in result["clause_kinds"]:
        _failure("invalid_configuration", "The host grammar requires RETURN")
    if result.get("return_type", "integer") not in {"integer", "boolean"}:
        _failure("unsupported_fragment", "Host return_type supports integer and boolean")
    if "clauses" in result:
        if not isinstance(result["clauses"], (tuple, list)):
            _failure("invalid_configuration", "Explicit host clauses require a sequence")
        kinds = tuple(result["clauses"])
        if (not kinds or kinds[-1] != "RETURN" or any(k not in CLAUSES for k in kinds)
                or "RETURN" in kinds[:-1] or kinds[0] == "OPTIONAL MATCH"):
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
    allowed = {"clause_kinds", "expression_operators", "subqueries", "optional_match", "with"}
    if set(profile) - allowed:
        _failure("invalid_configuration", "Unknown host capability field")
    result = dict(profile)
    for key in ("subqueries", "optional_match", "with"):
        if key in result and type(result[key]) is not bool:
            _failure("invalid_configuration", f"{key} requires a boolean")
    for key, choices in (("clause_kinds", CLAUSES), ("expression_operators", EXPRESSION_OPERATORS)):
        if key in result and (not isinstance(result[key], (tuple, list)) or not result[key]
                              or any(item not in choices for item in result[key])):
            _failure("invalid_configuration", f"Invalid capability {key}")
    return result


def _choices(grammar, capabilities):
    clauses = tuple(k for k in grammar["clause_kinds"]
                    if k in capabilities.get("clause_kinds", CLAUSES)
                    and (k != "OPTIONAL MATCH" or capabilities.get("optional_match", True))
                    and (k != "WITH" or capabilities.get("with", True)))
    operators = tuple(k for k in grammar["expression_operators"]
                      if k in capabilities.get("expression_operators", EXPRESSION_OPERATORS)
                      and (k not in {"count", "exists"} or capabilities.get("subqueries", True)))
    if "RETURN" not in clauses or not operators:
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
            candidates = [k for k in allowed if k != "RETURN"
                          and (k != "OPTIONAL MATCH" or index > 0)]
            if not candidates:
                break
            prefix.append(rng.choice(candidates))
        kinds = (*prefix, "RETURN")
    if len(kinds) > limit or any(k not in allowed for k in kinds):
        _failure("generation_exhausted", "Clause skeleton does not fit the capability/budget")
    slots = {"MATCH": ("matching", "predicate"), "OPTIONAL MATCH": ("matching", "predicate"),
             "WITH": ("projection", "predicate"), "RETURN": ("projection",)}
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

    def match(self, scope, optional=False):
        visible = [name for name, kind in scope.visible_symbols if kind.rstrip("?") == "node"]
        reuse = visible and self.rng.random() < .45
        left_name = self.rng.choice(visible) if reuse else self.fresh()
        labels = () if reuse else (self.slot("label", "string", (f"AstHost{left_name}",), scope),)
        left = self.node(ir.Node, left_name, labels)
        shape = "node"
        if self.rng.random() < .55:
            right_name = self.fresh()
            right = self.node(ir.Node, right_name,
                              (self.slot("label", "string", (f"AstHost{right_name}",), scope),))
            relation = self.node(ir.Relationship, type=self.slot("relationship_type", "string",
                                                                (f"AST_HOST_{right_name}",), scope))
            left = self.node(ir.Edge, left, relation, right, self.rng.choice(("out", "in", "undirected")))
            shape = "edge"
        clause = self.node(ir.Match, left, None, optional)
        self.trace.append({"production_id": f"match/{shape}", "scope_id": scope.scope_id,
                           "optional": optional, "imports": tuple(visible) if reuse else ()})
        return clause

    def leaf(self, expected, scope, depth):
        available = [name for name, kind in scope.visible_symbols if kind.rstrip("?") == expected]
        entities = [name for name, kind in scope.visible_symbols if kind.rstrip("?") in {"node", "relationship"}]
        if available and self.rng.random() < .3:
            name = self.rng.choice(available)
            self.uses.append({"scope_id": scope.scope_id, "name": name, "kind": "scalar"})
            return self.node(ir.Variable, name)
        if entities and (expected != "boolean" or depth > 0) and self.rng.random() < .35:
            name = self.rng.choice(entities)
            key = self.slot("property_key", expected, ("host_number" if expected == "integer" else "host_flag",), scope)
            prop = self.node(ir.Property, name, key)
            self.uses.append({"scope_id": scope.scope_id, "name": name, "kind": "property"})
            if expected == "boolean":
                return self.node(ir.Comparison, prop, "=", self.literal("boolean", scope))
            return prop
        return self.literal(expected, scope)

    def port_fits(self, expected, depth, subquery_depth):
        """Whether a port has a type-correct path within the remaining budgets."""
        can_query = (subquery_depth < self.budget["subquery_depth"]
                     and ("count" if expected == "integer" else "exists") in self.operators)
        if self.port_kind == "rows":
            return can_query
        port_type = "boolean" if self.port_kind == "predicate" else "integer"
        if expected == port_type:
            return True
        adapter = "case" if expected == "integer" else "comparison"
        return can_query or depth > 0 and adapter in self.operators

    def expression(self, expected, scope, depth, subquery_depth=0, need_port=False):
        candidates = ["leaf"]
        if depth > 0:
            candidates += ["case", "coalesce"]
            candidates += ["comparison", "and", "or", "not"] if expected == "boolean" else []
        if subquery_depth < self.budget["subquery_depth"]:
            candidates += ["count" if expected == "integer" else "exists"]
        candidates = [operator for operator in candidates if operator in self.operators]
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
            valid_children = {"comparison": ("integer",), "and": ("boolean",), "or": ("boolean",),
                              "not": ("boolean",), "case": ("boolean", expected), "coalesce": (expected,)}
            candidates = [op for op in candidates if op not in valid_children or any(
                self.port_fits(kind, child_depth, subquery_depth) for kind in valid_children[op])]
            if self.port_kind != "rows" and ((self.port_kind == "predicate") == (expected == "boolean")):
                candidates.append("port")
            if not candidates:
                return self.expression(expected, scope, 0, subquery_depth, True)
        operator = self.rng.choice(candidates)
        self.trace.append({"production_id": f"expression/{operator}", "expected_type": expected,
                           "scope_id": scope.scope_id, "depth": depth, "contains_port": need_port})
        child_depth = max(0, depth - 1)
        if operator == "port":
            return self.port(scope)
        if operator == "leaf":
            return self.leaf(expected, scope, depth)
        if operator in {"count", "exists"}:
            if need_port and self.port_kind == "rows" and (
                    depth == 0 or subquery_depth + 1 >= self.budget["subquery_depth"] or self.rng.random() < .7):
                body = self.port(scope)
            else:
                body = self.rows(scope, child_depth, subquery_depth + 1, need_port)
            return self.node(ir.Count if operator == "count" else ir.Exists, body=body)
        if operator == "comparison":
            left_port = need_port and self.rng.choice((True, False))
            left = self.expression("integer", scope, child_depth, subquery_depth, left_port)
            right = self.expression("integer", scope, child_depth, subquery_depth, need_port and not left_port)
            return self.node(ir.Comparison, left, self.rng.choice(("=", "<>")), right)
        if operator in {"and", "or"}:
            side = self.rng.randrange(2) if need_port else -1
            operands = tuple(self.expression("boolean", scope, child_depth, subquery_depth, need_port and index == side)
                             for index in range(2))
            return self.node(ir.Boolean, operator.upper(), operands)
        if operator == "not":
            return self.node(ir.Negation, self.expression("boolean", scope, child_depth, subquery_depth, need_port))
        if operator == "case":
            # The port may occur in the guard or either same-typed branch.
            sides = [index for index, kind in enumerate(("boolean", expected, expected))
                     if self.port_fits(kind, child_depth, subquery_depth)]
            side = self.rng.choice(sides) if need_port else -1
            condition = self.expression("boolean", scope, child_depth, subquery_depth, need_port and side == 0)
            yes = self.expression(expected, scope, child_depth, subquery_depth, need_port and side == 1)
            no = self.expression(expected, scope, child_depth, subquery_depth, need_port and side == 2)
            return self.node(ir.Case, condition, yes, no)
        side = self.rng.randrange(2) if need_port else -1
        arguments = tuple(self.expression(expected, scope, child_depth, subquery_depth, need_port and side == index)
                          for index in range(2))
        return self.node(ir.Call, "coalesce", arguments)

    def rows(self, parent, depth, subquery_depth, need_port=False):
        scope = self.new_scope(parent)
        pipeline = []
        if "MATCH" in self.clause_kinds and depth > 0 and self.rng.random() < .4:
            clause = self.match(scope)
            scope = advance_scope(clause, scope)
            pipeline.append(clause)
        expected = ("boolean" if self.port_kind == "predicate" else "integer") if need_port and self.port_kind != "rows" else self.rng.choice(("integer", "boolean"))
        expression = self.expression(expected, scope, depth, subquery_depth, need_port)
        self.scopes.append(scope)
        return self.node(ir.Query, None, None, (self.node(ir.Alias, expression, self.fresh("sub")),),
                         pipeline=tuple(pipeline))


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
            or skeleton.clauses[-1]["kind"] != "RETURN"):
        _failure("interface_mismatch", "A host clause skeleton must end in RETURN")
    if any(item["kind"] not in builder.clause_kinds for item in skeleton.clauses):
        _failure("unsupported_fragment", "Clause skeleton uses a disabled host capability")
    for item in skeleton.clauses[:-1]:
        kind = item["kind"]
        incoming = scope
        if kind in {"MATCH", "OPTIONAL MATCH"}:
            clause = builder.match(scope, kind == "OPTIONAL MATCH")
        elif kind == "WITH":
            # Projection reads the incoming scope, not sibling aliases.
            projections = [builder.node(ir.Variable, name) for name, _ in scope.visible_symbols if builder.rng.random() < .6]
            alias = builder.node(ir.Alias, builder.expression("integer", scope, min(1, limits["expression_depth"])),
                                 builder.fresh("alias"))
            projections.append(alias)
            clause = builder.node(ir.With, tuple(projections))
        else:
            _failure("interface_mismatch", f"Unsupported skeleton clause {kind}")
        scope = advance_scope(clause, scope)
        if builder.rng.random() < .3:
            condition = builder.expression("boolean", scope, min(1, limits["expression_depth"]))
            clause = replace(clause, where=condition)
        stages.append({"clause_id": item["clause_id"], "kind": kind,
                       "input_symbols": incoming.visible_symbols, "output_symbols": scope.visible_symbols})
        pipeline.append(clause)
    expected = grammar.get("return_type", builder.rng.choice(("integer", "boolean")))
    result = builder.expression(expected, scope, limits["expression_depth"], need_port=True)
    query = builder.node(ir.Query, None, None, (builder.node(ir.Alias, result, "value"),), pipeline=tuple(pipeline))
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
