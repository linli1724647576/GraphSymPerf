"""Adapter checks cover result fidelity, cancellation and scoped graph loading."""

import csv
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

import executor
from executor import CaseFailure
from state import GraphSpec


class Result:
    def __init__(self, names=(), types=(), rows=()):
        self.names, self.types, self.rows = list(names), list(types), iter(rows)
        self.next = next(self.rows, None)
        self.closed = False

    def get_column_names(self):
        return self.names

    def get_column_data_types(self):
        return self.types

    def has_next(self):
        return self.next is not None

    def get_next(self):
        row, self.next = self.next, next(self.rows, None)
        return row

    def close(self):
        self.closed = True


class Database:
    def __init__(self, path, **kwargs):
        self.path, self.options, self.closed = path, kwargs, False
        Path(path).touch()

    @staticmethod
    def get_version():
        return '0.21.2'

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, database, num_threads=0):
        self.database, self.calls, self.imports, self.tables = database, [], {}, {}
        self.timeout_ms, self.closed, self.interrupted = None, False, False
        self.response = None
        self.healthy, self.pybind = True, True
        self.last_result = None

    def set_query_timeout(self, timeout):
        self.timeout_ms = timeout

    def _using_pybind_backend(self):
        return self.pybind

    def interrupt(self):
        self.interrupted = True

    def execute(self, query, parameters=None):
        self.calls.append((query, parameters))
        if query == 'RETURN 1 AS connected':
            if not self.healthy:
                raise RuntimeError('Connection is closed')
            return Result(['connected'], ['INT64'], [[1]])
        if self.response is not None:
            if isinstance(self.response, Exception):
                raise self.response
            self.last_result = self.response
            return self.response
        if query.startswith('CREATE NODE TABLE'):
            name, fields = re.fullmatch(r'CREATE NODE TABLE `(\w+)`\((.*)\)', query).groups()
            self.tables[name] = [(column.split()[0].strip('`'), 'PRIMARY KEY' in column)
                                 for column in fields.split(', ')]
        if query.startswith('COPY'):
            label, path = re.match(r"COPY `(\w+)` FROM '([^']+)'", query).groups()
            with open(path, newline='', encoding='utf-8') as handle:
                rows = list(csv.reader(handle))
            self.imports.setdefault(label, []).extend(rows)
        if query.startswith('CALL table_info'):
            label = re.search(r"table_info\('([^']+)'\)", query)[1]
            return Result(['property id', 'name', 'type', 'default expression', 'primary key'],
                          ['INT32', 'STRING', 'STRING', 'STRING', 'BOOL'],
                          [[i, name, 'INT64', 'NULL', primary]
                           for i, (name, primary) in enumerate(self.tables[label])])
        return Result()

    def close(self):
        self.closed = True


@pytest.fixture
def database(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'ladybug', SimpleNamespace(Database=Database, Connection=Connection))
    unrelated = tmp_path / 'user-file.txt'
    unrelated.write_text('keep this file')
    instance = executor.LadybugDB({'database': 'gsp_test', 'path': str(tmp_path),
                                   'dedicated': True, 'allow_reset': True},
                                  {'timeout_seconds': 2, 'max_rows': 3})
    yield instance
    instance.close()
    assert unrelated.read_text() == 'keep this file'
    assert list(tmp_path.iterdir()) == [unrelated]


def test_preserves_types_nulls_and_duplicate_rows(database):
    cursor = Result(['i', 'b', 'n', 'items'], ['INT64', 'BOOL', 'STRING', 'INT64[]'],
                    [[1, True, None, [1, 2]], [1, True, None, [1, 2]]])
    database.connection.response = cursor
    result = database.execute('RETURN 1 AS i', {}, 0.125)
    assert result['rows'] == [{'i': 1, 'b': True, 'n': None, 'items': [1, 2]}] * 2
    assert result['complete'] and result['elapsed_ms'] > 0
    assert database.connection.timeout_ms == 125 and cursor.closed


@pytest.mark.parametrize('count,complete', [(3, True), (4, False)])
def test_row_limit_distinguishes_exact_limit_from_truncation(database, count, complete):
    database.connection.response = Result(['value'], ['INT64'], [[1]] * count)
    result = database.execute('RETURN 1 AS value', {}, 1)
    assert len(result['rows']) == 3 and result['complete'] is complete


@pytest.mark.parametrize('types,names', [(['NODE'], ['n']), (['INT64', 'INT64'], ['n', 'n'])])
def test_ambiguous_or_graph_object_results_cannot_prove_equivalence(database, types, names):
    cursor = Result(names, types, [])
    database.connection.response = cursor
    with pytest.raises(CaseFailure) as failure:
        database.execute('RETURN 1 AS n', {}, 1)
    assert failure.value.status == 'incomplete_result' and cursor.closed


@pytest.mark.parametrize('healthy', [True, False])
def test_interruption_requires_health_before_reusing_target(database, healthy):
    database.connection.response = RuntimeError('Interrupted.')
    database.connection.healthy = healthy
    with pytest.raises(CaseFailure) as failure:
        database.execute('RETURN 1 AS n', {}, 1)
    assert failure.value.status == 'timeout'
    assert failure.value.stop_target is not healthy
    assert failure.value.details['cancellation_verified'] is healthy
    assert database.connection.interrupted


def test_capi_wrapper_synthetic_interruption_is_not_a_performance_timeout(database):
    database.connection.pybind = False
    before = len(database.connection.calls)
    with pytest.raises(CaseFailure) as failure:
        database.execute('UNWIND range(1, 4) AS x UNWIND range(1, 4) AS y RETURN x', {}, 1)
    assert failure.value.status == 'unsupported_feature'
    assert len(database.connection.calls) == before


def test_native_profile_keeps_original_operator_fields(database):
    plan = 'PROFILE\nPRIMARY_KEY_SCAN_NODE_TABLE\nNumOutputTuples: 1\nExecutionTime: 0.01'
    database.connection.response = Result(['explain result'], ['STRING'], [[plan]])
    result = database.explain('MATCH (n:A) RETURN n.uid AS uid', {}, analyze=True)
    assert database.connection.calls[-1][0].startswith('PROFILE MATCH')
    assert result['rows'] == [{'explain result': plan}] and result['analyze']


def test_p2_edges_resolve_primary_id_instead_of_using_uid(database):
    spec = GraphSpec('P2', 3, 7)
    database.restore({'graph_spec': spec.to_dict()})
    nodes = {(n['labels'][0], n['uid']): n['properties'] for n in spec.iter_nodes()}
    expected = [[str(nodes[('GspLow', edge['source'])]['id']), str(edge['target']), str(edge['properties']['pair'])]
                for edge in spec.iter_edges() if edge['type'] == 'GSP_FORWARD']
    assert database.connection.imports['GSP_FORWARD'] == expected
    assert database.index_evidence[0]['kind'] == 'primary_key_hash'
    assert database.index_evidence[0]['verified'] is True


def test_path_state_has_only_real_endpoint_connections(database):
    spec = GraphSpec('P3', 2, 7, 'pruning_path')
    database.restore({'graph_spec': spec.to_dict()})
    ddl = next(query for query, _ in database.connection.calls if query.startswith('CREATE REL TABLE'))
    assert 'FROM `GspLow` TO `GspMiddle`' in ddl
    assert 'FROM `GspLow` TO `GspBad`' in ddl
    assert 'FROM `GspBad` TO `GspMiddle`' in ddl
    assert 'FROM `GspMiddle` TO `GspHigh`' in ddl
    assert 'FROM `GspLow` TO `GspHigh`' not in ddl


def test_null_guard_is_preserved_during_copy(database):
    database.restore({'graph_spec': GraphSpec('P15', 2, 7).to_dict()})
    assert ['3', ''] in database.connection.imports['GspAnchor']


def test_unsupported_multilabel_case_does_not_rebuild_database(database):
    original = database.db.path
    with pytest.raises(CaseFailure) as failure:
        database.restore({'graph_spec': GraphSpec('P7', 2, 7).to_dict()})
    assert failure.value.status == 'unsupported_feature' and database.db.path == original


def test_rebuild_removes_only_its_owned_previous_directory(database):
    initial_path = Path(database.db.path)
    database.restore({'graph_spec': GraphSpec('P1', 2, 7).to_dict()})
    assert not initial_path.exists() and Path(database.db.path).exists()
    database.restore({'graph_spec': GraphSpec('P2', 2, 7).to_dict()})
    assert Path(database.db.path).exists()


def test_legacy_text_setup_requires_semantic_graph_recipe(database):
    with pytest.raises(CaseFailure) as failure:
        database.restore({'setup': ['CREATE (:A {uid: 1})']})
    assert failure.value.status == 'unsupported_feature'
