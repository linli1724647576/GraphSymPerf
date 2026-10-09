"""Native same-graph AGE probes. Dry run unless --execute is supplied.

Time-gate signals require manual plan review and are never registered as BUGs.
The property-index symptom has a known upstream report; it is not presumed new.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from statistics import median
import sys
import uuid

WORKSPACE = next(parent for parent in Path(__file__).resolve().parents if (parent / "Src" / "executor.py").is_file())
sys.path.insert(0, str(WORKSPACE / 'Src'))

from executor import (CaseFailure, _measure, _quantile, _result, make_database,
                      measurement_settings, summarize, typed_bag)
from main import read_config

AGE_COMMIT = '806fa2ebdb300b3e76ef30cdba61803babbf2683'
SOURCES = {
    'property_constraint_lowering': f'https://raw.githubusercontent.com/apache/age/{AGE_COMMIT}/src/backend/parser/cypher_clause.c',
    'scalar_sublink_lowering': f'https://raw.githubusercontent.com/apache/age/{AGE_COMMIT}/src/backend/parser/cypher_expr.c',
    'containment_setting': f'https://raw.githubusercontent.com/apache/age/{AGE_COMMIT}/src/backend/utils/ag_guc.c',
    'native_label_id_defaults': f'https://raw.githubusercontent.com/apache/age/{AGE_COMMIT}/src/backend/commands/label_commands.c',
    'known_property_index_report': 'https://github.com/apache/age/issues/2348',
}


def recipe(mechanism, scale, roots):
    if not 1 <= scale <= 2000000 or not 1 <= roots <= 1000:
        raise ValueError('invalid native-probe scale or root count')
    if mechanism == 'property-index':
        queries = {
            'a': 'MATCH (n:ProbeNode) WHERE n.id=1 RETURN n.uid AS uid',
            'b': 'MATCH (n:ProbeNode {id:1}) RETURN n.uid AS uid',
        }
        return {'mechanism': mechanism, 'scale': scale, 'roots': None, 'labels': ['ProbeNode'],
            'indexes': [{'label': 'ProbeNode', 'property': 'id', 'name': 'probe_cost_lookup'}],
            'queries': queries, 'columns': ('uid',), 'expected_rows': [{'uid': 1}],
            'equivalence': 'Both pure predicates select the same non-null integer id=1; the state contains one matching node and no duplicate uid values.',
            'work_obligation': 'Recognize the property equality as an available index lookup; compare WHERE lowering with inline-map containment on the same graph and indexes.',
            'novelty': 'Known similar symptom in upstream issue #2348; a passing run is reproduction evidence, not a presumed new independent discovery.'}
    if mechanism == 'correlated-count':
        queries = {
            'a': ('MATCH (u:ProbeRoot) RETURN u.uid AS uid, '
                  'COUNT { MATCH (x:ProbeLeaf) WHERE x.key=u.key AND x.mark=true } AS value'),
            'b': ('MATCH (u:ProbeRoot) OPTIONAL MATCH (x:ProbeLeaf) '
                  'WHERE x.key=u.key AND x.mark=true '
                  'WITH u,count(x) AS value RETURN u.uid AS uid,value'),
        }
        return {'mechanism': mechanism, 'scale': scale, 'roots': roots,
            'labels': ['ProbeRoot', 'ProbeLeaf'],
            'indexes': [{'label': 'ProbeRoot', 'property': 'uid', 'name': 'probe_cost_lookup'}],
            'queries': queries, 'columns': ('uid', 'value'),
            'expected_rows': [{'uid': i, 'value': 1} for i in range(1, roots + 1)],
            'equivalence': 'COUNT of matched x equals OPTIONAL MATCH plus count(x), which ignores the NULL fallback. WITH groups by node u before projecting uid, preserving distinct roots even if their uid properties coincide. All roots have key=1 and one matching leaf.',
            'work_obligation': 'Avoid re-scanning the same leaf table for each root with the same correlation key; compare scalar SubPlan loops with one filtered leaf scan and a LEFT join/aggregation.',
            'novelty': 'Unverified native scalar-subquery decorrelation probe; do not label a missed optimization without actual work and ordinary-runtime evidence.'}
    raise ValueError(mechanism)


def write(path, record):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temp.replace(path)


def load_state(db, selected, settings):
    sql = db._sql_module
    statements = []

    def run(statement, params=None):
        # Archive complete, credential-free setup SQL as actually submitted.
        if params is not None:
            raise ValueError('native setup statements must have rendered literals')
        text = statement.as_string(db.connection) if hasattr(statement, 'as_string') else statement
        statements.append(text)
        return db._sql(statement)

    run("LOAD 'age'")
    run('SET search_path = ag_catalog, "$user", public')
    run('DEALLOCATE ALL')
    db._prepared.clear()
    run(sql.SQL("SELECT set_config('statement_timeout', {}, false)").format(
        sql.Literal(str(int(settings['timeout_seconds'] * 1000)))))
    existing = db._sql('SELECT name FROM ag_catalog.ag_graph WHERE name=%s', (db.graph,))
    if existing:
        run(sql.SQL('SELECT ag_catalog.drop_graph({},true)').format(sql.Literal(db.graph)))
    run(sql.SQL('SELECT ag_catalog.create_graph({})').format(sql.Literal(db.graph)))
    for label in selected['labels']:
        run(sql.SQL('SELECT ag_catalog.create_vlabel({},{})').format(sql.Literal(db.graph), sql.Literal(label)))

    if selected['mechanism'] == 'property-index':
        run(sql.SQL('INSERT INTO {}.{} (properties) '
                    "SELECT (jsonb_build_object('uid',i,'id',i)::text)::ag_catalog.agtype "
                    'FROM generate_series(1,{}) AS s(i)').format(
            sql.Identifier(db.graph), sql.Identifier('ProbeNode'), sql.Literal(selected['scale'])))
    else:
        run(sql.SQL('INSERT INTO {}.{} (properties) '
                    "SELECT (jsonb_build_object('uid',i,'key',1)::text)::ag_catalog.agtype "
                    'FROM generate_series(1,{}) AS s(i)').format(
            sql.Identifier(db.graph), sql.Identifier('ProbeRoot'), sql.Literal(selected['roots'])))
        run(sql.SQL('INSERT INTO {}.{} (properties) '
                    "SELECT (jsonb_build_object('uid',i,'key',1,'mark',false)::text)::ag_catalog.agtype "
                    'FROM generate_series(1,{}) AS s(i)').format(
            sql.Identifier(db.graph), sql.Identifier('ProbeLeaf'), sql.Literal(selected['scale'])))
        run(sql.SQL('INSERT INTO {}.{} (properties) VALUES '
                    "((jsonb_build_object('uid',{},'key',1,'mark',true)::text)::ag_catalog.agtype)").format(
            sql.Identifier(db.graph), sql.Identifier('ProbeLeaf'), sql.Literal(selected['scale'] + 1)))

    for index in selected['indexes']:
        run(sql.SQL('CREATE INDEX {} ON {}.{} USING gin (properties)').format(
            sql.Identifier(index['name']), sql.Identifier(db.graph), sql.Identifier(index['label'])))
        run(sql.SQL('CREATE INDEX {} ON {}.{} ((ag_catalog.agtype_access_operator('
                    'VARIADIC ARRAY[properties,{}::ag_catalog.agtype])))').format(
            sql.Identifier(index['name'] + '_scalar'), sql.Identifier(db.graph),
            sql.Identifier(index['label']), sql.Literal(json.dumps(index['property']))))
    for label in selected['labels']:
        run(sql.SQL('VACUUM (ANALYZE) {}.{}').format(sql.Identifier(db.graph), sql.Identifier(label)))
    run('DISCARD PLANS')
    db._columns.update({q: selected['columns'] for q in selected['queries'].values()})

    evidence = {'indexes': [], 'counts': {}, 'table_statistics': {}, 'setup_sql': statements,
                'preparation': 'fresh_graph_native_bulk_insert_vacuum_analyze_discard_plans'}
    for index in selected['indexes']:
        for name, kind, token in ((index['name'], 'GIN_property_map', 'USING gin (properties)'),
                                  (index['name'] + '_scalar', 'BTREE_property_expression', 'USING btree')):
            rows = db._sql('SELECT i.indisvalid,i.indisready,t.relname,pg_get_indexdef(i.indexrelid) '
                'FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid '
                'JOIN pg_class t ON t.oid=i.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace '
                'WHERE n.nspname=%s AND x.relname=%s', (db.graph, name))
            if len(rows) != 1 or rows[0][:3] != (True, True, index['label']) or token not in rows[0][3]:
                raise CaseFailure('invalid_state', f'index_not_ready:{name}')
            if kind == 'BTREE_property_expression' and (json.dumps(index['property']) not in rows[0][3]
                                                        or 'agtype_access_operator' not in rows[0][3]):
                raise CaseFailure('invalid_state', f'index_wrong_property:{name}')
            evidence['indexes'].append({**index, 'name': name, 'kind': kind, 'valid': True,
                                         'ready': True, 'definition': rows[0][3]})
    expected_counts = ({'ProbeNode': selected['scale']} if selected['mechanism'] == 'property-index'
                       else {'ProbeRoot': selected['roots'], 'ProbeLeaf': selected['scale'] + 1})
    for label, expected in expected_counts.items():
        rows = db._sql(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(db.graph), sql.Identifier(label)))
        if rows != [(expected,)]:
            raise CaseFailure('invalid_state', f'node_count_differs:{label}')
        evidence['counts'][label] = rows[0][0]
        evidence['table_statistics'][label] = db._sql(
            'SELECT c.reltuples,c.relpages FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
            'WHERE n.nspname=%s AND c.relname=%s', (db.graph, label))
    checks = ({'point_matches': ('MATCH(n:ProbeNode {id:1}) RETURN count(n) AS value', 1)}
              if selected['mechanism'] == 'property-index' else {
                  'same_key_roots': ('MATCH(n:ProbeRoot) WHERE n.key=1 RETURN count(n) AS value', selected['roots']),
                  'noise_leafs': ('MATCH(n:ProbeLeaf) WHERE n.key=1 AND n.mark=false RETURN count(n) AS value', selected['scale']),
                  'witness_leafs': ('MATCH(n:ProbeLeaf) WHERE n.key=1 AND n.mark=true RETURN count(n) AS value', 1)})
    evidence['cypher_checks'] = {}
    for name, (query, expected) in checks.items():
        db._columns[query] = ('value',)
        result, bag = _result(db, query, {}, settings)
        if bag != typed_bag([{'value': expected}]):
            raise CaseFailure('invalid_state', f'cypher_count_differs:{name}')
        evidence['cypher_checks'][name] = {'query': query, 'expected': expected, 'actual': result}
    evidence['containment_setting'] = db._sql('SHOW age.enable_containment')
    return evidence


def validate_pair(db, selected, settings):
    baseline = typed_bag(selected['expected_rows'])
    logical = {}
    for side, query in selected['queries'].items():
        result, bag = _result(db, query, {}, settings)
        if bag != baseline:
            raise CaseFailure('result_mismatch', f'query_{side}_differs_from_expected',
                              details={'actual': result['rows'], 'expected': selected['expected_rows']})
        logical[side] = result['rows']
    return baseline, logical


def diagnostics(db, selected, record):
    record['native_diagnostics'] = {}
    for side, query in selected['queries'].items():
        item = record['native_diagnostics'][side] = {'query': query}
        for kind, analyze in (('explain', False), ('profile', True)):
            try:
                item[kind] = db.explain(query, {}, analyze=analyze)
            except Exception as exc:
                item[kind] = {'unavailable': type(exc).__name__, 'reason': str(exc),
                              'details': getattr(exc, 'details', {})}
                if isinstance(exc, CaseFailure) and exc.stop_target:
                    raise


def probe(config, mechanism, scale, roots, seed, directory, screen_only=False):
    selected = recipe(mechanism, scale, roots)
    settings = measurement_settings({**config['measurement'], 'discovery_warmups': 3,
        'discovery_pairs': 6, 'confirmation_sessions': 2, 'confirmation_warmups': 5,
        'confirmation_pairs': 20, 'control_pairs': 10, 'ratio': 5.0, 'ci_low': 5.0,
        'min_delta_ms': 100.0, 'bootstrap_samples': 10000})
    case = {'query_a': selected['queries']['a'], 'query_b': selected['queries']['b'],
            'parameters': {}, 'expected_rows': selected['expected_rows'], 'seed': seed}
    record = {'dbms': 'age', 'language': 'cypher', 'mechanism': mechanism, 'status': 'started',
        'attribution': 'native_optimizer_rule_probe', 'pattern_id': None, 'recipe': selected,
        'settings': settings, 'sources': SOURCES, 'confirmation_sessions': []}
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / 'probe.json'
    for side, query in selected['queries'].items():
        (directory / f'query_{side}.cypher').write_text(query + ';\n')
    write(artifact, record)
    db = None
    try:
        db = make_database(config['database'], settings)
        record['database_version'] = db.version
        record['state_evidence'] = load_state(db, selected, settings)
        (directory / 'setup.sql').write_text(';\n\n'.join(record['state_evidence']['setup_sql']) + ';\n')
        baseline, logical = validate_pair(db, selected, settings)
        discovery = record['discovery'] = {'warmups': [], 'samples': [], 'logical_rows': logical}
        rng = random.Random(seed)
        _measure(db, case, baseline, settings, rng, 3, discovery['warmups'])
        _measure(db, case, baseline, settings, rng, 6, discovery['samples'])
        slow = 'a' if median(s['a_ms'] for s in discovery['samples']) >= median(s['b_ms'] for s in discovery['samples']) else 'b'
        discovery['summary'] = summarize(discovery['samples'], slow)
        strong = (discovery['summary']['median_ratio'] >= 5 and discovery['summary']['median_delta_ms'] > 100
                  and discovery['summary']['direction_consistency'] >= settings['direction_consistency'])
        diagnostics(db, selected, discovery)
        write(artifact, record)
        db.close()
        db = None
        if not strong or screen_only:
            record['status'] = 'screen_signal_unconfirmed' if strong else 'no_screen_signal'
            return record
        pooled, noise = [], []
        for number in range(2):
            db = make_database(config['database'], settings)
            session = {'number': number + 1, 'state_evidence': load_state(db, selected, settings),
                       'warmups': [], 'samples': [], 'control_a': [], 'control_b': []}
            record['confirmation_sessions'].append(session)
            fresh, session['logical_rows'] = validate_pair(db, selected, settings)
            if fresh != baseline:
                raise CaseFailure('result_mismatch', 'result_changed_after_rebuild')
            rng = random.Random(seed + 104729 * (number + 1))
            _measure(db, case, baseline, settings, rng, 5, session['warmups'])
            _measure(db, case, baseline, settings, rng, 20, session['samples'])
            for side in ('a', 'b'):
                query = case[f'query_{side}']
                _measure(db, case, baseline, settings, rng, 10, session[f'control_{side}'], queries=(query, query))
            session['summary'] = summarize(session['samples'], slow, bootstrap_samples=10000, seed=seed + number + 1)
            session['noise_p99_ms'] = max(_quantile([abs(s['a_ms'] - s['b_ms'])
                for s in session[f'control_{side}']], 0.99) for side in ('a', 'b'))
            pooled.extend(session['samples'])
            noise.append(session['noise_p99_ms'])
            diagnostics(db, selected, session)
            write(artifact, record)
            db.close()
            db = None
        summary = record['summary'] = summarize(pooled, slow, bootstrap_samples=10000, seed=seed)
        summary['noise_p99_ms'] = max(noise)
        stable = all(s['summary']['median_ratio'] >= 5 and s['summary']['ratio_ci_low'] >= 5 and
            s['summary']['median_delta_ms'] > 100 and s['summary']['median_delta_ms'] > s['noise_p99_ms'] and
            s['summary']['direction_consistency'] >= settings['direction_consistency']
            for s in record['confirmation_sessions'])
        passed = (stable and summary['median_ratio'] >= 5 and summary['ratio_ci_low'] >= 5 and
            summary['median_delta_ms'] > 100 and summary['median_delta_ms'] > summary['noise_p99_ms'])
        record['status'] = 'native_probe_signal_requires_plan_review' if passed else 'native_probe_not_confirmed'
        record['passed_time_gate'] = passed
        return record
    except Exception as exc:
        record.update(status='native_probe_failed', failure=type(exc).__name__, reason=str(exc),
                      details=getattr(exc, 'details', {}))
        return record
    finally:
        if db is not None:
            db.close()
        write(artifact, record)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--scale', type=int, default=100000)
    parser.add_argument('--roots', type=int, default=16)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--mechanism', choices=('all', 'property-index', 'correlated-count'), default='all')
    parser.add_argument('--screen-only', action='store_true')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    config = read_config(args.config)
    if config['database']['dbms'] != 'age' or config['database']['language'] != 'cypher':
        raise ValueError('requires an explicit dedicated Apache AGE Cypher configuration')
    mechanisms = ['property-index', 'correlated-count'] if args.mechanism == 'all' else [args.mechanism]
    if not args.execute:
        print(json.dumps({'dry_run': True, 'dbms': 'age', 'recipes':
            {m: recipe(m, args.scale, args.roots) for m in mechanisms}}, indent=2))
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    destination = WORKSPACE / 'results/detection-20261009/age' / ('native-' + stamp)
    for mechanism in mechanisms:
        record = probe(config, mechanism, args.scale, args.roots, args.seed, destination / mechanism, args.screen_only)
        print(json.dumps({'mechanism': mechanism, 'scale': args.scale, 'roots': args.roots,
            'status': record['status'], 'summary': record.get('summary') or record.get('discovery', {}).get('summary'),
            'directory': str(destination / mechanism)}, ensure_ascii=False), flush=True)
        if record['status'] == 'native_probe_failed':
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
