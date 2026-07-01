"""Safe boolean expressions for the controlled LDBC query subset."""

from __future__ import annotations


def safe_expression_for_template(template_name: str) -> str:
    expressions = {
        "person_age_filter": "p.gbp_id IS NOT NULL",
        "knows_city_filter": "f.gbp_id IS NOT NULL",
        "liked_post_creator": "post.length >= 0",
        "forum_member_age": "p.age >= 0",
        "post_tag_lookup": "tag.name IS NOT NULL",
    }
    return expressions[template_name]
