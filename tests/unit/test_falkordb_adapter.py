from __future__ import annotations

from graph_sym_perf.adapters.falkordb import _falkor_rows, _query_with_params


class FakeResult:
    header = ("<Column 'n'>", "<Column 'value'>")
    result_set = (({"id": 1}, 7),)
    statistics = {"Nodes created": 0}


def test_falkordb_query_parameters_are_rendered_as_cypher_preamble() -> None:
    query = _query_with_params(
        "MATCH (n) WHERE n.id = $id AND n.name = $name RETURN n",
        {"id": 1, "name": "Alice"},
    )

    assert query.startswith("CYPHER id=1 name='Alice' MATCH")


def test_falkordb_rows_use_result_headers_when_available() -> None:
    rows = _falkor_rows(FakeResult())

    assert rows == ({"n": {"id": 1}, "value": 7},)
