"""Best-effort lowering from GDsmith Cypher logs to replayable graph data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from graph_sym_perf.query_ir.expression import cypher_identifier


@dataclass(frozen=True)
class GdsmithReplayNode:
    node_id: int
    labels: tuple[str, ...]
    properties: Mapping[str, Any]


@dataclass(frozen=True)
class GdsmithReplayRelationship:
    relationship_id: int | None
    start_id: int
    end_id: int
    relationship_type: str
    properties: Mapping[str, Any]


@dataclass(frozen=True)
class GdsmithReplayGraph:
    nodes: tuple[GdsmithReplayNode, ...]
    relationships: tuple[GdsmithReplayRelationship, ...]

    @property
    def node_labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        for node in self.nodes:
            for label in node.labels or ("GdsmithNode",):
                if label not in labels:
                    labels.append(label)
        return tuple(labels)

    @property
    def relationship_types(self) -> tuple[str, ...]:
        types: list[str] = []
        for relationship in self.relationships:
            if relationship.relationship_type not in types:
                types.append(relationship.relationship_type)
        return tuple(types)


_NODE_RE = re.compile(
    r"^\s*CREATE\s*\(\s*[A-Za-z]\w*\s*(?P<labels>(?::\s*[A-Za-z]\w*)*)\s*"
    r"(?P<props>\{.*\})?\s*\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_REL_RE = re.compile(
    r"^\s*MATCH\s*\(\s*[A-Za-z]\w*\s*\{\s*id\s*:\s*(?P<start>-?\d+)\s*\}\s*\)\s*,\s*"
    r"\(\s*[A-Za-z]\w*\s*\{\s*id\s*:\s*(?P<end>-?\d+)\s*\}\s*\)\s*"
    r"MERGE\s*\(\s*[A-Za-z]\w*\s*\)\s*-\s*\[\s*[A-Za-z]\w*\s*:\s*(?P<type>[A-Za-z]\w*)\s*"
    r"(?P<props>\{.*\})?\s*\]\s*->\s*\(\s*[A-Za-z]\w*\s*\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_gdsmith_replay_graph(statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
    nodes: list[GdsmithReplayNode] = []
    relationships: list[GdsmithReplayRelationship] = []
    for statement in statements:
        stripped = statement.strip().rstrip(";")
        if not stripped:
            continue
        node_match = _NODE_RE.match(stripped)
        if node_match:
            props = _parse_cypher_map(node_match.group("props") or "{}")
            node_id = _required_int(props.get("id"), "GDsmith node id")
            labels = tuple(cypher_identifier(label) for label in re.findall(r":\s*([A-Za-z]\w*)", node_match.group("labels")))
            nodes.append(GdsmithReplayNode(node_id=node_id, labels=labels or ("GdsmithNode",), properties=props))
            continue
        rel_match = _REL_RE.match(stripped)
        if rel_match:
            props = _parse_cypher_map(rel_match.group("props") or "{}")
            relationships.append(
                GdsmithReplayRelationship(
                    relationship_id=_optional_int(props.get("id")),
                    start_id=int(rel_match.group("start")),
                    end_id=int(rel_match.group("end")),
                    relationship_type=cypher_identifier(rel_match.group("type")),
                    properties=props,
                )
            )
            continue
        raise ValueError(f"UNSUPPORTED_GDSMITH_GRAPH_STATEMENT: {stripped[:160]}")
    return GdsmithReplayGraph(nodes=tuple(nodes), relationships=tuple(relationships))


def gdsmith_read_query_is_replayable(query: str) -> bool:
    upper = query.upper()
    if not upper.lstrip().startswith("MATCH "):
        return False
    if re.search(r"\b(CALL|CREATE|MERGE|DELETE|SET|REMOVE|DROP|LOAD\s+CSV)\b", upper):
        return False
    if re.search(r"\[[^\]]*\*", query):
        return False
    if re.search(r"\[[^\]]*\|[^\]]*\]", query, flags=re.DOTALL):
        return False
    return True


def _parse_cypher_map(text: str) -> Mapping[str, Any]:
    body = text.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    if not body.strip():
        return {}
    properties: dict[str, Any] = {}
    for item in _split_top_level_commas(body):
        if not item.strip():
            continue
        key, value = _split_key_value(item)
        properties[cypher_identifier(key.strip())] = _parse_literal(value.strip())
    return properties


def _split_key_value(text: str) -> tuple[str, str]:
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == "\\":
                continue
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == ":" and depth == 0:
            return text[:index], text[index + 1 :]
    raise ValueError(f"invalid GDsmith property map item: {text!r}")


def _split_top_level_commas(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return tuple(parts)


def _parse_literal(text: str) -> Any:
    lowered = text.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        return _unescape_string(text[1:-1], text[0])
    return text


def _unescape_string(text: str, quote: str) -> str:
    return text.replace("\\\\", "\\").replace("\\" + quote, quote)


def _required_int(value: Any, label: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise ValueError(f"missing {label}")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
