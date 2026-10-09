import argparse, json, os, sys, time
from pathlib import Path
ROOT = Path('/Users/sakuraq/Documents/科研实习/GraphSymPerf')
sys.path.insert(0, str(ROOT / 'Src'))
from main import read_config, read_case, prepare_case, save_case, new_run_directory, write_run, case_digest
from executor import run_case
from dataclasses import replace
from ir import Alias, Call, Literal, from_data, to_data
from main import compile_case

parser = argparse.ArgumentParser()
parser.add_argument('config', type=Path)
parser.add_argument('--replay', type=Path)
parser.add_argument('--variant', default='original')
parser.add_argument('--control', choices=['coalesce-constant'])
args = parser.parse_args()
config = read_config(args.config)
credentials = args.config.parent / 'credentials.json'
if credentials.exists():
    os.environ.update(json.loads(credentials.read_text()))
database, settings = config['database'], config['measurement']
directory = new_run_directory(config['output'])
cache, completed = {}, 0
record = {'format_version':1, 'kind':'discovery', 'dbms':database['dbms'],
          'language':database.get('language','cypher'), 'database_version':'unknown',
          'patterns':config['patterns'], 'budgets':{}, 'per_pattern':{}}
for pattern in config['patterns']:
    start = time.monotonic()
    count = 0
    for scale in config['scales']:
        for seed in config['seeds']:
            if args.replay:
                case, _ = read_case(args.replay)
            else:
                case = prepare_case(None, pattern, scale, seed, config['generation']['data_seed'],
                                    config['generation']['complexity'], args.variant, database=database)
            if args.control == 'coalesce-constant':
                query = from_data(case['query_ir']['a'])
                projection = query.returns[0]
                expression = projection.expression if isinstance(projection, Alias) else projection
                assert isinstance(expression, Call) and expression.name == 'coalesce'
                literal = next(value for value in expression.args if isinstance(value, Literal) and value.value is not None)
                assert all(isinstance(value, Literal) for value in expression.args[:expression.args.index(literal)+1])
                result = replace(projection, expression=literal) if isinstance(projection, Alias) else literal
                case['query_ir']['b'] = to_data(replace(query, returns=(result,)))
                case['query_ir'].pop('ref', None)
                case.pop('query_ref', None)
                case.pop('reference_side', None)
                compile_case(case, database)
            path = directory / f'{completed+1:04d}-{pattern}-n{case["scale"]}-{case_digest(case)[:8]}'
            save_case(path, case, settings, {'status':'started','dbms':database['dbms']})
            report = run_case(case, database, settings, state_cache=cache)
            save_case(path, case, settings, report)
            count += 1; completed += 1
            record['database_version'] = report.get('database_version',record['database_version'])
            record['per_pattern'][pattern] = {'attempted':count,'executed':count,
                'elapsed_seconds':time.monotonic()-start,'stop_reason':'running'}
            write_run(directory, record)
            print(pattern,case['scale'],case['seed'],report['status'],report.get('reason'),report.get('summary'),path,flush=True)
            if report.get('stop_target'):
                record['per_pattern'][pattern]['stop_reason']='target_error'
                write_run(directory,record)
                raise SystemExit(1)
    record['per_pattern'][pattern]['stop_reason']='inputs_exhausted'
    write_run(directory,record)
print('Completed',completed,directory,flush=True)
