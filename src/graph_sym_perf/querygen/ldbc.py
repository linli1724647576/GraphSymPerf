"""Controlled LDBC-like Cypher query generation."""

from __future__ import annotations

from graph_sym_perf.core import DatasetConfig, QueryCase, QueryGenerationConfig
from graph_sym_perf.exprgen import safe_expression_for_template


def generate_query_cases(dataset: DatasetConfig, config: QueryGenerationConfig) -> tuple[QueryCase, ...]:
    limit = config.result_limit
    dataset_id = dataset.dataset_id
    max_cases = max(0, config.max_cases)
    cases: list[QueryCase] = []
    cities = ("Beijing", "Shanghai", "Hangzhou")
    tags = ("graph", "database", "testing", "cypher")

    for index in range(max_cases):
        family = index % 5
        case_id = f"case_{index + 1:04d}"
        if family == 0:
            min_age = 20 + (index % 41)
            cases.append(
                _case(
                    case_id,
                    "person_age_filter",
                    "p.gbp_dataset = $dataset_id AND p.age >= $min_age",
                    """
MATCH (p:Person)
WHERE p.gbp_dataset = $dataset_id AND p.age >= $min_age
RETURN p.gbp_id AS person_id, p.first_name AS first_name, p.age AS age
ORDER BY person_id
LIMIT $limit
""",
                    {"dataset_id": dataset_id, "min_age": min_age, "limit": limit},
                )
            )
        elif family == 1:
            city = cities[index % len(cities)]
            cases.append(
                _case(
                    case_id,
                    "knows_city_filter",
                    "p.gbp_dataset = $dataset_id AND f.gbp_dataset = $dataset_id AND p.city = $city",
                    """
MATCH (p:Person)-[:KNOWS]->(f:Person)
WHERE p.gbp_dataset = $dataset_id AND f.gbp_dataset = $dataset_id AND p.city = $city
RETURN p.gbp_id AS person_id, f.gbp_id AS friend_id, p.city AS city
ORDER BY person_id, friend_id
LIMIT $limit
""",
                    {"dataset_id": dataset_id, "city": city, "limit": limit},
                )
            )
        elif family == 2:
            min_length = 40 + (index % 160)
            cases.append(
                _case(
                    case_id,
                    "liked_post_creator",
                    (
                        "p.gbp_dataset = $dataset_id AND post.gbp_dataset = $dataset_id "
                        "AND author.gbp_dataset = $dataset_id AND post.length >= $min_length"
                    ),
                    """
MATCH (p:Person)-[:LIKES]->(post:Post)-[:HAS_CREATOR]->(author:Person)
WHERE p.gbp_dataset = $dataset_id AND post.gbp_dataset = $dataset_id AND author.gbp_dataset = $dataset_id AND post.length >= $min_length
RETURN p.gbp_id AS person_id, post.gbp_id AS post_id, author.gbp_id AS author_id, post.length AS length
ORDER BY person_id, post_id, author_id
LIMIT $limit
""",
                    {"dataset_id": dataset_id, "min_length": min_length, "limit": limit},
                )
            )
        elif family == 3:
            max_age = 21 + (index % 50)
            cases.append(
                _case(
                    case_id,
                    "forum_member_age",
                    "forum.gbp_dataset = $dataset_id AND p.gbp_dataset = $dataset_id AND p.age < $max_age",
                    """
MATCH (forum:Forum)-[:HAS_MEMBER]->(p:Person)
WHERE forum.gbp_dataset = $dataset_id AND p.gbp_dataset = $dataset_id AND p.age < $max_age
RETURN forum.gbp_id AS forum_id, p.gbp_id AS person_id, p.age AS age
ORDER BY forum_id, person_id
LIMIT $limit
""",
                    {"dataset_id": dataset_id, "max_age": max_age, "limit": limit},
                )
            )
        else:
            tag_name = tags[index % len(tags)]
            cases.append(
                _case(
                    case_id,
                    "post_tag_lookup",
                    "post.gbp_dataset = $dataset_id AND tag.gbp_dataset = $dataset_id AND tag.name = $tag_name",
                    """
MATCH (post:Post)-[:HAS_TAG]->(tag:Tag)
WHERE post.gbp_dataset = $dataset_id AND tag.gbp_dataset = $dataset_id AND tag.name = $tag_name
RETURN post.gbp_id AS post_id, tag.name AS tag_name, post.length AS length
ORDER BY post_id
LIMIT $limit
""",
                    {"dataset_id": dataset_id, "tag_name": tag_name, "limit": limit},
                )
            )
    return tuple(cases)


def _case(
    case_id: str,
    template_name: str,
    base_predicate: str,
    query: str,
    parameters: dict[str, object],
) -> QueryCase:
    return QueryCase(
        case_id=case_id,
        template_name=template_name,
        query=_compact(query),
        parameters=parameters,
        base_predicate=base_predicate,
        safe_expression=safe_expression_for_template(template_name),
    )


def _compact(query: str) -> str:
    return "\n".join(line.rstrip() for line in query.strip().splitlines())
