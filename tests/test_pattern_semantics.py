"""Small independent semantic counterexamples, not a second Cypher executor.

The truth rules are from Neo4j's CASE and predicate-function documentation;
MATCH-clause boundaries matter for relationship uniqueness. These examples
exercise nulls and bags which a render/parse round trip alone cannot detect.

Primary references:
https://neo4j.com/docs/cypher-manual/current/expressions/conditional-expressions/
https://neo4j.com/docs/cypher-manual/current/functions/predicate/
https://neo4j.com/docs/cypher-manual/current/patterns/reference/match-modes-and-path-modes/
https://neo4j.com/docs/cypher-manual/current/clauses/optional-match/
"""

from collections import Counter
from itertools import product

import pytest

from mutator import mutate
from query import (Alias, All, Boolean, Call, Case, Comparison, Concat, Count, Exists, ListExpr,
                   Literal, Negation, Property, QueryError, Variable, parse_query)


def truth(operator, left, right):
    if operator == 'AND':
        return False if left is False or right is False else None if left is None or right is None else True
    return True if left is True or right is True else None if left is None or right is None else False


def value(expression, environment=None):
    """Evaluate only the pure expressions under test with explicit null rules."""
    environment = environment or {}
    if isinstance(expression, Literal):
        return expression.value
    if isinstance(expression, Alias):
        return value(expression.expression, environment)
    if isinstance(expression, Variable):
        return environment[expression.name]
    if isinstance(expression, Property):
        return environment[expression.variable].get(expression.key)
    if isinstance(expression, (Exists, Count)):
        edge = expression.pattern
        anchor = environment[edge.left.variable]["uid"]
        candidates = environment["_edges"].get((edge.relationship.type, anchor), [])
        count = sum(expression.where is None or value(expression.where, environment | {
            edge.right.variable: leaf}) is True for leaf in candidates)
        return bool(count) if isinstance(expression, Exists) else count
    if isinstance(expression, Comparison):
        left, right = value(expression.left, environment), value(expression.right, environment)
        if expression.operator == 'IS NULL':
            return left is None
        if expression.operator == 'IS NOT NULL':
            return left is not None
        if left is None or right is None:
            return None
        return {'=': lambda: left == right, '<>': lambda: left != right,
                '<': lambda: left < right, '>': lambda: left > right,
                '<=': lambda: left <= right, '>=': lambda: left >= right}[expression.operator]()
    if isinstance(expression, Boolean):
        answer = value(expression.terms[0], environment)
        for term in expression.terms[1:]:
            answer = truth(expression.operator, answer, value(term, environment))
        return answer
    if isinstance(expression, Negation):
        answer = value(expression.expression, environment)
        return None if answer is None else not answer
    if isinstance(expression, Call) and expression.name.lower() == 'coalesce':
        return next((answer for arg in expression.args if (answer := value(arg, environment)) is not None), None)
    if isinstance(expression, Call) and expression.name.lower() == 'range':
        start, end, *steps = [value(arg, environment) for arg in expression.args]
        step = steps[0] if steps else 1
        return list(range(start, end + (1 if step > 0 else -1), step))
    if isinstance(expression, Case):
        branch = expression.then if value(expression.condition, environment) is True else expression.otherwise
        return value(branch, environment)
    if isinstance(expression, ListExpr):
        return [value(item, environment) for item in expression.items]
    if isinstance(expression, Concat):
        left, right = value(expression.left, environment), value(expression.right, environment)
        return None if left is None or right is None else left + right
    if isinstance(expression, All):
        items = value(expression.items, environment)
        if items is None:
            return None
        answers = [value(expression.predicate, {**environment, expression.variable: item}) for item in items]
        return False if False in answers else None if None in answers else True
    raise AssertionError(f'Independent evaluator does not cover {expression!r}')


@pytest.mark.parametrize('condition,expected', [('true', 11), ('false', 29), ('null', 29)])
def test_case_dual_preserves_nullable_condition(condition, expected):
    original = parse_query(f'RETURN CASE WHEN {condition} THEN 11 ELSE 29 END AS value')
    changed = mutate(original, 'P15')
    assert value(original.returns[0]) == expected
    assert value(changed.returns[0]) == expected


def test_naive_case_negation_has_a_real_null_counterexample():
    original = parse_query('RETURN CASE WHEN null THEN 11 ELSE 29 END AS value')
    unsound = parse_query('RETURN CASE WHEN NOT null THEN 29 ELSE 11 END AS value')
    assert value(original.returns[0]) != value(unsound.returns[0])
    assert value(mutate(original, 'P15').returns[0]) == 29


@pytest.mark.parametrize('left,right,expected', [
    ('[]', '[]', True), ('[true, true]', '[true]', True),
    ('[true]', '[null]', None), ('[null]', '[false]', False),
    ('[false, false]', '[true, null]', False), ('[null]', '[null]', None),
    ('null', '[true]', None), ('[false]', 'null', None),
])
def test_all_group_permutation_preserves_empty_duplicates_and_null(left, right, expected):
    query = parse_query(f'RETURN all(x IN ({left} + {right}) WHERE x = true) AS ok')
    assert value(query.returns[0]) is expected
    if left == right:
        with pytest.raises(QueryError, match='would not change'):
            mutate(query, 'P16')
        return
    changed = mutate(query, 'P16')
    assert value(changed.returns[0]) is expected
    original_all, changed_all = query.returns[0].expression, changed.returns[0].expression
    original_items, changed_items = value(original_all.items), value(changed_all.items)
    if original_items is None:
        assert changed_items is None
    else:
        assert Counter(original_items) == Counter(changed_items)


@pytest.mark.parametrize('operator,pattern', [('AND', 'P4'), ('OR', 'P5')])
def test_boolean_operand_order_with_nullable_properties(operator, pattern):
    query = parse_query(f'MATCH (u) WHERE u.a = true {operator} u.b = true RETURN u.id')
    changed = mutate(query, pattern)
    for a, b in product((True, False, None), repeat=2):
        environment = {'u': {'a': a, 'b': b}}
        assert value(query.where, environment) is value(changed.where, environment)


def _optional_product(left, right):
    return Counter((a, b) for a in (left or [None]) for b in (right or [None]))


@pytest.mark.parametrize('left,right', [([], []), ([1], []), ([], [2, 2]), ([1, 1], [2, 3])])
def test_independent_optional_branches_commute_as_bags_with_null_extension(left, right):
    query = parse_query('MATCH (u) OPTIONAL MATCH (u)-[:R]->(v) OPTIONAL MATCH (u)-[:S]->(w) RETURN u.id, v.id, w.id')
    changed = mutate(query, 'P13')
    # Output remains (v,w), although w is bound before v after mutation.
    assert changed.returns == query.returns
    forward = _optional_product(left, right)
    reverse = Counter((v, w) for (w, v), count in _optional_product(right, left).items() for _ in range(count))
    assert forward == reverse


def test_multiple_match_clauses_must_not_be_merged_into_a_relationship_unique_pattern():
    # One physical R edge u->v can satisfy each separate MATCH. A merged MATCH
    # with two relationship patterns would forbid reusing it and yield no row.
    query = parse_query('MATCH (u) MATCH (u)-[:R]->(v) MATCH (u)-[:R]->(w) RETURN u.id, v.id, w.id')
    changed = mutate(query, 'P11')
    from query import clauses
    assert len(clauses(query)) == len(clauses(changed)) == 3
    separate = Counter((1, target1, target2) for edge1, target1 in [(7, 2)] for edge2, target2 in [(7, 2)])
    merged = Counter((1, target1, target2) for edge1, target1 in [(7, 2)] for edge2, target2 in [(7, 2)] if edge1 != edge2)
    assert separate == Counter({(1, 2, 2): 1}) and not merged


def test_inverse_relation_equivalence_needs_bijective_relationship_multiplicity():
    # Matching endpoint sets is insufficient when parallel forward edges exist.
    forwards = [(1, 2, 'pair-1'), (1, 2, 'pair-2')]
    inverses = [(2, 1, 'pair-1'), (2, 1, 'pair-2')]
    expected = Counter((u, v) for u, v, _ in forwards)
    assert expected == Counter((u, v) for v, u, _ in inverses)
    assert expected != Counter((u, v) for v, u, _ in inverses[:1])


def test_union_all_branch_reordering_preserves_duplicates_not_just_the_set():
    query = parse_query('RETURN 1 AS value UNION ALL RETURN CASE WHEN true THEN 1 ELSE 2 END AS value')
    changed = mutate(query, 'P14')
    before = Counter(value(branch.returns[0]) for branch in query.branches)
    after = Counter(value(branch.returns[0]) for branch in changed.branches)
    assert before == after == Counter({1: 2})


@pytest.mark.parametrize("complexity", [0, 1, 2])
@pytest.mark.parametrize("pattern", ["P4", "P5", "P15"])
def test_every_reference_role_combination_preserves_rows_with_decisive_and_nullable_anchors(pattern, complexity):
    from generator import instantiate
    flags = {1: pattern == "P5", 2: pattern != "P5"}
    if pattern == "P15":
        flags = {1: True, 2: False, 3: None}
    original_edges = {1: [{"mark": False}] * 12, 2: [{"mark": True}], 3: []}
    retain_true = pattern == "P4"
    edges = {("GSP_EXPAND", uid): leaves for uid, leaves in original_edges.items()}
    edges.update({("GSP_REFERENCE", uid): leaves for uid, leaves in original_edges.items()
                  if bool(flags.get(uid)) is retain_true})
    for seed in range(54):
        case = instantiate(None, pattern, 3, seed, data_seed=0, complexity=complexity)
        def rows(query):
            result = []
            for uid, flag in flags.items():
                environment = {query.pattern.variable: {"uid": uid, "keep": flag}, "_edges": edges}
                if query.where is None or value(query.where, environment) is True:
                    result.append(tuple(value(projection, environment) for projection in query.returns))
            return Counter(result)
        before = rows(case["query_a"])
        assert before == rows(case["query_ref"])
        assert before == Counter(tuple(row.values()) for row in case["expected_rows"])


@pytest.mark.parametrize("complexity", [0, 1, 2])
def test_all_reference_discards_only_a_group_irrelevant_to_the_explicit_false(complexity):
    from generator import instantiate
    for seed in range(8):
        case = instantiate(None, "P16", 3, seed, data_seed=0, complexity=complexity)
        assert value(case["query_a"].returns[0]) is False
        assert value(case["query_ref"].returns[0]) is False
