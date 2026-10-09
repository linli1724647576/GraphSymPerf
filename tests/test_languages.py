from dataclasses import replace

import pytest

from ir import QueryError
from languages import UnsupportedFeature, UnsupportedLanguage, generate, render_saved, validate_language
from query import parse_query, render_query


@pytest.mark.parametrize("target", ["neo4j", "memgraph", "falkordb"])
def test_existing_targets_keep_original_text_and_column_names(target):
    tree = parse_query('MATCH (u:A) WHERE u.keep = true OR false RETURN u.uid, size([1, 2]) AS n')
    compiled = generate(tree, target)
    assert compiled.text == render_query(tree)
    assert compiled.columns == (("u.uid", "u.uid"), ("n", "n"))
    assert compiled.normalize_rows([{"u.uid": 7, "n": 2}]) == [{"u.uid": 7, "n": 2}]


@pytest.mark.parametrize("target", ["arcadedb", "age", "ladybug"])
def test_new_targets_alias_and_restore_columns_without_collapsing_bag(target):
    tree = parse_query('MATCH (u:A) RETURN u.uid, size([1, 2]) AS n')
    compiled = generate(tree, target)
    assert compiled.text.endswith('RETURN u.uid AS c0, size([1, 2]) AS c1')
    assert compiled.columns == (("c0", "u.uid"), ("c1", "n"))
    assert compiled.normalize_rows([{"c0": 7, "c1": 2}] * 2) == [{"u.uid": 7, "n": 2}] * 2
    with pytest.raises(QueryError, match="columns"):
        compiled.normalize_rows([{"c0": 7}])


def test_ladybug_paths_use_explicit_native_trail_in_subqueries_as_well():
    tree = parse_query('MATCH p=(u:A)-[:R*1..3]-(v:B) WHERE all(x IN nodes(p) WHERE x.keep = true) AND EXISTS { MATCH (u)-[:S*0..2]-(w) WHERE w.keep = true } RETURN length(p) AS hops')
    compiled = generate(tree, "ladybug")
    assert '[:R* TRAIL 1..3]' in compiled.text
    assert '[:S* TRAIL 0..2]' in compiled.text
    assert 'nodes(p)' in compiled.text
    assert 'EXISTS { MATCH' in compiled.text
    walk = replace(tree, pattern=replace(tree.pattern, relationship=replace(tree.pattern.relationship, path_mode="walk")))
    assert '[:R* WALK 1..3]' in generate(walk, "ladybug").text
    with pytest.raises(UnsupportedFeature) as rejected:
        generate(walk, "neo4j")
    assert rejected.value.features == frozenset({"path_mode_walk"})


def test_ladybug_label_conjunction_is_rejected_not_reinterpreted_as_union():
    tree = parse_query('MATCH (u:A:B) RETURN u.uid')
    with pytest.raises(UnsupportedFeature) as rejected:
        generate(tree, "ladybug")
    assert rejected.value.features == frozenset({"multi_label_conjunction"})


def test_output_aliases_do_not_change_clause_operand_or_union_order():
    tree = parse_query('MATCH (u:A) WHERE u.keep = true OR false RETURN u.uid AS id UNION ALL MATCH (v:B) RETURN v.uid AS id')
    compiled = generate(tree, "age")
    assert compiled.text.index('(u:A)') < compiled.text.index('(v:B)')
    assert '(u.keep = true) OR false' in compiled.text
    assert compiled.text.count('AS c0') == 2
    assert compiled.columns == (("c0", "id"),)


@pytest.mark.parametrize("language", ["gremlin", "gql", "SQL"])
def test_reserved_and_unknown_language_generators_fail_explicitly(language):
    with pytest.raises(UnsupportedLanguage):
        generate(parse_query('RETURN 1 AS value'), language=language)


def test_non_neo4j_conditional_query_is_explicitly_unsupported():
    tree = parse_query('CYPHER 25 WHEN true THEN { RETURN 1 AS value } ELSE { RETURN 2 AS value }')
    for target in ("memgraph", "falkordb", "arcadedb", "age", "ladybug"):
        with pytest.raises(UnsupportedFeature, match="conditional_query"):
            validate_language(tree, target)
    assert generate(tree).text.startswith('CYPHER 25 WHEN')


@pytest.mark.parametrize("text,feature", [
    ('MATCH (u)-[:R|S]->(v) RETURN u.uid', "relationship_alternatives"),
    ('MATCH (u:A) MATCH (u:A)-[:R]->(v) RETURN u.uid', "label_rebinding"),
    ('RETURN all(x IN [1, 2] WHERE x > 0) AS ok', "all"),
])
def test_age_verified_release_limits_fail_without_rewriting(text, feature):
    tree = parse_query(text)
    with pytest.raises(UnsupportedFeature) as rejected:
        generate(tree, "age")
    assert feature in rejected.value.features
    compiled = generate(tree, "age", verified_features=(feature,))
    assert feature in compiled.required_features
    assert "AS c0" in compiled.text
    assert generate(tree, "arcadedb").text == compiled.text


def test_age_rebinding_gate_does_not_reject_correlated_subquery_labels():
    tree = parse_query('MATCH (u:A) RETURN COUNT { MATCH (u:A)-[:R]->(v) } AS value')
    assert "COUNT { MATCH (u:A)" in generate(tree, "age").text


@pytest.mark.parametrize("expression", ['coalesce(null, true, 3)',
                                         'coalesce(null, CASE WHEN true THEN true ELSE null END, COUNT { MATCH (u:A) })'])
def test_ladybug_mixed_coalesce_rejects_known_cross_type_coercion(expression):
    tree = parse_query(f'RETURN {expression} AS value')
    with pytest.raises(UnsupportedFeature) as rejected:
        generate(tree, "ladybug")
    assert rejected.value.features == frozenset({"mixed_type_coalesce"})
    assert "coalesce" in generate(tree, "neo4j").text


@pytest.mark.parametrize("expression", ['coalesce(null, 1, 1.5)', 'coalesce(null, true, false)'])
def test_ladybug_compatible_coalesce_kinds_remain_available(expression):
    assert "coalesce" in generate(parse_query(f'RETURN {expression} AS value'), "ladybug").text


def test_ladybug_nested_case_is_not_hidden_by_a_syntax_limit_gate():
    tree = parse_query('MATCH (u:A) RETURN CASE WHEN CASE WHEN u.key = 1 THEN u.key = 1 ELSE null END THEN 7 ELSE 3 END AS value')
    assert "CASE WHEN CASE WHEN" in generate(tree, "ladybug").text


@pytest.mark.parametrize("expression", [
    'coalesce((u.keep = true), null)',
    'coalesce(((u.keep = true) AND (u.uid = u.uid)), false)',
    'coalesce((NOT (u.keep = false)), null)',
    'coalesce(null, (u.keep = true), false)',
])
def test_age_boolean_expression_coalesce_is_a_precise_tested_type_limit(expression):
    tree = parse_query(f'MATCH (u:A) RETURN {expression} AS value')
    with pytest.raises(UnsupportedFeature) as rejected:
        generate(tree, "age")
    assert rejected.value.features == frozenset({"boolean_expression_coalesce"})
    compiled = generate(tree, "age", verified_features=("boolean_expression_coalesce",))
    assert expression.startswith("coalesce") and "coalesce" in compiled.text
    assert "CASE" not in compiled.text
    assert render_saved(tree, "age") == compiled


@pytest.mark.parametrize("expression", ['coalesce(u.keep, false)', 'coalesce(null, true, false)',
                                         'coalesce((u.keep = true), (u.uid = 1))'])
def test_age_boolean_property_and_literal_coalesce_are_not_blanket_rejected(expression):
    tree = parse_query(f'MATCH (u:A) RETURN {expression} AS value')
    assert "boolean_expression_coalesce" not in generate(tree, "age").required_features


@pytest.mark.parametrize("target,text", [
    ("age", 'RETURN all(x IN [1, 2] WHERE x > 0) AS ok'),
    ("ladybug", 'RETURN coalesce(null, true, 3) AS value'),
])
def test_archived_text_readback_is_separate_from_current_target_support(target, text):
    tree = parse_query(text)
    with pytest.raises(UnsupportedFeature):
        generate(tree, target)
    archived = render_saved(tree, target)
    assert archived.text.endswith(" AS c0")
    with pytest.raises(UnsupportedFeature):
        validate_language(tree, target)


def test_saved_readback_still_requires_ir_and_language_validity():
    tree = parse_query('MATCH (u:A) RETURN u.uid')
    with pytest.raises(UnsupportedLanguage):
        render_saved(tree, language="gql")
    with pytest.raises(QueryError):
        render_saved(replace(tree, returns=(replace(tree.returns[0], variable="absent"),)))
