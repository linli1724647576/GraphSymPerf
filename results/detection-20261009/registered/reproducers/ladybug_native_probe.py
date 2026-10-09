"""Native same-graph probes; dry run unless --execute is supplied.

No issue is registered automatically. A time signal still requires review of
the complete results and native execution plans. This script stays outside Src.
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


def recipe(mechanism, scale):
    if not 1 <= scale < 900000:
        raise ValueError('scale must be in 1..899999; larger values need a separate resource review')
    keep = mechanism == 'coalesce-count'
    setup = [
        'CREATE NODE TABLE ProbeRoot(uid INT64 PRIMARY KEY, keep BOOLEAN)',
        'CREATE NODE TABLE ProbeMid(uid INT64 PRIMARY KEY, keep BOOLEAN)',
        'CREATE NODE TABLE ProbeHub(uid INT64 PRIMARY KEY, keep BOOLEAN)',
        'CREATE NODE TABLE ProbeLeaf(uid INT64 PRIMARY KEY, keep BOOLEAN)',
        'CREATE REL TABLE ProbeA(FROM ProbeRoot TO ProbeMid)',
        'CREATE REL TABLE ProbeB(FROM ProbeMid TO ProbeHub)',
        'CREATE REL TABLE ProbeC(FROM ProbeHub TO ProbeLeaf)',
        'CREATE (:ProbeRoot {uid:1,keep:true}), '
        f'(:ProbeHub {{uid:1,keep:{str(keep).lower()}}})',
        'MATCH (u:ProbeRoot {uid:1}), (h:ProbeHub {uid:1}) '
        f'UNWIND range(1,{scale}) AS i '
        'CREATE (u)-[:ProbeA]->(m:ProbeMid {uid:i,keep:true})-[:ProbeB]->(h)',
        'MATCH (h:ProbeHub {uid:1}) '
        f'UNWIND range(1,{scale}) AS i '
        'CREATE (h)-[:ProbeC]->(:ProbeLeaf {uid:1000000+i,keep:true})',
    ]
    counts = {
        'root_nodes': ('MATCH (n:ProbeRoot) RETURN count(n) AS value', 1),
        'mid_nodes': ('MATCH (n:ProbeMid) RETURN count(n) AS value', scale),
        'hub_nodes': ('MATCH (n:ProbeHub) RETURN count(n) AS value', 1),
        'leaf_nodes': ('MATCH (n:ProbeLeaf) RETURN count(n) AS value', scale),
        'A_edges': ('MATCH (:ProbeRoot)-[r:ProbeA]->(:ProbeMid) RETURN count(r) AS value', scale),
        'B_edges': ('MATCH (:ProbeMid)-[r:ProbeB]->(:ProbeHub) RETURN count(r) AS value', scale),
        'C_edges': ('MATCH (:ProbeHub)-[r:ProbeC]->(:ProbeLeaf) RETURN count(r) AS value', scale),
        'root_true': ('MATCH (n:ProbeRoot) WHERE n.keep=true RETURN count(n) AS value', 1),
        'mid_true': ('MATCH (n:ProbeMid) WHERE n.keep=true RETURN count(n) AS value', scale),
        'leaf_true': ('MATCH (n:ProbeLeaf) WHERE n.keep=true RETURN count(n) AS value', scale),
        'hub_true': ('MATCH (n:ProbeHub) WHERE n.keep=true RETURN count(n) AS value', int(keep)),
    }
    count = ('COUNT { MATCH (u)-[:ProbeA]->(m:ProbeMid)-[:ProbeB]->'
             '(h:ProbeHub)-[:ProbeC]->(l:ProbeLeaf) WHERE m.uid <> l.uid }')
    if mechanism == 'coalesce-count':
        queries = {
            'a': f'MATCH (u:ProbeRoot {{uid:1}}) RETURN coalesce(0, {count}) AS value',
            'b': 'MATCH (u:ProbeRoot {uid:1}) RETURN 0 AS value',
        }
        expected = [{'value': 0}]
        controls = {
            'visible_count': {'query': f'MATCH (u:ProbeRoot {{uid:1}}) RETURN {count} AS value',
                              'expected_rows': [{'value': scale * scale}]},
        }
        equivalence = ('The non-NULL integer zero is the first COALESCE argument, so the '
                       'COUNT cannot affect the output on any graph. Both queries retain '
                       'the same outer MATCH. The COUNT predicate compares two INT64 properties.')
        obligation = 'The plan should not execute the COUNT subplan discarded by COALESCE.'
    elif mechanism == 'path-pruning':
        prefix = ('MATCH p=(u:ProbeRoot {uid:1})-'
                  '[:ProbeA|ProbeB|ProbeC* TRAIL 3..3]->(v:ProbeLeaf)')
        suffix = 'RETURN u.uid AS root_uid, v.uid AS leaf_uid, length(p) AS hops'
        queries = {
            'a': prefix + ' WHERE all(n IN nodes(p) WHERE n.keep=true) ' + suffix,
            'b': ('MATCH (u:ProbeRoot {uid:1})-[:ProbeA]->(m:ProbeMid) '
                  'WHERE u.keep=true AND m.keep=true '
                  'MATCH (m)-[:ProbeB]->(h:ProbeHub) WHERE h.keep=true '
                  'MATCH (h)-[:ProbeC]->(v:ProbeLeaf) WHERE v.keep=true '
                  'RETURN u.uid AS root_uid, v.uid AS leaf_uid, 3 AS hops'),
        }
        inline = ('MATCH p=(u:ProbeRoot {uid:1})-'
                  '[:ProbeA|ProbeB|ProbeC* TRAIL 3..3 (e,n | WHERE n.keep=true)]->'
                  '(v:ProbeLeaf) WHERE u.keep=true ' + suffix)
        expected = []
        controls = {'recursive_inline_filter': {'query': inline, 'expected_rows': []}}
        equivalence = ('Schema permits only Root-A-Mid-B-Hub-C-Leaf, a three-hop DAG. '
                       'TRAIL cannot remove any of these paths because no relationship can '
                       'repeat. ALL over every path node is the conjunction of the four '
                       'node keep conditions, including Root. Hub.keep=false rejects each path.')
        obligation = ('Stop recursion at the false Hub, rather than construct all scale^2 '
                      'paths and filter afterward. The inline-node-filter control uses the '
                      'same three-hop TRAIL operator to distinguish pruning from fixed-vs-recursive cost.')
    else:
        raise ValueError(mechanism)
    return {'setup': setup, 'counts': counts, 'queries': queries, 'controls': controls,
            'expected_rows': expected, 'equivalence': equivalence, 'work_obligation': obligation,
            'scale': scale, 'nodes': 2 * scale + 2, 'edges': 3 * scale,
            'three_hop_paths': scale * scale, 'hub_keep': keep}


def write(path, record):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temp.replace(path)


def load_state(db, selected, settings):
    evidence = {'counts': {}, 'primary_keys': {}}
    for query in selected['setup']:
        db.execute(query, {}, settings['timeout_seconds'])
    for name, (query, count) in selected['counts'].items():
        result, bag = _result(db, query, {}, settings)
        if bag != typed_bag([{'value': count}]):
            raise CaseFailure('invalid_state', f'count_check_failed:{name}', details={'actual': result})
        evidence['counts'][name] = {'query': query, 'expected': count, 'actual': result}
    for table in ('ProbeRoot', 'ProbeMid', 'ProbeHub', 'ProbeLeaf'):
        result = db.execute(f"CALL table_info('{table}') RETURN *", {}, settings['timeout_seconds'])
        if not result['complete'] or not any(row.get('name') == 'uid' and row.get('primary key') is True
                                             for row in result['rows']):
            raise CaseFailure('invalid_state', f'primary_key_missing:{table}')
        evidence['primary_keys'][table] = result
    return evidence


def validate_pair(db, selected, settings):
    baseline = typed_bag(selected['expected_rows'])
    logical = {}
    for side, query in selected['queries'].items():
        result, bag = _result(db, query, {}, settings)
        logical[side] = result['rows']
        if bag != baseline:
            raise CaseFailure('result_mismatch', f'query_{side}_differs_from_constructed_result',
                              details={'actual': result['rows'], 'expected': selected['expected_rows']})
    return baseline, logical


def path_semantic_controls(config, settings):
    """Check path bags on a positive diamond and a NULL guard before empty timing."""
    controls = []
    for hub_keep in (True, None):
        selected = recipe('path-pruning', 2)
        literal = 'true' if hub_keep is True else 'null'
        selected['setup'][7] = ('CREATE (:ProbeRoot {uid:1,keep:true}), '
                               f'(:ProbeHub {{uid:1,keep:{literal}}})')
        selected['counts']['hub_true'] = (selected['counts']['hub_true'][0], int(hub_keep is True))
        selected['expected_rows'] = ([{'root_uid': 1, 'leaf_uid': 1000000 + leaf, 'hops': 3}
                                     for _ in range(2) for leaf in (1, 2)] if hub_keep is True else [])
        selected['controls']['recursive_inline_filter']['expected_rows'] = selected['expected_rows']
        db = make_database(config['database'], settings)
        try:
            evidence = load_state(db, selected, settings)
            baseline, logical = validate_pair(db, selected, settings)
            query = selected['controls']['recursive_inline_filter']['query']
            inline, inline_bag = _result(db, query, {}, settings)
            if inline_bag != baseline:
                raise CaseFailure('result_mismatch', 'inline_path_control_changes_positive_or_null_result')
            controls.append({'hub_keep': hub_keep, 'scale': 2, 'expected_rows': selected['expected_rows'],
                             'logical_rows': logical, 'inline_result': inline, 'state_evidence': evidence})
        finally:
            db.close()
    return controls


def diagnostics(db, selected, settings, record):
    record['native_diagnostics'] = {}
    for side, query in {**selected['queries'], **{name: c['query'] for name, c in selected['controls'].items()}}.items():
        item = record['native_diagnostics'][side] = {'query': query}
        try:
            if side in selected['controls']:
                expected = typed_bag(selected['controls'][side]['expected_rows'])
                item['executions'] = []
                for _ in range(3):
                    result, bag = _result(db, query, {}, settings)
                    if bag != expected:
                        raise CaseFailure('result_mismatch', f'control_{side}_differs_from_expected')
                    item['executions'].append(result)
            item['explain'] = db.explain(query, {}, analyze=False)
            item['profile'] = db.explain(query, {}, analyze=True)
        except Exception as exc:
            item['status'] = 'diagnostic_failed'
            item['failure'] = type(exc).__name__
            item['reason'] = str(exc)
            item['details'] = getattr(exc, 'details', {})
            if isinstance(exc, CaseFailure) and exc.status == 'result_mismatch':
                raise
            if isinstance(exc, CaseFailure) and exc.stop_target:
                raise


def probe(config, mechanism, scale, seed, directory, screen_only=False):
    selected = recipe(mechanism, scale)
    settings = measurement_settings({**config['measurement'], 'discovery_warmups': 3,
        'discovery_pairs': 6, 'confirmation_sessions': 2, 'confirmation_warmups': 5,
        'confirmation_pairs': 20, 'control_pairs': 10, 'ratio': 5.0, 'ci_low': 5.0,
        'min_delta_ms': 100.0, 'bootstrap_samples': 10000})
    case = {'query_a': selected['queries']['a'], 'query_b': selected['queries']['b'],
            'parameters': {}, 'expected_rows': selected['expected_rows'], 'seed': seed}
    record = {'dbms': 'ladybug', 'language': 'cypher', 'mechanism': mechanism,
        'status': 'started', 'attribution': 'native_optimizer_rule_probe', 'pattern_id': None,
        'recipe': selected, 'settings': settings, 'confirmation_sessions': [],
        'source_artifact': '/private/tmp/gsp-ladybug-probe/ladybug-0.21.2-official.tar.gz',
        'source_files': ['src/planner/plan/plan_subquery.cpp',
            'src/expression_evaluator/function_evaluator.cpp', 'src/function/utility/coalesce.cpp',
            'src/binder/bind/bind_graph_pattern.cpp', 'src/optimizer/filter_push_down_optimizer.cpp',
            'src/planner/plan/append_extend.cpp', 'src/antlr4/Cypher.g4']}
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / 'probe.json'
    (directory / 'setup.cypher').write_text(';\n\n'.join(selected['setup']) + ';\n')
    for side, query in selected['queries'].items():
        (directory / f'query_{side}.cypher').write_text(query + ';\n')
    write(artifact, record)
    db = None
    try:
        if mechanism == 'path-pruning':
            record['semantic_controls'] = path_semantic_controls(config, settings)
        db = make_database(config['database'], settings)
        record['database_version'] = db.version
        record['state_evidence'] = load_state(db, selected, settings)
        baseline, logical = validate_pair(db, selected, settings)
        discovery = record['discovery'] = {'warmups': [], 'samples': [], 'logical_rows': logical}
        rng = random.Random(seed)
        _measure(db, case, baseline, settings, rng, 3, discovery['warmups'])
        _measure(db, case, baseline, settings, rng, 6, discovery['samples'])
        slow = 'a' if median(s['a_ms'] for s in discovery['samples']) >= median(s['b_ms'] for s in discovery['samples']) else 'b'
        discovery['summary'] = summarize(discovery['samples'], slow)
        strong = (discovery['summary']['median_ratio'] >= 5 and
                  discovery['summary']['median_delta_ms'] > 100 and
                  discovery['summary']['direction_consistency'] >= settings['direction_consistency'])
        diagnostics(db, selected, settings, discovery)
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
            diagnostics(db, selected, settings, session)
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
    parser.add_argument('--scale', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--mechanism', choices=('all', 'coalesce-count', 'path-pruning'), default='all')
    parser.add_argument('--screen-only', action='store_true')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    config = read_config(args.config)
    if config['database']['dbms'] != 'ladybug' or config['database']['language'] != 'cypher':
        raise ValueError('requires an explicit dedicated LadybugDB Cypher configuration')
    mechanisms = ['coalesce-count', 'path-pruning'] if args.mechanism == 'all' else [args.mechanism]
    if not args.execute:
        print(json.dumps({'dry_run': True, 'dbms': 'ladybug', 'recipes':
            {m: recipe(m, args.scale) for m in mechanisms}}, indent=2))
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    destination = WORKSPACE / 'results/detection-20261009/ladybug' / ('native-' + stamp)
    for mechanism in mechanisms:
        record = probe(config, mechanism, args.scale, args.seed, destination / mechanism, args.screen_only)
        print(json.dumps({'mechanism': mechanism, 'scale': args.scale, 'status': record['status'],
            'summary': record.get('summary') or record.get('discovery', {}).get('summary'),
            'directory': str(destination / mechanism)}, ensure_ascii=False), flush=True)
        if record['status'] == 'native_probe_failed':
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
