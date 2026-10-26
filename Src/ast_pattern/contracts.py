"""Serializable stage boundaries. No generator, rule registry or database access."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
import hashlib
import json

from . import GRAMMAR_VERSION, METHOD_VERSION


class StageFailure(ValueError):
    def __init__(self, stage, code, reason, *, evidence=None):
        self.stage, self.code, self.reason = stage, code, str(reason)
        self.evidence = evidence or {}
        super().__init__(f"{stage}/{code}: {reason}")

    def to_data(self):
        return dict(stage=self.stage, code=self.code, reason=self.reason, evidence=encode(self.evidence))


def encode(value):
    if is_dataclass(value):
        return {"node": type(value).__name__, **{
            f.name: encode(getattr(value, f.name)) for f in fields(value) if f.metadata.get("serialize", True)}}
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((encode(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError(f"Unsupported stage record value: {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return tuple(decode(v) for v in value)
    if not isinstance(value, dict):
        return value
    if "node" not in value:
        return {k: decode(v) for k, v in value.items()}
    import ir
    allowed = {name: getattr(ir, name) for name in (
        "Literal", "Property", "Variable", "Comparison", "Boolean", "Negation", "Node", "Relationship",
        "Edge", "Exists", "Count", "Call", "Case", "ListExpr", "Concat", "All", "Alias", "Match", "With",
        "Query", "UnionAll", "RowLimit", "ConditionalQuery")}
    allowed.update({name: cls for name, cls in globals().items()
                    if isinstance(cls, type) and is_dataclass(cls)})
    # Historical symbolic terminals remain readable at the explicit adapter seam.
    from symbolic import Symbol, Hole, MutationMark, SymbolicSkeleton
    allowed.update(Symbol=Symbol, Hole=Hole, MutationMark=MutationMark, SymbolicSkeleton=SymbolicSkeleton)
    cls = allowed.get(value["node"])
    if cls is None:
        raise ValueError(f"Unsupported stage record node: {value['node']}")
    return cls(**{k: decode(v) for k, v in value.items() if k != "node"})


def digest(value):
    return hashlib.sha256(json.dumps(encode(value), sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def shape(value):
    """Query-only signature preserving operators and ordered AST topology.

    Graph identifiers and literal values are leaves, while operator names,
    traversal modes and clause modifiers are structural choices. This is
    not a fingerprint of stage metadata; use ``digest`` for those records.
    """
    import ir

    ast_types = tuple(getattr(ir, name) for name in (
        "Literal", "Property", "Variable", "Comparison", "Boolean", "Negation", "Node", "Relationship",
        "Edge", "Exists", "Count", "Call", "Case", "ListExpr", "Concat", "All", "Alias", "Match", "With",
        "Query", "UnionAll", "RowLimit", "ConditionalQuery"))
    structural_fields = {
        ir.Comparison: frozenset({"operator"}), ir.Boolean: frozenset({"operator"}),
        ir.Call: frozenset({"name"}), ir.Edge: frozenset({"direction"}),
        ir.Relationship: frozenset({"path_mode"}), ir.Match: frozenset({"optional"}),
        ir.With: frozenset({"distinct"}), ir.Query: frozenset({"distinct"}),
    }

    def terminal(item):
        if isinstance(item, TerminalSlot) or type(item).__name__ == "Symbol":
            kind = item.kind
            example = item.candidates[0] if item.candidates else None
            return {"leaf": "str" if kind in ("identifier", "label", "relationship_type", "property_key")
                    else "int" if kind == "integer" else type(example).__name__}
        return {"leaf": type(item).__name__}

    def visit(item):
        if isinstance(item, TerminalSlot) or type(item).__name__ == "Symbol":
            return terminal(item)
        if isinstance(item, Port):
            return {"node": "Port", "kind": item.kind, "expected_type": item.expected_type,
                    "imports": [terminal(name) for name in item.imports]}
        if isinstance(item, ast_types):
            result = {"node": type(item).__name__}
            keep = structural_fields.get(type(item), ())
            for field in fields(item):
                child = getattr(item, field.name)
                if field.name in keep:
                    result[field.name] = child
                elif isinstance(item, ir.Literal) and field.name == "value":
                    # An accepted nullable literal may materialize as NULL.
                    # Its position remains the same terminal slot; type and
                    # domain consistency are checked by the binding contract.
                    result[field.name] = {"leaf": "literal"}
                else:
                    result[field.name] = visit(child)
            return result
        if isinstance(item, (tuple, list)):
            return [visit(child) for child in item]
        if is_dataclass(item) or isinstance(item, dict):
            raise TypeError("shape accepts query AST nodes, not stage metadata")
        return terminal(item)

    if not isinstance(value, (*ast_types, Port, TerminalSlot)) and type(value).__name__ != "Symbol":
        raise TypeError("shape requires a query AST node or terminal")
    return visit(value)


def shape_digest(tree):
    return digest(shape(tree))


class Record:
    def to_data(self):
        return encode(self)

    @classmethod
    def from_data(cls, value):
        restored = decode(value)
        if not isinstance(restored, cls):
            raise ValueError(f"Expected {cls.__name__} record")
        return restored


@dataclass(frozen=True)
class TerminalSlot(Record):
    slot_id: str
    kind: str
    candidates: tuple
    type: str = "any"
    owner_scope: str = "scope:0"
    shared_with: tuple = ()

    @property
    def name(self):
        return self.slot_id

    def __post_init__(self):
        if not self.slot_id or not self.candidates:
            raise ValueError("A terminal requires a stable ID and a nonempty finite domain")


@dataclass(frozen=True)
class Port(Record):
    port_id: str = "port:0"
    kind: str = "rows"
    expected_type: str = "rows"
    imports: tuple = ()


@dataclass(frozen=True)
class EmbeddingPort(Record):
    port_id: str
    address: tuple
    kind: str = "rows"
    visible_symbols: tuple = ()
    input_schema: tuple = ()
    output_schema: tuple = ()
    observation_contract: str = "bag"
    reserved_budget: int = 64
    default_subtree: object = None


@dataclass(frozen=True)
class ClauseSkeleton(Record):
    clauses: tuple = ()
    transition_trace: tuple = ()
    skeleton_seed: int = 0

    @property
    def skeleton_digest(self):
        return digest(self)


@dataclass(frozen=True)
class ScopeState(Record):
    scope_id: str = "scope:0"
    parent_scope: str | None = None
    visible_symbols: tuple = ()
    definitions: tuple = ()
    exports: tuple = ()
    alias_sources: tuple = ()
    nullable_types: tuple = ()


@dataclass(frozen=True)
class HostAst(Record):
    tree: object
    ports: tuple = ()
    slots: tuple = ()
    scope_graph: dict = field(default_factory=dict)
    clause_skeleton: ClauseSkeleton | None = None
    expansion_trace: tuple = ()
    host_seed: int = 0
    grammar_version: str = GRAMMAR_VERSION
    budget: dict = field(default_factory=dict)

    @property
    def shape_digest(self):
        return shape_digest(self.tree)

    @property
    def host_digest(self):
        return digest(self)


@dataclass(frozen=True)
class PatternInstance(Record):
    instance_id: str
    pattern_id: str
    form: str
    port_id: str
    site_address: tuple = ()
    imports: tuple = ()
    exports: tuple = ()
    rule_version: str = "local-law-v1"


@dataclass(frozen=True)
class EmbeddedAst(Record):
    host: HostAst
    tree: object
    skeleton: object
    instances: tuple
    target_id: str
    origin_map: dict = field(default_factory=dict)
    outside_port_digest: str = ""

    @property
    def embedded_digest(self):
        return digest(self)


@dataclass(frozen=True)
class GraphState:
    graph_spec: dict
    population: object
    embedded_digest: str
    graph_digest: str
    solved: object = field(repr=False, compare=False)
    facts: dict = field(default_factory=dict)

    @property
    def spec(self):
        return self.population.spec


@dataclass(frozen=True)
class BindingPlan(Record):
    slot_assignments: dict
    embedded_digest: str
    graph_digest: str
    plan_digest: str


@dataclass(frozen=True)
class InstantiatedQuery:
    source: object
    replacement: object
    bindings: dict
    checks: tuple
    record: dict
    prepared: object
    embedded: EmbeddedAst
    state: GraphState
    plan: BindingPlan


@dataclass(frozen=True)
class DependencyReport(Record):
    static: dict
    runtime: dict = field(default_factory=lambda: {"status": "not_run", "result_relation": "not_run"})


def derive_seed(seed, stage, attempt=0):
    if type(seed) is not int or seed < 0 or type(attempt) is not int or attempt < 0:
        raise ValueError("Seeds and attempts must be nonnegative integers")
    return int(digest([seed, stage, attempt])[:16], 16)
