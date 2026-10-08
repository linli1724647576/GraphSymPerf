"""SCor graph adaptations on an explicitly supplied, dedicated Neo4j instance."""

from copy import deepcopy
import os
from pathlib import Path

import pytest

from executor import measurement_settings, run_case
from generator import PatternNotApplicable, instantiate, load_seeds
from main import read_case, save_case
from mutator import mutate
from query import render_query


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv('GRAPHSYMPERF_TEST_URI'), reason='no dedicated Neo4j test target supplied'),
]


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


def scor_case(real_seeds, pattern, scale=2, seed=11):
    for source in real_seeds:
        try:
            case = instantiate(source, pattern, scale, seed)
        except PatternNotApplicable:
            continue
        case['query_b'] = render_query(mutate(case['query_a'], pattern, bindings=case['bindings']))
        case['query_a'] = render_query(case['query_a'])
        assert case['source']['raw_query'] == source.text
        return case
    pytest.fail(f'No real GDsmith fixture seed supports {pattern}')


def assert_cost_rejected(report):
    assert report['status'] == 'cost_check_failed', report
    assert report['discovery']['warmups'] == []
    assert report['discovery']['samples'] == []
    assert report['confirmation_sessions'] == []


@pytest.mark.parametrize('pattern', [f'S{i}' for i in range(1, 11)])
@pytest.mark.parametrize('scale', [2, 5])
def test_scor_real_graph_execution_and_replay(tmp_path, database, settings, real_seeds, pattern, scale):
    case = scor_case(real_seeds, pattern, scale)
    report = run_case(case, database, settings)
    assert report['status'] in {'no_candidate', 'candidate', 'not_confirmed'}, report
    assert report['discovery']['samples']
    if pattern == 'S3':
        assert case['query_a'].startswith('CYPHER 25 WHEN ')
        assert case['query_b'].startswith('CYPHER 25 WHEN ')

    directory = tmp_path / f'{pattern}-{scale}'
    save_case(directory, case, settings, report)
    replayed, protocol = read_case(directory)
    assert replayed == case
    again = run_case(replayed, database, protocol)
    assert again['status'] in {'no_candidate', 'candidate', 'not_confirmed'}, again

    invalid = deepcopy(case)
    assert invalid['checks']
    invalid['checks'][0]['expected_rows'] = [{'__intentional_cost_failure__': True}]
    assert_cost_rejected(run_case(invalid, database, settings))


@pytest.mark.parametrize('pattern,seed,rows', [
    ('S4', 0, 1), ('S4', 1, 1), ('S4', 2, 1),
    ('S5', 0, 0), ('S5', 1, 0),
    ('S6', 0, 1), ('S6', 1, 2), ('S6', 2, 3),
    ('S10', 0, 0), ('S10', 1, 0),
])
def test_scor_case_null_limit_and_membership_variants(database, settings, real_seeds, pattern, seed, rows):
    case = scor_case(real_seeds, pattern, seed=seed)
    report = run_case(case, database, settings)
    assert report['status'] in {'no_candidate', 'candidate', 'not_confirmed'}, report
    assert report['result_rows'] == rows
    if pattern == 'S4' and seed == 2:
        assert 'CASE WHEN null THEN' in case['query_a']


@pytest.mark.parametrize('pattern', ['S6', 'S8'])
def test_scor_missing_reference_witness_prevents_measurement(database, settings, real_seeds, pattern):
    case = scor_case(real_seeds, pattern)
    # Delete only this newly restored case's reference relationships. The HIGH
    # region remains intact, so it cannot stand in for the missing REF witness.
    case['setup'].append('MATCH (:ScorRoot)-[r:GSP_SC_REF]->() DELETE r')
    assert_cost_rejected(run_case(case, database, settings))
