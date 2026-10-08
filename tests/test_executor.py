from copy import deepcopy

import pytest

import executor
from executor import CaseFailure, check_case, measurement_settings, run_case, typed_bag, validate_read_query


CASE = {'seed': 7, 'setup': ['CREATE (:A {id:1})'],
        'query_a': 'MATCH (n:A) WHERE n.id = 1 AND true RETURN n.id',
        'query_b': 'MATCH (n:A) WHERE true AND n.id = 1 RETURN n.id',
        'parameters': {}, 'checks': [{'query': 'MATCH (n:A) RETURN count(n) AS count', 'expected_rows': [{'count': 1}]}],
        'expected_rows': [{'n.id': 1}]}
SETTINGS = {'discovery_warmups': 0, 'discovery_pairs': 2,
            'confirmation_warmups': 0, 'confirmation_pairs': 4,
            'control_pairs': 2, 'bootstrap_samples': 100}


class FakeDatabase:
    instances = []
    version = 'fake-for-unit-test'
    mismatch = False
    incomplete = False
    cost_failure = False
    timeout = False
    reverse_session = None
    duration_b = 5.0

    def __init__(self, config, settings):
        self.ordinal = len(self.instances)
        self.instances.append(self)
        self.calls = []
        self.closed = False

    def restore(self, case):
        self.case = case

    def execute(self, query, parameters, timeout):
        self.calls.append(query)
        for check in self.case['checks']:
            if query == check['query']:
                rows = [{'count': -1}] if self.cost_failure else check['expected_rows']
                return {'rows': rows, 'complete': True, 'elapsed_ms': 1}
        if self.timeout and len(self.calls) >= 4:
            raise CaseFailure('timeout', 'test-timeout', stop_target=True)
        is_b = query == self.case['query_b']
        rows = [{'n.id': 2}] if is_b and self.mismatch else self.case['expected_rows']
        elapsed = self.duration_b if is_b else 1.0
        if self.ordinal == self.reverse_session:
            elapsed = 1 if is_b else 5
        return {'rows': rows, 'complete': not self.incomplete, 'elapsed_ms': elapsed}

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    class Database(FakeDatabase):
        instances = []
    monkeypatch.setattr(executor, 'Neo4j', Database)
    return Database


def test_typed_multisets_preserve_null_boolean_and_repeats():
    assert typed_bag([{'x': None}, {'x': 1}]) == typed_bag([{'x': 1}, {'x': None}])
    assert typed_bag([{'x': True}]) != typed_bag([{'x': 1}])
    assert typed_bag([{'x': 1}, {'x': 1}]) != typed_bag([{'x': 1}])
    assert typed_bag([{'x': [1, 2]}]) != typed_bag([{'x': [2, 1]}])
    with pytest.raises(CaseFailure):
        typed_bag([{'x': float('nan')}])


def test_candidate_has_balanced_samples_fresh_sessions_and_controls(fake):
    report = run_case(CASE, {}, SETTINGS)
    assert report['status'] == 'candidate'
    assert len(fake.instances) == 3 and all(db.closed for db in fake.instances)
    assert report['summary']['slow_side'] == 'b'
    assert report['summary']['median_ratio'] == 5
    for session in report['confirmation_sessions']:
        assert sorted(s['order'] for s in session['samples']) == ['AB', 'AB', 'BA', 'BA']
        assert len(session['control_a']) == len(session['control_b']) == 2
        assert session['noise_p99_ms'] == 0


def test_changed_direction_in_one_session_fails_confirmation(fake):
    fake.reverse_session = 2
    report = run_case(CASE, {}, SETTINGS)
    assert report['status'] == 'not_confirmed'
    assert report['summary']['slow_side'] == 'b'


@pytest.mark.parametrize('attribute,status', [('mismatch', 'result_mismatch'), ('incomplete', 'incomplete_result'), ('cost_failure', 'cost_check_failed')])
def test_invalid_result_or_cost_never_enters_measurement(fake, attribute, status):
    setattr(fake, attribute, True)
    report = run_case(CASE, {}, SETTINGS)
    assert report['status'] == status
    assert report['discovery']['samples'] == []
    assert report['confirmation_sessions'] == []


def test_reference_failure_even_if_a_and_b_agree(fake):
    case = deepcopy(CASE)
    # The database will return CASE's expected value, but the case expects another.
    original = fake.execute
    def execute(self, query, parameters, timeout):
        result = original(self, query, parameters, timeout)
        if query in (case['query_a'], case['query_b']):
            result['rows'] = [{'n.id': 999}]
        return result
    fake.execute = execute
    assert run_case(case, {}, SETTINGS)['status'] == 'reference_mismatch'


def test_timeout_keeps_partial_sample_without_inventing_duration(fake):
    fake.timeout = True
    report = run_case(CASE, {}, SETTINGS)
    assert report['status'] == 'timeout' and report['stop_target']
    assert len(fake.instances) == 1
    sample = report['discovery']['samples'][0]
    assert sample['a_ms'] is None and sample['b_ms'] is None
    assert 'timeout' in (sample['a_status'], sample['b_status'])


def test_no_signal_does_not_create_confirmation_sessions(fake):
    fake.duration_b = 1.05
    report = run_case(CASE, {}, SETTINGS)
    assert report['status'] == 'no_candidate'
    assert len(fake.instances) == 1


def test_read_gate_distinguishes_data_checks_and_queries():
    validate_read_query('MATCH (n) WHERE n.text = "DELETE" RETURN n.id')
    validate_read_query('SHOW INDEXES YIELD name RETURN name', schema_check=True)
    validate_read_query('CYPHER 25 WHEN true THEN { RETURN 1 AS value } ELSE { RETURN 2 AS value }')
    for text in ('MATCH (n) DELETE n RETURN n', 'RETURN 1; CREATE (n)', 'CALL db.labels()', 'SHOW INDEXES',
                 'CYPHER 25 WHEN true THEN { CREATE (n) RETURN 1 AS value } ELSE { RETURN 2 AS value }'):
        with pytest.raises(ValueError):
            validate_read_query(text)


def test_measurement_requires_balance_and_complete_confirmation():
    for settings in ({'confirmation_sessions': 1}, {'discovery_pairs': 3}, {'max_rows': True}, {'ratio': float('nan')}):
        with pytest.raises(ValueError):
            measurement_settings(settings)


def test_driver_consumes_results_before_stopping_clock_and_marks_row_cap(monkeypatch):
    events = []
    class Cursor:
        def __iter__(self):
            yield {'x': 1}
            yield {'x': 2}
        def consume(self):
            events.append('consume')
    class Session:
        def __enter__(self):
            events.append('session')
            return self
        def __exit__(self, *args):
            return False
        def run(self, query, parameters):
            events.append('run')
            assert query == ('RETURN 1', 10)
            return Cursor()
    class Driver:
        def session(self, **kwargs):
            assert kwargs == {'database': 'test'}
            return Session()
    clock = iter((1.0, 1.01))
    def now():
        events.append('clock')
        return next(clock)
    monkeypatch.setattr(executor, 'perf_counter', now)
    db = executor.Neo4j.__new__(executor.Neo4j)
    db.driver, db.database, db.settings = Driver(), 'test', {'max_rows': 1}
    db._query = lambda text, timeout: (text, timeout)
    db._server_error = RuntimeError
    result = db.execute('RETURN 1', {}, 10)
    assert result['rows'] == [{'x': 1}] and result['complete'] is False
    assert result['elapsed_ms'] == pytest.approx(10)
    assert events == ['session', 'clock', 'run', 'consume', 'clock']


def test_missing_credentials_is_actionable_without_leaking_values(monkeypatch):
    monkeypatch.delenv('NONEXISTENT_GSP_TEST_PASSWORD', raising=False)
    with pytest.raises(CaseFailure, match='password_env_missing'):
        executor.Neo4j({'database': 'neo4j', 'allow_reset': True, 'dedicated': True,
                        'password_env': 'NONEXISTENT_GSP_TEST_PASSWORD'}, measurement_settings({}))


def test_conditional_feature_is_probed_before_resetting_an_older_database(fake):
    def unsupported(self, query, parameters, timeout):
        assert query == 'CYPHER 25 RETURN 1 AS supported'
        raise CaseFailure('query_error', 'unsupported-language-version')
    fake.execute = unsupported
    report = run_case({**CASE, 'required_cypher': 25}, {}, SETTINGS)
    assert report['status'] == 'unsupported_feature'
    assert not report['stop_target']
    assert not hasattr(fake.instances[0], 'case')
    assert fake.instances[0].closed


def test_reference_first_and_common_slow_queries_are_detected(fake):
    original = fake.execute
    reference = 'RETURN 1 AS reference_value'
    def execute(self, query, parameters, timeout):
        result = original(self, query, parameters, timeout)
        result['elapsed_ms'] = 10 if query in (CASE['query_a'], CASE['query_b']) else 1
        return result
    fake.execute = execute
    report = run_case({**CASE, 'query_ref': reference}, {}, SETTINGS)
    assert fake.instances[0].calls[:3] == [CASE['checks'][0]['query'], reference, CASE['query_a']]
    assert report['discovery']['summary']['median_ratio'] == 1
    assert report['status'] == 'candidate'
    assert report['candidate_comparison'] in {'query_a_vs_ref', 'query_b_vs_ref'}
    assert all(c['status'] == 'candidate' for c in report['reference_comparisons'].values())
    assert all(len(c['confirmation_sessions']) == 2 for c in report['reference_comparisons'].values())
    assert len(fake.instances) == 3  # shared restores, not recursive runs


def test_independent_reference_mismatch_blocks_all_measurement(fake):
    original = fake.execute
    reference = 'RETURN 1 AS reference_value'
    def execute(self, query, parameters, timeout):
        result = original(self, query, parameters, timeout)
        if query == reference:
            result['rows'] = [{'n.id': 999}]
        return result
    fake.execute = execute
    report = run_case({**CASE, 'query_ref': reference}, {}, SETTINGS)
    assert report['status'] == 'reference_mismatch'
    assert report['discovery']['samples'] == []
    assert CASE['query_a'] not in fake.instances[0].calls


@pytest.mark.parametrize('cancelled', [True, False])
def test_high_timeout_with_fast_reference_keeps_lower_bound_and_stop_state(fake, cancelled):
    original = fake.execute
    def execute(self, query, parameters, timeout):
        if query == CASE['query_a']:
            self.calls.append(query)
            raise CaseFailure('timeout', 'server_timeout', stop_target=not cancelled,
                              details={'cancellation_verified': cancelled, 'elapsed_lower_bound_ms': 1000})
        return original(self, query, parameters, timeout)
    fake.execute = execute
    report = run_case({**CASE, 'reference_side': 'b'}, {}, SETTINGS)
    assert fake.instances[0].calls[1:3] == [CASE['query_b'], CASE['query_a']]
    assert report['status'] == 'timeout_candidate'
    assert report['stop_target'] is not cancelled
    assert report['ratio_lower_bound'] == 200
    assert report['summary'] is None and report['discovery']['samples'] == []
    assert report['timed_out_side'] == 'a'


def test_unknown_timeout_or_timed_out_reference_is_inconclusive(fake):
    original = fake.execute
    def execute(self, query, parameters, timeout):
        if query == CASE['query_b']:
            raise CaseFailure('timeout', 'client_timeout', stop_target=True,
                              details={'cancellation_verified': False, 'elapsed_lower_bound_ms': None})
        return original(self, query, parameters, timeout)
    fake.execute = execute
    report = run_case({**CASE, 'reference_side': 'b'}, {}, SETTINGS)
    assert report['status'] == 'timeout' and report['stop_target']
    assert 'ratio_lower_bound' not in report


def test_state_reuse_preserves_checks_and_forces_confirmation_restores(monkeypatch):
    class Database(FakeDatabase):
        instances = []
        restores = 0
        state = CASE
        duration_b = 1.05
        def __init__(self, config, settings):
            super().__init__(config, settings)
            self.case = type(self).state
        def restore(self, case):
            super().restore(case)
            type(self).state = case
            type(self).restores += 1
        def health(self):
            return True
    monkeypatch.setattr(executor, 'make_database', lambda *args: Database(*args))
    cache = {}
    first = run_case(CASE, {}, SETTINGS, state_cache=cache)
    second = run_case({**CASE, 'seed': 8}, {}, SETTINGS, state_cache=cache)
    assert not first['state_reused'] and second['state_reused']
    assert Database.restores == 1
    assert Database.instances[1].calls[0] == CASE['checks'][0]['query']
    Database.duration_b = 5
    confirmed = run_case(CASE, {}, SETTINGS, state_cache=cache)
    assert confirmed['status'] == 'candidate' and confirmed['state_reused']
    assert Database.restores == 3
    Database.cost_failure = True
    assert run_case(CASE, {}, SETTINGS, state_cache=cache)['status'] == 'cost_check_failed'
    assert cache == {}


def test_state_fingerprint_changes_with_setup_and_checks_but_not_query_order():
    baseline = executor.state_fingerprint(CASE, {})
    assert executor.state_fingerprint({**CASE, 'seed': 999, 'pattern': 'P5', 'query_a': CASE['query_b']}, {}) == baseline
    assert executor.state_fingerprint({**CASE, 'setup': ['CREATE (:B)']}, {}) != baseline
    assert executor.state_fingerprint({**CASE, 'checks': []}, {}) != baseline


def test_scor_requires_tested_query_to_be_slower_than_reference(fake):
    report = run_case({**CASE, 'reference_side': 'b'}, {}, SETTINGS)
    # The fake's B is slower. A slow oracle is not an omitted optimization.
    assert report['discovery']['summary']['slow_side'] == 'b'
    assert report['status'] == 'no_candidate'
    assert report['reference_direction'] == 'reference_not_faster'
    assert len(fake.instances) == 1


def test_slow_independent_reference_records_difference_without_bug_candidate(fake):
    original = fake.execute
    reference = 'RETURN 1 AS reference_value'
    def execute(self, query, parameters, timeout):
        result = original(self, query, parameters, timeout)
        result['elapsed_ms'] = 10 if query == reference else 1
        return result
    fake.execute = execute
    report = run_case({**CASE, 'query_ref': reference, 'reference_side': 'ref'}, {}, SETTINGS)
    assert report['status'] == 'no_candidate'
    for comparison in report['reference_comparisons'].values():
        assert comparison['discovery']['summary']['median_ratio'] == 10
        assert comparison['reference_direction'] == 'reference_not_faster'
        assert comparison['status'] == 'no_candidate'
    assert len(fake.instances) == 1
