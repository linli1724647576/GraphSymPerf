import json
from pathlib import Path
import random

import pytest
import yaml

import main
from generator import instantiate, load_seeds
from mutator import mutate
from query import render_query


FIXTURE = Path(__file__).parent / 'fixtures/gdsmith/neo4j_seeds.log'


def make_case():
    source = load_seeds({'log_files': [str(FIXTURE)]})[0]
    case = instantiate(source, 'P1', 2, 0)
    case['query_b'] = render_query(mutate(case['query_a'], 'P1'))
    case['query_a'] = render_query(case['query_a'])
    return case


def test_save_and_read_are_lossless_and_detect_changed_input(tmp_path):
    case = make_case()
    settings = main.measurement_settings({})
    main.save_case(tmp_path, case, settings, {'status': 'no_candidate'})
    assert {p.name for p in tmp_path.iterdir()} == {'setup.cypher', 'query_a.cypher', 'query_b.cypher', 'result.json'}
    restored, protocol = main.read_case(tmp_path)
    assert restored == case and protocol == settings
    (tmp_path / 'setup.cypher').write_text('CREATE (:Different);\n')
    with pytest.raises(ValueError, match='changed'):
        main.read_case(tmp_path)


def test_run_and_replay_use_same_inputs_without_calling_generator_again(tmp_path, monkeypatch):
    config = {'database': {}, 'generation': {'log_files': [str(FIXTURE)]},
              'patterns': ['P1', 'P4', 'P5'], 'scales': [2], 'seeds': [0],
              'max_cases': 3, 'max_seconds': 60, 'output': str(tmp_path), 'measurement': main.measurement_settings({})}
    cases = []
    def check(case, database, measurement, **kwargs):
        cases.append(case)
        return {'status': 'no_candidate'}
    monkeypatch.setattr(main, 'run_case', check)
    assert main.run(config) == 0
    expected = ['P1', 'P4', 'P5']
    random.Random(0).shuffle(expected)
    assert [case['pattern'] for case in cases] == expected
    results = list(tmp_path.rglob('result.json'))
    assert len(results) == 3
    monkeypatch.setattr(main, 'load_seeds', lambda _: pytest.fail('replay invoked the generator'))
    assert main.replay(results[0].parent, config) == 0
    assert len(list(tmp_path.rglob('result.json'))) == 4


def test_configuration_never_enables_reset_implicitly(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'database': {'dedicated': True, 'allow_reset': False}}))
    with pytest.raises(ValueError, match='allow_reset'):
        main.read_config(path)


def test_environment_failure_stops_later_cases(tmp_path, monkeypatch):
    config = {'database': {}, 'generation': {'log_files': [str(FIXTURE)]},
              'patterns': ['P1', 'P4', 'P5'], 'scales': [2], 'seeds': [0],
              'max_cases': 3, 'max_seconds': 60, 'output': str(tmp_path), 'measurement': {}}
    monkeypatch.setattr(main, 'run_case', lambda *args, **kwargs: {'status': 'timeout', 'stop_target': True})
    assert main.run(config) == 1
    assert len(list(tmp_path.rglob('result.json'))) == 1


def test_default_budget_reaches_every_pattern_before_repeating_sources(tmp_path, monkeypatch):
    config = {'database': {}, 'generation': {'log_files': [str(FIXTURE)]},
              'patterns': list(main.PATTERNS), 'scales': [2, 5], 'seeds': [0],
              'max_cases': 2 * len(main.PATTERNS), 'max_seconds': 60, 'output': str(tmp_path), 'measurement': {}}
    executed = []
    def check(case, database, measurement, **kwargs):
        executed.append((case['pattern'], case['scale']))
        return {'status': 'no_candidate'}
    monkeypatch.setattr(main, 'run_case', check)
    assert main.run(config) == 0
    order = list(main.PATTERNS)
    random.Random(0).shuffle(order)
    assert executed == [(pattern, scale) for pattern in order for scale in (2, 5)]
    for path in tmp_path.rglob('result.json'):
        # Includes UNION/CASE/quantifier cases, not only the former single MATCH.
        restored, _ = main.read_case(path.parent)
        assert restored['pattern'] in main.PATTERNS


def test_builtin_templates_do_not_require_gdsmith(tmp_path, monkeypatch):
    config = {'database': {}, 'generation': {}, 'patterns': ['P4'], 'scales': [2],
              'seeds': [0], 'cases_per_pattern': 1, 'seconds_per_pattern': 10,
              'output': str(tmp_path), 'measurement': {}}
    monkeypatch.setattr(main, 'load_seeds', lambda _: pytest.fail('built-in mode called GDsmith'))
    monkeypatch.setattr(main, 'run_case', lambda *args, **kwargs: {'status': 'no_candidate'})
    assert main.run(config) == 0
    record = json.loads(next(tmp_path.rglob('result.json')).read_text())
    assert record['case']['source']['generator'] != 'gdsmith'


def test_slow_pattern_cannot_spend_other_patterns_reserved_time(tmp_path, monkeypatch):
    config = {'database': {}, 'generation': {}, 'patterns': ['P1', 'P4', 'P5'],
              'scales': [2], 'seeds': [0, 1, 2], 'cases_per_pattern': 3,
              'seconds_per_pattern': 1, 'output': str(tmp_path), 'measurement': {}}
    now, executed = [0.0], []
    monkeypatch.setattr(main.time, 'monotonic', lambda: now[0])
    def check(case, database, measurement, **kwargs):
        now[0] += 5.0
        executed.append(case['pattern'])
        return {'status': 'no_candidate'}
    monkeypatch.setattr(main, 'run_case', check)
    assert main.run(config) == 0
    assert set(executed) == set(config['patterns']) and len(executed) == 3
    record = json.loads(next(tmp_path.rglob('run.json')).read_text())
    assert all(p['executed'] == 1 and p['stop_reason'] == 'time_budget'
               for p in record['per_pattern'].values())


def test_reference_query_is_saved_replayed_and_protected_by_hash(tmp_path):
    case = make_case()
    case['query_ref'] = case['query_a']
    main.save_case(tmp_path, case, {}, {'status': 'no_candidate'})
    restored, _ = main.read_case(tmp_path)
    assert restored == case
    (tmp_path / 'query_ref.cypher').write_text('RETURN 123 AS value;\n')
    with pytest.raises(ValueError, match='changed'):
        main.read_case(tmp_path)


def test_old_format_one_inputs_still_replay(tmp_path):
    case = make_case()
    main.save_case(tmp_path, case, {}, {'status': 'no_candidate'})
    path = tmp_path / 'result.json'
    record = json.loads(path.read_text())
    record['format_version'] = 1
    record['input_hash'] = main.case_digest(case, 1)
    path.write_text(json.dumps(record))
    assert main.read_case(tmp_path)[0] == case


def test_old_format_replay_keeps_case_identity(tmp_path, monkeypatch):
    case = make_case()
    original = tmp_path / 'original'
    main.save_case(original, case, {}, {'status': 'no_candidate'}, format_version=1)
    monkeypatch.setattr(main, 'run_case', lambda *args, **kwargs: {'status': 'no_candidate', 'database_version': 'Neo4j/test'})
    config = {'database': {}, 'output': str(tmp_path / 'replayed')}
    assert main.replay(original, config) == 0
    before = json.loads((original / 'result.json').read_text())
    after = json.loads(next((tmp_path / 'replayed').rglob('result.json')).read_text())
    assert before['input_hash'] == after['input_hash']


def test_unsupported_pattern_is_visible_in_run_manifest(tmp_path, monkeypatch):
    config = {'database': {}, 'generation': {}, 'patterns': ['P1', 'S3'], 'scales': [2],
              'seeds': [0], 'cases_per_pattern': 1, 'seconds_per_pattern': 10,
              'output': str(tmp_path), 'measurement': {}}
    monkeypatch.setattr(main, 'supported_patterns', lambda _: {'P1'})
    monkeypatch.setattr(main, 'run_case', lambda *args, **kwargs: {'status': 'no_candidate'})
    assert main.run(config) == 0
    record = json.loads(next(tmp_path.rglob('run.json')).read_text())
    assert record['per_pattern']['S3']['stop_reason'] == 'unsupported_feature'
    assert record['per_pattern']['S3']['executed'] == 0


def test_small_legacy_budget_requires_explicit_pattern_subset():
    with pytest.raises(ValueError, match='fewer patterns'):
        main.pattern_budgets({'patterns': ['P1', 'P2'], 'max_cases': 1})
