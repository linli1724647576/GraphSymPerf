"""Read finished local issue letters; verify and summarize their actual pairs.

No database access. Native probes remain distinct from built-in Pattern runs.
The inventory recomputes a 95% paired-ratio bootstrap CI with a declared seed
of zero; native artifacts do not consistently record their original CI seed.
"""

import csv
import io
import json
import math
from pathlib import Path
import random
import re
from statistics import median

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "Src" / "executor.py").is_file())
OUTPUT = ROOT / 'results/detection-20261009'
TARGETS = {'Memgraph': 'memgraph', 'FalkorDB': 'falkordb', 'ArcadeDB': 'arcadedb',
           'Apache AGE': 'age', 'LadybugDB': 'ladybug', 'JanusGraph': 'janusgraph',
           'HugeGraph': 'hugegraph', 'Apache HugeGraph': 'hugegraph'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def normalized(text):
    # Remove formatting whitespace only outside strings/quoted identifiers.
    tokens = re.findall(r"'(?:\\.|''|[^'])*'|\"(?:\\.|\"\"|[^\"])*\"|`(?:``|[^`])*`|\s+|.",
                        text, re.S)
    return ''.join(token for token in tokens if not token.isspace()).rstrip(';')


def report_queries(text):
    found = {}
    for _, block in re.findall(r'```([^\n]*)\n(.*?)```', text, re.S):
        labels = list(re.finditer(r'(?m)^\s*(?://|--|#)\s*q([12])\s*\n', block))
        for index, label in enumerate(labels):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(block)
            query = block[label.end():end].strip()
            sql = re.search(r'(\$[A-Za-z_0-9]*\$)(.*?)\1', query, re.S)
            if sql:
                query = sql.group(2)  # AGE's exact Cypher inside its SQL wrapper.
            key = 'q' + label.group(1)
            require(key not in found, f'{key} is written more than once')
            found[key] = query
    require(set(found) == {'q1', 'q2'}, 'complete q1/q2 code blocks are required')
    return found


def quantile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bag(rows):
    return sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) for row in rows)


def source_pair(path, document, queries):
    data = json.loads(path.read_text())
    if 'result' in data and 'case' in data:
        kind, record, settings = 'standard_result', data['result'], data['measurement']
        source_queries = {side: (path.parent / f'query_{side}.cypher').read_text()
                          for side in ('a', 'b', 'ref')
                          if (path.parent / f'query_{side}.cypher').exists()}
        options = [('ab', ('a', 'b'), record['comparisons']['ab'])]
        for key, sides in [('query_a_vs_ref', ('a', 'ref')), ('query_b_vs_ref', ('b', 'ref'))]:
            if key in record.get('reference_comparisons', {}):
                options.append((key, sides, record['reference_comparisons'][key]))
        pattern, expected = data['case']['pattern'], data['case']['expected_rows']
        require(re.fullmatch(r'[PS]\d+', pattern), 'unknown built-in Pattern')
    else:
        kind, record, settings = 'native_probe', data, data['settings']
        source_queries = data.get('queries') or data['recipe']['queries']
        options = [('native_ab', ('a', 'b'), data)]
        pattern, expected = 'native:' + data['mechanism'], data['expected_rows'] if 'expected_rows' in data else data['recipe']['expected_rows']
        require(data.get('passed_time_gate') is True, 'native probe did not pass its time gate')
    matched = []
    for name, sides, comparison in options:
        if not all(side in source_queries for side in sides):
            continue
        for mapping in (sides, sides[::-1]):
            if all(normalized(queries[q]) == normalized(source_queries[side])
                   for q, side in zip(('q1', 'q2'), mapping)):
                # Sample a/b are coordinates of this comparison, not q1/q2.
                coordinates = tuple('a' if side == sides[0] else 'b' for side in mapping)
                matched.append((name, coordinates, comparison))
    if len(matched) > 1 and 'result.comparisons.ab' in document:
        matched = [item for item in matched if item[0] == 'ab']
    require(len(matched) == 1, f'exact report pair is missing or ambiguous: {path}')
    name, coordinates, comparison = matched[0]
    require(kind != 'standard_result' or comparison['status'] == 'candidate',
            'reported standard comparison was not confirmed')
    return data, record, settings, kind, pattern, expected, name, coordinates, comparison


def summarize_report(report):
    text = report.read_text()
    footer = re.search(r'<!--(.*?)-->\s*$', text, re.S)
    require(footer, 'local provenance footer is missing')
    document = footer.group(1)
    queries = report_queries(text)
    paths = list(dict.fromkeys(re.findall(
        r'((?:\.\./){2,4})(results/[^\s,;]+(?:result|probe)\.json)', document)))
    require(paths, 'runtime result.json/probe.json provenance is missing')
    matches = []
    for prefix, relative in paths:
        path = ROOT / relative
        require((report.parent / prefix / relative).resolve() == path.resolve(),
                f'provenance relative depth is wrong: {prefix}{relative}')
        require(path.exists(), f'missing source: {path}')
        require(path.is_relative_to(OUTPUT), f'not from the current detection round: {path}')
        for parent in (path.parent, *path.parents):
            exclusion = parent / 'EXCLUDED.json'
            if exclusion.exists():
                require(json.loads(exclusion.read_text()).get('include_in_analysis') is not False,
                        f'excluded source: {path}')
            if parent == OUTPUT:
                break
        try:
            selected = source_pair(path, document, queries)
        except (KeyError, ValueError):
            continue
        matches.append((path, selected))
    require(len(matches) == 1, 'one exact, confirmed runtime source must match the report')
    path, (data, record, settings, kind, pattern, expected, pair, coordinates, comparison) = matches[0]
    report_parts = report.relative_to(ROOT / 'BUG list').parts
    database_folder = report_parts[0]
    dbms = TARGETS[database_folder]
    require(record['dbms'] == dbms, 'report database and source database differ')
    expected_folders = ('Native', data['mechanism']) if kind == 'native_probe' else (data['case']['pattern'],)
    require(report_parts[1:-1] == expected_folders,
            f'report folder classification differs from formal source: expected {expected_folders}')
    version = record['database_version']
    version_section = text.split(' versions are affected?', 1)[1].split('### How to reproduce', 1)[0]
    tokens = re.findall(r'\d+\.\d+(?:\.\d+)?|module_version=\d+', version)
    require(tokens and all(token.removeprefix('module_version=') in version_section for token in tokens),
            'reported version differs from runtime source')
    build = re.search(r'build ([a-f0-9]{40})', version)
    require(not build or build.group(1) in version_section, 'snapshot build differs from runtime source')
    if kind == 'standard_result':
        plan_sides = ('a', 'b') if pair == 'ab' else ('a' if pair == 'query_a_vs_ref' else 'b', 'ref')
        require(all(record.get('plans', {}).get(side) and 'error' not in record['plans'][side]
                    for side in plan_sides), 'standard comparison plan evidence is missing')
    sessions = comparison['confirmation_sessions']
    require(len(sessions) == 2, 'two independent confirmation sessions are required')
    samples = []
    noise = []
    for session in sessions:
        measured = session['samples']
        require(len(measured) == 20, 'each confirmation must contain twenty pairs')
        require(sum(x['order'] == 'AB' for x in measured) == 10 and
                sum(x['order'] == 'BA' for x in measured) == 10, 'unbalanced AB/BA pairs')
        require(len(session['warmups']) == settings['confirmation_warmups'], 'warm-up count differs from protocol')
        require(session['summary']['median_ratio'] >= 5 and session['summary']['median_delta_ms'] > 100,
                'one independent confirmation misses the strict gate')
        if 'ratio_ci_low' in session['summary']:
            require(session['summary']['ratio_ci_low'] >= 5, 'native session CI misses the strict gate')
        for sample in measured:
            require(sample['a_status'] == sample['b_status'] == 'ok', 'incomplete runtime pair')
            require(all(type(sample[f'{side}_ms']) in (int, float) and math.isfinite(sample[f'{side}_ms'])
                        and sample[f'{side}_ms'] > 0 for side in ('a', 'b')), 'invalid runtime')
        samples.extend(measured)
        for control in ('control_a', 'control_b'):
            require(len(session[control]) == settings['control_pairs'], 'missing same-query noise controls')
            require(all(x['a_status'] == x['b_status'] == 'ok' for x in session[control]), 'incomplete noise control')
            noise.append(quantile([abs(x['a_ms'] - x['b_ms']) for x in session[control]], .99))
        if kind == 'native_probe':
            require(all(bag(session['logical_rows'][side]) == bag(expected) for side in ('a', 'b')),
                    'native query results differ from constructed expectations')
            require(all(session.get('native_diagnostics', {}).get(side) for side in ('a', 'b')),
                    'native plan evidence is missing')
    require(len(samples) == 40, 'exactly forty complete confirmation pairs are required')
    q1, q2 = ([x[f'{side}_ms'] for x in samples] for side in coordinates)
    q1_slow = median(a - b for a, b in zip(q1, q2)) > 0
    slow, fast = (q1, q2) if q1_slow else (q2, q1)
    ratios = [a / b for a, b in zip(slow, fast)]
    deltas = [a - b for a, b in zip(slow, fast)]
    ratio, delta, control_noise = median(ratios), median(deltas), max(noise)
    rng = random.Random(0)
    draws = [median(ratios[rng.randrange(40)] for _ in range(40)) for _ in range(10000)]
    ci_low, ci_high = quantile(draws, .025), quantile(draws, .975)
    summary = comparison['summary']
    require(summary['slow_side'] == coordinates[0 if q1_slow else 1], 'source slow-side direction differs')
    require(math.isclose(ratio, summary['median_ratio'], rel_tol=1e-10) and
            math.isclose(delta, summary['median_delta_ms'], rel_tol=1e-10) and
            math.isclose(control_noise, summary['noise_p99_ms'], rel_tol=1e-10), 'source summary differs from raw pairs')
    require(ratio >= 5 and delta > 100 and ci_low >= 5 and summary['ratio_ci_low'] >= 5 and delta > control_noise,
            'report fails the ratio, absolute difference, CI or noise gate')
    require(sum(x > 0 for x in deltas) / 40 >= settings['direction_consistency'], 'unstable timing direction')
    displayed = re.findall(r'Median runtime:\s*\*{0,2}([\d,.]+)\s*(ms|s)', text)
    require(len(displayed) == 2, 'two reported median runtimes are required')
    for actual, (shown, unit) in zip((median(q1), median(q2)), displayed):
        number = float(shown.replace(',', '')) * (1000 if unit == 's' else 1)
        require(abs(actual - number) <= .0051, 'reported median runtime differs from source pair')
    return {'dbms': dbms, 'report': str(report.relative_to(ROOT)), 'pattern_or_mechanism': pattern,
            'language': record.get('language', data.get('case', {}).get('language', 'cypher')),
            'database_version': version, 'source_kind': kind, 'source_comparison': pair,
            'q1_sample_side': coordinates[0], 'q2_sample_side': coordinates[1],
            'median_q1_ms': median(q1), 'median_q2_ms': median(q2),
            'ratio_direction': 'q1/q2' if q1_slow else 'q2/q1', 'median_paired_ratio': ratio,
            'median_paired_delta_ms': delta, 'delta_over_100_ms': delta > 100,
            'ratio_ci_low': ci_low, 'ratio_ci_high': ci_high,
            'source_ratio_ci_low': summary['ratio_ci_low'], 'source_ratio_ci_high': summary['ratio_ci_high'],
            'noise_p99_ms': control_noise, 'pairs': 40,
            'source': str(path.relative_to(ROOT)), 'source_input_hash': data.get('input_hash', '')}


def main():
    reports = sorted(path for name in TARGETS for path in (ROOT / 'BUG list' / name).rglob('BUG*.md'))
    require(reports, 'no finished non-Neo4j reports')
    rows = []
    for report in reports:
        try:
            rows.append(summarize_report(report))
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError(f'{report.relative_to(ROOT)}: {exc}') from exc
    # Validate every report before replacing either inventory file.
    payload = {'scope': 'completed non-Neo4j local issue letters; not developer-confirmed status',
               'ci_method': '95% percentile bootstrap of paired directed ratios; 10000 draws; seed 0',
               'thresholds': {'median_paired_ratio_min': 5, 'median_paired_delta_ms_strict_min': 100,
                              'ratio_ci_low_min': 5, 'delta_must_exceed_control_noise': True},
               'reports': rows}
    csv_text = io.StringIO()
    writer = csv.DictWriter(csv_text, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'issue_inventory.csv').write_text(csv_text.getvalue())
    (OUTPUT / 'issue_inventory.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(f'{len(rows)} verified issue reports; wrote CSV and JSON inventories')


if __name__ == '__main__':
    main()
