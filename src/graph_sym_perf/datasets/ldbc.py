"""Deterministic small LDBC-like property graph data."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Mapping

from graph_sym_perf.core import DatasetConfig


@dataclass(frozen=True)
class MiniLdbcNode:
    label: str
    properties: Mapping[str, Any]


@dataclass(frozen=True)
class MiniLdbcRelationship:
    relationship_type: str
    start_label: str
    start_id: str
    end_label: str
    end_id: str
    properties: Mapping[str, Any]


@dataclass(frozen=True)
class MiniLdbcGraph:
    dataset_id: str
    nodes: tuple[MiniLdbcNode, ...]
    relationships: tuple[MiniLdbcRelationship, ...]

    def nodes_by_label(self, label: str) -> tuple[MiniLdbcNode, ...]:
        return tuple(node for node in self.nodes if node.label == label)


def generate_mini_ldbc(config: DatasetConfig) -> MiniLdbcGraph:
    rng = random.Random(config.seed)
    dataset_id = config.dataset_id
    nodes: list[MiniLdbcNode] = []
    relationships: list[MiniLdbcRelationship] = []

    places = [
        {"gbp_id": "place_001", "name": "Beijing", "kind": "city"},
        {"gbp_id": "place_002", "name": "Shanghai", "kind": "city"},
        {"gbp_id": "place_003", "name": "Hangzhou", "kind": "city"},
    ]
    organisations = [
        {"gbp_id": "org_001", "name": "AHU", "kind": "university"},
        {"gbp_id": "org_002", "name": "Graph Lab", "kind": "company"},
    ]
    tags = [
        {"gbp_id": f"tag_{index + 1:03d}", "name": name}
        for index, name in enumerate(["graph", "database", "testing", "cypher"][: config.tag_count])
    ]

    for item in places:
        nodes.append(_node("Place", dataset_id, item))
    for item in organisations:
        nodes.append(_node("Organisation", dataset_id, item))
    for item in tags:
        nodes.append(_node("Tag", dataset_id, item))

    first_names = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Heidi"]
    cities = [place["name"] for place in places]
    for index in range(config.person_count):
        person_id = f"person_{index + 1:03d}"
        city = cities[index % len(cities)]
        org = organisations[index % len(organisations)]["gbp_id"]
        nodes.append(
            _node(
                "Person",
                dataset_id,
                {
                    "gbp_id": person_id,
                    "first_name": first_names[index % len(first_names)],
                    "last_name": f"User{index + 1}",
                    "age": 20 + (index * 7) % 41,
                    "city": city,
                    "language": "en" if index % 2 == 0 else "zh",
                    "creation_day": 20200101 + index,
                },
            )
        )
        relationships.append(
            _relationship(
                "IS_LOCATED_IN",
                "Person",
                person_id,
                "Place",
                places[index % len(places)]["gbp_id"],
                dataset_id,
                {},
            )
        )
        relationships.append(
            _relationship("WORKS_AT", "Person", person_id, "Organisation", org, dataset_id, {"work_from": 2018 + index})
        )

    for index in range(config.forum_count):
        forum_id = f"forum_{index + 1:03d}"
        nodes.append(
            _node(
                "Forum",
                dataset_id,
                {
                    "gbp_id": forum_id,
                    "title": f"Forum {index + 1}",
                    "creation_day": 20200201 + index,
                },
            )
        )
        for member_index in range(index, config.person_count, max(config.forum_count, 1)):
            relationships.append(
                _relationship(
                    "HAS_MEMBER",
                    "Forum",
                    forum_id,
                    "Person",
                    f"person_{member_index + 1:03d}",
                    dataset_id,
                    {"join_day": 20200301 + member_index},
                )
            )

    for index in range(config.post_count):
        post_id = f"post_{index + 1:03d}"
        creator_id = f"person_{(index % config.person_count) + 1:03d}"
        forum_id = f"forum_{(index % max(config.forum_count, 1)) + 1:03d}"
        length = 40 + rng.randint(0, 160)
        nodes.append(
            _node(
                "Post",
                dataset_id,
                {
                    "gbp_id": post_id,
                    "content": f"GraphSymPerf post {index + 1}",
                    "length": length,
                    "creation_day": 20200401 + index,
                },
            )
        )
        relationships.append(_relationship("HAS_CREATOR", "Post", post_id, "Person", creator_id, dataset_id, {}))
        relationships.append(_relationship("CONTAINER_OF", "Forum", forum_id, "Post", post_id, dataset_id, {}))
        relationships.append(
            _relationship(
                "HAS_TAG",
                "Post",
                post_id,
                "Tag",
                tags[index % len(tags)]["gbp_id"],
                dataset_id,
                {},
            )
        )

    for index in range(config.person_count):
        person_id = f"person_{index + 1:03d}"
        friend_id = f"person_{((index + 1) % config.person_count) + 1:03d}"
        post_id = f"post_{(index % config.post_count) + 1:03d}"
        relationships.append(_relationship("KNOWS", "Person", person_id, "Person", friend_id, dataset_id, {"since": 2010 + index}))
        relationships.append(_relationship("LIKES", "Person", person_id, "Post", post_id, dataset_id, {"creation_day": 20200501 + index}))

    return MiniLdbcGraph(dataset_id=dataset_id, nodes=tuple(nodes), relationships=tuple(relationships))


def _node(label: str, dataset_id: str, properties: Mapping[str, Any]) -> MiniLdbcNode:
    merged = {"gbp_dataset": dataset_id}
    merged.update(properties)
    return MiniLdbcNode(label=label, properties=merged)


def _relationship(
    relationship_type: str,
    start_label: str,
    start_id: str,
    end_label: str,
    end_id: str,
    dataset_id: str,
    properties: Mapping[str, Any],
) -> MiniLdbcRelationship:
    merged = {"gbp_dataset": dataset_id}
    merged.update(properties)
    return MiniLdbcRelationship(
        relationship_type=relationship_type,
        start_label=start_label,
        start_id=start_id,
        end_label=end_label,
        end_id=end_id,
        properties=merged,
    )
