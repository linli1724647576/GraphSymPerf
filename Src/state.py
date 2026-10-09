"""Finite graph recipes shared by query languages and database adapters.

Recipes are built from pattern semantics, never recovered from setup text.  A
recipe stores only scale and seeds; rows are streamed in bounded batches.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any, Iterator


@dataclass(frozen=True)
class _Affine:
    base: int
    step: int = 1

    def at(self, i: int) -> int:
        return self.base + self.step * i


def _value(value: Any, i: int) -> Any:
    return value.at(i) if isinstance(value, _Affine) else value


@dataclass(frozen=True)
class _Nodes:
    count: int
    labels: tuple[str, ...]
    properties: dict[str, Any]


@dataclass(frozen=True)
class _Edges:
    count: int
    source: int | _Affine
    target: int | _Affine
    source_label: str
    target_label: str
    type: str
    properties: dict[str, Any]


_PATTERNS = {f"P{i}" for i in range(1, 17)} | {f"S{i}" for i in range(1, 11)}
_VARIANTS = {"original", "empty_low", "multi_low", "pruning_path"}


@dataclass(frozen=True)
class GraphSpec:
    pattern: str
    scale: int
    data_seed: int
    state_variant: str = "original"
    include_reference: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported graph recipe version")
        if self.pattern not in _PATTERNS:
            raise ValueError("unsupported graph pattern")
        if type(self.scale) is not int or self.scale <= 0:
            raise ValueError("scale must be a positive integer")
        if type(self.data_seed) is not int or self.data_seed < 0:
            raise ValueError("data_seed must be a nonnegative integer")
        if self.state_variant not in _VARIANTS:
            raise ValueError("unsupported state variant")
        allowed = {"empty_low": {"P10", "P11"}, "multi_low": {"P11", "P13"}, "pruning_path": {"P3"}}
        if self.state_variant != "original" and self.pattern not in allowed[self.state_variant]:
            raise ValueError("state variant is not applicable to this pattern")
        if type(self.include_reference) is not bool:
            raise ValueError("include_reference must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "pattern": self.pattern, "scale": self.scale,
                "data_seed": self.data_seed, "state_variant": self.state_variant,
                "include_reference": self.include_reference}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GraphSpec:
        return cls(**value)

    @property
    def indexes(self) -> list[dict[str, str]]:
        if self.pattern == "P16":
            return []
        if self.pattern.startswith("S"):
            label, key = "ScorRoot", "id"
        elif self.pattern == "P7":
            label, key = "GspRare", "uid"
        elif self.pattern in {"P4", "P5", "P8", "P11", "P13", "P15"}:
            label, key = "GspAnchor", "uid"
        elif self.pattern == "P14":
            label, key = "GspLow", "uid"
        else:
            label, key = "GspLow", "id"
        return [{"name": "gsp_cost_lookup", "label": label, "property": key}]

    def _groups(self) -> tuple[list[_Nodes], list[_Edges]]:
        p, n = self.pattern, self.scale
        o = random.Random(self.data_seed).randrange(10_000, 1_000_000)
        nodes: list[_Nodes] = []
        edges: list[_Edges] = []

        def node(label, uid, count=1, labels=None, **props):
            nodes.append(_Nodes(count, tuple(labels or (label,)), {"uid": uid, **props}))

        def edge(source, target, source_label, target_label, kind="GSP_LINK", count=1, **props):
            edges.append(_Edges(count, source, target, source_label, target_label, kind, props))

        if p == "P16":
            return nodes, edges
        if p.startswith("S"):
            node("ScorRoot", 1, id=1, key=1, expected_count=4 * n)
            node("ScorTarget", _Affine(o + 1), 4 * n, mark=True, key=1)
            node("ScorTarget", _Affine(2), 3, mark=True, key=1)
            edge(1, _Affine(o + 1), "ScorRoot", "ScorTarget", "GSP_SC_HIGH", 4 * n)
            edge(1, _Affine(2), "ScorRoot", "ScorTarget", "GSP_SC_REF", 3)
        elif p == "P1":
            node("GspLow", 1, id=1)
            node("GspHigh", 2)
            edge(1, 2, "GspLow", "GspHigh")
            node("GspHigh", _Affine(o + 5, 5), n)
            for j in range(1, 5):
                node("GspLow", _Affine(o + 5 + j, 5), n, id=_Affine(o + 5 + j, 5))
                edge(_Affine(o + 5 + j, 5), _Affine(o + 5, 5), "GspLow", "GspHigh", count=n)
        elif p == "P2":
            node("GspLow", 1, id=1)
            node("GspHigh", 2)
            node("GspLow", _Affine(o + 2, 2), n, id=_Affine(o + 1))
            node("GspHigh", _Affine(o + 3, 2), n)
            for source, target, kind, sl, tl in [(1, 2, "GSP_FORWARD", "GspLow", "GspHigh"),
                                                (2, 1, "GSP_REVERSE", "GspHigh", "GspLow")]:
                edge(source, target, sl, tl, kind, pair=1)
                source_range = _Affine(o + 2, 2) if source == 1 else _Affine(o + 3, 2)
                target_range = _Affine(o + 3, 2) if source == 1 else _Affine(o + 2, 2)
                edge(source_range, target_range, sl, tl, kind, n, pair=_Affine(2))
        elif p in {"P3", "P12"}:
            prune = self.state_variant == "pruning_path"
            attrs = {"keep": True} if prune else {}
            node("GspLow", 1, id=1, **attrs)
            node("GspMiddle", 2, **attrs)
            node("GspHigh", 3, **attrs)
            edge(1, 2, "GspLow", "GspMiddle")
            edge(2, 3, "GspMiddle", "GspHigh")
            if prune:
                node("GspBad", 0, keep=False)
                node("GspMiddle", _Affine(o + 2, 2), n, keep=True)
                node("GspHigh", _Affine(o + 3, 2), n, keep=True)
                edge(1, 0, "GspLow", "GspBad")
                edge(0, _Affine(o + 2, 2), "GspBad", "GspMiddle", count=n)
                edge(_Affine(o + 2, 2), _Affine(o + 3, 2), "GspMiddle", "GspHigh", count=n)
            else:
                node("GspLow", _Affine(o + 3, 3), n, id=_Affine(o + 1))
                node("GspMiddle", _Affine(o + 4, 3), n)
                node("GspHigh", _Affine(o + 5, 3), n)
                edge(_Affine(o + 3, 3), _Affine(o + 4, 3), "GspLow", "GspMiddle", count=n)
                edge(_Affine(o + 4, 3), _Affine(o + 5, 3), "GspMiddle", "GspHigh", count=n)
        elif p in {"P6", "P9"}:
            node("GspLow", 1, id=1)
            node("GspHigh", 2)
            node("GspLow", _Affine(o + 2, 2), n, id=_Affine(o + 1))
            node("GspHigh", _Affine(o + 3, 2), n)
            edge(1, 2, "GspLow", "GspHigh")
            edge(_Affine(o + 2, 2), _Affine(o + 3, 2), "GspLow", "GspHigh", count=n)
        elif p == "P7":
            node("GspRare", 1, labels=("GspRare", "GspCommon"))
            node("GspTarget", 2)
            node("GspCommon", _Affine(o + 2, 2), n)
            node("GspTarget", _Affine(o + 3, 2), n)
            edge(1, 2, "GspRare", "GspTarget")
            edge(_Affine(o + 2, 2), _Affine(o + 3, 2), "GspCommon", "GspTarget", count=n)
        elif p in {"P4", "P5", "P15"}:
            if p == "P15":
                node("GspAnchor", 1, keep=True)
                node("GspAnchor", 2, keep=False)
                node("GspAnchor", 3)
                witness = 4
            else:
                node("GspAnchor", 1, keep=p == "P5")
                node("GspAnchor", 2, keep=p == "P4")
                witness = 3
            node("GspLeaf", witness, mark=True)
            node("GspLeaf", _Affine(o + 1), 4 * n, mark=False)
            edge(2, witness, "GspAnchor", "GspLeaf", "GSP_EXPAND")
            edge(1, _Affine(o + 1), "GspAnchor", "GspLeaf", "GSP_EXPAND", 4 * n)
            if self.include_reference:
                edge(2, witness, "GspAnchor", "GspLeaf", "GSP_REFERENCE")
        elif p == "P8":
            node("GspAnchor", 1)
            node("GspTarget", 2, keep=True)
            node("GspTarget", _Affine(o + 1), 4 * n, keep=False)
            edge(1, 2, "GspAnchor", "GspTarget", "GSP_LOW")
            edge(1, _Affine(o + 1), "GspAnchor", "GspTarget", "GSP_HIGH", 4 * n)
        elif p == "P10":
            node("GspLow", 1, id=0 if self.state_variant == "empty_low" else 1)
            node("GspHigh", 2)
            node("GspTarget", 3, keep=True)
            edge(2, 3, "GspHigh", "GspTarget")
            node("GspHigh", _Affine(o + 5, 5), n)
            for j in range(1, 5):
                node("GspTarget", _Affine(o + 5 + j, 5), n, keep=False)
                edge(_Affine(o + 5, 5), _Affine(o + 5 + j, 5), "GspHigh", "GspTarget", count=n)
        elif p == "P11":
            empty = self.state_variant == "empty_low"
            node("GspAnchor", 1)
            if empty:
                node("GspAnchor", 0)
            node("GspLow", 2)
            node("GspHigh", 3, keep=True)
            node("GspHigh", _Affine(o + 1), 4 * n, keep=False)
            edge(0 if empty else 1, 2, "GspAnchor", "GspLow", "GSP_LOW")
            edge(1, 3, "GspAnchor", "GspHigh", "GSP_HIGH")
            edge(1, _Affine(o + 1), "GspAnchor", "GspHigh", "GSP_HIGH", 4 * n)
            if self.state_variant == "multi_low":
                node("GspLow", _Affine(-1, -1), 7)
                edge(1, _Affine(-1, -1), "GspAnchor", "GspLow", "GSP_LOW", 7)
        elif p == "P13":
            for uid in (1, 2, 3):
                node("GspAnchor", uid)
            for uid in (10, 11):
                node("GspLow", uid)
            for uid in (20, 21):
                node("GspHigh", uid, keep=True)
            node("GspHigh", _Affine(o + 1), 4 * n, keep=False)
            edge(1, 10, "GspAnchor", "GspLow", "GSP_LOW")
            edge(2, 11, "GspAnchor", "GspLow", "GSP_LOW")
            edge(1, 20, "GspAnchor", "GspHigh", "GSP_HIGH")
            edge(3, 21, "GspAnchor", "GspHigh", "GSP_HIGH")
            edge(1, _Affine(o + 1), "GspAnchor", "GspHigh", "GSP_HIGH", 4 * n)
            if self.state_variant == "multi_low":
                node("GspLow", _Affine(-1, -1), 7)
                edge(1, _Affine(-1, -1), "GspAnchor", "GspLow", "GSP_LOW", 7)
        elif p == "P14":
            node("GspLow", 1)
            node("GspHigh", 2, keep=True)
            node("GspHigh", _Affine(o + 1), 4 * n, keep=False)
        return nodes, edges

    def iter_nodes(self) -> Iterator[dict[str, Any]]:
        for group in self._groups()[0]:
            for i in range(group.count):
                props = {key: _value(value, i) for key, value in group.properties.items()}
                yield {"uid": props["uid"], "labels": list(group.labels), "properties": props}

    def iter_edges(self) -> Iterator[dict[str, Any]]:
        for group in self._groups()[1]:
            for i in range(group.count):
                yield {"source": _value(group.source, i), "target": _value(group.target, i),
                       "source_label": group.source_label, "target_label": group.target_label,
                       "type": group.type,
                       "properties": {key: _value(value, i) for key, value in group.properties.items()}}

    def iter_batches(self, kind: str, batch_size: int = 1000) -> Iterator[list[dict[str, Any]]]:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if kind not in {"nodes", "edges"}:
            raise ValueError("batch kind must be nodes or edges")
        batch: list[dict[str, Any]] = []
        for row in self.iter_nodes() if kind == "nodes" else self.iter_edges():
            batch.append(row)
            if len(batch) == batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def counts(self) -> dict[str, int]:
        nodes, edges = self._groups()
        return {"nodes": sum(group.count for group in nodes), "edges": sum(group.count for group in edges)}

    def schema(self) -> dict[str, Any]:
        nodes, edges = self._groups()
        labels: dict[str, dict[str, str]] = {}
        relationships: dict[str, dict[str, Any]] = {}
        for group in nodes:
            for label in group.labels:
                fields = labels.setdefault(label, {})
                for key, value in group.properties.items():
                    fields[key] = "boolean" if type(value) is bool else "integer"
        for group in edges:
            relationship = relationships.setdefault(group.type, {"type": group.type, "source_labels": [],
                                                                "target_labels": [], "properties": {}, "endpoints": []})
            for key, value in group.properties.items():
                relationship["properties"][key] = "boolean" if type(value) is bool else "integer"
            for key, value in (("source_labels", group.source_label), ("target_labels", group.target_label)):
                if value not in relationship[key]:
                    relationship[key].append(value)
            endpoint = {"source_label": group.source_label, "target_label": group.target_label}
            if endpoint not in relationship["endpoints"]:
                relationship["endpoints"].append(endpoint)
        return {"nodes": [{"label": label, "properties": props} for label, props in labels.items()],
                "relationships": list(relationships.values())}

    def state_checks(self) -> list[dict[str, Any]]:
        """Finite population and witness facts; index readiness is separate.

        Aggregate checks cover the actual loaded graph, not only the recipe.
        Inverse/reference checks verify relationship identity and direction.
        """
        nodes, edges = self._groups()
        checks: list[dict[str, Any]] = []
        primary_labels: dict[str, set[str]] = {}
        for group in nodes:
            primary_labels.setdefault(group.labels[0], set()).update(group.labels)
        if self.pattern == "P16":
            return [{"kind": "literal", "value": False, "expected": False}]
        for label in sorted({label for group in nodes for label in group.labels}):
            count = sum(group.count for group in nodes if label in group.labels)
            checks.append({"kind": "node_count", "labels": [label], "properties": {}, "expected": count})
            checks.append({"kind": "distinct_values", "label": label, "property": "uid", "expected": count})
        # Constant property populations establish selectivity and boolean guards.
        for group in nodes:
            constant = {key: value for key, value in group.properties.items() if not isinstance(value, _Affine)}
            if type(group.properties["uid"]) is int:
                # Missing optional properties must also remain NULL; P15 uses
                # such an anchor to exercise the CASE ELSE branch.
                fields = next(row["properties"] for row in self.schema()["nodes"] if row["label"] == group.labels[0])
                witness = {key: group.properties.get(key) for key in fields}
                checks.append({"kind": "node_properties", "label": group.labels[0], "uid": constant["uid"],
                               "properties": witness})
            if constant:
                count = sum(other.count for other in nodes if set(group.labels).issubset(other.labels)
                            and all(other.properties.get(key) == value for key, value in constant.items()))
                check = {"kind": "node_count", "labels": list(group.labels), "properties": constant, "expected": count}
                if check not in checks:
                    checks.append(check)
        for kind in sorted({group.type for group in edges}):
            checks.append({"kind": "edge_count", "type": kind, "expected": sum(group.count for group in edges if group.type == kind)})
        for group in edges:
            source_props = {"uid": group.source} if type(group.source) is int else {}
            target_props = {"uid": group.target} if type(group.target) is int else {}
            check = {"kind": "edge_count", "type": group.type, "source_label": group.source_label,
                     "target_label": group.target_label, "source_properties": source_props,
                     "target_properties": target_props}
            count = sum(other.count for other in edges if other.type == group.type
                        and group.source_label in primary_labels[other.source_label]
                        and group.target_label in primary_labels[other.target_label]
                        and (not source_props or other.source == group.source)
                        and (not target_props or other.target == group.target))
            check["expected"] = count
            if check not in checks:
                checks.append(check)
        if self.pattern == "P2":
            for forward, reverse in (("GSP_FORWARD", "GSP_REVERSE"), ("GSP_REVERSE", "GSP_FORWARD")):
                checks.extend([{"kind": "distinct_values", "type": forward, "property": "pair", "expected": self.scale + 1},
                               {"kind": "inverse_edges", "forward": forward, "reverse": reverse,
                                "pair_key": "pair", "expected": 0}])
        if self.pattern in {"P4", "P5", "P15"} and self.include_reference:
            checks.append({"kind": "reference_edges", "reference": "GSP_REFERENCE", "original": "GSP_EXPAND",
                           "source_label": "GspAnchor", "target_label": "GspLeaf", "guard_property": "keep",
                           "guard_value": self.pattern == "P4", "expected": 0})
        if self.pattern.startswith("S"):
            for relation, count in (("GSP_SC_HIGH", 4 * self.scale), ("GSP_SC_REF", 3)):
                checks.append({"kind": "edge_count", "type": relation, "source_label": "ScorRoot",
                               "target_label": "ScorTarget", "source_properties": {"id": 1, "key": 1},
                               "target_properties": {"mark": True, "key": 1}, "expected": count})
        return checks


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return str(value).lower()
    if type(value) is int:
        return str(value)
    raise ValueError("state checks only support integer, boolean and null values")


def _properties(value: dict[str, Any]) -> str:
    return " {" + ", ".join(f"{key}: {_literal(item)}" for key, item in value.items()) + "}" if value else ""


def render_state_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render this finite check vocabulary as Cypher (no input-text rewriting).

    Database adapters may execute the structured checks directly or render
    them using their own frontend. These statements do not inspect indexes.
    """
    rendered = []
    for check in checks:
        kind = check["kind"]
        expected = [{"count": check["expected"]}] if "expected" in check else None
        if kind == "node_count":
            labels = "".join(":" + label for label in check["labels"])
            query = f"MATCH (n{labels}{_properties(check['properties'])}) RETURN count(n) AS `count`"
        elif kind == "edge_count":
            source = ":" + check["source_label"] if check.get("source_label") else ""
            target = ":" + check["target_label"] if check.get("target_label") else ""
            query = (f"MATCH (u{source}{_properties(check.get('source_properties', {}))})"
                     f"-[r:{check['type']}]->(v{target}{_properties(check.get('target_properties', {}))}) "
                     "RETURN count(r) AS `count`")
        elif kind == "node_properties":
            query = f"MATCH (n:{check['label']} {{uid: {check['uid']}}}) RETURN " + ", ".join(
                f"n.{key} AS `{key}`" for key in check["properties"])
            expected = [check["properties"]]
        elif kind == "distinct_values":
            query = (f"MATCH (n:{check['label']}) RETURN count(DISTINCT n.{check['property']}) AS `count`"
                     if "label" in check else
                     f"MATCH ()-[r:{check['type']}]->() RETURN count(DISTINCT r.{check['property']}) AS `count`")
        elif kind == "inverse_edges":
            query = (f"MATCH (u)-[f:{check['forward']}]->(v) OPTIONAL MATCH (v)-[b:{check['reverse']}]->(u) "
                     f"WHERE b.{check['pair_key']} = f.{check['pair_key']} "
                     "WITH f, count(b) AS `inverse_count` WHERE inverse_count <> 1 RETURN count(f) AS `count`")
        elif kind == "reference_edges":
            # Check both subset and completeness for the non-decisive anchors.
            for first, second, guard in ((check["reference"], check["original"], ""),
                                         (check["original"], check["reference"],
                                          f"coalesce(u.{check['guard_property']}, false) = {_literal(check['guard_value'])} AND ")):
                query = (f"MATCH (u:{check['source_label']})-[:{first}]->(v:{check['target_label']}) "
                         f"WHERE {guard}NOT EXISTS {{ MATCH (u)-[:{second}]->(v) }} RETURN count(v) AS `count`")
                rendered.append({"query": query, "expected_rows": [{"count": check["expected"]}]})
            continue
        elif kind == "literal":
            query = f"RETURN {_literal(check['value'])} AS `value`"
            expected = [{"value": check["expected"]}]
        else:
            raise ValueError(f"unsupported state check {kind}")
        rendered.append({"query": query, "expected_rows": expected})
    return rendered
