import json, os, sys, time
from pathlib import Path
import yaml
sys.path.insert(0, str(Path.cwd() / 'Src'))
from main import read_case
from executor import ArcadeDB, measurement_settings, _bind_outputs, _result, check_case, typed_bag
root = Path.cwd()
source_root = root / 'results/detection-20261009/arcadedb/discovery'
sources = {'S7': source_root / '20261009T134200Z-afe43e/0001-S7-n100000-11972218',
           'P4': source_root / '20261009T133356Z-770cac/0001-P4-n100000-17effe43'}
output = root / 'results/detection-20261009/arcadedb/native-work.json'
secrets = json.loads(Path('/private/tmp/gsp-detection-20261009/credentials.json').read_text())
os.environ['GRAPHSYMPERF_ARCADE_PASSWORD'] = secrets['GRAPHSYMPERF_ARCADE_PASSWORD']
config = yaml.safe_load(Path('/private/tmp/gsp-detection-20261009/arcade-coalesce.yaml').read_text())
settings = measurement_settings(config['measurement'])
commit = 'ebdf527641ba444364bb9e29aa020e9e04824862'
base = 'https://github.com/ArcadeData/arcadedb/blob/' + commit + '/'
evidence = {'experiment_date': '2026-10-09', 'commit': commit,
 'measurement_scope': 'Separate native-profiler windows, each containing only one complete original query execution; not formal runtime confirmation samples.',
 'counter_meanings': {'readRecord': 'DatabaseStats lookup counter incremented by LocalDatabase.lookupByRID and lookupByKey, including cached record lookups; not edge visits or Neo4j DB Hits.',
 'iterateType': 'Number of database type-iteration operations, not number of yielded records.',
 'scanType': 'Number of database type-scan operations, not number of yielded records.'},
 'official_sources': [base + 'server/src/main/java/com/arcadedb/server/monitor/ServerQueryProfiler.java#L100-L127',
 base + 'server/src/main/java/com/arcadedb/server/http/handler/PostServerCommandHandler.java#L1196-L1222',
 base + 'engine/src/main/java/com/arcadedb/database/LocalDatabase.java#L967-L981',
 base + 'engine/src/main/java/com/arcadedb/query/opencypher/executor/ExpressionEvaluator.java#L129-L155',
 base + 'engine/src/main/java/com/arcadedb/query/opencypher/executor/CypherExecutionPlan.java#L524-L538'],
 'cases': []}
def save():
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
def native(result):
    return {key: value for key, value in result.items() if key != '_client_elapsed_ms'}
db = None
active = False
try:
    db = ArcadeDB(config['database'], settings)
    evidence['database_version'] = db.version
    if commit not in db.version:
        raise RuntimeError('native_version_does_not_match_fixed_source')
    # Ensure no previous profiler recording can be mistaken for our first window.
    evidence['initial_profiler_stop'] = native(db._request('/api/v1/server', {'command': 'profiler stop'}, settings['timeout_seconds']))
    for pattern, directory in sources.items():
        case, _ = read_case(directory)
        record = json.loads((directory / 'result.json').read_text())
        _bind_outputs(db, case)
        entry = {'pattern': pattern, 'source_case': str(directory.relative_to(root)),
          'source_input_hash': record['input_hash'], 'exact_queries': {side: (directory / f'query_{side}.cypher').read_text() for side in ['a','b']},
          'execution_queries': {side: case[f'query_{side}'] for side in ['a','b']},
          'parameters': case.get('parameters', {}), 'expected_rows': case['expected_rows'],
          'expected_typed_bag': list(typed_bag(case['expected_rows']).items()),
          'index_requirements': case.get('index_requirements'), 'graph_spec': case.get('graph_spec'),
          'windows': []}
        evidence['cases'].append(entry)
        save()
        if pattern == 'P4':
            db.restore(case)
            baseline, row_count = check_case(db, case, settings)
            entry['preparation'] = {'kind': 'fresh_native_restore_and_current_state_checks',
                 'state_evidence': db._state_evidence, 'result_rows': row_count,
                 'baseline_typed_bag': list(baseline.items())}
        else:
            entry['preparation'] = {'kind': 'existing_state_from_completed_formal_S7_run',
                'prior_state_evidence': record['result'].get('state_evidence'),
                'prior_result_rows': record['result'].get('result_rows')}
        for iteration in [1,2]:
            for side in ['a','b']:
                window = {'iteration': iteration, 'side': side, 'query': case[f'query_{side}']}
                entry['windows'].append(window)
                window['profiler_start'] = native(db._request('/api/v1/server', {'command': 'profiler start'}, settings['timeout_seconds']))
                active = True
                save()
                try:
                    result, bag = _result(db, case[f'query_{side}'], case.get('parameters',{}), settings)
                    window['complete_result'] = result
                    window['typed_bag'] = list(bag.items())
                    window['equivalent_to_constructed_expected'] = bag == typed_bag(case['expected_rows'])
                    if not window['equivalent_to_constructed_expected']:
                        raise RuntimeError('native_profiler_query_result_mismatch')
                finally:
                    # No query is repeated after failure. stop is a control request.
                    try:
                        window['native_profiler'] = native(db._request('/api/v1/server', {'command': 'profiler stop'}, settings['timeout_seconds']))
                        active = False
                    except Exception as exc:
                        window['profiler_stop_error'] = type(exc).__name__
                        save()
                        raise
                profile = window['native_profiler']
                snapshots = profile['summary']
                before = snapshots['snapshotStart']['databases'][db.database]
                after = snapshots['snapshotStop']['databases'][db.database]
                window['database_counters_before'] = before
                window['database_counters_after'] = after
                window['database_counter_deltas'] = {key: after[key] - before[key] for key in before if type(before[key]) is int and type(after.get(key)) is int}
                if profile.get('totalQueries') != 1:
                    raise RuntimeError('profiler_window_did_not_contain_exactly_one_query')
                save()
                print(pattern, iteration, side, {k: window['database_counter_deltas'].get(k) for k in ['readRecord','iterateType','scanType']}, flush=True)
        entry['typed_equivalence_validated'] = all(w['equivalent_to_constructed_expected'] for w in entry['windows'])
        save()
    evidence['status'] = 'complete'
except Exception as exc:
    evidence['status'] = 'failed'
    evidence['error_type'] = type(exc).__name__
    print('failed', type(exc).__name__, str(exc)[:250], flush=True)
    raise
finally:
    if db is not None:
        if active:
            try:
                evidence['cleanup_profiler_stop'] = native(db._request('/api/v1/server', {'command': 'profiler stop'}, settings['timeout_seconds']))
            except Exception as exc:
                evidence['cleanup_profiler_stop_error'] = type(exc).__name__
        db.close()
    save()
