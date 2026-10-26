"""Lexical bindings and structural rewrite sites for the restricted query IR.

Addresses name dataclass fields and tuple indices. They survive serialization
and never depend on Python object identity or rendered variable names.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace

import ir


Address = tuple[str | int, ...]


def _node_id(address: Address) -> str:
    return "root" + "".join(f"/{part}" for part in address)


def node_at(value, address: Address):
    """Resolve a structural address, rejecting invalid fields and indices."""
    try:
        for part in address:
            if type(part) is int and isinstance(value, tuple) and 0 <= part < len(value):
                value = value[part]
            elif isinstance(part, str) and is_dataclass(value) and part in {item.name for item in fields(value)}:
                value = getattr(value, part)
            else:
                raise ir.QueryError(f"Invalid structural address: {address!r}")
    except (TypeError, AttributeError) as exc:
        raise ir.QueryError(f"Invalid structural address: {address!r}") from exc
    return value


def replace_at(value, address: Address, replacement):
    """Replace exactly one addressed subtree of an immutable IR value."""
    node_at(value, address)
    if not address:
        return replacement
    part, *tail = address
    if type(part) is int:
        items = list(value)
        items[part] = replace_at(items[part], tuple(tail), replacement)
        return tuple(items)
    return replace(value, **{part: replace_at(getattr(value, part), tuple(tail), replacement)})


@dataclass(frozen=True)
class P3Site:
    address: Address
    scope_id: str
    endpoint_symbols: dict[str, str]
    path_symbol: str | None
    guards: dict

    @property
    def paper_pattern(self) -> str:
        return "P3"

    @property
    def form(self) -> str:
        return "bounded_path"

    @property
    def legacy_pattern(self) -> str:
        return "P3"

    @property
    def path(self) -> Address:
        return self.address

    def to_dict(self) -> dict:
        return {"address": list(self.address), "node_id": _node_id(self.address),
                "scope_id": self.scope_id, "endpoint_symbols": dict(self.endpoint_symbols),
                "path_symbol": self.path_symbol, "guards": self.guards,
                "paper_pattern": self.paper_pattern, "form": self.form,
                "legacy_pattern": self.legacy_pattern}


@dataclass(frozen=True)
class MutationSite:
    address: Address
    scope_id: str
    paper_pattern: str
    form: str | None
    guards: dict

    @property
    def path(self) -> Address:
        return self.address

    @property
    def legacy_pattern(self) -> str:
        return {("P10", "shared_anchor"): "P11", ("P10", "chain"): "P12",
                ("P11", "optional_branches"): "P13", ("P12", "union_all"): "P14",
                ("P13", "case_dual"): "P15"}.get((self.paper_pattern, self.form), self.paper_pattern)

    def to_dict(self) -> dict:
        return {"address": list(self.address), "node_id": _node_id(self.address),
                "scope_id": self.scope_id, "paper_pattern": self.paper_pattern,
                "form": self.form, "legacy_pattern": self.legacy_pattern, "guards": self.guards}


@dataclass(frozen=True)
class DependencyAnalysis:
    scopes: tuple[dict, ...]
    symbols: tuple[dict, ...]
    uses: tuple[dict, ...]
    _sites: tuple[dict, ...] = ()
    _expression_sites: tuple[dict, ...] = ()
    _query_sites: tuple[dict, ...] = ()
    row_uses: tuple[dict, ...] = ()
    _environments: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {"scopes": list(self.scopes), "symbols": list(self.symbols), "uses": list(self.uses),
                "row_uses": list(self.row_uses)}


def _with(value) -> bool:
    return isinstance(value, getattr(ir, "With", ()))


def _row_limit(value) -> bool:
    return isinstance(value, getattr(ir, "RowLimit", ()))


def _located(value, address=()):
    yield address, value
    if isinstance(value, tuple):
        for index, child in enumerate(value):
            yield from _located(child, (*address, index))
    elif is_dataclass(value):
        for field in fields(value):
            yield from _located(getattr(value, field.name), (*address, field.name))


def _clause_addresses(query, address):
    if getattr(query, "pipeline", ()):
        return tuple((clause, address + ("pipeline", index)) for index, clause in enumerate(query.pipeline))
    result = []
    if query.pattern is not None:
        result.append((ir.Match(query.pattern, query.where), address))
    result.extend((clause, address + ("matches", index)) for index, clause in enumerate(query.matches))
    return tuple(result)


def _node_local(expression, binder: str) -> bool:
    if isinstance(expression, ir.Literal):
        return True
    if isinstance(expression, ir.Property):
        return expression.variable == binder
    if isinstance(expression, (ir.Comparison, ir.Concat)):
        children = expression.left, expression.right
    elif isinstance(expression, ir.Boolean):
        children = expression.terms
    elif isinstance(expression, ir.Negation):
        children = (expression.expression,)
    elif isinstance(expression, ir.Case):
        children = expression.condition, expression.then, expression.otherwise
    elif isinstance(expression, ir.Call) and expression.name == "coalesce":
        children = expression.args
    else:
        return False
    return all(_node_local(child, binder) for child in children)


class _Analyzer:
    def __init__(self):
        self.scopes = []
        self.symbols = []
        self.uses = []
        self.sites = []
        self.expression_sites = []
        self.query_sites = []
        self.row_uses = []
        self.environments = []
        self.symbol_map = {}
        self.scope_map = {}

    def scope(self, address, parent, kind):
        scope_id = _node_id(address) + ":" + kind
        record = {"scope_id": scope_id, "node_id": _node_id(address), "kind": kind,
                  "parent_scope": parent, "imports": [], "definitions": [], "exports": {},
                  "input_bindings": {}}
        self.scopes.append(record)
        self.scope_map[scope_id] = record
        return scope_id

    def define(self, name, kind, address, scope, sources=()):
        symbol_id = f"{scope}::{name}@{_node_id(address)}"
        record = {"symbol_id": symbol_id, "name": name, "kind": kind, "scope_id": scope,
                  "definition": _node_id(address), "sources": sorted(set(sources))}
        self.symbols.append(record)
        self.symbol_map[symbol_id] = record
        self.scope_map[scope]["definitions"].append(symbol_id)
        self.import_sources(scope, sources)
        return symbol_id

    def import_sources(self, scope, sources):
        imports = self.scope_map[scope]["imports"]
        for symbol_id in sorted(set(sources)):
            if self.symbol_map[symbol_id]["scope_id"] != scope and symbol_id not in imports:
                imports.append(symbol_id)

    def consume_rows(self, producer, consumer, address, **metadata):
        self.row_uses.append({"producer": producer, "consumer": consumer,
                              "consumer_node": _node_id(address), "address": list(address),
                              "columns": dict(self.scope_map[producer]["exports"]), **metadata})

    def use(self, name, env, address, scope, observation="value"):
        if name not in env:
            raise ir.QueryError(f"Unbound variable: {name}")
        symbol_id = env[name]
        self.uses.append({"node_id": _node_id(address), "address": list(address),
                          "scope_id": scope, "symbol_id": symbol_id, "name": name,
                          "observation": observation})
        if self.symbol_map[symbol_id]["scope_id"] != scope:
            imports = self.scope_map[scope]["imports"]
            if symbol_id not in imports:
                imports.append(symbol_id)
        return symbol_id

    def pattern(self, pattern, address, env, scope):
        self.environments.append({"address": address, "bindings": dict(env), "scope_id": scope})
        env = dict(env)
        if isinstance(pattern, ir.Node):
            roles = ((pattern.variable, "node", address + ("variable",)),)
        else:
            roles = [(pattern.left.variable, "node", address + ("left", "variable")),
                     (pattern.right.variable, "node", address + ("right", "variable"))]
            if pattern.relationship.variable is not None:
                roles.append((pattern.relationship.variable, "relationship", address + ("relationship", "variable")))
            if pattern.path_variable is not None:
                roles.append((pattern.path_variable, "path", address + ("path_variable",)))
        for name, kind, location in roles:
            if name in env:
                self.use(name, env, location, scope, "pattern_binding")
            else:
                env[name] = self.define(name, kind, location, scope)
        if isinstance(pattern, ir.Edge):
            self.sites.append({"address": address, "edge": pattern, "scope_id": scope,
                               "bindings": dict(env)})
        return env

    def expression(self, expression, address, env, scope, observation="value"):
        self.environments.append({"address": address, "bindings": dict(env), "scope_id": scope})
        if expression is None or isinstance(expression, ir.Literal):
            return set()
        if isinstance(expression, ir.Boolean) and expression.operator == "AND":
            self.expression_sites.append({"address": address, "expression": expression,
                                          "scope_id": scope, "bindings": dict(env)})
        if isinstance(expression, ir.Property):
            return {self.use(expression.variable, env, address, scope, "property")}
        if isinstance(expression, ir.Variable):
            return {self.use(expression.name, env, address, scope, observation)}
        if isinstance(expression, ir.Alias):
            return self.expression(expression.expression, address + ("expression",), env, scope)
        if isinstance(expression, (ir.Count, ir.Exists)):
            body = getattr(expression, "body", None)
            if body is not None:
                child_scope = self.query(body, address + ("body",), env, scope)
                self.consume_rows(child_scope, type(expression).__name__, address)
                refs = set(self.scope_map[child_scope]["imports"])
                self.import_sources(scope, refs)
                return refs
            child_scope = self.scope(address, scope, type(expression).__name__.lower())
            inner = self.pattern(expression.pattern, address + ("pattern",), env, child_scope)
            refs = self.expression(expression.where, address + ("where",), inner, child_scope)
            self.import_sources(child_scope, refs)
            refs = set(self.scope_map[child_scope]["imports"])
            self.import_sources(scope, refs)
            return refs
        if isinstance(expression, ir.All):
            local_all = (isinstance(expression.items, ir.Call) and expression.items.name == "nodes"
                         and len(expression.items.args) == 1
                         and isinstance(expression.items.args[0], ir.Variable)
                         and _node_local(expression.predicate, expression.variable))
            refs = self.expression(expression.items, address + ("items",), env, scope,
                                   "all_nodes_local" if local_all else "value")
            binder_scope = self.scope(address + ("predicate",), scope, "all")
            inner = dict(env)
            inner[expression.variable] = self.define(expression.variable, "node" if local_all else "scalar",
                                                      address + ("variable",), binder_scope)
            self.expression(expression.predicate, address + ("predicate",), inner, binder_scope)
            return refs | (set(self.scope_map[binder_scope]["imports"]))
        if isinstance(expression, ir.Call):
            invariant = expression.name == "length" and len(expression.args) == 1
            refs = set()
            for index, arg in enumerate(expression.args):
                kind = "length" if invariant else observation if expression.name == "nodes" else "value"
                refs |= self.expression(arg, address + ("args", index), env, scope, kind)
            return refs
        refs = set()
        if is_dataclass(expression):
            for field in fields(expression):
                value = getattr(expression, field.name)
                if isinstance(value, tuple):
                    for index, child in enumerate(value):
                        if is_dataclass(child):
                            refs |= self.expression(child, address + (field.name, index), env, scope)
                elif is_dataclass(value):
                    refs |= self.expression(value, address + (field.name,), env, scope)
        return refs

    def projections(self, items, address, env, scope, *, passthrough=False):
        exported = {}
        for index, item in enumerate(items):
            location = address + (index,)
            if isinstance(item, ir.Variable) and passthrough:
                exported[item.name] = self.use(item.name, env, location, scope, "passthrough")
                continue
            refs = self.expression(item, location, env, scope)
            name = item.name if isinstance(item, ir.Alias) else (
                item.name if isinstance(item, ir.Variable) else
                f"{item.variable}.{item.key}" if isinstance(item, ir.Property) else f"column_{index + 1}")
            kind = 'scalar'
            if passthrough and isinstance(item, ir.Alias) and isinstance(item.expression, ir.Variable):
                kind = self.symbol_map[env[item.expression.name]]['kind']
            exported[name] = self.define(name, kind, location, scope, refs)
            self.symbol_map[exported[name]]["producer_node"] = _node_id(
                location + ("expression",) if isinstance(item, ir.Alias) else location)
        return exported

    def query(self, query, address=(), outer=None, parent=None):
        self.environments.append({"address": address, "bindings": dict(outer or {})})
        if _row_limit(query):
            scope = self.scope(address, parent, "row_limit")
            self.scope_map[scope]["input_bindings"] = dict(outer or {})
            child = self.query(query.body, address + ("body",), outer, scope)
            self.consume_rows(child, "RowLimit", address, limit=query.limit)
            self.scope_map[scope]["exports"] = dict(self.scope_map[child]["exports"])
            self.import_sources(scope, self.scope_map[child]["imports"])
            return scope
        if isinstance(query, ir.UnionAll):
            scope = self.scope(address, parent, "union_all")
            self.scope_map[scope]["input_bindings"] = dict(outer or {})
            outputs = {}
            for index, branch in enumerate(query.branches):
                child = self.query(branch, address + ("branches", index), outer, scope)
                self.consume_rows(child, "UnionAll", address, branch=index)
                self.import_sources(scope, self.scope_map[child]["imports"])
                for name, symbol in self.scope_map[child]["exports"].items():
                    outputs.setdefault(name, []).append(symbol)
            for index, (name, sources) in enumerate(outputs.items()):
                # Branch outputs are internal producers, not lexical imports.
                output = self.define(name, "scalar", address + ("outputs", index), scope)
                self.symbol_map[output]["sources"] = sources
                self.symbol_map[output]["producer_node"] = _node_id(address)
                self.scope_map[scope]["exports"][name] = output
            return scope
        if isinstance(query, ir.ConditionalQuery):
            scope = self.scope(address, parent, "conditional")
            self.scope_map[scope]["input_bindings"] = dict(outer or {})
            self.expression(query.condition, address + ("condition",), outer or {}, scope)
            outputs = {}
            for branch in ("then", "otherwise"):
                child = self.query(getattr(query, branch), address + (branch,), outer, scope)
                self.consume_rows(child, "ConditionalQuery", address, branch=branch)
                self.import_sources(scope, self.scope_map[child]["imports"])
                for name, symbol in self.scope_map[child]["exports"].items():
                    outputs.setdefault(name, []).append(symbol)
            for index, (name, sources) in enumerate(outputs.items()):
                symbol = self.define(name, "scalar", address + ("outputs", index), scope)
                self.symbol_map[symbol]["sources"] = sources
                self.symbol_map[symbol]["producer_node"] = _node_id(address)
                self.scope_map[scope]["exports"][name] = symbol
            return scope
        scope = self.scope(address, parent, "query")
        self.scope_map[scope]["input_bindings"] = dict(outer or {})
        query_scope = scope
        env = dict(outer or {})
        query_site = {"address": address, "query": query, "stages": []}
        self.query_sites.append(query_site)
        for clause, location in _clause_addresses(query, address):
            if _with(clause):
                next_scope = self.scope(location, scope, "with")
                self.scope_map[next_scope]["input_bindings"] = dict(env)
                self.environments.append({"address": location, "bindings": dict(env), "scope_id": next_scope})
                env = self.projections(clause.projections, location + ("projections",), env, next_scope,
                                       passthrough=True)
                self.scope_map[next_scope]["exports"] = dict(env)
                self.expression(clause.where, location + ("where",), env, next_scope)
                scope = next_scope
                query_site["stages"].append({"clause": clause, "address": location})
            else:
                before = dict(env)
                self.environments.append({"address": location, "bindings": dict(env), "scope_id": scope})
                env = self.pattern(clause.pattern, location + ("pattern",), env, scope)
                refs = self.expression(clause.where, location + ("where",), env, scope)
                query_site["stages"].append({"clause": clause, "address": location,
                                             "scope_id": scope, "before": before,
                                             "after": dict(env), "where_symbols": refs})
        exports = self.projections(query.returns, address + ("returns",), env, scope)
        self.scope_map[query_scope]["exports"] = exports
        # A query imports only symbols defined outside all its local stages.
        prefix = _node_id(address)
        imports = self.scope_map[query_scope]["imports"]
        for record in self.uses:
            if record["node_id"].startswith(prefix + "/"):
                symbol = self.symbol_map[record["symbol_id"]]
                if not (symbol["definition"] == prefix or symbol["definition"].startswith(prefix + "/")):
                    if symbol["symbol_id"] not in imports:
                        imports.append(symbol["symbol_id"])
        return query_scope


def analyze_dependencies(query) -> DependencyAnalysis:
    """Validate and resolve definitions/uses, including nested lexical scopes."""
    ir.validate_query(query)
    analyzer = _Analyzer()
    analyzer.query(query)
    for symbol in analyzer.symbols:
        symbol["consumers"] = [use["node_id"] for use in analyzer.uses
                               if use["symbol_id"] == symbol["symbol_id"]]
    return DependencyAnalysis(tuple(analyzer.scopes), tuple(analyzer.symbols), tuple(analyzer.uses),
                              tuple(analyzer.sites), tuple(analyzer.expression_sites), tuple(analyzer.query_sites),
                              tuple(analyzer.row_uses), tuple(analyzer.environments))


def site_environment(query, address):
    """Return inferred input kinds at this exact expression/row interface."""
    analysis = analyze_dependencies(query)
    symbols = {symbol["symbol_id"]: symbol for symbol in analysis.symbols}
    available = next((item["bindings"] for item in analysis._environments if item["address"] == tuple(address)), None)
    if available is None:
        scope = _scope_for(analysis, tuple(address))
        available = next(record["input_bindings"] for record in analysis.scopes if record["scope_id"] == scope)
    return {name: symbols[symbol]["kind"] if symbols[symbol]["kind"] != "scalar" else "unknown"
            for name, symbol in available.items()}


def _walk(value):
    yield value
    if isinstance(value, tuple):
        for item in value:
            yield from _walk(item)
    elif is_dataclass(value):
        for field in fields(value):
            yield from _walk(getattr(value, field.name))


def find_p3_sites(query) -> tuple[P3Site, ...]:
    """Find bounded undirected paths whose bound path has invariant consumers."""
    analysis = analyze_dependencies(query)
    if any((isinstance(value, ir.Query) and value.limit is not None) or _row_limit(value)
           for value in _walk(query)):
        return ()
    sites = []
    for item in analysis._sites:
        edge = item["edge"]
        rel = edge.relationship
        if (edge.direction != "undirected" or type(rel.min_hops) is not int
                or type(rel.max_hops) is not int or rel.variable is not None or edge.left == edge.right):
            continue
        bindings = item["bindings"]
        path_symbol = bindings.get(edge.path_variable) if edge.path_variable is not None else None
        consumers = [use for use in analysis.uses if use["symbol_id"] == path_symbol]
        if any(use["observation"] not in {"length", "all_nodes_local", "passthrough"} for use in consumers):
            continue
        sites.append(P3Site(item["address"], item["scope_id"],
                            {"left": bindings[edge.left.variable], "right": bindings[edge.right.variable]},
                            path_symbol, {"bounded": [rel.min_hops, rel.max_hops],
                                          "undirected": True, "endpoint_bindings_preserved": True,
                                          "endpoint_names": {"left": edge.left.variable, "right": edge.right.variable},
                                          "path_consumers": consumers, "reversal_invariant": True}))
    return tuple(sites)


def _pure(value) -> bool:
    """The checked read-only fragment has no volatile or partial scalar calls."""
    if isinstance(value, ir.Query) and value.limit is not None:
        return False
    if isinstance(value, ir.Call) and value.name not in {"length", "coalesce", "range", "size", "nodes"}:
        return False
    if is_dataclass(value):
        return all(_pure(getattr(value, field.name)) for field in fields(value))
    if isinstance(value, tuple):
        return all(_pure(item) for item in value)
    return True


def _streamable(value) -> bool:
    """The supported UNION branches have no full-input/blocking operators."""
    if _row_limit(value):
        return False
    if isinstance(value, ir.UnionAll):
        return all(_streamable(branch) for branch in value.branches)
    if not isinstance(value, ir.Query) or value.limit is not None or value.distinct:
        return False
    return (_pure(value)
            and not any(isinstance(item, ir.Count) or _row_limit(item)
                        or _with(item) and item.distinct for item in _walk(value)))


def _constant_columns(value):
    if isinstance(value, ir.UnionAll):
        alternatives = [_constant_columns(branch) for branch in value.branches]
        return alternatives[0] if alternatives and alternatives[0] is not None and all(
            item == alternatives[0] for item in alternatives[1:]) else None
    if not isinstance(value, ir.Query):
        return None
    result = []
    names = ir.projection_names(value)
    for name, item in zip(names, value.returns):
        expression = item.expression if isinstance(item, ir.Alias) else item
        if not isinstance(expression, ir.Literal):
            return None
        result.append((name, expression))
    return tuple(result)


def _short_circuit_sites(query, paper_pattern, form, analysis):
    expected_form = {"S6": "bounded_demand", "S8": "existential_witness"}[paper_pattern]
    if form not in (None, expected_form):
        return ()
    sites = []
    for address, envelope in _located(query):
        is_limit = paper_pattern == "S6" and _row_limit(envelope) and envelope.limit > 0
        is_exists = paper_pattern == "S8" and isinstance(envelope, ir.Exists)
        if not (is_limit or is_exists):
            continue
        union = getattr(envelope, "body", None)
        if not isinstance(union, ir.UnionAll) or len(union.branches) != 2 or not _streamable(union):
            continue
        constants = _constant_columns(union) if is_limit else None
        if is_limit and constants is None:
            continue
        # A second limit in an enclosing consumer needs its own order proof.
        if any((isinstance(value, ir.Query) and value.limit is not None)
               or _row_limit(value) and location != address
               for location, value in _located(query)):
            continue
        union_address = address + ("body",)
        scope = next(record for record in analysis.scopes if record["node_id"] == _node_id(union_address))
        sites.append(MutationSite(union_address + ("branches", 1), scope["scope_id"], paper_pattern,
                                  expected_form,
                                  {"envelope_site": list(address), "union_site": list(union_address),
                                   "witness_site": list(union_address + ("branches", 0)),
                                   "required_rows": envelope.limit if is_limit else 1,
                                   "streamable": True,
                                   "constant_columns": [{"name": name, "value": literal.value,
                                                         "type": type(literal.value).__name__}
                                                        for name, literal in constants] if constants is not None else None,
                                   "witness_condition": "requires_checked_state_evidence"}))
    return tuple(sites)


def _existing_sites(query, paper_pattern: str, form: str | None = None) -> tuple[P3Site | MutationSite, ...]:
    """Find checked local rewrites using paper numbering, separate from legacy IDs.

    Paper P10/shared_anchor is the former code P11 mandatory-branch rule.
    Paper P11 (OPTIONAL branches) is not implemented by this entry point.
    """
    if not isinstance(paper_pattern, str) or form is not None and not isinstance(form, str):
        raise ir.QueryError("Paper pattern and form require string identifiers")
    if paper_pattern in {"S6", "S8"}:
        return _short_circuit_sites(query, paper_pattern, form, analyze_dependencies(query))
    if paper_pattern == "P3" and form in (None, "bounded_path"):
        return find_p3_sites(query)
    if paper_pattern not in {"P4", "P10"} or (paper_pattern == "P4" and form not in (None, "and")):
        return ()
    if paper_pattern == "P10" and form not in (None, "shared_anchor"):
        return ()
    analysis = analyze_dependencies(query)
    sites = []
    if any((isinstance(value, ir.Query) and value.limit is not None) or _row_limit(value)
           for value in _walk(query)):
        return ()
    if paper_pattern == "P4":
        for item in analysis._expression_sites:
            expression = item["expression"]
            if _pure(expression) and expression.terms != tuple(reversed(expression.terms)):
                sites.append(MutationSite(item["address"], item["scope_id"], "P4", "and",
                                          {"operator": "AND", "arity": len(expression.terms),
                                           "pure": True, "three_valued_commutativity": True}))
        return tuple(sites)
    for item in analysis._query_sites:
        for index, (first, second) in enumerate(zip(item["stages"], item["stages"][1:])):
            left, right = first["clause"], second["clause"]
            if (not isinstance(left, ir.Match) or not isinstance(right, ir.Match)
                    or left.optional or right.optional or not isinstance(left.pattern, ir.Edge)
                    or not isinstance(right.pattern, ir.Edge) or left == right
                    or first["scope_id"] != second["scope_id"] or not _pure((left, right))):
                continue
            left_names, right_names = set(ir.pattern_variables(left.pattern)), set(ir.pattern_variables(right.pattern))
            incoming = set(first["before"])
            shared = left_names & right_names
            if (len(shared) != 1 or not shared <= incoming
                    or left_names & incoming != shared or right_names & incoming != shared):
                continue
            anchor, = shared
            anchor_symbol = first["before"][anchor]
            symbol = next(value for value in analysis.symbols if value["symbol_id"] == anchor_symbol)
            if symbol["kind"] != "node":
                continue
            left_symbols = {first["after"][name] for name in left_names}
            right_symbols = {second["after"][name] for name in right_names}
            if (not first["where_symbols"] <= left_symbols or not second["where_symbols"] <= right_symbols
                    or left_symbols & right_symbols != {anchor_symbol}):
                continue
            sites.append(MutationSite(first["address"], first["scope_id"], "P10", "shared_anchor",
                                      {"query_address": list(item["address"]), "clause_indices": [index, index + 1],
                                       "anchor_symbol": anchor_symbol, "anchor_name": anchor,
                                       "left_new_symbols": sorted(left_symbols - {anchor_symbol}),
                                       "right_new_symbols": sorted(right_symbols - {anchor_symbol}),
                                       "predicates_branch_local": True, "mandatory": True,
                                       "per_binding_bag_product": True}))
    return tuple(sites)


_FORMS = {"P1": "edge_mirror", "P2": "inverse_edge", "P3": "bounded_path", "P4": "and", "P5": "or",
          "P6": "equality", "P7": "labels", "P8": "types", "P9": "predicate_placement",
          "P11": "optional_branches", "P12": "union_all", "P13": "case_dual",
          "S1": "absorbing_or", "S2": "absorbing_and", "S3": "conditional", "S4": "case_guard",
          "S5": "empty_limit", "S6": "bounded_demand", "S7": "coalesce", "S8": "existential_witness",
          "S9": "empty_input", "S10": "membership"}


def _scope_for(analysis, address):
    # Structural ancestry uses addresses, never variable-name equality.
    for length in range(len(address), -1, -1):
        prefix = address[:length]
        environment = next((item for item in analysis._environments if item["address"] == prefix and "scope_id" in item), None)
        if environment is not None:
            return environment["scope_id"]
        scope = next((record for record in reversed(analysis.scopes) if record["node_id"] == _node_id(prefix)), None)
        if scope is not None:
            return scope["scope_id"]
    return "root:query"


def _generic_sc_sites(query, pattern, analysis, located):
    sites = []
    def add(address, envelope, premises=(), **guards):
        sites.append(MutationSite(address, _scope_for(analysis, address), pattern, _FORMS[pattern],
                                  {"envelope_site": list(envelope), "premises": list(premises), **guards}))
    def premise(address, relation):
        return {"address": list(address), "relation": relation}
    for address, value in located:
        if not _pure(value) and not (_row_limit(value) or isinstance(value, ir.Query)):
            continue
        if (pattern in {"S1", "S2"} and isinstance(value, ir.Boolean) and len(value.terms) == 2
                and value.operator == ("OR" if pattern == "S1" else "AND")):
            add((*address, "terms", 1), address, (premise((*address, "terms", 0), "true" if pattern == "S1" else "false"),))
        elif pattern == "S3" and isinstance(value, ir.ConditionalQuery):
            add((*address, "otherwise"), address, (premise((*address, "condition"), "true"),))
        elif pattern == "S4" and isinstance(value, ir.Case):
            add((*address, "otherwise"), address, (premise((*address, "condition"), "true"),))
        elif pattern == "S7" and isinstance(value, ir.Call) and value.name == "coalesce":
            for index in range(1, len(value.args)):
                prefix, decisive = value.args[:index - 1], value.args[index - 1]
                if (any(isinstance(part, ir.Literal) and part.value is not None for part in prefix)
                        or isinstance(decisive, ir.Literal) and decisive.value is None):
                    continue
                facts = [premise((*address, 'args', i), 'null') for i in range(index - 1)]
                facts.append(premise((*address, 'args', index - 1), 'nonnull'))
                add((*address, 'args', index), address, facts)
        elif pattern == "S5" and _row_limit(value) and value.limit == 0:
            add((*address, "body"), address, (), zero_limit=True)
        elif pattern == "S5" and isinstance(value, ir.Query) and value.limit == 0 and value.pattern is not None:
            add((*address, "pattern"), address, (), zero_limit=True)
        elif pattern == "S10" and isinstance(value, ir.Query) and value.distinct:
            for clause, location in _clause_addresses(value, address):
                predicate = clause.where
                if not (isinstance(clause, ir.Match) and not clause.optional and isinstance(predicate, ir.Boolean)
                        and predicate.operator == "AND" and len(predicate.terms) == 2):
                    continue
                membership = predicate.terms[1]
                positive = membership.expression if isinstance(membership, ir.Negation) else membership
                if not isinstance(positive, ir.Exists):
                    continue
                predicates = [positive.where] if positive.where is not None else []
                if positive.body is not None:
                    predicates += [clause.where for part in _walk(positive.body) if isinstance(part, ir.Query)
                                   for clause in ir.clauses(part) if clause.where is not None]
                nullsafe = bool(predicates) and all(_total_boolean(part) for part in predicates)
                if not nullsafe:
                    continue
                where_address = (*location, "where")
                membership_address = (*where_address, "terms", 1, *(("expression",) if isinstance(membership, ir.Negation) else ()))
                field = "body" if positive.body is not None else "pattern"
                add((*membership_address, field), where_address,
                    (premise((*where_address, "terms", 0), "false"),),
                    preserve_distinct=True, preserve_not=isinstance(membership, ir.Negation), nullsafe_membership=True)
    if pattern == "S9":
        for owner in analysis._query_sites:
            for first, second in zip(owner["stages"], owner["stages"][1:]):
                a, b = first["clause"], second["clause"]
                if (not isinstance(a, ir.Match) or not isinstance(b, ir.Match) or a.optional or b.optional
                        or a.where is None or first["scope_id"] != second["scope_id"]):
                    continue
                downstream = {second["after"][name] for name in ir.pattern_variables(b.pattern)}
                if downstream & set(first["after"].values()):
                    continue
                add(second["address"], owner["address"],
                    (premise((*first["address"], "where"), "false"),), empty_mandatory_input=True,
                    independent_downstream=True)
    return tuple(sites)


def _nonnullable(value):
    if isinstance(value, ir.Literal):
        return value.value is not None
    if isinstance(value, (ir.Count, ir.Exists)):
        return True
    if isinstance(value, ir.Call) and value.name == "coalesce":
        return any(_nonnullable(part) for part in value.args)
    return _total_boolean(value)


def _total_boolean(value):
    if isinstance(value, ir.Literal):
        return type(value.value) is bool
    if isinstance(value, ir.Exists):
        return True
    if isinstance(value, ir.Comparison):
        return value.operator in {"IS NULL", "IS NOT NULL"} or _nonnullable(value.left) and _nonnullable(value.right)
    if isinstance(value, ir.Boolean):
        return all(_total_boolean(part) for part in value.terms)
    if isinstance(value, ir.Negation):
        return _total_boolean(value.expression)
    if isinstance(value, ir.Call) and value.name == "coalesce":
        return any(_total_boolean(part) for part in value.args)
    return False


def premise_observation(expression, relation, value=None):
    """Observe an exact scalar/domain fact as a non-null Boolean predicate."""
    if not isinstance(relation, str):
        raise ir.QueryError('A premise relation requires a supported string identifier')
    if relation in {'true', 'false'}:
        return ir.Comparison(expression, '=', ir.Literal(relation == 'true'))
    if relation in {'null', 'nonnull'}:
        return ir.Comparison(expression, 'IS NULL' if relation == 'null' else 'IS NOT NULL', ir.Literal(None))
    if relation == 'equal':
        ir._validate_literal(ir.Literal(value))
        return ir.Comparison(expression, 'IS NULL' if value is None else '=', ir.Literal(value))
    raise ir.QueryError(f'Unsupported premise relation: {relation}')


def _matching_context(query, address):
    for length in range(len(address), -1, -1):
        owner = address[:length]
        value = node_at(query, owner)
        if isinstance(value, (ir.Count, ir.Exists)) and value.body is None and address[:len(owner) + 1] == (*owner, 'pattern'):
            virtual = ir.Query(value.pattern, value.where, (ir.Literal(1),))
            return owner, virtual, ((ir.Match(value.pattern, value.where), owner),), 0
        if not isinstance(value, ir.Query):
            continue
        parts = _clause_addresses(value, owner)
        for index, (clause, location) in enumerate(parts):
            pattern = (*location, 'pattern')
            if isinstance(clause, ir.Match) and address[:len(pattern)] == pattern:
                return owner, value, parts, index
    raise ir.QueryError('A binding observation must address an actual MATCH Node or pattern')


def _row_fact(parts, predicate, *, nonempty):
    pipeline = tuple(clause for clause, _ in parts)
    rows = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'row'),), pipeline=pipeline)
    bad = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'row'),), pipeline=(*pipeline,
        ir.With((ir.Alias(predicate, 'observation_ok'),), ir.Negation(ir.Variable('observation_ok')))))
    holds = ir.Comparison(ir.Count(body=bad), '=', ir.Literal(0))
    return ir.Boolean('AND', (ir.Comparison(ir.Count(body=rows), '>', ir.Literal(0)), holds)) if nonempty else holds


def _close_row_fact(query, owner, expression, observations, *, nonempty):
    descendant = owner
    for length in range(len(owner) - 1, -1, -1):
        outer_path = owner[:length]
        outer = node_at(query, outer_path)
        if not isinstance(outer, ir.Query):
            continue
        parts = _clause_addresses(outer, outer_path)
        prefix, ignored = None, None
        if descendant[:len(outer_path) + 1] == (*outer_path, 'returns'):
            if outer.limit == 0:
                raise ir.QueryError('A zero-limit query has no scalar observation inputs')
            prefix = parts
        else:
            for index, (clause, location) in enumerate(parts):
                where, projections = (*location, 'where'), (*location, 'projections')
                if clause.where is not None and descendant[:len(where)] == where:
                    # An OPTIONAL's WHERE observes real matching candidates,
                    # before filtering and before a possible NULL extension.
                    options = {'where': None}
                    if isinstance(clause, ir.Match):
                        options['optional'] = False
                    prefix, ignored = (*parts[:index], (replace(clause, **options), location)), index
                    break
                if isinstance(clause, ir.With) and descendant[:len(projections)] == projections:
                    prefix = parts[:index]
                    break
        if prefix is None:
            raise ir.QueryError('A nested binding observation has no supported query input domain')
        observations.append({'query_address': list(outer_path), 'clauses': len(prefix), 'ignored_where': ignored})
        expression = _row_fact(prefix, expression, nonempty=nonempty)
        descendant = outer_path
    ir.validate_query(ir.Query(None, None, (ir.Alias(expression, 'fact'),)))
    return expression


def entity_symbol_origin(analysis, symbol_id):
    """Follow type-preserving WITH aliases to their actual entity definition."""
    records = {record['symbol_id']: record for record in analysis.symbols}
    uses = {use['node_id']: use['symbol_id'] for use in analysis.uses}
    seen = set()
    while symbol_id in records:
        if symbol_id in seen:
            raise ir.QueryError('An entity alias has a cyclic producer')
        seen.add(symbol_id)
        record = records[symbol_id]
        source = uses.get(record.get('producer_node'))
        if record['kind'] != 'node' or source is None or records[source]['kind'] != 'node':
            return record
        symbol_id = source
    raise ir.QueryError('An entity alias has no actual node definition')


def entity_binding_observation(query, address, relation):
    """Close a nonvacuous entity-binding fact over its actual row domain.

    A new Node is observed after its MATCH; an imported/reused Node is
    observed before the current MATCH, even if that match returns no rows.
    """
    address = tuple(address)
    node = node_at(query, address)
    if not isinstance(node, ir.Node) or relation not in ('null', 'nonnull'):
        raise ir.QueryError('An entity binding demand needs a Node occurrence and null/nonnull relation')
    analysis = analyze_dependencies(query)
    sid = next((use['symbol_id'] for use in analysis.uses if use['node_id'] == _node_id((*address, 'variable'))), None)
    if sid is None:
        sid = next((record['symbol_id'] for record in analysis.symbols
                    if record['definition'] == _node_id((*address, 'variable'))), None)
    record = next((record for record in analysis.symbols if record['symbol_id'] == sid), None)
    if record is None or record['kind'] != 'node':
        raise ir.QueryError('An entity binding demand has no actual lexical Node symbol')
    owner, _, parts, index = _matching_context(query, address)
    defined_here = record['definition'] == _node_id((*address, 'variable'))
    stop = index + int(defined_here)
    observations = [{'query_address': list(owner), 'clauses': stop, 'ignored_where': None}]
    predicate = ir.Comparison(ir.Property(node.variable, 'uid'), 'IS NULL' if relation == 'null' else 'IS NOT NULL', ir.Literal(None))
    expression = _close_row_fact(query, owner, _row_fact(parts[:stop], predicate, nonempty=True), observations, nonempty=True)
    return {'address': list(address), 'relation': relation, 'expression': expression, 'imports': [],
            'symbol_id': sid, 'position': 'after_match' if defined_here else 'before_match',
            'row_observers': observations}


def scalar_value_observation(query, address, relation='equal', value=None):
    """Observe a scalar on the actual nonempty lexical row domain.

    In particular, a path or a local WITH alias cannot be imported into a
    separate anchor-only check. Retaining the producer prefix also preserves
    scope drops, path bindings and the position before a filter is applied.
    """
    address = tuple(address)
    expression = node_at(query, address)
    if relation not in ('equal', 'nonnull') or not isinstance(expression, ir.Expression):
        raise ir.QueryError('A scalar observation needs an expression and an exact/non-NULL relation')
    predicate = ir.Call('coalesce', (premise_observation(expression, relation, value), ir.Literal(False)))
    for length in range(len(address) - 1, -1, -1):
        owner = address[:length]
        tree = node_at(query, owner)
        if (isinstance(tree, (ir.Count, ir.Exists)) and tree.body is None
                and address[:len(owner) + 1] == (*owner, 'where')):
            parts = ((ir.Match(tree.pattern), owner),)
            observers = [{'query_address': list(owner), 'clauses': 1, 'ignored_where': 0}]
            closed = _close_row_fact(query, owner, _row_fact(parts, predicate, nonempty=True),
                                     observers, nonempty=True)
            return {'address': list(address), 'relation': relation, 'expression': closed, 'imports': [],
                    'row_observers': observers}
        if not isinstance(tree, ir.Query):
            continue
        parts = _clause_addresses(tree, owner)
        prefix, ignored = None, None
        if address[:len(owner) + 1] == (*owner, 'returns'):
            if tree.limit == 0:
                raise ir.QueryError('A zero-limit query has no scalar output domain')
            prefix = parts
        else:
            for index, (clause, location) in enumerate(parts):
                where, projections = (*location, 'where'), (*location, 'projections')
                if clause.where is not None and address[:len(where)] == where:
                    options = {'where': None}
                    if isinstance(clause, ir.Match):
                        options['optional'] = False
                    prefix, ignored = (*parts[:index], (replace(clause, **options), location)), index
                    break
                if isinstance(clause, ir.With) and address[:len(projections)] == projections:
                    prefix = parts[:index]
                    break
        if prefix is None:
            raise ir.QueryError('A scalar observation lacks a supported lexical row domain')
        observers = [{'query_address': list(owner), 'clauses': len(prefix), 'ignored_where': ignored}]
        closed = _close_row_fact(query, owner, _row_fact(prefix, predicate, nonempty=True),
                                 observers, nonempty=True)
        return {'address': list(address), 'relation': relation, 'expression': closed, 'imports': [],
                'row_observers': observers}
    raise ir.QueryError('A scalar observation needs an enclosing row producer')


def matching_quantity_observation(query, address, *, minimum=0, exact=None, relationship_type=None, logical_source_role='left'):
    """Observe actual filtered matches per non-NULL logical source binding."""
    address = tuple(address)
    pattern = node_at(query, address)
    if not isinstance(pattern, (ir.Node, ir.Edge)):
        raise ir.QueryError('A matching quantity must address an actual matching atom')
    owner, _, parts, index = _matching_context(query, address)
    clause, _ = parts[index]
    if logical_source_role not in ('left', 'right'):
        raise ir.QueryError('A matching degree needs an actual logical endpoint role')
    if isinstance(pattern, ir.Edge) and logical_source_role == 'right':
        pattern = replace(pattern, left=pattern.right, right=pattern.left,
                          direction={'out': 'in', 'in': 'out', 'undirected': 'undirected'}[pattern.direction])
    if isinstance(pattern, ir.Edge) and (relationship_type is not None or pattern.relationship.alternatives):
        kind = relationship_type or pattern.relationship.type
        pattern = replace(pattern, relationship=replace(pattern.relationship, type=kind, alternatives=()))
    analysis = analyze_dependencies(query)
    location = parts[index][1]
    before = next((item['bindings'] for item in analysis._environments if item['address'] == location), {})
    released = None
    predicate_where = clause.where
    if isinstance(pattern, ir.Edge) and pattern.right.variable in before:
        record = entity_symbol_origin(analysis, before[pattern.right.variable])
        root = _node_id(owner)
        if record['definition'].startswith(root + '/'):
            released = pattern.right.variable
            fresh = 'gsp_degree_right'
            strings = {part for part in _walk(query) if isinstance(part, str)}
            while fresh in strings:
                fresh += '_'
            target_sid = before[released]
            uses = {use['node_id']: use['symbol_id'] for use in analysis.uses}
            def rename(value, path):
                if isinstance(value, ir.Property) and uses.get(_node_id(path)) == target_sid:
                    return replace(value, variable=fresh)
                if isinstance(value, ir.Variable) and uses.get(_node_id(path)) == target_sid:
                    return ir.Variable(fresh)
                if isinstance(value, ir.Node) and uses.get(_node_id((*path, 'variable'))) == target_sid:
                    return replace(value, variable=fresh)
                if is_dataclass(value):
                    return replace(value, **{field.name: rename(getattr(value, field.name), (*path, field.name)) for field in fields(value)})
                if isinstance(value, tuple):
                    return tuple(rename(item, (*path, i)) for i, item in enumerate(value))
                return value
            predicate_where = rename(predicate_where, (*location, 'where'))
            pattern = replace(pattern, right=replace(pattern.right, variable=fresh))
    names = tuple(ir.Variable(name) for name in before if name != released)
    if isinstance(pattern, ir.Edge) and pattern.left.variable not in {name.name for name in names}:
        names = (*names, ir.Variable(pattern.left.variable))
    imports = names or (ir.Alias(ir.Literal(1), 'matching_input'),)
    matched = ir.Count(body=ir.Query(None, None, (ir.Alias(ir.Literal(1), 'row'),), pipeline=(
        ir.With(imports), ir.Match(pattern, predicate_where))))
    predicate = ir.Comparison(matched, '=' if exact is not None else '>=', ir.Literal(exact if exact is not None else minimum))
    if exact is not None and exact < minimum:
        raise ir.QueryError('An exact matching quantity contradicts its minimum')
    if isinstance(pattern, ir.Edge):
        source_guard = None
        if pattern.left.variable not in before:
            # A fresh source binding is introduced by the relationship atom,
            # not by an independent label scan. Other same-label vertices
            # without this relationship do not constitute zero-degree inputs.
            raw = replace(pattern, right=ir.Node(pattern.right.variable))
            source_guard = ir.Comparison(ir.Count(body=ir.Query(None, None, (ir.Literal(1),), pipeline=(
                ir.With(imports), ir.Match(raw)))), '>', ir.Literal(0))
        predicate = _row_fact(((ir.Match(pattern.left, source_guard), ()),), predicate, nonempty=False)
    observations = [{'query_address': list(owner), 'clauses': index, 'ignored_where': None}]
    expression = _close_row_fact(query, owner, _row_fact(parts[:index], predicate, nonempty=False), observations, nonempty=False)
    return {'address': list(address), 'expression': expression, 'imports': [], 'row_observers': observations,
            'quantity_kind': 'logical_source_domain_degree', 'released_internal_target': released}


def premise_obligations(query, site):
    """Close scalar aliases through actual def-use edges, then expose Boolean facts."""
    analysis = analyze_dependencies(query)
    symbols = {symbol["symbol_id"]: symbol for symbol in analysis.symbols}
    uses = {use["node_id"]: use["symbol_id"] for use in analysis.uses}
    locations = {_node_id(address): (address, value) for address, value in _located(query)}
    def imports_at(address, seen=frozenset()):
        prefix = _node_id(address)
        found = set()
        for use in analysis.uses:
            if use["node_id"] != prefix and not use["node_id"].startswith(prefix + "/"):
                continue
            symbol = symbols[use["symbol_id"]]
            if symbol["definition"].startswith(prefix + "/"):
                continue
            if symbol["kind"] == "scalar":
                producer = symbol.get("producer_node")
                if producer is None or producer not in locations or symbol["symbol_id"] in seen:
                    raise ir.QueryError("A premise alias lacks an acyclic scalar producer")
                found |= imports_at(locations[producer][0], seen | {symbol["symbol_id"]})
            else:
                found.add(symbol["symbol_id"])
        return found
    def close(value, address, seen=frozenset(), local_roots=()):
        if isinstance(value, ir.Variable):
            symbol = uses.get(_node_id(address))
            record = symbols.get(symbol, {})
            definition = record.get('definition', '')
            # A fact may contain COUNT/EXISTS with local WITH aliases. Those
            # bindings remain inside the retained row producer; inlining their
            # producer after WITH would reintroduce a variable it has dropped.
            if (record.get('kind') != 'scalar'
                    or any(definition == root or definition.startswith(root + '/') for root in local_roots)):
                return value
            producer = record.get("producer_node")
            if producer is None or producer not in locations or symbol in seen:
                raise ir.QueryError("A premise alias lacks an acyclic scalar producer")
            location, expression = locations[producer]
            return close(expression, location, seen | {symbol}, (*local_roots, _node_id(location)))
        if is_dataclass(value):
            return replace(value, **{field.name: close(getattr(value, field.name), (*address, field.name), seen, local_roots)
                                     for field in fields(value)})
        if isinstance(value, tuple):
            return tuple(close(item, (*address, index), seen, local_roots) for index, item in enumerate(value))
        return value
    result = []
    for item in site.guards.get("premises", ()):
        address = tuple(item["address"])
        expression = close(node_at(query, address), address, local_roots=(_node_id(address),))
        expression = premise_observation(expression, item['relation'], item.get('value'))
        result.append({**item, "expression": expression, "imports": sorted(imports_at(address))})
    return tuple(result)


def verify_rule_premises(query, site, bindings):
    """Recompute each exact scalar obligation and require its runtime state check."""
    from population import evaluate_query
    from query import parse_query
    from state import GraphSpec
    obligations = premise_obligations(query, site)
    if not obligations:
        return {"syntactic": True, "zero_limit": site.guards.get("zero_limit", False)}
    if not isinstance(bindings, dict):
        raise ir.QueryError("A data-dependent law requires checked premises")
    checks, required = bindings.get("premise_checks"), bindings.get("required_state_checks")
    if not isinstance(checks, (list, tuple)) or len(checks) != len(obligations) or not isinstance(required, (list, tuple)):
        raise ir.QueryError("Every premise requires its exact mandatory state check")
    try:
        spec = GraphSpec.from_dict(bindings["validation_graph_spec"])
        if spec.counts()["nodes"] > 5000:
            raise ir.QueryError("Premise proof graphs must remain bounded")
        for obligation, check in zip(obligations, checks):
            if not isinstance(check, dict) or set(check) != {"query", "expected_rows"} or check not in required:
                raise ir.QueryError("A premise check is absent from the mandatory runtime checks")
            checked = parse_query(check["query"])
            if (not isinstance(checked, ir.Query) or not isinstance(checked.pattern, ir.Node) or not checked.pattern.labels
                    or not checked.pattern.properties or checked.where is not None or checked.matches or checked.pipeline
                    or checked.limit is not None or checked.distinct):
                raise ir.QueryError("Premise checks require one fixed labeled anchor")
            anchor = checked.pattern
            expected = (ir.Alias(ir.Property(anchor.variable, "uid"), "anchor_uid"), ir.Alias(obligation["expression"], "premise"))
            if checked.returns != expected:
                raise ir.QueryError("A premise check must observe the actual unchanged guard")
            if not obligation["imports"]:
                if any(isinstance(part, ir.Node) and part.variable == anchor.variable
                       for part in _walk(obligation["expression"])):
                    raise ir.QueryError("The premise state check would capture a local graph binding")
                unbound = ir.Query(None, None, (ir.Alias(obligation["expression"], "premise"),))
                independent = evaluate_query(unbound, spec)
                if independent != [{"premise": True}]:
                    raise ir.QueryError("The premise check changes an uncorrelated guard domain")
            # Bindings outside a nested graph read must refer to the real fixed domain.
            analysis = analyze_dependencies(query)
            records = {symbol["symbol_id"]: symbol for symbol in analysis.symbols}
            for symbol_id in obligation["imports"]:
                record = records[symbol_id]
                definitions = [value for location, value in _located(query) if isinstance(value, ir.Node)
                               and _node_id((*location, "variable")) == record["definition"]]
                if record["kind"] != "node" or definitions != [anchor]:
                    raise ir.QueryError("The premise check changes an imported anchor domain")
            observed = evaluate_query(checked, spec)
            if (observed != check["expected_rows"] or len(observed) != 1
                    or set(observed[0]) != {"anchor_uid", "premise"} or type(observed[0]["anchor_uid"]) is not int
                    or observed[0]["premise"] is not True):
                raise ir.QueryError("The proof graph does not establish the exact premise for every anchor")
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ir.QueryError):
            raise
        raise ir.QueryError(f"Invalid premise proof: {error}") from error
    return {"premise_checks": list(checks), "proof_model_checked": True, "database_check_required": True}


def verify_inverse_pair(query, site, bindings):
    """Validate the actual global edge pairing and require its six DB checks."""
    from state import GraphSpec, render_state_checks, _Affine
    if not isinstance(bindings, dict):
        raise ir.QueryError("An inverse-edge law requires checked pairing evidence")
    pairing = bindings.get("inverse_relationship", {})
    forward, reverse, key = (pairing.get(name) for name in ("forward_type", "reverse_type", "pair_key"))
    if (not all(isinstance(value, str) and value for value in (forward, reverse, key)) or forward == reverse
            or node_at(query, site.address).relationship.type not in {forward, reverse}):
        raise ir.QueryError("An inverse-edge law requires two matching distinct relationship domains")
    for identifier in (forward, reverse, key):
        ir._identifier(identifier)
    try:
        spec = GraphSpec.from_dict(bindings["validation_graph_spec"])
        if spec.counts()["edges"] > 10000:
            raise ir.QueryError("An inverse proof graph must remain bounded")
        domains = []
        for kind in (forward, reverse):
            edges = [edge for edge in spec.iter_edges() if edge["type"] == kind]
            indexed = {}
            for edge in edges:
                pair = edge["properties"].get(key)
                token = (type(pair).__name__, pair)
                if pair is None or token in indexed:
                    raise ir.QueryError("Inverse edges require unique non-null pair keys")
                indexed[token] = edge
            domains.append(indexed)
        if domains[0].keys() != domains[1].keys() or any(
                (edge["source"], edge["target"]) != (domains[1][pair]["target"], domains[1][pair]["source"])
                for pair, edge in domains[0].items()):
            raise ir.QueryError("The proof graph does not establish a bijective inverse-edge pairing")
        full = GraphSpec.from_dict(bindings.get("inverse_graph_spec", bindings["validation_graph_spec"]))
        _, groups = full._groups()
        def coefficients(value, count):
            if type(value) is int:
                return value, 0
            if isinstance(value, _Affine):
                return value.base, value.step if count > 1 else 0
            raise ir.QueryError("A compact inverse proof requires integer affine values")
        def compact(kind):
            result, intervals, total = {}, [], 0
            for group in groups:
                if group.type != kind:
                    continue
                pair = coefficients(group.properties.get(key), group.count)
                if group.count > 1 and pair[1] == 0:
                    raise ir.QueryError("An amplified inverse group repeats a pair key")
                bounds = sorted((pair[0], pair[0] + (group.count - 1) * pair[1]))
                if any(not (bounds[1] < low or bounds[0] > high) for low, high in intervals):
                    raise ir.QueryError("Compact inverse pair-key domains overlap")
                intervals.append(tuple(bounds))
                signature = (group.count, pair)
                result[signature] = (coefficients(group.source, group.count), coefficients(group.target, group.count))
                total += group.count
            return result, total
        first, forward_count = compact(forward)
        second, reverse_count = compact(reverse)
        if first.keys() != second.keys() or any(endpoints != tuple(reversed(second[pair])) for pair, endpoints in first.items()):
            raise ir.QueryError("The compact full graph does not establish the inverse-edge pairing")
        checks = []
        for kind, other, count in ((forward, reverse, forward_count), (reverse, forward, reverse_count)):
            checks.extend([{"kind": "edge_count", "type": kind, "expected": count},
                           {"kind": "distinct_values", "type": kind, "property": key, "expected": count},
                           {"kind": "inverse_edges", "forward": kind, "reverse": other, "pair_key": key, "expected": 0}])
        expected = render_state_checks(checks)
        required = bindings.get("required_state_checks", ())
        if bindings.get("inverse_checks") != expected or not all(check in required for check in expected):
            raise ir.QueryError("The inverse pairing requires all exact mandatory DB checks")
    except (KeyError, ValueError, TypeError) as error:
        if isinstance(error, ir.QueryError):
            raise
        raise ir.QueryError(f"Invalid inverse pairing proof: {error}") from error
    return {"inverse_checks": expected, "proof_model_checked": True, "database_check_required": True,
            "inverse_types": [forward, reverse], "pair_key": key, "compact_full_graph_checked": True}


def find_mutation_sites(query, paper_pattern: str, form: str | None = None):
    """Resolve paper laws at actual typed AST sites, including recursive scopes."""
    if not isinstance(paper_pattern, str) or form is not None and not isinstance(form, str):
        raise ir.QueryError("Paper pattern and form require string identifiers")
    if paper_pattern in {"P3", "P4", "S6", "S8"}:
        return _existing_sites(query, paper_pattern, form)
    if paper_pattern not in _FORMS and paper_pattern != "P10":
        return ()
    if paper_pattern != "P10" and form not in (None, _FORMS[paper_pattern]):
        return ()
    analysis = analyze_dependencies(query)
    located = list(_located(query))
    if paper_pattern.startswith("S"):
        return _generic_sc_sites(query, paper_pattern, analysis, located)
    if any((isinstance(value, ir.Query) and value.limit is not None) or _row_limit(value) for _, value in located):
        return ()
    sites = []
    def add(address, guards, actual_form=None):
        sites.append(MutationSite(address, _scope_for(analysis, address), paper_pattern,
                                  actual_form or _FORMS.get(paper_pattern), guards))
    for address, value in located:
        if paper_pattern in {"P1", "P2"} and isinstance(value, ir.Edge):
            rel = value.relationship
            if rel.min_hops is not None or rel.max_hops is not None or value.path_variable is not None:
                continue
            if paper_pattern == "P1" and value.direction == "undirected" and value.left != value.right:
                add(address, {"undirected": True, "endpoint_bindings_preserved": True})
            if (paper_pattern == "P2" and value.direction in {"in", "out"} and rel.type
                    and not rel.alternatives and rel.variable is None and not rel.properties):
                add(address, {"inverse_pairing_required": True, "relationship_identity_unobserved": True})
        elif paper_pattern == "P5" and isinstance(value, ir.Boolean) and value.operator == "OR" and _pure(value):
            if value.terms != tuple(reversed(value.terms)):
                add(address, {"three_valued_commutativity": True})
        elif (paper_pattern == "P6" and isinstance(value, ir.Comparison) and value.operator == "="
              and ((isinstance(value.left, ir.Property) and isinstance(value.right, ir.Literal))
                   or (isinstance(value.right, ir.Property) and isinstance(value.left, ir.Literal)))):
            add(address, {"property_literal_equality": True})
        elif paper_pattern == "P7" and isinstance(value, ir.Node) and len(set(value.labels)) > 1:
            if value.labels != tuple(reversed(value.labels)):
                add(address, {"same_conjunctive_labels": True})
        elif paper_pattern == "P8" and isinstance(value, ir.Relationship) and value.type and value.alternatives:
            types = (value.type, *value.alternatives)
            if len(set(types)) > 1 and types != tuple(reversed(types)):
                add(address, {"same_alternative_types": True})
        elif paper_pattern == "P12" and isinstance(value, ir.UnionAll) and _pure(value):
            if value.branches != tuple(reversed(value.branches)):
                add(address, {"identical_output_columns": True, "no_order_sensitive_consumer": True})
        elif paper_pattern == "P13" and isinstance(value, ir.Case) and _pure(value) and value.then != value.otherwise:
            add(address, {"null_aware_guard_complement": True})
    if paper_pattern == "P10" and form in (None, "shared_anchor"):
        sites.extend(_existing_sites(query, "P10", "shared_anchor"))
    if paper_pattern not in {"P9", "P10", "P11"}:
        return tuple(sites)
    for owner in analysis._query_sites:
        for index, (first, second) in enumerate(zip(owner["stages"], owner["stages"][1:])):
            a, b = first["clause"], second["clause"]
            if (not isinstance(a, ir.Match) or not isinstance(b, ir.Match)
                    or first["scope_id"] != second["scope_id"] or not _pure((a, b))):
                continue
            left = {first["after"][name] for name in ir.pattern_variables(a.pattern)}
            right = {second["after"][name] for name in ir.pattern_variables(b.pattern)}
            before = set(first["before"].values())
            guards = {"query_address": list(owner["address"]), "clause_indices": [index, index + 1]}
            if paper_pattern == "P9":
                if a.optional or b.optional or (a.where is None) == (b.where is None):
                    continue
                refs = first["where_symbols"] if a.where is not None else second["where_symbols"]
                if not refs <= set(first["after"].values()):
                    continue
                add(first["address"], guards | {"predicate_available_at_both_sites": sorted(refs), "mandatory": True})
                continue
            if paper_pattern == "P11":
                shared = left & right
                if (not a.optional or not b.optional or len(shared) != 1 or not shared <= before
                        or left & before != shared or right & before != shared
                        or not first["where_symbols"] <= left or not second["where_symbols"] <= right):
                    continue
                # A prior OPTIONAL cannot serve as a guaranteed mandatory anchor.
                anchor, = shared
                definition = entity_symbol_origin(analysis, anchor)['definition']
                if not any(isinstance(stage["clause"], ir.Match) and not stage["clause"].optional
                           and any(_node_id((*stage["address"], "pattern", *role)) == definition
                                   for role in (("variable",), ("left", "variable"), ("right", "variable")))
                           for candidate in analysis._query_sites for stage in candidate["stages"]):
                    continue
                add(first["address"], guards | {"only_mandatory_anchor_shared": True, "anchor_symbol": anchor})
                continue
            if a.optional or b.optional:
                continue
            if (not left & right and not second["where_symbols"] & (left - before)
                    and first["where_symbols"] <= left | before and second["where_symbols"] <= right | before
                    and form in (None, "independent")):
                add(first["address"], guards | {"independent_bindings": True}, "independent")
            if (isinstance(a.pattern, ir.Edge) and isinstance(b.pattern, ir.Edge)
                    and a.pattern.direction == b.pattern.direction == "out"
                    and a.pattern.relationship.min_hops is None and b.pattern.relationship.min_hops is None
                    and len(left & right) == 1 and first["where_symbols"] <= left | before
                    and second["where_symbols"] <= right | before and form in (None, "chain")):
                shared, = left & right
                lookup = {use["node_id"]: use["symbol_id"] for use in analysis.uses}
                lookup.update({symbol["definition"]: symbol["symbol_id"] for symbol in analysis.symbols})
                ar = lookup.get(_node_id((*first["address"], "pattern", "right", "variable")))
                bl = lookup.get(_node_id((*second["address"], "pattern", "left", "variable")))
                if ar == bl == shared:
                    add(first["address"], guards | {"chain_joint_symbol": shared}, "chain")
    return tuple(sites)


def select_mutation_site(query, paper_pattern: str, form: str | None, mark_address):
    """Resolve an explicit occurrence mark without requiring a unique law in the tree.

    A mark names one supported structural occurrence. Other occurrences of the
    same law remain ordinary, unchanged query content, including nested ones.
    """
    if (not isinstance(mark_address, (tuple, list))
            or any(not (isinstance(part, str) or type(part) is int) for part in mark_address)):
        raise ir.QueryError("A mutation mark requires a structural address")
    address = tuple(mark_address)
    node_at(query, address)
    selected = [site for site in find_mutation_sites(query, paper_pattern, form) if site.address == address]
    if len(selected) != 1:
        raise ir.QueryError("The explicit mutation mark is not one applicable law occurrence")
    return selected[0]


def verify_short_circuit_witness(query, site: MutationSite, bindings) -> dict:
    """Check the bounded proof model and an exact, required pre-execution check.

    The database check remains a condition of the relation until execution has
    checked the loaded state. Arbitrary row-count metadata is insufficient.
    """
    from population import evaluate_query
    from query import parse_query
    from state import GraphSpec

    if site.paper_pattern not in {"S6", "S8"} or not isinstance(bindings, dict):
        raise ir.QueryError("A short-circuit site requires witness evidence")
    for key in ("union_site", "witness_site"):
        supplied = bindings.get(key)
        if not isinstance(supplied, (list, tuple)) or tuple(supplied) != tuple(site.guards[key]):
            raise ir.QueryError(f"Witness evidence has a different {key}")
    if site.paper_pattern == "S6" and bindings.get("limit", site.guards["required_rows"]) != site.guards["required_rows"]:
        raise ir.QueryError("Witness evidence has a different limit")
    check = bindings.get("witness_check")
    required_checks = bindings.get("required_state_checks")
    if (not isinstance(check, dict) or set(check) != {"query", "expected_rows"}
            or not isinstance(check["query"], str)
            or not isinstance(required_checks, (list, tuple)) or check not in required_checks):
        raise ir.QueryError("A witness requires its exact mandatory state check")
    witness = node_at(query, tuple(site.guards["witness_site"]))
    checked = parse_query(check["query"])
    if (not isinstance(checked, ir.Query) or not isinstance(checked.pattern, ir.Node)
            or checked.where is not None or checked.matches or checked.pipeline
            or checked.limit is not None or checked.distinct or not checked.pattern.labels
            or not checked.pattern.properties or len(checked.returns) != 2):
        raise ir.QueryError("Witness checking requires one fixed labeled anchor")
    anchor = checked.pattern
    expected_projection = (ir.Alias(ir.Property(anchor.variable, "uid"), "anchor_uid"),
                           ir.Alias(ir.Count(body=witness), "witness_rows"))
    if checked.returns != expected_projection:
        raise ir.QueryError("The state check must count the unchanged witness branch")
    analysis = analyze_dependencies(query)
    scope = next(record for record in analysis.scopes
                 if record["node_id"] == _node_id(tuple(site.guards["witness_site"])))
    symbols = {symbol["symbol_id"]: symbol for symbol in analysis.symbols}
    if any(symbols[symbol]["name"] != anchor.variable or symbols[symbol]["kind"] != "node"
           for symbol in scope["imports"]):
        raise ir.QueryError("The witness check does not cover every imported binding")
    for symbol_id in scope["imports"]:
        definitions = [value for address, value in _located(query) if isinstance(value, ir.Node)
                       and _node_id((*address, "variable")) == symbols[symbol_id]["definition"]]
        if definitions != [anchor]:
            raise ir.QueryError("The witness check must enumerate the actual imported anchor domain")
    spec_data = bindings.get("validation_graph_spec")
    if not isinstance(spec_data, dict):
        raise ir.QueryError("A witness requires an independently evaluable proof graph")
    try:
        spec = GraphSpec.from_dict(spec_data)
        if spec.counts()["nodes"] > 5000:
            raise ir.QueryError("A witness proof graph must remain bounded")
        observed = evaluate_query(checked, spec)
        if not scope["imports"]:
            actual_count = len(evaluate_query(witness, spec))
            if len(observed) != 1 or actual_count != observed[0].get("witness_rows"):
                raise ir.QueryError("The witness check changes an uncorrelated witness domain")
    except (ValueError, TypeError, KeyError) as exc:
        raise ir.QueryError(f"Invalid witness proof graph: {exc}") from exc
    rows = check["expected_rows"]
    if (observed != rows or not isinstance(rows, list) or len(rows) != 1
            or set(rows[0]) != {"anchor_uid", "witness_rows"}
            or type(rows[0]["anchor_uid"]) is not int or type(rows[0]["witness_rows"]) is not int
            or rows[0]["witness_rows"] < site.guards["required_rows"]):
        raise ir.QueryError("The proof graph does not establish the per-anchor witness requirement")
    return {"witness_site": site.guards["witness_site"], "required_rows": site.guards["required_rows"],
            "witness_check": check, "proof_model_checked": True, "database_check_required": True,
            "outer_imports": scope["imports"]}


def propagate_equivalence(query, site: P3Site | MutationSite, bindings=None) -> list[dict]:
    """Explain exact local equivalence through actual ancestors and bindings.

    Each rule establishes a per-binding local relation: a path bijection,
    three-valued Boolean equality, or an independent mandatory bag product.
    The shared consumer rules then preserve scalar values and complete bags
    through their actual lexical contexts.
    """
    applicable = next((candidate for candidate in find_mutation_sites(query, site.paper_pattern, site.form)
                       if candidate.address == site.address and candidate.scope_id == site.scope_id
                       and (not isinstance(site, P3Site) or candidate.path_symbol == site.path_symbol)), None)
    if applicable is None:
        raise ir.QueryError(f"Equivalence propagation requires an applicable {site.paper_pattern} site")
    site = applicable
    analysis = analyze_dependencies(query)
    local = {"node_id": _node_id(site.address), "address": list(site.address),
             "paper_pattern": site.paper_pattern, "form": site.form}
    if site.paper_pattern in {"S6", "S8"}:
        witness = verify_short_circuit_witness(query, site, bindings)
        envelope = tuple(site.guards["envelope_site"])
        local.update(operator="RowLimit" if site.paper_pattern == "S6" else "Exists",
                     node_id=_node_id(envelope), address=list(envelope),
                     relation="bag_equal" if site.paper_pattern == "S6" else "boolean_equal",
                     premise="unchanged_Q0_satisfies_positive_demand" if site.paper_pattern == "S6"
                     else "unchanged_Q0_supplies_a_witness", witness=witness,
                     replacement_bags_may_differ=True)
        ancestor_length = len(envelope) - 1
    elif site.paper_pattern.startswith("S"):
        evidence = verify_rule_premises(query, site, bindings)
        envelope = tuple(site.guards["envelope_site"])
        operator = node_at(query, envelope)
        local.update(operator=type(operator).__name__, node_id=_node_id(envelope), address=list(envelope),
                     relation="bag_equal" if isinstance(operator, ir.RowStream) else "scalar_equal",
                     premise="unchanged_checked_decisive_fact", evidence=evidence,
                     replacement_values_or_bags_may_differ=True)
        ancestor_length = len(envelope) - 1
    elif site.paper_pattern == "P3":
        local.update(operator="Edge", relation="per_binding_path_bijection",
                     endpoint_symbols=dict(site.endpoint_symbols), path_observations=site.guards["path_consumers"])
        ancestor_length = len(site.address) - 1
    elif site.paper_pattern in {"P4", "P5"}:
        local.update(operator="Boolean", relation="boolean_equal",
                     premise=f"pure_three_valued_{node_at(query, site.address).operator}_commutativity")
        ancestor_length = len(site.address) - 1
    elif site.paper_pattern in {"P9", "P10", "P11"}:
        premise = {"P9": "mandatory_filter_placement_with_same_lexical_inputs",
                   "P10": "independent_mandatory_bag_product",
                   "P11": "independent_optional_null_extensions"}[site.paper_pattern]
        if site.paper_pattern == "P10" and site.form == "chain":
            premise = "mandatory_bag_join_commutativity_on_shared_bindings"
            local["join_symbols"] = [site.guards["chain_joint_symbol"]]
        elif site.paper_pattern == "P10" and site.form == "shared_anchor":
            premise = "conditional_bag_product_per_shared_anchor"
            local["join_symbols"] = [site.guards["anchor_symbol"]]
        local.update(operator="MatchPair", relation="per_binding_bag_equal",
                     premise=premise, guards=site.guards)
        ancestor_length = len(site.guards["query_address"])
    else:
        local.update(operator=type(node_at(query, site.address)).__name__, relation="per_binding_bag_equal"
                     if site.paper_pattern in {"P1", "P2", "P7", "P8", "P12"} else "scalar_equal",
                     premise={"P1": "undirected_endpoint_bijection", "P2": "checked_inverse_edge_bijection",
                              "P6": "null_aware_property_literal_equality", "P7": "same_conjunctive_labels",
                              "P8": "same_relationship_type_set", "P12": "same_branch_bag_union",
                              "P13": "NOT_coalesce_guard_preserves_NULL_else_selection"}[site.paper_pattern])
        ancestor_length = len(site.address) - 1
        if site.paper_pattern == "P2":
            local["pairing"] = verify_inverse_pair(query, site, bindings)
    steps = [local]
    expression_rules = {
        ir.Count: ("equal_input_bags", "integer_equal"),
        ir.Exists: ("equal_input_bags", "boolean_equal"),
        ir.Comparison: ("equal_operand_values", "boolean_equal"),
        ir.Boolean: ("equal_operand_values", "boolean_equal"),
        ir.Negation: ("equal_operand_values", "boolean_equal"),
        ir.Case: ("equal_condition_or_branch_values", "scalar_equal"),
        ir.Call: ("equal_argument_values", "scalar_equal"),
        ir.Alias: ("equal_expression_values", "alias_equal"),
        ir.ListExpr: ("equal_element_values", "list_equal"),
        ir.Concat: ("equal_list_values", "list_equal"),
        ir.All: ("equal_input_and_predicate_values", "boolean_equal"),
    }
    ancestor_address = (tuple(site.guards["query_address"]) if site.paper_pattern in {"P9", "P10", "P11"} else
                        tuple(site.guards["envelope_site"]) if site.paper_pattern.startswith("S") else site.address)
    for length in range(ancestor_length, -1, -1):
        address = ancestor_address[:length]
        value = node_at(query, address)
        if not is_dataclass(value):
            continue
        step = {"node_id": _node_id(address), "address": list(address),
                "operator": type(value).__name__}
        if isinstance(value, ir.Query):
            if value.limit is not None:
                raise ir.QueryError("An enclosing unordered LIMIT needs an independent order-invariance proof")
            scope = next(record for record in analysis.scopes
                         if record["node_id"] == _node_id(address) and record["kind"] == "query")
            stages = []
            relative = site.address[len(address):]
            if site.paper_pattern in {"P9", "P10", "P11"} and address == ancestor_address:
                first_affected = site.guards["clause_indices"][0]
            elif relative and relative[0] == "pipeline":
                first_affected = relative[1]
            elif relative and relative[0] in {"pattern", "where"}:
                first_affected = 0
            elif relative and relative[0] == "matches":
                first_affected = relative[1] + 1
            else:
                first_affected = len(_clause_addresses(value, address))
            for index, (clause, location) in enumerate(_clause_addresses(value, address)):
                if _with(clause) and index >= first_affected:
                    with_scope = next(record for record in analysis.scopes
                                      if record["node_id"] == _node_id(location) and record["kind"] == "with")
                    aliases = [symbol for symbol in analysis.symbols
                               if symbol["scope_id"] == with_scope["scope_id"]]
                    stage = {"node_id": _node_id(location), "operator": "With",
                             "relation": "bag_equal_on_exported_scalar_observations", "aliases": aliases,
                             "consumers": [use for use in analysis.uses
                                           if use["symbol_id"] in with_scope["exports"].values()]}
                    if site.paper_pattern == "P3":
                        stage["path_passthrough"] = "reversal_bijection_until_invariant_observation"
                    stages.append(stage)
            premise = ("per_binding_match_bijection_and_invariant_scalar_outputs" if site.paper_pattern == "P3"
                       else "per_binding_equal_bags_and_equal_scalar_values")
            step.update(premise=premise, relation="bag_equal",
                        scope_id=scope["scope_id"], imports=scope["imports"],
                        exports=scope["exports"], stages=stages)
        elif isinstance(value, ir.Match):
            premise = ("per_binding_path_bijection_and_preserved_predicate" if site.paper_pattern == "P3"
                       else "equal_boolean_predicate_preserves_each_input_row")
            step.update(premise=premise,
                        relation="bag_equal_on_invariant_observations")
        elif isinstance(value, (ir.Node, ir.Edge, ir.Relationship)):
            step.update(premise="same_matching_constraints_and_bindings", relation="per_binding_bag_equal")
        elif _with(value):
            step.update(premise="equal_projection_or_filter_values", relation="per_binding_bag_equal")
        elif isinstance(value, ir.UnionAll):
            step.update(premise="equal_branch_bags", relation="bag_equal")
        elif _row_limit(value):
            raise ir.QueryError("An enclosing limit needs an independent order-invariance proof")
        elif isinstance(value, ir.ConditionalQuery):
            step.update(premise="equal_condition_or_selected_branch", relation="bag_equal")
        elif type(value) in expression_rules:
            premise, relation = expression_rules[type(value)]
            if isinstance(value, (ir.Count, ir.Exists)) and value.body is None:
                premise = ("per_binding_path_bijection_and_preserved_predicate" if site.paper_pattern == "P3"
                           else "equal_filtered_input_bags")
            step.update(premise=premise, relation=relation)
        else:
            raise ir.QueryError(f"Unsupported equivalence consumer: {type(value).__name__}")
        steps.append(step)
    return steps
