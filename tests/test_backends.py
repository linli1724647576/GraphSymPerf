"""Backend protocol tests; no database is contacted or modified."""

import sys
from types import SimpleNamespace

import pytest

import executor
from executor import CaseFailure, FalkorDB, Memgraph, Neo4j, measurement_settings, typed_bag


INDEX = 'CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (u:GspLow) ON (u.id)'


def test_factory_and_conservative_pattern_coverage(monkeypatch):
    for name, adapter in [('neo4j', 'Neo4j'), ('memgraph', 'Memgraph'), ('falkordb', 'FalkorDB')]:
        monkeypatch.setattr(executor, adapter, lambda config, settings, name=name: name)
        assert executor.make_database({'dbms': name}, {}) == name
    assert executor.make_database({}, {}) == 'neo4j'
    assert 'S3' not in executor.supported_patterns({'dbms': 'memgraph'})
    assert {'P1', 'P16', 'S5', 'S6', 'S9'} <= executor.supported_patterns({'dbms': 'falkordb'})
    assert 'S8' not in executor.supported_patterns({'dbms': 'falkordb'})
    with pytest.raises(CaseFailure, match='unsupported_dbms'):
        executor.make_database({'dbms': 'unknown'}, {})


def test_memgraph_translates_only_the_known_cost_index_and_checks_real_metadata():
    db = Memgraph.__new__(Memgraph)
    db.prepare_state({'setup': [INDEX]})
    calls = []
    def execute(query, parameters, timeout):
        calls.append(query)
        rows = [{'index type': 'label+property', 'label': 'GspLow', 'property': 'id', 'count': 1}] if query == 'SHOW INDEX INFO' else []
        return {'rows': rows, 'complete': True, 'elapsed_ms': 1}
    db._execute = execute
    db.execute(INDEX, {}, 10)
    assert calls == ['CREATE INDEX ON :GspLow(id)']
    assert db.execute(executor._INDEX_CHECK, {}, 10)['rows'] == [executor._canonical_index(('GspLow', 'id'))]
    with pytest.raises(CaseFailure, match='unsupported_index_definition'):
        db.execute('CREATE INDEX other FOR (u:GspLow) ON (u.id)', {}, 10)
    with pytest.raises(CaseFailure, match='unsupported_memgraph_syntax'):
        db.execute('SHOW INDEXES YIELD *', {}, 10)
    with pytest.raises(CaseFailure, match='unknown_memgraph_index_metadata'):
        db._index_row({'index type': 'label+property_vector', 'label': 'GspLow', 'property': 'id'})


def test_memgraph_restore_uses_its_schema_operations_without_neo4j_await():
    db = Memgraph.__new__(Memgraph)
    db.settings = measurement_settings({})
    calls = []
    def execute(query, parameters, timeout):
        calls.append(query)
        rows = [{'index type': 'label+property', 'label': 'GspAnchor', 'property': 'uid'}] if query == 'SHOW INDEX INFO' else []
        return {'rows': rows, 'complete': True, 'elapsed_ms': 1}
    db._execute = execute
    db.restore({'setup': [INDEX, 'CREATE (:GspLow {id:1})']})
    assert calls == ['SHOW INDEX INFO', 'SHOW CONSTRAINT INFO', 'DROP INDEX ON :GspAnchor(uid)',
                     'MATCH (n) DETACH DELETE n', 'CREATE INDEX ON :GspLow(id)', 'CREATE (:GspLow {id:1})']


@pytest.mark.parametrize('properties,expected', [(None, None), ([], None), ('uid', 'uid'), (['uid'], 'uid')])
def test_memgraph_index_metadata_accepts_real_single_property_list(properties, expected):
    assert Memgraph._index_row({'index type': 'label' if expected is None else 'label+property',
                                'label': 'GspAnchor', 'property': properties, 'count': 3}) == ('GspAnchor', expected)


@pytest.mark.parametrize('properties', [['uid', 'id'], [1], {}, 1])
def test_memgraph_index_metadata_rejects_composite_or_unknown_properties(properties):
    with pytest.raises(CaseFailure, match='unknown_memgraph_index_metadata'):
        Memgraph._index_row({'index type': 'label+property', 'label': 'GspAnchor', 'property': properties})


def test_memgraph_index_metadata_requires_kind_and_property_to_agree():
    for kind, prop in [('label', ['uid']), ('label+property', [])]:
        with pytest.raises(CaseFailure, match='unknown_memgraph_index_metadata'):
            Memgraph._index_row({'index type': kind, 'label': 'GspAnchor', 'property': prop})


def test_falkordb_result_columns_and_python_values_preserve_multiset_semantics():
    result = SimpleNamespace(header=[[1, 'x'], [1, 'values']],
                             result_set=[[True, [1, None]], [True, [1, None]], [1, [1, None]]])
    rows = FalkorDB._rows(result)
    assert len(rows) == 3
    assert typed_bag(rows) != typed_bag(rows[:2])
    assert typed_bag(rows[:1]) != typed_bag(rows[2:])
    with pytest.raises(CaseFailure, match='ambiguous_result_columns'):
        FalkorDB._rows(SimpleNamespace(header=[[1, 'x'], [1, 'x']], result_set=[[1, 2]]))


def test_falkordb_does_not_rewrite_count_or_exists_into_different_mechanisms():
    db = FalkorDB.__new__(FalkorDB)
    calls = []
    db._execute = lambda query, parameters, timeout: calls.append(query)
    db.execute(INDEX, {}, 10)
    assert calls == ['CREATE INDEX FOR (n:GspLow) ON (n.id)']
    for query in ('MATCH (u) RETURN COUNT { MATCH (u)-->() } AS c',
                  'MATCH (u) WHERE EXISTS { MATCH (u)-->() } RETURN u.id',
                  'CYPHER 25 WHEN true THEN { RETURN 1 AS value }'):
        with pytest.raises(CaseFailure, match='unsupported_falkordb_syntax'):
            db.execute(query, {}, 10)
    assert len(calls) == 1


@pytest.mark.parametrize('limit,accepted', [(-1, True), (101, True), (100, False)])
def test_falkordb_requires_observable_complete_results_and_reports_actual_module_version(monkeypatch, limit, accepted):
    class ResponseError(Exception):
        pass
    class RedisTimeoutError(Exception):
        pass
    class Client:
        closed = False
        def config_get(self, setting):
            assert setting == 'RESULTSET_SIZE'
            return limit
        def execute_command(self, *args):
            assert args == ('MODULE', 'LIST')
            return [[b'name', b'graph', b'ver', 41808]]
        def select_graph(self, name):
            assert name == 'gsp-test'
            return object()
        def close(self):
            self.closed = True
    client = Client()
    monkeypatch.setitem(sys.modules, 'falkordb', SimpleNamespace(FalkorDB=SimpleNamespace(from_url=lambda *args, **kwargs: client)))
    monkeypatch.setitem(sys.modules, 'redis.exceptions', SimpleNamespace(ResponseError=ResponseError, TimeoutError=RedisTimeoutError))
    config = {'uri': 'redis://127.0.0.1:6379', 'database': 'gsp-test', 'dedicated': True, 'allow_reset': True}
    if accepted:
        db = FalkorDB(config, measurement_settings({'max_rows': 100}))
        assert db.version == 'FalkorDB/module_version=41808'
        assert db.server_result_limit == limit
        db.close()
    else:
        with pytest.raises(CaseFailure, match='falkordb_resultset_size_too_small'):
            FalkorDB(config, measurement_settings({'max_rows': 100}))
    assert client.closed


@pytest.mark.parametrize('code,message,status', [
    ('Neo.ClientError.Statement.SyntaxError', 'unexpected input', 'unsupported_feature'),
    ('Neo.ClientError.Statement.ExecutionFailed', 'Not yet implemented: Exists', 'unsupported_feature'),
    ('Memgraph.ClientError.MemgraphError.MemgraphError',
     "Error on line 1 position 59 with the ( token. The underlying parsing error is no viable alternative at input '(trueOR(COUNT{MATCH('. Take a look at clauses around and try to fix the query.",
     'unsupported_feature'),
    ('Memgraph.ClientError.MemgraphError.MemgraphError', 'Query runtime error: invalid argument', 'query_error'),
    ('Neo.ClientError.Statement.ExecutionFailed',
     'Procedure runtime exception: no viable alternative at input record', 'query_error'),
    ('Neo.TransientError.General.MemoryPoolOutOfMemoryError', 'out of memory', 'query_error'),
    ('Neo.ClientError.Statement.TypeError', 'wrong argument type', 'query_error'),
])
def test_bolt_errors_do_not_hide_runtime_failures_as_unsupported(code, message, status):
    class ServerError(Exception):
        pass
    error = ServerError(message)
    error.code = code
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def run(self, *args):
            raise error
    db = Neo4j.__new__(Neo4j)
    db.database, db.settings = 'test', measurement_settings({})
    db.driver = SimpleNamespace(session=lambda **kwargs: Session())
    db._query, db._server_error = lambda text, **kwargs: text, ServerError
    with pytest.raises(CaseFailure) as failure:
        db.execute('RETURN 1', {}, 10)
    assert failure.value.status == status
    assert failure.value.details['server_message'] == message


@pytest.mark.parametrize('acknowledged,healthy', [(True, True), (True, False), (False, True)])
def test_bolt_timeout_requires_server_acknowledgement_and_health(monkeypatch, acknowledged, healthy):
    class ServerError(Exception):
        code = 'Neo.ClientError.Transaction.TransactionTimedOut'
    error = ServerError('server terminated transaction') if acknowledged else TimeoutError('socket timed out')
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def run(self, *args):
            raise error
    db = Neo4j.__new__(Neo4j)
    db.database, db.settings = 'test', measurement_settings({})
    db.driver = SimpleNamespace(session=lambda **kwargs: Session())
    db._query, db._server_error = lambda text, **kwargs: text, ServerError
    health_calls = []
    db.health = lambda: health_calls.append(True) or healthy
    clock = iter([1.0, 2.0])
    monkeypatch.setattr(executor, 'perf_counter', lambda: next(clock))
    with pytest.raises(CaseFailure) as failure:
        db.execute('RETURN 1', {}, 10)
    exc = failure.value
    assert exc.status == 'timeout'
    assert exc.stop_target is not (acknowledged and healthy)
    assert exc.details['cancellation_verified'] is (acknowledged and healthy)
    assert exc.details['elapsed_lower_bound_ms'] == (1000 if acknowledged else None)
    assert bool(health_calls) is acknowledged


def test_falkordb_timeout_uses_milliseconds_and_never_invents_a_duration(monkeypatch):
    class ResponseError(Exception):
        pass
    class RedisTimeoutError(Exception):
        pass
    calls = []
    def query(text, **kwargs):
        calls.append(kwargs)
        raise ResponseError('Query timed out')
    db = FalkorDB.__new__(FalkorDB)
    db.graph = SimpleNamespace(query=query)
    db._response_error, db._timeout_error = ResponseError, RedisTimeoutError
    db.health = lambda: True
    clock = iter([1.0, 1.003])
    monkeypatch.setattr(executor, 'perf_counter', lambda: next(clock))
    with pytest.raises(CaseFailure) as failure:
        db._execute('RETURN 1', {}, 0.0011)
    assert calls == [{'params': None, 'timeout': 2}]
    assert failure.value.status == 'timeout' and not failure.value.stop_target
    assert failure.value.details['elapsed_lower_bound_ms'] == pytest.approx(1.1)
    assert 'elapsed_ms' not in failure.value.details
