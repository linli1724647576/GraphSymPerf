"""Certificate-carrying graph-state symmetries derived from a GDsmith graph.

The builder treats GDsmith's ``id`` property as a replay identity rather than a
semantic property.  It materializes two disconnected copies of the parsed base
graph and records the involution that exchanges them.  Query pairs combine that
state relation with graph-pattern symmetries; they are not arbitrary equivalent
query rewrites.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from graph_sym_perf.gdsmith.replay import (
    GdsmithReplayGraph,
    GdsmithReplayNode,
    GdsmithReplayRelationship,
    parse_gdsmith_replay_graph,
)


@dataclass(frozen=True)
class SymmetryQueryPair:
    """A query pair justified by an explicit graph-state symmetry certificate."""

    query_a: str
    query_b: str
    rule_id: str
    certificate: Mapping[str, object]


@dataclass(frozen=True)
class InvolutiveSymmetryState:
    """Replayable symmetric graph plus the query pairs instantiated from it."""

    graph_statements: tuple[str, ...]
    query_pairs: tuple[SymmetryQueryPair, ...]
    metadata: Mapping[str, object]


def build_involutive_symmetry_state(
    graph_statements: tuple[str, ...] | list[str],
    *,
    max_pairs: int,
    seed: int,
) -> InvolutiveSymmetryState:
    """Build a two-copy, order-two state symmetry from one GDsmith graph."""

    if max_pairs <= 0:
        raise ValueError("max_pairs must be positive")
    base = parse_gdsmith_replay_graph(graph_statements)
    _validate_base_graph(base)
    copied_graph, node_orbits = _copy_graph(base)
    _verify_involutive_copy(base, copied_graph, node_orbits)
    candidates = _deduplicate_pairs(
        _endpoint_pairs(base, node_orbits)
        + _two_hop_path_pairs(base, node_orbits)
        + _incident_pattern_order_pairs(base, node_orbits)
    )
    source_graph_sha256 = _source_hash(graph_statements)
    ordered_pairs = sorted(candidates, key=lambda pair: _seeded_pair_key(pair, seed))[:max_pairs]
    certified_pairs = tuple(
        SymmetryQueryPair(
            query_a=pair.query_a,
            query_b=pair.query_b,
            rule_id=pair.rule_id,
            certificate={
                "construction": "gdsmith_involutive_double_v1",
                "source_graph_sha256": source_graph_sha256,
                **dict(pair.certificate),
            },
        )
        for pair in ordered_pairs
    )
    return InvolutiveSymmetryState(
        graph_statements=_serialize_graph(copied_graph),
        query_pairs=certified_pairs,
        metadata={
            "construction": "gdsmith_involutive_double_v1",
            "source_graph_sha256": source_graph_sha256,
            "source_nodes": len(base.nodes),
            "source_relationships": len(base.relationships),
            "materialized_nodes": len(copied_graph.nodes),
            "materialized_relationships": len(copied_graph.relationships),
            "state_symmetry_order": 2,
            "state_relation": "involutive_isomorphism_modulo_replay_id",
            "semantic_identity_exclusion": "id",
            "label_projection": "portable_primary_label",
            "involution_verified": True,
            "generated_symmetry_pairs": len(certified_pairs),
        },
    )


def _validate_base_graph(graph: GdsmithReplayGraph) -> None:
    if not graph.nodes:
        raise ValueError("GDsmith graph contains no replayable nodes")
    node_ids = [node.node_id for node in graph.nodes]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("GDsmith graph contains duplicate node ids")
    known = set(node_ids)
    for relationship in graph.relationships:
        if relationship.start_id not in known or relationship.end_id not in known:
            raise ValueError("GDsmith relationship references an unknown node id")


def _copy_graph(
    base: GdsmithReplayGraph,
) -> tuple[GdsmithReplayGraph, Mapping[int, tuple[int, int]]]:
    ordered_nodes = sorted(base.nodes, key=lambda node: node.node_id)
    node_orbits = {
        node.node_id: (index + 1, len(ordered_nodes) + index + 1)
        for index, node in enumerate(ordered_nodes)
    }
    nodes: list[GdsmithReplayNode] = []
    for copy_index in (0, 1):
        for node in ordered_nodes:
            new_id = node_orbits[node.node_id][copy_index]
            properties = dict(node.properties)
            properties["id"] = new_id
            nodes.append(
                GdsmithReplayNode(
                    node_id=new_id,
                    labels=(node.labels or ("GdsmithNode",))[:1],
                    properties=properties,
                )
            )

    ordered_relationships = sorted(
        base.relationships,
        key=lambda relationship: (
            relationship.start_id,
            relationship.end_id,
            relationship.relationship_type,
            relationship.relationship_id if relationship.relationship_id is not None else -1,
        ),
    )
    relationships: list[GdsmithReplayRelationship] = []
    next_relationship_id = len(nodes) + 1
    for copy_index in (0, 1):
        for relationship in ordered_relationships:
            properties = dict(relationship.properties)
            properties["id"] = next_relationship_id
            relationships.append(
                GdsmithReplayRelationship(
                    relationship_id=next_relationship_id,
                    start_id=node_orbits[relationship.start_id][copy_index],
                    end_id=node_orbits[relationship.end_id][copy_index],
                    relationship_type=relationship.relationship_type,
                    properties=properties,
                )
            )
            next_relationship_id += 1
    return GdsmithReplayGraph(tuple(nodes), tuple(relationships)), node_orbits


def _verify_involutive_copy(
    base: GdsmithReplayGraph,
    copied: GdsmithReplayGraph,
    node_orbits: Mapping[int, tuple[int, int]],
) -> None:
    copied_nodes = {node.node_id: node for node in copied.nodes}
    for node in base.nodes:
        left = copied_nodes[node_orbits[node.node_id][0]]
        right = copied_nodes[node_orbits[node.node_id][1]]
        if left.labels != right.labels or _semantic_properties(left.properties) != _semantic_properties(
            right.properties
        ):
            raise AssertionError("materialized node orbit violates the state symmetry")

    relationship_count = len(base.relationships)
    if len(copied.relationships) != relationship_count * 2:
        raise AssertionError("materialized relationship count violates the state symmetry")
    left_relationships = copied.relationships[:relationship_count]
    right_relationships = copied.relationships[relationship_count:]
    right_by_left = {left: right for left, right in node_orbits.values()}
    for left, right in zip(left_relationships, right_relationships, strict=True):
        expected_right_start = right_by_left.get(left.start_id)
        expected_right_end = right_by_left.get(left.end_id)
        if (
            expected_right_start is None
            or expected_right_end is None
            or right.start_id != expected_right_start
            or right.end_id != expected_right_end
            or right.relationship_type != left.relationship_type
            or _semantic_properties(right.properties) != _semantic_properties(left.properties)
        ):
            raise AssertionError("materialized relationship orbit violates the state symmetry")


def _semantic_properties(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in properties.items() if key != "id"}


def _endpoint_pairs(
    base: GdsmithReplayGraph,
    node_orbits: Mapping[int, tuple[int, int]],
) -> list[SymmetryQueryPair]:
    nodes_by_id = {node.node_id: node for node in base.nodes}
    pairs: list[SymmetryQueryPair] = []
    for relationship in base.relationships:
        if relationship.start_id == relationship.end_id:
            continue
        for anchor_id, opposite_id in (
            (relationship.start_id, relationship.end_id),
            (relationship.end_id, relationship.start_id),
        ):
            anchor = nodes_by_id[anchor_id]
            opposite = nodes_by_id[opposite_id]
            left_anchor, right_anchor = node_orbits[anchor.node_id]
            anchor_pattern = _node_pattern("a", anchor, identity=left_anchor)
            opposite_pattern = _node_pattern("b", opposite)
            right_anchor_pattern = _node_pattern("a", anchor, identity=right_anchor)
            relationship_pattern = f"[r:{relationship.relationship_type}]"
            pairs.append(
                SymmetryQueryPair(
                    query_a=(
                        f"MATCH {anchor_pattern}-{relationship_pattern}-{opposite_pattern} "
                        "RETURN count(*) AS gsp_count"
                    ),
                    query_b=(
                        f"MATCH {opposite_pattern}-{relationship_pattern}-{right_anchor_pattern} "
                        "RETURN count(*) AS gsp_count"
                    ),
                    rule_id="undirected_endpoint_orbit_symmetry",
                    certificate={
                        "state_relation": "involutive_isomorphism_modulo_replay_id",
                        "syntactic_symmetry": "undirected_endpoint_reversal",
                        "identity_key": "id",
                        "anchor_orbit": (left_anchor, right_anchor),
                        "base_edge": (
                            anchor_id,
                            relationship.relationship_type,
                            opposite_id,
                        ),
                    },
                )
            )
    return pairs


def _two_hop_path_pairs(
    base: GdsmithReplayGraph,
    node_orbits: Mapping[int, tuple[int, int]],
) -> list[SymmetryQueryPair]:
    nodes_by_id = {node.node_id: node for node in base.nodes}
    pairs: list[SymmetryQueryPair] = []
    for middle_id, incident_edges in _incident_edges(base).items():
        for (first, start_id), (second, end_id) in combinations(incident_edges, 2):
            if start_id == middle_id or end_id == middle_id:
                continue
            start = nodes_by_id[start_id]
            middle = nodes_by_id[middle_id]
            end = nodes_by_id[end_id]
            left_anchor, right_anchor = node_orbits[start_id]
            pairs.append(
                SymmetryQueryPair(
                    query_a=(
                        f"MATCH {_node_pattern('a', start, identity=left_anchor)}"
                        f"-[r1:{first.relationship_type}]-{_node_pattern('m', middle)}"
                        f"-[r2:{second.relationship_type}]-{_node_pattern('b', end)} "
                        "RETURN count(*) AS gsp_count"
                    ),
                    query_b=(
                        f"MATCH {_node_pattern('b', end)}"
                        f"-[r2:{second.relationship_type}]-{_node_pattern('m', middle)}"
                        f"-[r1:{first.relationship_type}]-{_node_pattern('a', start, identity=right_anchor)} "
                        "RETURN count(*) AS gsp_count"
                    ),
                    rule_id="two_hop_path_endpoint_orbit_symmetry",
                    certificate={
                        "state_relation": "involutive_isomorphism_modulo_replay_id",
                        "syntactic_symmetry": "two_hop_path_endpoint_reversal",
                        "identity_key": "id",
                        "anchor_orbit": (left_anchor, right_anchor),
                        "base_path": (
                            start_id,
                            first.relationship_type,
                            middle_id,
                            second.relationship_type,
                            end_id,
                        ),
                    },
                )
            )
    return pairs


def _incident_pattern_order_pairs(
    base: GdsmithReplayGraph,
    node_orbits: Mapping[int, tuple[int, int]],
) -> list[SymmetryQueryPair]:
    nodes_by_id = {node.node_id: node for node in base.nodes}
    pairs: list[SymmetryQueryPair] = []
    for anchor_id, incident_edges in _incident_edges(base).items():
        for (first, first_end_id), (second, second_end_id) in combinations(incident_edges, 2):
            if first_end_id == anchor_id or second_end_id == anchor_id:
                continue
            anchor = nodes_by_id[anchor_id]
            first_end = nodes_by_id[first_end_id]
            second_end = nodes_by_id[second_end_id]
            left_anchor, right_anchor = node_orbits[anchor_id]
            first_pattern_left = (
                f"{_node_pattern('a', anchor, identity=left_anchor)}"
                f"-[r1:{first.relationship_type}]-{_node_pattern('b', first_end)}"
            )
            second_pattern_left = (
                f"{_node_pattern('a', anchor)}"
                f"-[r2:{second.relationship_type}]-{_node_pattern('c', second_end)}"
            )
            second_pattern_right = (
                f"{_node_pattern('a', anchor)}"
                f"-[r2:{second.relationship_type}]-{_node_pattern('c', second_end)}"
            )
            first_pattern_right = (
                f"{_node_pattern('a', anchor, identity=right_anchor)}"
                f"-[r1:{first.relationship_type}]-{_node_pattern('b', first_end)}"
            )
            pairs.append(
                SymmetryQueryPair(
                    query_a=(
                        f"MATCH {first_pattern_left}, {second_pattern_left} "
                        "RETURN count(*) AS gsp_count"
                    ),
                    query_b=(
                        f"MATCH {second_pattern_right}, {first_pattern_right} "
                        "RETURN count(*) AS gsp_count"
                    ),
                    rule_id="incident_pattern_order_orbit_symmetry",
                    certificate={
                        "state_relation": "involutive_isomorphism_modulo_replay_id",
                        "syntactic_symmetry": "incident_pattern_order_permutation",
                        "identity_key": "id",
                        "anchor_orbit": (left_anchor, right_anchor),
                        "base_incident_patterns": (
                            (anchor_id, first.relationship_type, first_end_id),
                            (anchor_id, second.relationship_type, second_end_id),
                        ),
                    },
                )
            )
    return pairs


def _incident_edges(
    graph: GdsmithReplayGraph,
) -> Mapping[int, list[tuple[GdsmithReplayRelationship, int]]]:
    incident: dict[int, list[tuple[GdsmithReplayRelationship, int]]] = {
        node.node_id: [] for node in graph.nodes
    }
    for relationship in graph.relationships:
        incident[relationship.start_id].append((relationship, relationship.end_id))
        if relationship.end_id != relationship.start_id:
            incident[relationship.end_id].append((relationship, relationship.start_id))
    return incident


def _node_pattern(variable: str, node: GdsmithReplayNode, *, identity: int | None = None) -> str:
    label = (node.labels or ("GdsmithNode",))[0]
    properties = f" {{id: {identity}}}" if identity is not None else ""
    return f"({variable}:{label}{properties})"


def _deduplicate_pairs(pairs: list[SymmetryQueryPair]) -> list[SymmetryQueryPair]:
    unique: dict[tuple[str, str], SymmetryQueryPair] = {}
    for pair in pairs:
        unique.setdefault((pair.query_a, pair.query_b), pair)
    return list(unique.values())


def _seeded_pair_key(pair: SymmetryQueryPair, seed: int) -> str:
    payload = f"{seed}:{pair.rule_id}:{pair.query_a}:{pair.query_b}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _serialize_graph(graph: GdsmithReplayGraph) -> tuple[str, ...]:
    statements: list[str] = []
    for node in graph.nodes:
        labels = "".join(f":{label}" for label in node.labels)
        statements.append(f"CREATE (n{labels} {_map_literal(node.properties)})")
    for relationship in graph.relationships:
        statements.append(
            f"MATCH (n0 {{id: {relationship.start_id}}}), "
            f"(n1 {{id: {relationship.end_id}}}) "
            f"MERGE (n0)-[r:{relationship.relationship_type} "
            f"{_map_literal(relationship.properties)}]->(n1)"
        )
    return tuple(statements)


def _map_literal(properties: Mapping[str, Any]) -> str:
    ordered_keys = sorted(properties, key=lambda key: (key != "id", key))
    return "{" + ", ".join(f"{key}: {_literal(properties[key])}" for key in ordered_keys) + "}"


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _source_hash(statements: tuple[str, ...] | list[str]) -> str:
    normalized = "\n".join(statement.strip().rstrip(";") for statement in statements)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
