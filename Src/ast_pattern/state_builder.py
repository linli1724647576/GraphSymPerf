"""M3: solve one fixed embedded AST and freeze its joint terminal binding."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass, replace
from itertools import product
import random

import ir
from population import PopulationError
from dependencies import node_at
from symbolic import (Hole, Symbol, _resolve, _walk, construct_state,
                      _population_contract, _validate_solved_contract)

from .contracts import (BindingPlan, GraphState, Port, StageFailure, TerminalSlot,
                        digest, encode)


@dataclass(frozen=True)
class ConstraintTask:
    address: tuple = ()
    binding_domain_id: str = "actual_inputs"
    required_observation: object = None
    quantifier: str = "forall_reachable"
    provenance: str = "fixed_ast_requirement"


@dataclass(frozen=True)
class ConstraintAlternatives:
    alternatives: tuple = ()
    analysis_status: str = "decomposed"
    reason: str = ""


UNKNOWN = "unknown"


def _truth(operator, values):
    decisive = operator == "OR"
    if any(value is decisive for value in values):
        return decisive
    return None if any(value is None for value in values) else not decisive


def _count_intervals(operator, threshold, desired=True):
    if desired is None:
        return ()
    if not desired:
        operator = {"=": "<>", "<>": "=", "!=": "=", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}[operator]
    raw = {"=": ((threshold, threshold),), "<>": ((0, threshold - 1), (threshold + 1, None)),
           "!=": ((0, threshold - 1), (threshold + 1, None)), "<": ((0, threshold - 1),),
           "<=": ((0, threshold),), ">": ((threshold + 1, None),), ">=": ((threshold, None),)}[operator]
    return tuple((max(0, lower), upper) for lower, upper in raw
                 if upper is None or max(0, lower) <= upper)


def decompose_requirement(fixed_node, requirement, binding_domain=None):
    """Lower a demand without generating any expression or changing its shape.

    Alternatives are disjunctions of task conjunctions, not graph witnesses.
    ``None`` is a known NULL target. Unknown analysis is an explicit status.
    """
    relation = requirement.get("relation", "equal") if isinstance(requirement, dict) else "equal"
    desired = requirement.get("value") if isinstance(requirement, dict) else requirement
    domain = binding_domain or {}
    if isinstance(domain, str):
        domain = {"id": domain}
    if domain.get("analysis_status") == UNKNOWN:
        return ConstraintAlternatives((), UNKNOWN, "input binding domain is not closed")
    if relation == UNKNOWN or isinstance(requirement, dict) and requirement.get("analysis_status") == UNKNOWN:
        return ConstraintAlternatives((), UNKNOWN, "required observation is unknown")
    if domain.get("cardinality") == 0 and domain.get("requires_nonempty", False):
        return ConstraintAlternatives((), "refuted", "the target has no reachable input")
    domain_id, quantifier = domain.get("id", "actual_inputs"), domain.get("quantifier", "forall_reachable")

    def task(path, value, rel="equal"):
        return ConstraintTask(tuple(path), domain_id, {"relation": rel, "value": value}, quantifier)

    if isinstance(fixed_node, ir.Literal):
        actual = fixed_node.value
        satisfied = actual is not None if relation == "nonnull" else type(actual) is type(desired) and actual == desired
        return ConstraintAlternatives(((),) if satisfied else (), "proved" if satisfied else "refuted")
    if relation == "nonnull":
        return ConstraintAlternatives(((task((), None, "nonnull"),),))
    if isinstance(fixed_node, ir.Negation) and (type(desired) is bool or desired is None):
        target = None if desired is None else not desired
        return ConstraintAlternatives(((task(("expression",), target),),))
    if isinstance(fixed_node, ir.Boolean) and fixed_node.operator in ("AND", "OR"):
        if len(fixed_node.terms) > 5:
            return ConstraintAlternatives((), UNKNOWN, "Boolean decomposition exceeds the finite branch budget")
        if type(desired) is not bool and desired is not None:
            return ConstraintAlternatives((), "refuted", "a Boolean demand requires TRUE, FALSE or NULL")
        choices = []
        for values in product((True, False, None), repeat=len(fixed_node.terms)):
            if _truth(fixed_node.operator, values) is desired:
                choices.append(tuple(task(("terms", index), value) for index, value in enumerate(values)))
        return ConstraintAlternatives(tuple(choices))
    if isinstance(fixed_node, ir.Comparison):
        left, right, operator = fixed_node.left, fixed_node.right, fixed_node.operator
        path = ("left",)
        if isinstance(right, ir.Count) and isinstance(left, ir.Literal):
            left, right, path = right, left, ("right",)
            operator = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "<>": "<>", "!=": "!="}.get(operator, operator)
        if isinstance(left, ir.Count) and isinstance(right, ir.Literal) and type(right.value) is int and operator in {"=", "<>", "!=", "<", "<=", ">", ">="}:
            if type(desired) is not bool and desired is not None:
                return ConstraintAlternatives((), "refuted", "a comparison requires a Boolean observation")
            intervals = _count_intervals(operator, right.value, desired)
            return ConstraintAlternatives(tuple((task(path, interval, "integer_interval"),) for interval in intervals),
                                          "decomposed" if intervals else "refuted")
    if isinstance(fixed_node, ir.Count) and type(desired) is int:
        if desired < 0:
            return ConstraintAlternatives((), "refuted", "COUNT cannot be negative")
        return ConstraintAlternatives(((task(("body",) if fixed_node.body is not None else (),
                                            (desired, desired), "row_interval"),),))
    if isinstance(fixed_node, ir.Exists) and type(desired) is bool:
        return ConstraintAlternatives(((task(("body",) if fixed_node.body is not None else (),
                                            (1, None) if desired else (0, 0), "row_interval"),),))
    if isinstance(fixed_node, ir.Case):
        return ConstraintAlternatives(((task(("condition",), True), task(("then",), desired)),
                                       (task(("condition",), False), task(("otherwise",), desired)),
                                       (task(("condition",), None), task(("otherwise",), desired))))
    if isinstance(fixed_node, ir.Call) and fixed_node.name == "coalesce":
        if desired is None:
            return ConstraintAlternatives((tuple(task(("args", i), None) for i in range(len(fixed_node.args))),))
        return ConstraintAlternatives(tuple(tuple([*(task(("args", j), None) for j in range(i)),
                                                   task(("args", i), desired)]) for i in range(len(fixed_node.args))))
    if isinstance(fixed_node, (ir.Property, ir.Variable)):
        return ConstraintAlternatives(((task((), desired),),))
    return ConstraintAlternatives((), UNKNOWN, f"no demand rule for {type(fixed_node).__name__}")


def fixed_skeleton(embedded):
    """Adapt terminal records to the existing semantic solver, preserving topology."""
    def convert(value):
        if isinstance(value, Port):
            raise StageFailure("M3", "unexpanded_structure", "a Pattern port remains after embedding")
        if isinstance(value, Hole):
            raise StageFailure("M3", "unexpanded_structure", "an ordinary structure hole remains after M2")
        if isinstance(value, TerminalSlot):
            if any(candidate is not None and type(candidate) not in (str, bool, int, float) for candidate in value.candidates):
                raise StageFailure("M3", "unexpanded_structure", "a terminal domain contains an AST node")
            kind = "identifier" if value.kind in {"identifier", "label", "relationship_type", "property_key"} else "integer" if value.kind == "integer" else "scalar"
            return Symbol(value.slot_id, kind, value.candidates)
        if isinstance(value, Symbol):
            if any(candidate is not None and type(candidate) not in (str, bool, int, float) for candidate in value.candidates):
                raise StageFailure("M3", "unexpanded_structure", "a symbolic terminal contains AST structure")
            return value
        if is_dataclass(value):
            return replace(value, **{field.name: convert(getattr(value, field.name)) for field in fields(value)})
        if isinstance(value, tuple):
            return tuple(convert(item) for item in value)
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value
    return convert(embedded.skeleton)


def _symbols(skeleton):
    symbols = {}
    for tree in (skeleton.source, skeleton.replacement, skeleton.anchor, skeleton.intents):
        for _, value in _walk(tree):
            if not isinstance(value, Symbol):
                continue
            signature = lambda symbol: (symbol.kind, tuple((type(item).__name__, item) for item in symbol.candidates))
            if value.name in symbols and signature(symbols[value.name]) != signature(value):
                raise StageFailure("M3", "constraint_conflict", "a shared terminal has incompatible domains")
            symbols[value.name] = value
    return symbols


def _assignments(skeleton, seed):
    symbols = _symbols(skeleton)
    rng = random.Random(seed)
    names = sorted(symbols)
    domains = []
    for name in names:
        domain = list(symbols[name].candidates)
        rng.shuffle(domain)
        domains.append(domain)
    for values in product(*domains):
        yield {"symbols": dict(zip(names, values)), "holes": {}}


def _plan_digest(assignments, embedded_digest, graph_digest):
    return digest({"assignments": assignments, "embedded_digest": embedded_digest, "graph_digest": graph_digest})


def validate_state_and_plan(embedded, state, plan):
    """Detect mutation of any accepted mutable graph, assignment or certificate."""
    skeleton = fixed_skeleton(embedded)
    if embedded.embedded_digest != state.embedded_digest or embedded.embedded_digest != plan.embedded_digest:
        raise StageFailure("M4", "contract_drift", "the embedded AST changed after construction")
    if digest(state.graph_spec) != state.graph_digest or state.graph_spec != state.population.spec.to_dict():
        raise StageFailure("M4", "contract_drift", "the accepted graph state changed")
    if plan.graph_digest != state.graph_digest or plan.plan_digest != _plan_digest(plan.slot_assignments, plan.embedded_digest, plan.graph_digest):
        raise StageFailure("M4", "contract_drift", "the accepted joint binding plan changed")
    if plan.slot_assignments != state.solved.assignment.get("symbols"):
        raise StageFailure("M4", "contract_drift", "the plan no longer matches the accepted solver assignment")
    facts = {key: value for key, value in state.facts.items() if key != "facts_digest"}
    if state.facts.get("facts_digest") != digest(facts):
        raise StageFailure("M4", "contract_drift", "construction facts changed")
    try:
        _validate_solved_contract(skeleton, state.solved)
    except ValueError as error:
        raise StageFailure("M4", "contract_drift", str(error)) from error
    return skeleton


def construct_graph_state(embedded, scale_spec, state_seed=0, solver_budget=None):
    """Construct a shared v2 state from normalized demands, never a graph recipe ID."""
    scale = scale_spec.get("scale", scale_spec.get("size")) if isinstance(scale_spec, dict) else scale_spec
    if type(scale) is not int or scale < 1 or type(state_seed) is not int or state_seed < 0:
        raise StageFailure("M3", "invalid_budget", "scale must be positive and state_seed nonnegative")
    limits = solver_budget or {}
    if type(limits) is int:
        limits = {"terminal_assignments": limits}
    if not isinstance(limits, dict) or set(limits) - {"terminal_assignments", "max_nodes", "max_edges"}:
        raise StageFailure("M3", "invalid_budget", "unknown solver budget field")
    max_assignments = limits.get("terminal_assignments", 32)
    for name in ("max_nodes", "max_edges"):
        if name in limits and (type(limits[name]) is not int or limits[name] < 0):
            raise StageFailure("M3", "invalid_budget", f"{name} must be a nonnegative integer")
    if type(max_assignments) is not int or max_assignments < 1:
        raise StageFailure("M3", "invalid_budget", "terminal assignment budget must be positive")
    skeleton = fixed_skeleton(embedded)
    embedded_hash = embedded.embedded_digest
    failures, attempts = [], 0
    for assignment in _assignments(skeleton, state_seed):
        if attempts >= max_assignments:
            break
        attempts += 1
        try:
            candidate = _resolve(skeleton.source, assignment)
            local_lowering = []
            for goal in skeleton.goals:
                address = tuple(goal.get("address", ()))
                if goal.get("kind") != "value" or address[:1] != ("query",):
                    continue
                lowered = decompose_requirement(node_at(candidate, address[1:]), goal,
                                                {"id": "actual_reachable_inputs", "quantifier": "forall_reachable"})
                local_lowering.append({"address": list(address), "result": encode(lowered)})
                if lowered.analysis_status == "refuted":
                    raise PopulationError("a fixed expression contradicts its required value")
                # A local rule may be incomplete; the shared requirement solver
                # must still close the complete binding-domain obligation below.
            solved = construct_state(skeleton, scale=scale, data_seed=state_seed, assignment=assignment)
            if solved.population.spec.version != 2:
                raise ValueError("new AST construction must produce GraphSpec v2")
            counts = solved.population.spec.counts()
            if any(counts[key] > limits.get(name, counts[key])
                   for key, name in (("nodes", "max_nodes"), ("edges", "max_edges"))):
                raise ValueError("the constructed graph exceeds its node/edge budget")
            spec = deepcopy(solved.population.spec.to_dict())
            graph_hash = digest(spec)
            plan = BindingPlan(deepcopy(assignment["symbols"]), embedded_hash, graph_hash,
                               _plan_digest(assignment["symbols"], embedded_hash, graph_hash))
            record = {**deepcopy(solved.construction_record), "method_stage": "M3",
                      "embedded_digest": embedded_hash, "graph_digest_v1": graph_hash,
                      "binding_plan_digest": plan.plan_digest, "terminal_assignment_attempts": attempts,
                      "joint_binding": True, "structure_fixed_before_search": True,
                      "fixed_ast_requirement_lowering": local_lowering}
            facts = {"construction_record": deepcopy(record), "certificate_digest": digest(_population_contract(solved.population))}
            facts["facts_digest"] = digest(facts)
            state = GraphState(spec, solved.population, embedded_hash, graph_hash, solved, facts)
            return state, plan, record
        except (ValueError, PopulationError) as error:
            failures.append(str(error))
    raise StageFailure("M3", "budget_exhausted", "no shared state found within the bounded joint search",
                       evidence={"terminal_assignment_attempts": attempts, "causes": failures[-8:]})
