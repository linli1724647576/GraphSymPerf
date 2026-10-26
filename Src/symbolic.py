"""Recursive symbolic skeletons and the two paper algorithm stages.

Algorithm 1 fixes a bounded syntax substitution, then searches a graph model.
Substituted terms inside that search are candidates, not emitted test queries.
Algorithm 2 materializes exactly the accepted substitution, copies the query,
and applies the marked law. It never changes a threshold to fit a graph.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import json
import random

import ir
from composition import compose_fragment, composition_options
from dependencies import analyze_dependencies, find_mutation_sites, node_at, replace_at, propagate_equivalence
from paper_patterns import compositional_rule
from population import PopulationError, PopulationResult, evaluate_query, synthesize_state
from row_grammar import RowGrammar
from scalar_grammar import ScalarGrammar


_SCALAR_LAWS = {"P4": ("and", "commute"), "P5": ("or", "commute"),
                "P13": ("case", "dual"), "S1": ("or", "replace"),
                "S2": ("and", "replace"), "S4": ("case", "replace"),
                "S7": ("coalesce", "replace")}


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    candidates: tuple

    def __post_init__(self):
        if not self.name or self.kind not in {"identifier", "integer", "scalar"} or not self.candidates:
            raise ValueError("A symbol requires a name, supported type and nonempty finite domain")
        if self.kind == "identifier" and any(type(value) is not str for value in self.candidates):
            raise ValueError("Identifier symbols require string candidates")
        if self.kind == "integer" and any(type(value) is not int for value in self.candidates):
            raise ValueError("Integer symbols require integer candidates")
        for value in self.candidates:
            if self.kind == "identifier":
                ir.validate_query(ir.Query(ir.Node("binding", (value,)), None, (ir.Literal(1),)))
            else:
                ir.validate_query(ir.Query(None, None, (ir.Literal(value),)))


@dataclass(frozen=True)
class Hole:
    name: str
    kind: str
    candidates: tuple

    def __post_init__(self):
        if not self.name or self.kind not in {"rows", "predicate", "scalar"} or not self.candidates:
            raise ValueError("A hole requires a name, supported interface and candidates")


def _walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from _walk(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from _walk(item, (*address, index))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, (*address, key))


def _encode(value):
    if is_dataclass(value):
        return {"node": type(value).__name__, **{field.name: _encode(getattr(value, field.name)) for field in fields(value)}}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Unsupported symbolic skeleton value")


def _decode(value):
    if isinstance(value, list):
        return tuple(_decode(item) for item in value)
    if isinstance(value, dict):
        if "node" not in value:
            return {key: _decode(item) for key, item in value.items()}
        registry = {name: getattr(ir, name) for name in (
            "Literal", "Property", "Variable", "Comparison", "Boolean", "Negation", "Node", "Relationship",
            "Edge", "Exists", "Count", "Call", "Case", "ListExpr", "Concat", "All", "Alias", "Match",
            "With", "Query", "UnionAll", "RowLimit", "ConditionalQuery")}
        registry.update(Symbol=Symbol, Hole=Hole, SymbolicSkeleton=SymbolicSkeleton, MutationMark=MutationMark)
        cls = registry.get(value["node"])
        if cls is SymbolicSkeleton and "intents" not in value:
            value = {**value, "intents": []}
        if cls is SymbolicSkeleton:
            value = {"mark": None, "goals": [], **value}
        if cls is None or set(value) != {"node", *(field.name for field in fields(cls))}:
            raise ValueError("Unknown symbolic node or fields")
        return cls(**{key: _decode(item) for key, item in value.items() if key != "node"})
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _population_contract(population):
    return {"graph_spec": population.spec.to_dict(), "expected_rows": population.expected_rows,
            "checks": population.checks, "constraints": population.constraints, "evidence": population.evidence}


@dataclass(frozen=True)
class MutationMark:
    """One chosen lexical occurrence; other applicable occurrences may coexist."""
    site_id: str
    address: tuple

    def __post_init__(self):
        if not self.site_id or not isinstance(self.site_id, str) or not isinstance(self.address, tuple):
            raise ValueError("A mutation mark requires a stable ID and a structural address")
        if any(not (isinstance(part, str) or type(part) is int and part >= 0) for part in self.address):
            raise ValueError("A mutation mark contains an invalid address")


@dataclass(frozen=True)
class SymbolicSkeleton:
    paper_pattern: str
    form: str
    source: object
    replacement: object | None
    anchor: ir.Node
    options: dict
    seed: int
    intents: tuple = ()
    mark: MutationMark | None = None
    goals: tuple = ()

    def to_data(self):
        return {"version": 1, "skeleton": _encode(self)}

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict) or set(data) != {"version", "skeleton"} or data["version"] != 1:
            raise ValueError("Unsupported symbolic skeleton archive")
        result = _decode(data["skeleton"])
        if not isinstance(result, cls):
            raise ValueError("Archive is not a symbolic skeleton")
        compositional_rule(result.paper_pattern, result.form)
        return result


@dataclass(frozen=True)
class SolvedSkeleton:
    population: PopulationResult
    assignment: dict
    construction_record: dict


@dataclass(frozen=True)
class QueryPair:
    query_a: object
    query_b: object
    bindings: dict
    mutation_record: dict
    instantiation_record: dict
    checks: tuple[dict, ...] = ()


def _flag(variable, key, *, port=None):
    identity = port or variable
    key = Symbol(f"property:{identity}:{key}", "identifier", (key,))
    prop = ir.Property(variable, key)
    return Hole(f"predicate:{identity}:{key.name}", "predicate", (
        ir.Comparison(prop, "=", ir.Literal(True)),
        ir.Negation(ir.Comparison(prop, "=", ir.Literal(False))),
        ir.Comparison(prop, "<>", ir.Literal(False))))


def _region(anchor, name):
    """One row-producing syntax production; quantities and realization are separate."""
    variable = "item" if anchor.variable != "item" else "item_0"
    endpoint = ir.Node(variable, (Symbol(f"label:{name}", "identifier", (f"Gsp_{name}", f"Region_{name}")),))
    rel = ir.Relationship(type=Symbol(f"type:{name}", "identifier", (f"GSP_{name}", f"EDGE_{name}")))
    edge = ir.Edge(anchor, rel, endpoint)
    constant = ir.Literal(Symbol("projection:constant", "scalar", (1, 7)))
    return Hole(name, "rows", (ir.Query(edge, _flag(variable, "keep", port=f"region:{name}"),
                                          (ir.Alias(constant, "c"),)),))


def _fragment_address(tree, fragment):
    """Locate a grammar production before substitution, without schema matching."""
    found = []
    def visit(value, path):
        if value is fragment:
            found.append(path)
        if isinstance(value, Hole):
            for choice in value.candidates:
                visit(choice, path)
        elif is_dataclass(value):
            for field in fields(value):
                visit(getattr(value, field.name), (*path, field.name))
        elif isinstance(value, tuple):
            for index, child in enumerate(value):
                visit(child, (*path, index))
    visit(tree, ())
    if len(found) != 1:
        raise ValueError("The marked production must have one lexical occurrence")
    return found[0]


def _recursive_rows(pattern, anchor, options, seed, *, form=None):
    """Choose syntax budgets, then use the same typed row productions."""
    matching_law = pattern in {"P10", "P11"}
    path_law = pattern == "P3"
    binding = {"independent": "fresh", "shared_anchor": "shared", "optional_branches": "shared"}.get(form, "chain")
    optional = pattern == "P11"
    default_length = random.Random(seed).randint(2, 4) if matching_law and binding == "chain" else 2 if matching_law else 1
    length = 1 if path_law else options.setdefault("chain_length", default_length)
    depth = options.setdefault("conditional_depth", 0 if matching_law or path_law else random.Random(seed).randint(1, 3))
    if matching_law and length < 2 or not (matching_law or path_law) and depth < 1:
        raise ValueError("The selected law requires a matching pair or a conditional envelope")
    kind = options.setdefault("guard_kind", random.Random(seed).choice(("property", "count", "exists")))
    grammar = RowGrammar(anchor, seed=seed, symbol=Symbol, flag=_flag, region=_region)
    rows = grammar.expand(length=length, depth=depth, guard_kind=kind,
                          branch_path=options.get("branch_path"),
                          projection="bindings" if depth == 0 else "constant", binding=binding, optional=optional,
                          path_bounds=options["path_bounds"] if path_law else None)
    options["branch_path"] = rows.branch_path
    site = rows.matching_site if matching_law or path_law else rows.conditional_site
    suffix = () if matching_law or path_law else ("otherwise",)
    replacement = None if matching_law or path_law else grammar.matching(length, name="replacement", projection="constant")
    return rows.source, site, suffix, replacement, rows.binding_ports


def _recursive_scalar(pattern, anchor, options, seed):
    """A law supplies an operator/port; observations supply value goals."""
    operator, action = _SCALAR_LAWS[pattern]
    kind = options.setdefault("scalar_type", "boolean" if operator in {"and", "or"} else "integer")
    if operator in {"and", "or"} and kind != "boolean":
        raise ValueError("A Boolean law requires scalar_type=boolean")
    default = ({"boolean": operator != "and", "integer": 7, "float": 1.5, "string": "kept"}[kind])
    expected = options.setdefault("scalar_value", default)
    expected_type = {"boolean": bool, "integer": int, "float": float, "string": str}[kind]
    if expected is not None and type(expected) is not expected_type:
        raise ValueError("scalar_value does not match its declared scalar_type")
    length = options.setdefault("chain_length", 1)
    depth = options.get("scalar_depth", random.Random(seed).randint(1, 3))
    grammar = ScalarGrammar(anchor, seed=seed, symbol=Symbol, flag=_flag, region=_region, length=length)
    produced = grammar.expand(operator, action, kind=kind, depth=depth, contexts=options.get("scalar_contexts"))
    options.pop("scalar_depth", None)
    options["scalar_contexts"] = produced.contexts
    column = "predicate" if pattern == "P4" else "value"
    kernel = ir.Query(anchor, None, (ir.Property(anchor.variable, "uid"), ir.Alias(produced.source, column)))
    return kernel, produced, expected


def build_symbolic_skeleton(pattern, options, seed=0):
    if not isinstance(options, dict):
        raise ValueError("composition requires an options dictionary")
    rule = compositional_rule(pattern, options.get("form"))
    if type(seed) is not int or seed < 0:
        raise ValueError("The query seed must be nonnegative")
    if pattern != "P3" and "path_bounds" in options:
        raise ValueError("path_bounds requires P3")
    if pattern != "S6" and "row_limit" in options:
        raise ValueError("row_limit requires S6")
    if pattern != "P10" and "form" in options:
        raise ValueError("form requires P10")
    if pattern != "P11" and "match_state" in options:
        raise ValueError("match_state requires P11")
    recursive_rows = pattern in {"S3", "P10", "P11", "P3"}
    recursive_scalar = pattern in _SCALAR_LAWS
    if "chain_length" in options and not (recursive_rows and pattern != "P3" or recursive_scalar):
        raise ValueError("chain_length requires a row-producing grammar")
    if not recursive_rows and any(key in options for key in ("conditional_depth", "guard_kind", "branch_path")):
        raise ValueError("Row grammar options require a matching-chain or conditional row law")
    if not recursive_scalar and any(key in options for key in ("scalar_depth", "scalar_type", "scalar_value", "scalar_contexts")):
        raise ValueError("Scalar grammar options require a scalar law")
    if options.get("match_state", "both") not in {"both", "left_only", "right_only", "neither"}:
        raise ValueError("match_state must be both, left_only, right_only or neither")
    options = composition_options(options, seed)
    if pattern != "P3":
        options.pop("path_bounds")
    anchor_label = "GspLow" if pattern == "P3" else "GspRoot"
    anchor = ir.Node("u", (Symbol("label:anchor", "identifier", (anchor_label,)),),
                     ((Symbol("key:anchor", "identifier", ("id",)), ir.Literal(Symbol("value:anchor", "integer", (1,)))),))
    replacement = None
    intents = []
    marked_fragment, marked_suffix, goals = None, (), ()
    scalar_observation = None
    binding_observations = ()

    def quantity(subject, role, valid=1, growth="candidates", relationship_type=None):
        intents.append({"kind": "quantity", "subject": subject, "role": role,
                        "valid": valid, "growth": growth, "relationship_type": relationship_type})
        return subject

    def region(name, valid=1, growth="candidates", root=anchor):
        term = _region(root, name)
        quantity(term.candidates[0].pattern, name, valid, growth)
        return term

    def edge(root, variable, name, *, valid=1, growth="candidates", direction="out", labels=None, alternatives=()):
        target = ir.Node(variable, labels or (Symbol(f"label:{name}", "identifier", (f"Gsp_{name}",)),))
        result = ir.Edge(root, ir.Relationship(type=Symbol(f"type:{name}", "identifier", (f"GSP_{name}",)),
                                               alternatives=alternatives), target, direction)
        quantity(result, name, valid, growth, result.relationship.type if alternatives else None)
        return result

    if recursive_scalar:
        kernel, produced, expected_value = _recursive_scalar(pattern, anchor, options, seed)
        marked_fragment, marked_suffix = produced.marked, produced.suffix
        replacement = produced.replacement
        scalar_observation = produced.source
        goals = ({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed})
    elif pattern == "P3":
        kernel, marked_fragment, marked_suffix, replacement, _ = _recursive_rows(pattern, anchor, options, seed)
        goals = ({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed})
    elif pattern in {"S6", "S8"}:
        union = ir.UnionAll((_region(anchor, "witness"), _region(anchor, "low")))
        replacement = _region(anchor, "high")
        if pattern == "S6":
            count = options.setdefault("row_limit", 3)
            kernel = ir.RowLimit(union, Symbol("limit", "integer", (count,)))
        else:
            kernel = ir.Query(anchor, None, (ir.Property("u", "uid"), ir.Alias(ir.Exists(body=union), "value")))
    elif pattern in {"P1", "P2", "P8"}:
        alternatives = (Symbol("type:alternative", "identifier", ("GSP_ALT",)),) if pattern == "P8" else ()
        relation = edge(anchor, "v", "edge", direction="undirected" if pattern == "P1" else "out",
                        alternatives=alternatives)
        kernel = ir.Query(relation, _flag("v", "keep"), (ir.Property("u", "uid"), ir.Property("v", "uid")))
        if pattern == "P2":
            intents.append({"kind": "inverse", "forward": relation.relationship.type,
                            "reverse": Symbol("type:inverse", "identifier", ("GSP_INVERSE",)), "pair_key": "pair"})
    elif pattern == "P6":
        # Only the equality itself is a law site; anchor lookup is not a second equality AST.
        node = ir.Node("v", (Symbol("label:equality", "identifier", ("GspLookup",)),))
        quantity(node, "lookup")
        kernel = ir.Query(node, ir.Comparison(ir.Property("v", "lookup"), "=", ir.Literal(1)),
                          (ir.Property("v", "uid"),))
    elif pattern == "P7":
        node = ir.Node("v", (Symbol("label:rare", "identifier", ("GspRare",)),
                             Symbol("label:common", "identifier", ("GspCommon",))))
        quantity(node, "label_intersection")
        kernel = ir.Query(node, None, (ir.Property("v", "uid"),))
    elif pattern == "P9":
        relation = edge(anchor, "v", "cost", labels=("GspCost", "GspAccepted"))
        kernel = ir.Query(anchor, _flag("u", "enabled"), (ir.Property("u", "uid"), ir.Property("v", "uid")),
                          matches=(ir.Match(relation),))
    elif pattern in {"P10", "P11"}:
        kernel, marked_fragment, marked_suffix, replacement, binding_observations = _recursive_rows(
            pattern, anchor, options, seed, form=rule.form)
        goals = ({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed})
    elif pattern == "P12":
        kernel = ir.UnionAll((region("low", growth="none"), region("cost")))
    elif pattern == "S3":
        kernel, marked_fragment, marked_suffix, replacement, _ = _recursive_rows(pattern, anchor, options, seed)
        goals = ({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed})
    elif pattern == "S5":
        kernel = ir.RowLimit(region("low", growth="none"), 0)
        replacement = region("high", growth="valid")
    elif pattern == "S9":
        root = ir.Node("r", (Symbol("label:second_root", "identifier", ("GspSecondRoot",)),), (("id", ir.Literal(2)),))
        low = region("low", growth="none", root=root).candidates[0]
        high = region("high", growth="valid", root=root).candidates[0]
        kernel = ir.Query(anchor, _flag("u", "enabled"), (ir.Property("u", "uid"),),
                          matches=(ir.Match(low.pattern, low.where),))
        replacement = ir.Match(high.pattern, high.where)
    elif pattern == "S10":
        low = region("low", growth="none").candidates[0]
        high = region("high", growth="valid").candidates[0]
        nullsafe = ir.Comparison(ir.Call("coalesce", (ir.Property("item", "keep"), ir.Literal(False))), "=", ir.Literal(True))
        low = ir.Query(low.pattern, nullsafe, low.returns)
        high = ir.Query(high.pattern, nullsafe, high.returns)
        kernel = ir.Query(anchor, ir.Boolean("AND", (_flag("u", "enabled"), ir.Negation(ir.Exists(body=low)))),
                          (ir.Property("u", "uid"),), distinct=True)
        replacement = high
    else:
        raise ValueError(f"No symbolic grammar production for {pattern}")
    source = compose_fragment(kernel, "rows", anchor, options["contexts"], threshold=options["threshold"])
    mark = (MutationMark("test-site:0", (*_fragment_address(source, marked_fragment), *marked_suffix))
            if marked_fragment is not None else None)
    if scalar_observation is not None:
        goals += ({"kind": "value", "address": ("query", *_fragment_address(source, scalar_observation)),
                   "relation": "equal", "value": expected_value},)
    if binding_observations:
        state = options.get("match_state", "both")
        matched = (state in {"both", "left_only"}, state in {"both", "right_only"})
        goals += tuple({"kind": "binding", "address": ("query", *_fragment_address(source, node)),
                        "relation": "nonnull" if present else "null"}
                       for node, present in zip(binding_observations, matched))
    options = {key: tuple(value) if isinstance(value, list) else value for key, value in options.items()}
    return SymbolicSkeleton(pattern, rule.form, source, replacement, anchor, options, seed, tuple(intents), mark, goals)


def _assignment(skeleton):
    rng = random.Random(skeleton.seed)
    symbols, holes = {}, {}
    for tree in (skeleton.source, skeleton.replacement, skeleton.anchor, skeleton.intents):
        for _, value in _walk(tree):
            target = symbols if isinstance(value, Symbol) else holes if isinstance(value, Hole) else None
            if target is None:
                continue
            if value.name in target and target[value.name] != value:
                raise ValueError("A shared symbolic name has incompatible definitions")
            target[value.name] = value
    return {"symbols": {name: rng.choice(value.candidates) for name, value in sorted(symbols.items())},
            "holes": {name: rng.randrange(len(value.candidates)) for name, value in sorted(holes.items())}}


def _resolve(value, assignment):
    if isinstance(value, Symbol):
        chosen = assignment["symbols"].get(value.name)
        if not any(type(chosen) is type(candidate) and chosen == candidate for candidate in value.candidates):
            raise ValueError("A symbolic assignment is outside its declared domain")
        return chosen
    if isinstance(value, Hole):
        index = assignment["holes"].get(value.name)
        if type(index) is not int or not 0 <= index < len(value.candidates):
            raise ValueError("A hole assignment is outside its candidate domain")
        result = _resolve(value.candidates[index], assignment)
        if value.kind == "rows" and not isinstance(result, ir.RowStream):
            raise ValueError("A row hole requires a row-producing term")
        if value.kind == "predicate" and not isinstance(result, (ir.Comparison, ir.Boolean, ir.Negation, ir.Exists)):
            raise ValueError("A predicate hole requires a Boolean term")
        if value.kind == "scalar" and not isinstance(result, ir.Expression):
            raise ValueError("A scalar hole requires an expression, not a row stream")
        return result
    if is_dataclass(value):
        return type(value)(**{field.name: _resolve(getattr(value, field.name), assignment) for field in fields(value)})
    if isinstance(value, tuple):
        return tuple(_resolve(item, assignment) for item in value)
    if isinstance(value, dict):
        return {key: _resolve(item, assignment) for key, item in value.items()}
    return value


def construct_state(skeleton, *, scale, data_seed):
    """Algorithm 1: accept a joint model and binding before query emission."""
    assignment = _assignment(skeleton)
    candidate = _resolve(skeleton.source, assignment)
    ir.validate_query(candidate)
    dependencies = analyze_dependencies(candidate)
    if skeleton.mark is not None:
        from dependencies import select_mutation_site
        site = select_mutation_site(candidate, skeleton.paper_pattern, skeleton.form, skeleton.mark.address)
    else:
        sites = find_mutation_sites(candidate, skeleton.paper_pattern, skeleton.form)
        if len(sites) != 1:
            raise PopulationError("A skeleton requires exactly one applicable marked law or an explicit mark")
        site = sites[0]
    requirement_evidence = None
    if skeleton.goals:
        from mutator import plan_mutation_with_correspondence
        from population import synthesize_relational_state
        from requirements import derive_requirement_plans
        replacement = _resolve(skeleton.replacement, assignment)
        alternative, correspondence = plan_mutation_with_correspondence(candidate, site, replacement=replacement)
        failure = None
        minimum = max((goal.get("min_rows", 0) for goal in skeleton.goals if goal["kind"] == "output"), default=0)
        for plan in derive_requirement_plans(candidate, alternative, site, goals=skeleton.goals,
                                             scale=scale, correspondence=correspondence):
            try:
                populated = synthesize_relational_state(candidate, alternatives=(alternative,), scale=scale,
                    data_seed=data_seed, demands=plan.demands, obligations=plan.obligations,
                    correspondence=correspondence, min_result_rows=minimum)
                requirement_evidence = {"goals": _encode(skeleton.goals), "derivation": _encode(plan.evidence),
                                        "demands": _encode(plan.demands), "obligations": _encode(plan.obligations),
                                        "correspondence": _encode(correspondence)}
                populated.constraints["semantic_requirements"] = requirement_evidence
                break
            except PopulationError as error:
                failure = error
        else:
            raise PopulationError("No shared state satisfies the semantic and work goals"
                                  + (f": {failure}" if failure else ""))
    elif skeleton.intents:
        from mutator import plan_mutation
        from population import MatchDemand, ValueDemand, InversePair, synthesize_relational_state
        intents = _resolve(skeleton.intents, assignment)
        pairing = next((item for item in intents if item["kind"] == "inverse"), None)
        replacement = _resolve(skeleton.replacement, assignment)
        alternative = plan_mutation(candidate, site, replacement=replacement,
                                    inverse_types=(pairing["forward"], pairing["reverse"]) if pairing else None)

        def same_atom(left, right):
            if isinstance(left, ir.Node) and isinstance(right, ir.Node):
                return left == right
            if not isinstance(left, ir.Edge) or not isinstance(right, ir.Edge):
                return False
            types = lambda edge: {edge.relationship.type, *edge.relationship.alternatives}
            equal_types = types(left) == types(right)
            if pairing and types(left) | types(right) == {pairing["forward"], pairing["reverse"]}:
                equal_types = True
            return (equal_types and {left.left, left.right} == {right.left, right.right}
                    and left.relationship.min_hops == right.relationship.min_hops
                    and left.relationship.max_hops == right.relationship.max_hops)

        demands = []
        for prefix, tree in ((('query',), candidate), (('alternatives', 0), alternative)):
            for address, atom in _walk(tree):
                for item in intents:
                    if item["kind"] != "quantity" or not same_atom(atom, item["subject"]):
                        continue
                    growth, valid = item["growth"], item["valid"]
                    demands.append(MatchDemand((*prefix, *address), item["role"],
                                               exact_matches=None if growth == "valid" else valid,
                                               candidate_matches=scale + valid if growth == "candidates" else None,
                                               scale_matches=growth == "valid",
                                               relationship_type=item.get("relationship_type")))
        obligations = []
        for premise in site.guards.get("premises", ()):
            relation = premise["relation"]
            obligations.append(ValueDemand(("query", *premise["address"]), relation == "true",
                                           relation="nonnull" if relation == "nonnull" else "equal"))
        if pairing:
            obligations.append(InversePair(pairing["forward"], pairing["reverse"], pairing["pair_key"]))
        populated = synthesize_relational_state(candidate, alternatives=(alternative,), scale=scale,
                                                data_seed=data_seed, demands=tuple(demands),
                                                obligations=tuple(obligations))
    elif skeleton.replacement is None:
        populated = synthesize_state(candidate, scale=scale, data_seed=data_seed)
    else:
        from population import MatchDemand, synthesize_symbolic_state
        replacement = _resolve(skeleton.replacement, assignment)
        alternative = replace_at(candidate, site.address, replacement)
        demands = (MatchDemand(("query", *site.address, "pattern"), "low", exact_matches=1),
                   MatchDemand(("alternatives", 0, *site.address, "pattern"), "high", scale_matches=True))
        requested = candidate.limit if isinstance(candidate, ir.RowLimit) else 1
        populated = synthesize_symbolic_state(candidate, demands=demands, alternatives=(alternative,),
                                              scale=scale, data_seed=data_seed, result_rows=requested)
    record = {"algorithm": 1, "stage": "graph_construction", "skeleton_digest": _digest(skeleton.to_data()),
              "assignment_digest": _digest(assignment), "graph_digest": _digest(populated.spec.to_dict()),
              "population_digest": _digest(_population_contract(populated)),
              "site": list(site.address), "dependency_analysis": dependencies.to_dict(),
              "binding_mode": "fixed_syntax_then_joint_state_search", "candidate_terms_are_not_emitted_queries": True}
    if skeleton.mark is not None:
        record["mutation_mark"] = _encode(skeleton.mark)
    if requirement_evidence is not None:
        record["requirements"] = requirement_evidence
    return SolvedSkeleton(populated, assignment, record)


def _checked_observation(anchor, obligation, proof):
    """Close one fact without importing an anchor into its local bindings."""
    expression = obligation["expression"]
    if not obligation["imports"]:
        names = set()
        for _, part in _walk(expression):
            if isinstance(part, (ir.Variable, ir.Alias)):
                names.add(part.name)
            name = getattr(part, "variable", None)
            if isinstance(name, str):
                names.add(name)
        if anchor.variable in names:
            index = 0
            while f"gsp_check_anchor_{index}" in names:
                index += 1
            anchor = replace(anchor, variable=f"gsp_check_anchor_{index}")
    check_tree = ir.Query(anchor, None, (ir.Alias(ir.Property(anchor.variable, "uid"), "anchor_uid"),
                                        ir.Alias(expression, "premise")))
    ir.validate_query(check_tree)
    return {"query": check_tree.render(), "expected_rows": evaluate_query(check_tree, proof)}


def instantiate_pair(skeleton, solved):
    """Algorithm 2: instantiate the accepted model, then copy and mutate."""
    from executor import typed_bag
    from mutator import mutate
    from query import render_query
    from state import GraphSpec

    record = solved.construction_record
    if (record["skeleton_digest"] != _digest(skeleton.to_data())
            or record["assignment_digest"] != _digest(solved.assignment)
            or record["graph_digest"] != _digest(solved.population.spec.to_dict())
            or record["population_digest"] != _digest(_population_contract(solved.population))):
        raise ValueError("A skeleton, assignment or population contract changed after construction")
    if skeleton.goals and record.get("requirements") != solved.population.constraints.get("semantic_requirements"):
        raise ValueError("The accepted semantic requirement contract changed after construction")
    source = _resolve(skeleton.source, solved.assignment)
    ir.validate_query(source)
    if skeleton.mark is not None:
        from dependencies import select_mutation_site
        site = select_mutation_site(source, skeleton.paper_pattern, skeleton.form, skeleton.mark.address)
        if tuple(record["site"]) != site.address or record.get("mutation_mark") != _encode(skeleton.mark):
            raise ValueError("The accepted explicit mutation mark changed after construction")
    else:
        site = next(item for item in find_mutation_sites(source, skeleton.paper_pattern, skeleton.form)
                    if item.address == tuple(record["site"]))
    rule = compositional_rule(skeleton.paper_pattern, skeleton.form)
    bindings = {"paper_pattern": rule.paper_pattern, "legacy_pattern": rule.legacy_pattern,
                "form": rule.form, "mutation_site": list(site.address), "pattern_catalog": "paper-v1"}
    if skeleton.mark is not None:
        bindings["mutation_mark"] = _encode(skeleton.mark)
    checks = []
    proof = GraphSpec.from_dict(solved.population.evidence["validation_graph_spec"])
    if skeleton.paper_pattern in {"S6", "S8"}:
        replacement = _resolve(skeleton.replacement, solved.assignment)
        witness = node_at(source, tuple(site.guards["witness_site"]))
        anchor = _resolve(skeleton.anchor, solved.assignment)
        witness_query = ir.Query(anchor, None, (ir.Alias(ir.Property(anchor.variable, "uid"), "anchor_uid"),
                                               ir.Alias(ir.Count(body=witness), "witness_rows")))
        witness_check = {"query": render_query(witness_query), "expected_rows": evaluate_query(witness_query, proof)}
        checks.append(witness_check)
        bindings.update(replacement=ir.to_data(replacement), witness_check=witness_check,
                        required_state_checks=checks, validation_graph_spec=proof.to_dict(),
                        witness_site=site.guards["witness_site"], union_site=site.guards["union_site"])
    elif skeleton.replacement is not None:
        from dependencies import premise_obligations
        replacement = _resolve(skeleton.replacement, solved.assignment)
        anchor = _resolve(skeleton.anchor, solved.assignment)
        for obligation in premise_obligations(source, site):
            checks.append(_checked_observation(anchor, obligation, proof))
        bindings.update(replacement=ir.encode_fragment(replacement), premise_checks=list(checks),
                        required_state_checks=checks, validation_graph_spec=proof.to_dict())
    inverse = next((item for item in _resolve(skeleton.intents, solved.assignment) if item["kind"] == "inverse"), None)
    if inverse:
        from state import render_state_checks
        forward, reverse, key = inverse["forward"], inverse["reverse"], inverse["pair_key"]
        domains = []
        for name, other in ((forward, reverse), (reverse, forward)):
            count = sum(group.count for group in solved.population.spec._groups()[1] if group.type == name)
            domains.extend(({"kind": "edge_count", "type": name, "expected": count},
                            {"kind": "distinct_values", "type": name, "property": key, "expected": count},
                            {"kind": "inverse_edges", "forward": name, "reverse": other,
                             "pair_key": key, "expected": 0}))
        inverse_checks = render_state_checks(domains)
        checks.extend(inverse_checks)
        bindings.update(inverse_relationship={"forward_type": forward, "reverse_type": reverse, "pair_key": key},
                        inverse_checks=inverse_checks, required_state_checks=checks,
                        validation_graph_spec=proof.to_dict(), inverse_graph_spec=solved.population.spec.to_dict())
    target = mutate(source, rule.legacy_pattern, bindings=bindings)
    if skeleton.goals:
        from dependencies import premise_obligations, verify_rule_premises
        anchor = _resolve(skeleton.anchor, solved.assignment)
        goal_checks, binding_checks = [], []
        for fact in record["requirements"]["obligations"]:
            address, expected = tuple(fact["address"]), fact.get("value")
            if address[:1] == ("query",):
                tree, local = source, address[1:]
            elif address[:2] == ("alternatives", 0):
                tree, local = target, address[2:]
            else:
                raise ValueError("A semantic goal fact has no source or target occurrence")
            if fact["node"] == "EntityBindingDemand":
                from dependencies import entity_binding_observation
                if fact["relation"] not in {"null", "nonnull"}:
                    raise ValueError("An entity binding requires a NULL/non-NULL observation")
                obligation = entity_binding_observation(tree, local, fact["relation"])
                observation = ir.Query(None, None, (ir.Alias(obligation["expression"], "binding_fact"),))
                ir.validate_query(observation)
                rows = [{"binding_fact": True}]
                if typed_bag(evaluate_query(observation, proof)) != typed_bag(rows):
                    raise ValueError("The accepted entity binding is false on its actual row domain")
                check = {"query": render_query(observation), "expected_rows": rows}
                if check not in binding_checks:
                    binding_checks.append(check)
            else:
                if fact["node"] != "ValueDemand" or fact["relation"] not in {"equal", "nonnull"}:
                    raise ValueError("A semantic goal requires a supported checked fact")
                if fact["relation"] == "equal":
                    ir.validate_query(ir.Query(None, None, (ir.Literal(expected),)))
                if any(isinstance(part, ir.Edge) and part.relationship.max_hops is not None
                       for _, part in _walk(tree)):
                    from dependencies import scalar_value_observation
                    obligation = scalar_value_observation(tree, local, fact["relation"], expected)
                    observation = ir.Query(None, None, (ir.Alias(obligation["expression"], "scalar_fact"),))
                    rows = [{"scalar_fact": True}]
                    if typed_bag(evaluate_query(observation, proof)) != typed_bag(rows):
                        raise ValueError("The accepted scalar fact is false on its actual row domain")
                    check = {"query": render_query(observation), "expected_rows": rows}
                else:
                    fact_site = replace(site, guards={"premises": [{"address": list(local),
                                                                   "relation": fact["relation"], "value": expected}]})
                    obligation, = premise_obligations(tree, fact_site)
                    check = _checked_observation(anchor, obligation, proof)
                    verify_rule_premises(tree, fact_site, {"premise_checks": [check], "required_state_checks": [check],
                                                          "validation_graph_spec": proof.to_dict()})
            if check not in goal_checks:
                goal_checks.append(check)
            if check not in checks:
                checks.append(check)
        bindings.update(goal_checks=goal_checks, required_state_checks=checks)
        if binding_checks:
            bindings["binding_checks"] = binding_checks
    matching_checks = solved.population.evidence.get("required_matching_checks", ())
    for check in matching_checks:
        if check not in solved.population.checks:
            raise ValueError("An accepted matching-domain check was dropped from the population contract")
        if check not in checks:
            checks.append(check)
    if matching_checks:
        bindings.update(matching_checks=list(matching_checks), required_state_checks=checks)
    path_checks = solved.population.evidence.get("required_path_checks", ())
    for check in path_checks:
        if check not in solved.population.checks:
            raise ValueError("An accepted path-domain check was dropped from the population contract")
        if check not in checks:
            checks.append(check)
    if path_checks:
        bindings.update(path_checks=list(path_checks), required_state_checks=checks)
    for tree in (source, target):
        if typed_bag(evaluate_query(tree, proof)) != typed_bag(solved.population.expected_rows):
            raise ValueError("The instantiated pair violates its construction result bag")
        if solved.population.spec.counts()["nodes"] <= 5000:
            if typed_bag(evaluate_query(tree, solved.population.spec)) != typed_bag(solved.population.expected_rows):
                raise ValueError("The full graph violates its constructed result bag")
    propagation = propagate_equivalence(source, site, bindings=bindings)
    mutation = {"rule": rule.form, "paper_pattern": rule.paper_pattern, "site": site.to_dict(),
                "direction": "L_to_H" if skeleton.replacement is not None else "symmetry",
                "propagation": propagation, "relation": "bag_equal_per_binding"}
    instantiation = {"algorithm": 2, "stage": "instantiate_then_mutate", "uses_construction_assignment": record["assignment_digest"],
                     "skeleton_digest": record["skeleton_digest"], "graph_digest": record["graph_digest"],
                     "unchanged_graph": True, "marked_site": list(site.address)}
    return QueryPair(source, target, bindings, mutation, instantiation, tuple(checks))
