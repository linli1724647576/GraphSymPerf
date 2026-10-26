"""M6: semantic propagation and logical demand, independently of physical cost.

Concrete observations below are an in-memory semantic model, never execution
counts from the tested DBMS. Runtime evidence is attached by evidence_adapter.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import fields, is_dataclass

import ir
from dependencies import (analyze_dependencies, entity_symbol_origin, node_at,
                          propagate_equivalence, select_mutation_site)
from population import (PopulationError, _Interpreter, _Path, _Vertex, _Relation,
                        _compare, _distinct, _logical, _typed, evaluate_query)
from query import projection_names
from state import GraphSpec

from .contracts import DependencyReport, StageFailure, digest


def _walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for item in fields(value):
            yield from _walk(getattr(value, item.name), (*address, item.name))
    elif isinstance(value, tuple):
        for i, item in enumerate(value):
            yield from _walk(item, (*address, i))


def _key(address):
    return "root" + "".join(f"/{part}" for part in address)


def _environment(env):
    def scalar(value):
        if isinstance(value, _Vertex):
            return {"node_uid": value.uid}
        if isinstance(value, _Relation):
            return {"relationship_index": value.index}
        if isinstance(value, _Path):
            return {"path_uids": list(value.vertices)}
        if isinstance(value, (list, tuple)):
            return [scalar(item) for item in value]
        return value
    return {name: scalar(value) for name, value in sorted(env.items())}


class _LogicalTrace(_Interpreter):
    """Trace lexical semantic inputs with lazy value demand.

    EXISTS and positive LIMIT enumerate semantic producers to establish their
    result; their downstream requests are therefore bounds, not measured calls.
    """
    def __init__(self, spec, max_events=100_000):
        super().__init__(spec)
        self.inputs = defaultdict(list)
        self.syntactic_inputs = defaultdict(list)
        self.flow_demands = defaultdict(set)
        self.projection_requirements = {}
        self.max_events = max_events
        self.events = 0

    def record(self, address, env):
        self.events += 1
        if self.events > self.max_events:
            raise PopulationError("Logical dependency observation budget exhausted")
        self.inputs[tuple(address)].append(dict(env))

    def record_pattern(self, pattern, address, env):
        # Node labels, relationship types and path metadata are structural
        # parts of one matching request. They have the match's input domain,
        # unlike scalar children skipped by a short-circuit consumer.
        for relative, item in _walk(pattern):
            if is_dataclass(item) or isinstance(item, tuple):
                self.record((*address, *relative), env)

    def expression_at(self, value, env, address):
        self.record(address, env)
        child = lambda item, field: self.expression_at(item, env, (*address, field))
        if isinstance(value, (ir.Literal, ir.Variable, ir.Property)):
            return super().expression(value, env)
        if isinstance(value, ir.Alias):
            return child(value.expression, "expression")
        if isinstance(value, ir.Comparison):
            return _compare(child(value.left, "left"), value.operator, child(value.right, "right"))
        if isinstance(value, ir.Negation):
            result = child(value.expression, "expression")
            return None if result is None else not result
        if isinstance(value, ir.Boolean):
            values = []
            decisive = False if value.operator == "AND" else True
            for i, term in enumerate(value.terms):
                result = self.expression_at(term, env, (*address, "terms", i))
                values.append(result)
                if result is decisive:
                    return decisive
            return _logical(value.operator, values)
        if isinstance(value, ir.Case):
            field = "then" if child(value.condition, "condition") is True else "otherwise"
            return child(getattr(value, field), field)
        if isinstance(value, ir.Call):
            if value.name == "coalesce":
                for i, term in enumerate(value.args):
                    result = self.expression_at(term, env, (*address, "args", i))
                    if result is not None:
                        return result
                return None
            args = [self.expression_at(term, env, (*address, "args", i)) for i, term in enumerate(value.args)]
            if value.name == "nodes":
                return None if args[0] is None else [_Vertex(uid) for uid in args[0].vertices]
            if value.name == "length":
                return None if args[0] is None else len(args[0].vertices) - 1
            if value.name == "size":
                return None if args[0] is None else len(args[0])
            raise PopulationError(f"Unsupported demand function: {value.name}")
        if isinstance(value, (ir.Count, ir.Exists)):
            if value.body is not None:
                rows = self.run_at(value.body, (*address, "body"), env,
                                   demand="count" if isinstance(value, ir.Count) else "exists")
            else:
                rows = []
                self.record_pattern(value.pattern, (*address, "pattern"), env)
                for row in self._match(value.pattern, env):
                    if value.where is None or self.expression_at(value.where, row, (*address, "where")) is True:
                        rows.append(row)
            return len(rows) if isinstance(value, ir.Count) else bool(rows)
        if isinstance(value, ir.All):
            items = child(value.items, "items")
            if items is None:
                return None
            return _logical("AND", [self.expression_at(value.predicate, env | {value.variable: item},
                                                       (*address, "predicate")) for item in items])
        if isinstance(value, ir.ListExpr):
            return [self.expression_at(item, env, (*address, "items", i)) for i, item in enumerate(value.items)]
        if isinstance(value, ir.Concat):
            left, right = child(value.left, "left"), child(value.right, "right")
            return None if left is None or right is None else left + right
        raise PopulationError(f"Unsupported logical expression: {type(value).__name__}")

    def _project_at(self, items, env, address, field, required=None):
        names = projection_names(ir.Query(None, None, items))
        required = set(range(len(items))) if required is None else set(required)
        for i in range(len(items)):
            self.syntactic_inputs[(*address, field, i)].append(dict(env))
        return {name: self.expression_at(item, env, (*address, field, i))
                for i, (name, item) in enumerate(zip(names, items)) if i in required}

    def _required_with_projections(self, flow, steps, values_needed, demand):
        """Backpropagate lexical uses through WITH boundaries.

        Every filter and match consumes bindings. Final projected values are
        needed only by a value consumer or count-sensitive DISTINCT. This is a
        logical liveness analysis; no physical projection pushdown is assumed.
        """
        needed = set().union(*(ir.expression_variables(item.expression if isinstance(item, ir.Alias) else item)
                               for item in flow.returns)) if values_needed else set()
        plans = {}
        for clause, location in reversed(steps):
            if isinstance(clause, ir.Match):
                needed.update(ir.pattern_variables(clause.pattern))
                if clause.where is not None:
                    needed.update(ir.expression_variables(clause.where))
            elif isinstance(clause, ir.With):
                names = projection_names(ir.Query(None, None, clause.projections))
                outputs_needed = set(needed)
                if clause.where is not None:
                    outputs_needed.update(ir.expression_variables(clause.where))
                live = {i for i, name in enumerate(names) if name in outputs_needed}
                if clause.distinct and demand != "exists":
                    live = set(range(len(names)))
                plans[location] = live
                needed = set().union(*(ir.expression_variables(
                    item.expression if isinstance(item, ir.Alias) else item)
                    for i, item in enumerate(clause.projections) if i in live)) if live else set()
        return plans

    def run_at(self, flow, address=(), outer=None, *, demand="values"):
        env = dict(outer or {})
        self.record(address, env)
        self.flow_demands[address].add(demand)
        if isinstance(flow, ir.ConditionalQuery):
            field = "then" if self.expression_at(flow.condition, env, (*address, "condition")) is True else "otherwise"
            return self.run_at(getattr(flow, field), (*address, field), env, demand=demand)
        if isinstance(flow, ir.UnionAll):
            return [row for i, branch in enumerate(flow.branches)
                    for row in self.run_at(branch, (*address, "branches", i), env, demand=demand)]
        if isinstance(flow, ir.RowLimit):
            return [] if flow.limit == 0 else self.run_at(flow.body, (*address, "body"), env, demand=demand)[:flow.limit]
        if not isinstance(flow, ir.Query):
            raise PopulationError("Unsupported logical row producer")
        if flow.pipeline:
            steps = [(clause, (*address, "pipeline", i)) for i, clause in enumerate(flow.pipeline)]
        else:
            steps = [(ir.Match(flow.pattern, flow.where), address)] if flow.pattern is not None else []
            steps += [(clause, (*address, "matches", i)) for i, clause in enumerate(flow.matches)]
        values_needed = demand == "values" or flow.distinct and demand == "count"
        required_returns = set(range(len(flow.returns))) if values_needed else set()
        self.projection_requirements[(address, "returns")] = required_returns
        with_plans = self._required_with_projections(flow, steps, values_needed, demand)
        for location, live in with_plans.items():
            self.projection_requirements[(location, "projections")] = live
        rows = [env]
        for clause, location in steps:
            if isinstance(clause, ir.Match):
                matched = []
                for row in rows:
                    if location != address:
                        self.record(location, row)
                    self.record_pattern(clause.pattern, (*location, "pattern"), row)
                    candidates = [item for item in self._match(clause.pattern, row)
                                  if clause.where is None or self.expression_at(clause.where, item, (*location, "where")) is True]
                    if clause.optional and not candidates:
                        names = ir.pattern_variables(clause.pattern)
                        candidates = [row | {name: None for name in names if name not in row}]
                    matched.extend(candidates)
                rows = matched
            elif isinstance(clause, ir.With):
                rows = [self._project_at(clause.projections, row, location, "projections", with_plans[location]) for row in rows]
                if clause.distinct and demand != "exists":
                    rows = _distinct(rows)
                if clause.where is not None:
                    rows = [row for row in rows if self.expression_at(clause.where, row, (*location, "where")) is True]
            else:
                raise PopulationError("Unsupported logical clause")
        result = [self._project_at(flow.returns, row, address, "returns", required_returns) for row in rows]
        if flow.distinct and demand != "exists":
            result = _distinct(result)
        return result[:flow.limit] if flow.limit is not None else result


def _ancestor_demands(tree, address, trace):
    steps = []
    for length in range(len(address) - 1, -1, -1):
        location = address[:length]
        value = node_at(tree, location)
        if not is_dataclass(value):
            continue
        child = address[length]
        step = {"address": list(location), "node_id": _key(location), "operator": type(value).__name__,
                "logical_demand": "full", "value_dependency": "used", "child_field": child}
        if child in ("args", "terms"):
            step["child_index"] = address[length + 1]
        inputs = trace.inputs.get(location, ())
        if isinstance(value, (ir.Query, ir.With)) and child in ("returns", "projections"):
            live = trace.projection_requirements.get((location, child))
            index = address[length + 1]
            if live is not None and index not in live:
                step.update(logical_demand="masked", value_dependency="unused", reason="unobserved_projection",
                            row_consumers=sorted(trace.flow_demands.get(location, ())))
        if isinstance(value, ir.Exists):
            step["logical_demand"] = "witness"
        elif isinstance(value, ir.Count):
            step["logical_demand"] = "full"
        elif isinstance(value, ir.RowLimit):
            step.update(logical_demand="masked" if value.limit == 0 else "bounded", limit=value.limit)
        elif isinstance(value, ir.Query) and value.limit is not None:
            step.update(logical_demand="masked" if value.limit == 0 else "bounded", limit=value.limit)
        if isinstance(value, ir.Query):
            # Earlier mandatory filters can suppress a later MATCH or projection.
            # Predicate inputs are observed after their own match, so a failed
            # predicate is not incorrectly treated as shielding its own scan.
            guards = []
            if value.pipeline:
                stop = address[length + 1] if child == "pipeline" else len(value.pipeline)
                for i, clause in enumerate(value.pipeline[:stop]):
                    if isinstance(clause, ir.Match) and not clause.optional and clause.where is not None:
                        guards.append(((*location, "pipeline", i, "where"), clause.where))
            elif child in ("matches", "returns"):
                if value.where is not None:
                    guards.append(((*location, "where"), value.where))
                stop = address[length + 1] if child == "matches" else len(value.matches)
                guards.extend(((*location, "matches", i, "where"), clause.where)
                              for i, clause in enumerate(value.matches[:stop])
                              if not clause.optional and clause.where is not None)
            for guard_address, guard in guards:
                guard_inputs = trace.inputs.get(guard_address, ())
                truths = [trace.expression(guard, env) for env in guard_inputs]
                if truths and all(truth is not True for truth in truths):
                    step.update(logical_demand="masked", value_dependency="unused", reason="empty_mandatory_input")
                    break
        elif isinstance(value, (ir.Case, ir.ConditionalQuery)) and child in ("then", "otherwise"):
            selected = []
            for env in inputs:
                truth = trace.expression(value.condition, env)
                selected.append("then" if truth is True else "otherwise")
            if selected and all(field != child for field in selected):
                step.update(logical_demand="masked", value_dependency="unused")
            elif not selected or any(field != child for field in selected):
                step.update(logical_demand="conditional", value_dependency="conditional")
        elif isinstance(value, ir.Call) and value.name == "coalesce" and child == "args":
            index = address[length + 1]
            masks = [any(trace.expression(term, env) is not None for term in value.args[:index]) for env in inputs]
            if masks and all(masks):
                step.update(logical_demand="masked", value_dependency="unused", reason="nonnull_prefix")
            elif not masks or any(masks):
                step.update(logical_demand="conditional", value_dependency="conditional")
        elif isinstance(value, ir.Boolean) and child == "terms":
            index = address[length + 1]
            decisive = False if value.operator == "AND" else True
            masks = [any(trace.expression(term, env) is decisive for i, term in enumerate(value.terms) if i != index)
                     for env in inputs]
            if masks and all(masks):
                step.update(logical_demand="masked", value_dependency="unused", reason="decisive_sibling")
            elif not masks or any(masks):
                step.update(logical_demand="conditional", value_dependency="conditional")
        # LIMIT/filter annotations describe row demand; they cannot revive a
        # projected scalar that its row consumer never observes.
        if isinstance(value, (ir.Query, ir.With)) and child in ("returns", "projections"):
            live = trace.projection_requirements.get((location, child))
            if live is not None and address[length + 1] not in live:
                step.update(logical_demand="masked", value_dependency="unused", reason="unobserved_projection",
                            row_consumers=sorted(trace.flow_demands.get(location, ())))
        steps.append(step)
    return steps


def _candidate_reads(tree, address, entries, spec):
    """Count local counterfactual matching candidates, with no time prediction."""
    fragment = node_at(tree, address)
    model = _Interpreter(spec)
    reads = []
    located = list(_walk(fragment))
    edges = {path for path, value in located if isinstance(value, ir.Edge)}
    for relative, value in located:
        if not isinstance(value, (ir.Node, ir.Edge)):
            continue
        if isinstance(value, ir.Node) and relative[:-1] in edges:
            continue
        counts = []
        for env in entries:
            count = 0
            for _ in model._match(value, env):
                count += 1
                if count > 100_000:
                    raise PopulationError("Counterfactual candidate budget exhausted")
            counts.append(count)
        reads.append({"address": list((*address, *relative)), "kind": type(value).__name__,
                      "per_input_candidates": counts, "total_candidates": sum(counts),
                      "filter_applied": False})
    return reads


def _context_patterns(tree, target_address, trace, analysis):
    """Recognize enclosing tests and consumers through lexical and alias edges.

    Recognition does not assert a registered mutation. Decisive premises are
    checked on every observed semantic input; an empty input domain proves none.
    """
    target_key = _key(target_address)
    affected = set()
    for symbol in analysis.symbols:
        definition = symbol["definition"]
        if definition.startswith(target_key + "/"):
            affected.add(symbol["symbol_id"])
        elif target_key.startswith(definition + "/") and symbol["kind"] in {"alias", "scalar"}:
            affected.add(symbol["symbol_id"])
    while True:
        extended = affected | {symbol["symbol_id"] for symbol in analysis.symbols
                               if set(symbol["sources"]) & affected}
        if extended == affected:
            break
        affected = extended
    paths = [(target_address, "lexical")]
    paths += [(tuple(use["address"]), "defined_use") for use in analysis.uses if use["symbol_id"] in affected]
    records = {}

    def add(pattern, location, child_path, via, *, premise=None, values=None,
            demand="unknown", value_dependency="conditional", description=""):
        inputs = trace.inputs.get(location, ())
        key = (pattern, location)
        status = ("satisfied" if premise is True else "refuted" if premise is False else "unknown")
        record = {"instance_id": f"recognized:{pattern}:{_key(location)}", "pattern_id": pattern,
                  "address": list(location), "registration": "recognized_context",
                  "target_address": list(target_address), "consumer_use_address": list(child_path),
                  "dependency_via": via, "premise_status": status,
                  "logical_demand": demand, "value_dependency": value_dependency,
                  "semantic_input_count": len(inputs), "input_observation": "logical_model",
                  "physical_invocations": "unknown", "description": description,
                  "exposure_status": "exposed" if inputs else "unreachable"}
        if values is not None:
            record["premise_values"] = values[:64]
        if key not in records or via == "lexical":
            records[key] = record

    def all_fact(values):
        return all(values) if values else None

    for path, via in paths:
        for length in range(len(path) - 1, -1, -1):
            location = path[:length]
            value = node_at(tree, location)
            child = path[length]
            inputs = trace.inputs.get(location, ())
            if isinstance(value, ir.Call) and value.name == "coalesce" and child == "args":
                index = path[length + 1]
                if not index:
                    continue
                prefix_values = [[trace.expression(term, env) for term in value.args[:index]] for env in inputs]
                facts = [any(item is not None for item in values) for values in prefix_values]
                premise = all_fact(facts)
                add("S7", location, path, via, premise=premise, values=prefix_values,
                    demand="masked" if premise is True else "conditional" if premise is None else "full",
                    value_dependency="unused" if premise is True else "conditional",
                    description="A non-NULL earlier argument shields the target-dependent argument")
            elif isinstance(value, ir.Boolean) and child == "terms":
                index = path[length + 1]
                decisive = False if value.operator == "AND" else True
                siblings = [[trace.expression(term, env) for i, term in enumerate(value.terms) if i != index]
                            for env in inputs]
                premise = all_fact([any(term is decisive for term in row) for row in siblings])
                add("S2" if value.operator == "AND" else "S1", location, path, via,
                    premise=premise, values=siblings,
                    demand="masked" if premise is True else "conditional" if premise is None else "full",
                    value_dependency="unused" if premise is True else "conditional",
                    description="A decisive sibling makes the target value irrelevant")
                add("P4" if value.operator == "AND" else "P5", location, path, via,
                    premise=None, demand="conditional", description="Boolean symmetry context; local rewrite premises not re-certified")
            elif isinstance(value, (ir.Case, ir.ConditionalQuery)) and child in {"then", "otherwise"}:
                truths = [trace.expression(value.condition, env) for env in inputs]
                shielded = [("then" if truth is True else "otherwise") != child for truth in truths]
                premise = all_fact(shielded)
                add("S4" if isinstance(value, ir.Case) else "S3", location, path, via,
                    premise=premise, values=truths,
                    demand="masked" if premise is True else "conditional",
                    value_dependency="unused" if premise is True else "conditional",
                    description="The target-dependent subtree is in the unselected branch")
                if isinstance(value, ir.Case):
                    add("P13", location, path, via, description="CASE duality context; transformation is not activated")
            elif isinstance(value, ir.Exists):
                # One qualifying row is enough for existence, but an arbitrary
                # target subtree is not automatically a replaceable S8 region.
                add("S8", location, path, via, demand="witness",
                    description="Existence consumer; unchanged witness/replacement premises remain unproved")
            elif isinstance(value, ir.RowLimit) or isinstance(value, ir.Query) and value.limit is not None:
                limit = value.limit
                add("S5" if limit == 0 else "S6", location, path, via,
                    premise=bool(inputs) if limit == 0 and inputs else None,
                    demand="masked" if limit == 0 else "bounded",
                    value_dependency="unused" if limit == 0 else "conditional",
                    description="Zero demand" if limit == 0 else "Bounded output consumer; constant-row saturation is not assumed")
            elif isinstance(value, ir.Count):
                add("COUNT", location, path, via, demand="full", value_dependency="used",
                    description="Counts its direct input rows, preserving nested RETURN boundaries")
    return list(records.values()), sorted(affected)


def _shared_reads(tree, address, spec=None, analysis=None):
    """Conservatively connect graph, entity and property read domains.

    Label names alone never establish disjoint node domains: one graph entity
    can have multiple labels. Actual intersections are counted only within the
    inspected GraphSpec; unread nodes leave an explicit unknown upper bound.
    """
    analysis = analysis or analyze_dependencies(tree)
    located = list(_walk(tree))
    by_id = {_key(path): (path, value) for path, value in located}
    symbols = {item["definition"]: item["symbol_id"] for item in analysis.symbols}
    symbols.update({item["node_id"]: item["symbol_id"] for item in analysis.uses})
    source_symbols = {item["symbol_id"]: item for item in analysis.symbols}
    graph_nodes, complete = [], False
    if spec is not None:
        for node in spec.iter_nodes():
            if len(graph_nodes) >= 100_000:
                break
            graph_nodes.append(node)
        complete = len(graph_nodes) == spec.counts()["nodes"]
    domain_cache = {}

    def origin(symbol):
        if not symbol:
            return None, None
        record = source_symbols[symbol]
        if record["kind"] == "node":
            record = entity_symbol_origin(analysis, symbol)
        definition = by_id.get(record["definition"])
        if definition and definition[0] and definition[0][-1] == "variable":
            owner = node_at(tree, definition[0][:-1])
            if isinstance(owner, ir.Node):
                return record["symbol_id"], owner
        return record["symbol_id"], None

    def domain(pattern):
        if spec is None or pattern is None:
            return None
        key = digest((pattern.labels, pattern.properties))
        if key not in domain_cache:
            domain_cache[key] = {node["uid"] for node in graph_nodes
                if set(pattern.labels) <= set(node["labels"])
                and all(_compare(node["properties"].get(key), "=", value.value) is True
                        for key, value in pattern.properties)}
        return domain_cache[key]

    reads = []
    for path, value in located:
        if isinstance(value, ir.Edge):
            reads.append({"path": path, "kind": "relationship", "types":
                          {value.relationship.type, *value.relationship.alternatives} - {None}})
        elif isinstance(value, ir.Node):
            symbol, _ = origin(symbols.get(_key((*path, "variable"))))
            reads.append({"path": path, "kind": "node", "pattern": value, "symbol": symbol})
        elif isinstance(value, ir.Property):
            symbol, pattern = origin(symbols.get(_key(path)))
            reads.append({"path": path, "kind": "property", "pattern": pattern,
                          "symbol": symbol, "property_key": value.key})
    def is_local(read):
        if read["path"][:len(address)] == address:
            return True
        # A relationship-type order is metadata on its owner Edge request.
        if (read["kind"] == "relationship" and address[:len(read["path"])] == read["path"]
                and address[len(read["path"]):len(read["path"]) + 1] == ("relationship",)):
            return True
        return False
    local = [read for read in reads if is_local(read)]
    outer = [read for read in reads if not is_local(read)]
    result, seen = [], set()
    for first in local:
        for second in outer:
            if first["kind"] == "relationship" or second["kind"] == "relationship":
                if first["kind"] != second["kind"]:
                    continue
                shared = sorted(first["types"] & second["types"])
                # An untyped relationship may read any relationship domain.
                if not shared and first["types"] and second["types"]:
                    continue
                record = {"shared_relationship_types": shared, "overlap_status": "possible",
                          "basis": "relationship_type_overlap" if shared else "untyped_relationship_read"}
            else:
                first_domain, second_domain = domain(first.get("pattern")), domain(second.get("pattern"))
                same_binding = bool(first.get("symbol") and first.get("symbol") == second.get("symbol"))
                record = {"shared_entity_binding": same_binding,
                          "target_labels": list(first["pattern"].labels) if first.get("pattern") else None,
                          "outer_labels": list(second["pattern"].labels) if second.get("pattern") else None,
                          "property_keys": sorted({read["property_key"] for read in (first, second)
                                                   if "property_key" in read})}
                if first_domain is not None and second_domain is not None:
                    overlap = first_domain & second_domain
                    if not overlap and complete and not same_binding:
                        continue
                    record.update(overlap_status="observed" if overlap else "possible" if same_binding else "unknown",
                                  domain_intersection_interval=[len(overlap), len(overlap) if complete else None],
                                  domain_observation_scope="full_graph" if complete else "partial_graph_spec",
                                  basis="GraphSpec_entity_domain_intersection" if overlap else
                                        "same_lexical_entity_binding" if same_binding else "incomplete_graph_domain",
                                  shared_entity_uid_sample=sorted(overlap)[:16])
                else:
                    record.update(overlap_status="possible" if same_binding else "unknown",
                                  domain_intersection_interval=[0, None], domain_observation_scope="unknown",
                                  basis="same_lexical_entity_binding" if same_binding else "labels_do_not_prove_disjointness")
            marker = (first["path"], second["path"], first["kind"], second["kind"])
            if marker in seen:
                continue
            seen.add(marker)
            result.append({"address": list(second["path"]), "target_read_address": list(first["path"]),
                           "target_read_kind": first["kind"], "outer_read_kind": second["kind"],
                           "dependency_kind": "shared_graph_read_domain", "physical_effect": "unknown", **record})
    return result


def _proof_spec(graph_state):
    spec = graph_state.spec
    if spec.counts()["nodes"] <= 5000:
        return spec, "full_graph"
    evidence = graph_state.population.evidence
    if not evidence.get("growth_certificates"):
        raise PopulationError("Large dependency model requires a growth certificate")
    return GraphSpec.from_dict(evidence["validation_graph_spec"]), "certified_proof_graph"


def _local_forest_address(address):
    address = tuple(address)
    if address[:1] == ("query",):
        return address[1:]
    if address[:2] == ("alternatives", 0):
        return address[2:]
    return address


def _global_mask_evidence(tree, steps, trace, evidence):
    """Find a masking fact whose value is independent of model compression."""
    scalar_certificates = {
        (_local_forest_address(item.get("address", ())), item.get("relation"))
        for item in evidence.get("scalar_value_certificates", ())
        if item.get("proof") == "constant_scalar_under_growth"
    }
    for step in steps:
        if step.get("logical_demand") != "masked":
            continue
        address = tuple(step["address"])
        node = node_at(tree, address)
        if step.get("reason") == "unobserved_projection":
            return {"kind": "syntactic_unobserved_projection", "address": list(address),
                    "row_consumers": step.get("row_consumers", [])}
        if isinstance(node, ir.RowLimit) and node.limit == 0 or isinstance(node, ir.Query) and node.limit == 0:
            return {"kind": "syntactic_zero_limit", "address": list(address)}
        inputs = trace.inputs.get(address, ())
        if isinstance(node, ir.Call) and node.name == "coalesce":
            for i, term in enumerate(node.args[:step["child_index"]]):
                location = (*address, "args", i)
                if isinstance(term, ir.Literal) and term.value is not None:
                    return {"kind": "constant_nonnull_prefix", "address": list(location)}
                if ((location, "nonnull") in scalar_certificates and inputs
                        and all(trace.expression(term, env) is not None for env in inputs)):
                    return {"kind": "M3_growth_stable_nonnull_prefix", "address": list(location),
                            "certificate": "scalar_value_certificates/constant_scalar_under_growth"}
        if isinstance(node, ir.Boolean):
            decisive = False if node.operator == "AND" else True
            for i, term in enumerate(node.terms):
                if i == step["child_index"]:
                    continue
                location = (*address, "terms", i)
                if isinstance(term, ir.Literal) and term.value is decisive:
                    return {"kind": "constant_decisive_boolean", "address": list(location)}
                if ((location, "equal") in scalar_certificates and inputs
                        and all(trace.expression(term, env) is decisive for env in inputs)):
                    return {"kind": "M3_growth_stable_decisive_boolean", "address": list(location),
                            "certificate": "scalar_value_certificates/constant_scalar_under_growth"}
        if isinstance(node, (ir.Case, ir.ConditionalQuery)):
            location = (*address, "condition")
            if isinstance(node.condition, ir.Literal):
                return {"kind": "constant_branch_condition", "address": list(location)}
            if (location, "equal") in scalar_certificates:
                return {"kind": "M3_growth_stable_branch_condition", "address": list(location),
                        "certificate": "scalar_value_certificates/constant_scalar_under_growth"}
    return None


def check_dependencies(pair, embedded, graph_state, evidence_context=None):
    """Check the changed site's semantic relation and outward logical demand."""
    static = {"target_id": embedded.target_id, "graph_digest": digest(graph_state.graph_spec),
              "query_digest": digest({"a": ir.to_data(pair.query_a), "b": ir.to_data(pair.query_b)}),
              "bindings_digest": digest(pair.bindings), "embedded_digest": embedded.embedded_digest,
              "result_relation": "unknown", "unknown_reasons": [], "physical_invocations": "unknown",
              "physical_cost_guaranteed": False}
    runtime = {"status": "not_run", "result_relation": "not_run", "physical_plan_mapping": "unknown",
               "physical_invocations": "unknown"}
    try:
        if (static["graph_digest"] != graph_state.graph_digest or
                digest(graph_state.population.spec.to_dict()) != graph_state.graph_digest or
                embedded.embedded_digest != graph_state.embedded_digest):
            raise StageFailure("M6", "contract_drift", "Graph or embedded AST changed after construction")
        mutation = pair.mutation_record
        if mutation.get("method_stage") == "M5":
            expected = {"target_id": embedded.target_id, "graph_digest": graph_state.graph_digest,
                        "embedded_digest": embedded.embedded_digest,
                        "source_digest": digest(pair.query_a), "target_digest": digest(pair.query_b)}
            if any(mutation.get(key) != value for key, value in expected.items()):
                raise StageFailure("M6", "contract_drift", "The pair no longer matches the M5 mutation checkpoint")
        static["binding_plan_digest"] = mutation.get("binding_plan_digest")
        pattern = pair.mutation_record["paper_pattern"]
        form = pair.mutation_record["site"].get("form")
        address = tuple(pair.mutation_record["site"]["address"])
        site = select_mutation_site(pair.query_a, pattern, form, address)
        propagation = propagate_equivalence(pair.query_a, site, bindings=pair.bindings)
        spec, scope = _proof_spec(graph_state)
        left, right = evaluate_query(pair.query_a, spec), evaluate_query(pair.query_b, spec)
        expected = graph_state.population.expected_rows
        if Counter(_typed(row) for row in left) != Counter(_typed(row) for row in right):
            static["result_relation"] = "refuted"
        elif Counter(_typed(row) for row in left) != Counter(_typed(row) for row in expected):
            static["result_relation"] = "refuted"
        else:
            static["result_relation"] = "proved"
        analysis = analyze_dependencies(pair.query_a)
        trace = _LogicalTrace(spec)
        trace.run_at(pair.query_a)
        entry_address = tuple(site.guards.get("envelope_site", address))
        entries = trace.inputs.get(entry_address, ())
        syntactic_entries = entries
        if not syntactic_entries:
            for length in range(len(entry_address), -1, -1):
                syntactic_entries = trace.syntactic_inputs.get(entry_address[:length], ())
                if syntactic_entries:
                    break
        steps = _ancestor_demands(pair.query_a, address, trace)
        external_steps = _ancestor_demands(pair.query_a, entry_address, trace)
        masked = any(step["logical_demand"] == "masked" for step in steps)
        external_mask = any(step["logical_demand"] == "masked" for step in external_steps)
        bounded = any(step["logical_demand"] in ("witness", "bounded", "conditional") for step in external_steps)
        exposure = "masked" if external_mask else "exposed" if entries else "unreachable"
        if not entries and any(step["logical_demand"] == "conditional" for step in external_steps):
            exposure = "conditional"
        counts = Counter(digest(_environment(env)) for env in entries)
        potential = {side: _candidate_reads(tree, address, entries, spec)
                     for side, tree in (("a", pair.query_a), ("b", pair.query_b))}
        contexts, affected_symbols = _context_patterns(pair.query_a, address, trace, analysis)
        compressed = scope != "full_graph"
        global_mask = (_global_mask_evidence(pair.query_a, steps, trace, graph_state.population.evidence)
                       if compressed and masked else None)
        known_mask = masked and (not compressed or global_mask is not None)
        observed_input_interval = [0 if bounded else len(entries), len(entries)]
        observed_request_interval = [0, 0] if masked else observed_input_interval
        input_interval = [0, None] if compressed else observed_input_interval
        request_interval = [0, 0] if known_mask else [0, None] if compressed else observed_request_interval
        for step in steps:
            step["observation_scope"] = scope
        for context in contexts:
            context["input_observation_scope"] = scope
            context["proof_model_semantic_input_count"] = context["semantic_input_count"]
            context["semantic_input_cardinality_interval"] = ([0, None] if compressed else
                                                              [context["semantic_input_count"], context["semantic_input_count"]])
            if compressed:
                context["semantic_input_count"] = None
        for reads in potential.values():
            for read in reads:
                read["candidate_count_scope"] = scope
                if compressed:
                    read["proof_model_per_input_candidates"] = read.pop("per_input_candidates")
                    read["proof_model_total_candidates"] = read.pop("total_candidates")
                    read["full_graph_candidate_interval"] = [0, None]
        static.update(semantic_propagation=propagation, analysis=analysis.to_dict(),
                      input_observation_scope=scope, ancestor_steps=steps,
                      logical_demand="masked" if known_mask else "unknown" if compressed and masked else "bounded" if bounded else "full",
                      value_dependency="unused" if known_mask else "unknown" if compressed and masked else "conditional" if bounded else "used",
                      entry_address=list(entry_address), changed_work_address=list(address),
                      input_cardinality_interval=input_interval,
                      distinct_input_bindings=None if compressed else len(counts),
                      input_multiplicities=None if compressed else sorted(counts.values()),
                      input_bindings=[_environment(env) for env in entries[:64]],
                      input_bindings_scope=scope,
                      proof_model_input_cardinality_interval=observed_input_interval,
                      proof_model_distinct_input_bindings=len(counts),
                      proof_model_input_multiplicities=sorted(counts.values()),
                      proof_model_syntactic_entry_count=len(syntactic_entries),
                      proof_model_logical_request_interval=observed_request_interval,
                      proof_model_exposure_status=exposure,
                      global_mask_evidence=global_mask,
                      potential_matching_reads=potential,
                      potential_work_interpretation="Counterfactual local enumeration; not DBMS visits or a runtime bound",
                      logical_request_interval=request_interval,
                      exposure_status=exposure,
                      exposure_observation_scope=scope,
                      protected_work_masked=known_mask and not external_mask,
                      target_valid=static["result_relation"] == "proved" and bool(entries) and not external_mask,
                      target_valid_scope="M3_preserved_construction_witness" if compressed else "full_graph",
                      target_entry_evidence={"scope": scope, "proof_model_entries": len(entries),
                          "basis": "M3 construction and preserved stable witness" if compressed else "complete logical graph observation",
                          "construction": graph_state.population.evidence.get("construction"),
                          "witness_certificate": graph_state.population.evidence.get("witness_uid_allocation"),
                          "cardinality_certified": not compressed,
                          "required_state_checks": "M3 matching/path checks retained for loaded-state validation"},
                      scope_binding_edges=analysis.to_dict()["uses"],
                      row_consumption_edges=analysis.to_dict()["row_uses"],
                      context_patterns=contexts, affected_symbols=affected_symbols,
                      shared_graph_reads={side: _shared_reads(tree, address, graph_state.spec)
                                          for side, tree in (("a", pair.query_a), ("b", pair.query_b))},
                      growth_read_dependencies=graph_state.population.evidence.get("growth_certificates", []),
                      cross_pattern_effects=[{"instance_id": item.instance_id,
                          "registration": "registered", "pattern_id": item.pattern_id,
                          "premise_status": "satisfied" if item.instance_id == embedded.target_id and static["result_relation"] == "proved" else "unknown",
                          "exposure_status": exposure if item.instance_id == embedded.target_id else "unknown"}
                          for item in embedded.instances] + contexts)
    except StageFailure:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        static["unknown_reasons"].append(f"{type(exc).__name__}: {exc}")
    static["certificate_digest"] = digest(static)
    report = DependencyReport(static, runtime)
    if evidence_context is not None:
        from .evidence_adapter import runtime_evidence
        runtime = runtime_evidence(evidence_context, graph_digest=static["graph_digest"],
                                   query_digest=static["query_digest"], target_id=embedded.target_id)
        report = DependencyReport(static, runtime)
    return report
