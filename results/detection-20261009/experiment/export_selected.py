"""Export verified issue metadata for Src/summarize.py; never touch a DB.

Native records are evidence summaries, not main.py replay inputs. Reproduce
them through the archived native probe scripts and their original artifacts.
"""

import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path('/Users/sakuraq/Documents/科研实习/GraphSymPerf')
PRIVATE = Path('/private/tmp/gsp-detection-20261009')
DETECTION = ROOT / 'results/detection-20261009'
DESTINATION = DETECTION / 'registered'
BUG_FIELDS = ('case_id', 'dbms', 'bug_id', 'status', 'issue_url', 'database_version')
PROBE_SCRIPTS = {'age': 'age_native_probe.py', 'ladybug': 'ladybug_native_probe.py',
                 'janusgraph': 'gremlin_native_probe.py', 'hugegraph': 'gremlin_native_probe.py'}
WRAPPERS = {('age', 'property-index'): 'age_coalesce_index.py',
            ('ladybug', 'path-pruning'): 'ladybug_inline_probe.py'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def compact_pair(pair):
    return {key: pair[key] for key in ('pair_kind', 'status', 'summary') if key in pair}


def native_input(source):
    # Keep actual load statements and schema mapping in the identity. Do not
    # label a reused GraphSpec recipe as evidence of a built-in Pattern hit.
    recipe = source.get('recipe')
    setup = (source.get('state_evidence') or {}).get('setup_sql')
    if not setup and source.get('confirmation_sessions'):
        setup = source['confirmation_sessions'][0]['state_evidence'].get('setup_sql')
    payload = {'dbms': source['dbms'], 'language': source['language'],
               'queries': source.get('queries') or recipe['queries'],
               'actual_setup': setup or (recipe or {}).get('setup'),
               'recipe': recipe, 'graph_spec': source.get('graph_spec'),
               'native_schema': source.get('native_schema'),
               'native_schema_mapping': source.get('native_schema_mapping'),
               'parameters': source.get('parameters', {})}
    require(payload['actual_setup'] or payload['recipe'] or payload['graph_spec'],
            'native source lacks actual graph input')
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest(), payload


def main():
    spec = importlib.util.spec_from_file_location('issue_validator', PRIVATE / 'summarize_issues.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    inventory = json.loads((DETECTION / 'issue_inventory.json').read_text())
    rows = inventory['reports']
    require(rows, 'verified issue inventory is empty')
    documents, annotations, scripts, archive_manifest, commands = {}, [], {}, {}, []
    identities = set()
    for row in rows:
        report = ROOT / row['report']
        require(report.exists(), f'missing verified report: {report}')
        # Recheck the exact report pair, original samples, version and gates.
        require(validator.summarize_report(report) == row, f'inventory is stale: {report}')
        source = json.loads((ROOT / row['source']).read_text())
        provenance = {'report': row['report'], 'evidence': row['source'],
                      'source_kind': row['source_kind'], 'selected_comparison': row['source_comparison'],
                      'q1_sample_side': row['q1_sample_side'], 'q2_sample_side': row['q2_sample_side'],
                      'manual_review': 'Selected local issue letter after complete-result and native-work review.',
                      'developer_confirmed': False, 'submitted': False}
        if row['source_kind'] == 'standard_result':
            identity = source['input_hash']
            require(identity == row['source_input_hash'] and re.fullmatch(r'[0-9a-f]{64}', identity),
                    'standard case identity differs from original evidence')
            case = source['case']
            original = source['result']
            require(case['pattern'] == row['pattern_or_mechanism'], 'standard Pattern differs from inventory')
            result = {key: original[key] for key in ('status', 'reason', 'dbms', 'language', 'database_version')
                      if key in original}
            result['comparisons'] = {key: compact_pair(pair)
                                     for key, pair in original.get('comparisons', {}).items()}
            result['reference_comparisons'] = {key: compact_pair(pair)
                                               for key, pair in original.get('reference_comparisons', {}).items()}
            document = {'format_version': 'registered-evidence-summary-v1', 'kind': 'standard_issue_summary',
                        'source_format_version': source['format_version'], 'case': case, 'result': result}
        else:
            require(row['source_kind'] == 'native_probe' and source.get('passed_time_gate') is True,
                    'unconfirmed native probe cannot be registered')
            identity, actual_input = native_input(source)
            require(row['pattern_or_mechanism'] == 'native:' + source['mechanism'],
                    'native mechanism was misclassified as a built-in Pattern')
            base_name = PROBE_SCRIPTS[row['dbms']]
            script_name = WRAPPERS.get((row['dbms'], source['mechanism']), base_name)
            for name in dict.fromkeys((base_name, script_name)):
                script_path = PRIVATE / name
                require(script_path.exists(), f'missing native reproducer script: {script_path}')
                original = script_path.read_text()
                archived = original
                if name == base_name:
                    archived, replacements = re.subn(
                        r'^WORKSPACE\s*=\s*Path\([^\n]*\)$',
                        'WORKSPACE = next(parent for parent in Path(__file__).resolve().parents '
                        'if (parent / "Src" / "executor.py").is_file())', original, flags=re.M)
                    require(replacements == 1, 'base script WORKSPACE assignment must be located exactly once')
                scripts[name] = archived
                archive_manifest[name] = {'original_path': str(script_path),
                    'original_sha256': hashlib.sha256(original.encode()).hexdigest(),
                    'archived_sha256': hashlib.sha256(archived.encode()).hexdigest(),
                    'changes': 'Only WORKSPACE locates Src through __file__ ancestors.' if name == base_name else 'Unmodified wrapper.'}
            provenance['reproducer'] = str((DESTINATION / 'reproducers' / script_name).relative_to(ROOT))
            provenance['reproducer_hashes'] = archive_manifest[script_name]
            provenance['reproducer_dependencies'] = {base_name: archive_manifest[base_name]}
            recipe = source.get('recipe') or source['graph_spec']
            command = (f'python results/detection-20261009/registered/reproducers/{script_name} '
                       f'--config /absolute/dedicated/{row["dbms"]}.yaml '
                       f'--mechanism {source["mechanism"]} --scale {recipe["scale"]}')
            if row['dbms'] == 'age' and recipe.get('roots') is not None:
                command += f' --roots {recipe["roots"]}'
            commands.append(command + ' --execute')
            document = {'format_version': 'native-evidence-summary-v1', 'kind': 'native_optimization_rule',
                        'note': 'Metadata for statistical summary only; not IR and not main.py --replay input. '
                                'Reproduce with the archived native probe script and the referenced original probe recipe.',
                        'native_input': actual_input,
                        'case': {'pattern': row['pattern_or_mechanism'], 'language': row['language'],
                                 'mechanism': source['mechanism'], 'expected_rows': source.get('expected_rows')
                                 if 'expected_rows' in source else source['recipe']['expected_rows']},
                        'result': {'status': 'timing_observation', 'reason': 'native_rule_evidence_manually_reviewed',
                                   'dbms': row['dbms'], 'language': row['language'],
                                   'database_version': row['database_version'],
                                   'source_status': source['status'], 'summary': source['summary']}}
        require((row['dbms'], identity) not in identities, 'two report IDs refer to the same database input')
        identities.add((row['dbms'], identity))
        document.update(input_hash=identity, dbms=row['dbms'], language=row['language'], replayable=False,
                        provenance=provenance, verified_report_statistics=row)
        documents[DESTINATION / row['dbms'] / report.stem / 'result.json'] = document
        annotations.append({'case_id': identity, 'dbms': row['dbms'], 'bug_id': report.stem,
                            'status': 'reproduced', 'issue_url': '', 'database_version': row['database_version']})
    # Refuse stale/unselected files rather than deleting unknown results.
    stale = set(DESTINATION.rglob('result.json')) - set(documents)
    require(not stale, f'registered contains entries absent from the verified inventory: {sorted(stale)}')
    for path, document in documents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    for name, content in scripts.items():
        path = DESTINATION / 'reproducers' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (DESTINATION / 'reproducers' / 'manifest.json').write_text(
        json.dumps(archive_manifest, ensure_ascii=False, indent=2) + '\n')
    with (DESTINATION / 'bugs.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=BUG_FIELDS)
        writer.writeheader()
        writer.writerows(annotations)
    (DESTINATION / 'README.md').write_text(
        '# Selected local issue evidence\n\n'
        'Generated only from the verified issue inventory. These records annotate local reproductions; '
        'they are neither submitted issues nor developer confirmations. This directory\'s bugs.csv is the '
        'selected annotation export; the repository owner can synchronize it to the root bugs.csv.\n\n'
        'Standard records retain their original input_hash and case metadata. Full samples, graph files and '
        'plans remain in the original evidence paths. These compact files cannot be passed to main.py --replay.\n\n'
        'Native records use format_version native-evidence-summary-v1 and kind native_optimization_rule. '
        'Their pattern is native:<mechanism>, their result status is timing_observation, and their annotations '
        'are reproduced after manual plan/work review. No query IR or built-in Pattern hit is invented. '
        'Reproduce them using the archived scripts under reproducers/, a dedicated database configuration, '
        'and the commands below. The AGE COALESCE property-index case requires age_coalesce_index.py, '
        'and the Ladybug inline-path control requires ladybug_inline_probe.py; each wrapper imports its '
        'matching base script. Base scripts locate this repository\'s Src directory through __file__ '
        'ancestors. Only that path resolution was changed; queries, data and measurement defaults remain '
        'unchanged. reproducers/manifest.json records original_sha256 and archived_sha256 for each file.\n\n'
        'The --execute option rebuilds the explicitly dedicated test target. Omitting it gives a dry run; '
        'these examples do not use main.py --replay. Set any required password environment variables '
        'from the dedicated configuration before running one target at a time.\n\n'
        '```bash\n' + '\n'.join(commands) + '\n```\n\n'
        'This selected directory has no experiment-budget manifests. Its summary must not be used to '
        'infer a discovery rate, full Pattern coverage or bugs per hour.\n')
    print(f'{len(documents)} verified issue metadata records and local reproduced annotations exported')


if __name__ == '__main__':
    main()
