"""Only run when a dedicated Neo4j target is explicitly supplied by the caller."""

from copy import deepcopy
import os
from pathlib import Path

import pytest

from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case, typed_bag
from generator import PatternNotApplicable, instantiate, load_seeds
from main import read_case, save_case
from mutator import mutate
from query import parse_query, render_query


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv('GRAPHSYMPERF_TEST_URI'), reason='no dedicated Neo4j test target supplied')]


@pytest.fixture
def database():
    return {'uri': os.environ['GRAPHSYMPERF_TEST_URI'], 'user': 'neo4j',
            'password_env': 'GRAPHSYMPERF_NEO4J_PASSWORD', 'database': 'neo4j',
            'dedicated': True, 'allow_reset': True}


@pytest.fixture(scope='module')
def real_seeds():
    fixture = Path(__file__).parent / 'fixtures/gdsmith/neo4j_seeds.log'
    return load_seeds({'log_files': [str(fixture)]})


@pytest.fixture(scope='module')
def settings():
    return measurement_settings({'discovery_pairs': 2, 'discovery_warmups': 1,
                                 'confirmation_pairs': 4, 'confirmation_warmups': 1,
                                 'control_pairs': 2, 'bootstrap_samples': 100})


def populated_case(real_seeds, pattern, scale=2):
    for source in real_seeds:
        try:
            case = instantiate(source, pattern, scale, 11)
        except PatternNotApplicable:
            continue
        case['query_b'] = render_query(mutate(case['query_a'], pattern, bindings=case['bindings']))
        case['query_a'] = render_query(case['query_a'])
        if case.get('query_ref') is not None:
            case['query_ref'] = render_query(case['query_ref'])
        assert case['source']['raw_query'] == source.text
        return case
    pytest.fail(f'No real GDsmith fixture seed supports {pattern}')


@pytest.mark.parametrize('pattern', [f'P{i}' for i in range(1, 17)])
@pytest.mark.parametrize('scale', [2, 5])
def test_real_gdsmith_to_database_save_and_replay(tmp_path, database, settings, real_seeds, pattern, scale):
    case = populated_case(real_seeds, pattern, scale)
    report = run_case(case, database, settings)
    assert report['status'] in {'no_candidate', 'candidate', 'not_confirmed'}, report
    assert report['discovery']['samples']
    directory = tmp_path / f'{pattern}-{scale}'
    save_case(directory, case, settings, report)
    replayed, protocol = read_case(directory)
    assert replayed == case
    again = run_case(replayed, database, protocol)
    assert again['status'] in {'no_candidate', 'candidate', 'not_confirmed'}, again

    # This applies to every pattern, including index-free P16. A deliberately
    # wrong cost fact must stop execution before warmup or timed samples.
    invalid = deepcopy(case)
    assert invalid['checks']
    invalid['checks'][0]['expected_rows'] = [{'__intentional_cost_failure__': True}]
    rejected = run_case(invalid, database, settings)
    assert rejected['status'] == 'cost_check_failed', rejected
    assert rejected['discovery']['warmups'] == []
    assert rejected['discovery']['samples'] == []
    assert rejected['confirmation_sessions'] == []


def test_p2_missing_inverse_relationship_invalidates_state(database, settings, real_seeds):
    case = populated_case(real_seeds, 'P2')
    db = Neo4j(database, settings)
    try:
        db.restore(case)
        check_case(db, case, settings)
        db.execute('MATCH ()-[r:GSP_REVERSE]->() WITH r LIMIT 1 DELETE r', {}, 10)
        with pytest.raises(CaseFailure, match='cost_check'):
            check_case(db, case, settings)
    finally:
        db.close()


def assert_database_pair(db, text, pattern, expected):
    query = parse_query(text)
    changed = mutate(query, pattern)
    assert render_query(changed) != render_query(query)
    for variant in (query, changed):
        result = db.execute(render_query(variant), {}, 10)
        assert result['complete']
        assert typed_bag(result['rows']) == typed_bag(expected)


def test_p13_optional_null_extension_and_parallel_relationships(database, settings):
    db = Neo4j(database, settings)
    try:
        db.restore({'setup': [
            'CREATE (u1:A {uid:1}), (u2:A {uid:2}), (u3:A {uid:3}), '
            '(v1:B {uid:10}), (v2:B {uid:11}), (w1:C {uid:20}), (w3:C {uid:21}), '
            '(u1)-[:R]->(v1), (u1)-[:R]->(v1), (u1)-[:S]->(w1), '
            '(u2)-[:R]->(v2), (u3)-[:S]->(w3)'
        ]})
        assert_database_pair(
            db,
            'MATCH (u:A) OPTIONAL MATCH (u)-[:R]->(v:B) '
            'OPTIONAL MATCH (u)-[:S]->(w:C) RETURN u.uid AS u, v.uid AS v, w.uid AS w',
            'P13',
            [{'u': 1, 'v': 10, 'w': 20}, {'u': 1, 'v': 10, 'w': 20},
             {'u': 2, 'v': 11, 'w': None}, {'u': 3, 'v': None, 'w': 21}],
        )
    finally:
        db.close()


@pytest.mark.parametrize('condition,expected', [('true', 11), ('false', 29), ('null', 29)])
def test_p15_case_preserves_true_false_and_null(database, settings, condition, expected):
    db = Neo4j(database, settings)
    try:
        assert_database_pair(db, f'RETURN CASE WHEN {condition} THEN 11 ELSE 29 END AS value',
                             'P15', [{'value': expected}])
    finally:
        db.close()


@pytest.mark.parametrize('left,right,expected', [
    ('[]', 'range(1, 0)', True),
    ('[]', '[true, null]', None),
    ('[false]', '[null]', False),
    ('[null]', '[false]', False),
    ('null', '[true]', None),
    ('[true]', 'null', None),
])
def test_p16_list_groups_preserve_empty_and_null_semantics(database, settings, left, right, expected):
    db = Neo4j(database, settings)
    try:
        assert_database_pair(db, f'RETURN all(x IN ({left} + {right}) WHERE x = true) AS ok',
                             'P16', [{'ok': expected}])
    finally:
        db.close()
